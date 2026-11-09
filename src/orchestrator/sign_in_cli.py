"""`canopy sign-in` — a person signs an agent's session in to a third party, for this session only.

Some sign-ins cannot be done by an agent and should not be kept: Claude Design's OAuth only
runs outside a Claude Code session, wants a browser the runner does not have, and its
credential should not outlive the work. The person does the one thing only they can (approve on
the provider's own consent page and put the code it shows into the chat's Secrets, never as
chat text); this box does the rest:

    canopy sign-in start design     # starts the provider's sign-in as its own process, prints the link
    canopy sign-in code design      # hands over the code the person put in the chat's Secrets
    canopy sign-in status [design]
    canopy sign-in stop [design]    # sign out now: drop what the sign-in wrote, end the process

The same shape as the Google mint (``google_mint.py``): the PKCE verifier lives in the process
that made the challenge, so a code relayed through chat or canopy-web is useless to anyone
else and canopy-web never holds the credential. Unlike ``act-as`` nothing here is a canopy-web
login, so there is no server side and no second approval: the provider's consent page is the
approval, and ``code`` is the person's yes.

**Ephemeral by construction.** What the provider writes is removed again at ``stop`` and once
``LIFETIME`` (30 minutes, never extended) has passed, whichever is first; the SessionEnd hook
(``hooks/sign_in_cleanup.py``) sweeps expired ones but does not end a live one, because in a chat
every turn is a new Claude process; a sign-in that was already on the box before ``start`` is never touched. The code never
leaves this process's memory and the output never prints a token.

**Honest limits.** The credential sits where the provider's own CLI keeps it, so every session
on this box can use it while it lives (it is the person's design access, not this session's).
Removing it locally does not revoke it at the provider: ``/design-revoke`` does, from an
interactive session. Providers are a table (``PROVIDERS``); only a provider whose sign-in is a
separate process with a pasted-code leg fits. The code travels as a chat secret (`canopy secret`:
sealed to this machine, opened here), so it never enters the transcript.

State: ``~/.canopy/sign-in/<claude session>.json`` (0600, dir 0700), plus the process's stdin
FIFO and output file beside it. Keyed by the Claude session, like ``act-as``.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import click

from orchestrator import act_as_cli, secret_cli

#: How long a finished sign-in lives on this box, from the code. Never extended.
LIFETIME = 30 * 60
#: How long to wait for the provider's process to print its link.
START_WAIT = 20
#: How long `code --wait` waits for the person to add the secret (under the fleet's 270 s cap).
SECRET_WAIT = 240
#: How long to wait for it to trade the code for a credential.
CODE_WAIT = 40
#: A code is `<code>#<state>`: URL-safe characters only, bounded. Refusing the rest means
#: nothing but a code can ever reach the process's stdin.
_CODE = re.compile(r"^[A-Za-z0-9_\-.~]{10,300}#[A-Za-z0-9_\-.~]{10,300}$")

_KEY = re.compile(r"^[A-Za-z0-9._-]{1,100}$")

# Test seams.
NOW = time.time
SLEEP = time.sleep


@dataclass(frozen=True)
class Provider:
    name: str
    argv: tuple  # the provider's own sign-in command (binary resolved at start)
    link_event: str  # JSON event carrying the link
    link_field: str
    done_event: str
    present: Callable[[], bool]  # is a credential of this provider on the box now
    clear: Callable[[], bool]  # remove it; True when something was removed
    what: str  # "Claude Design access" — for the person
    secret: str  # the chat-secret name the person puts the pasted code under


# --- Claude Design: the provider's CLI keeps `designOauth` in ~/.claude/.credentials.json -----

def _claude_credentials() -> Path:
    return Path.home() / ".claude" / ".credentials.json"


def _design_present() -> bool:
    try:
        return "designOauth" in json.loads(_claude_credentials().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False


def _design_clear() -> bool:
    """Remove ONLY the `designOauth` key, atomically, keeping the rest (mcpOAuth …) and 0600."""
    path = _claude_credentials()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(doc, dict) or doc.pop("designOauth", None) is None:
        return False
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    os.replace(tmp, path)
    return True


PROVIDERS = {
    "design": Provider(
        name="design", argv=("design-login", "--json"), link_event="pages", link_field="manual_url",
        done_event="done", present=_design_present, clear=_design_clear,
        what="Claude Design access", secret="CLAUDE_DESIGN_CODE"),
}


# --- state ----------------------------------------------------------------------------------

def root() -> Path:
    return Path.home() / ".canopy" / "sign-in"


def _paths(key: str) -> dict:
    d = root()
    return {"meta": d / f"{key}.json", "fifo": d / f"{key}.fifo", "out": d / f"{key}.out"}


def read_meta(key: str) -> Optional[dict]:
    try:
        doc = json.loads(_paths(key)["meta"].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def write_meta(key: str, doc: dict) -> None:
    d = root()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    meta = _paths(key)["meta"]
    tmp = meta.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    os.replace(tmp, meta)


def _alive(pid) -> bool:
    """Is `pid` still our provider process (not a recycled pid)?"""
    try:
        cmd = Path(f"/proc/{int(pid)}/cmdline").read_bytes().split(b"\0")
    except (OSError, ValueError, TypeError):
        return False
    return any(part == b"design-login" for part in cmd)


def _end(key: str, *, clear: bool) -> bool:
    """Kill the process, drop the credential (when this sign-in made it) and the files."""
    meta = read_meta(key) or {}
    provider = PROVIDERS.get(meta.get("provider", ""))
    if _alive(meta.get("pid")):
        try:
            os.killpg(int(meta["pid"]), signal.SIGTERM)
        except (OSError, ValueError):
            pass
    cleared = False
    if clear and provider is not None and meta.get("made_credential"):
        cleared = provider.clear()
    for p in _paths(key).values():
        try:
            p.unlink()
        except OSError:
            pass
    return cleared


def _fail(key: str, message: str) -> None:
    """End the sign-in but keep a short record of why, for `status` (no code, no token)."""
    meta = read_meta(key) or {}
    _end(key, clear=True)
    write_meta(key, {"provider": meta.get("provider", ""), "phase": "failed",
                     "error": str(message)[:300], "expires_at": NOW() + 15 * 60,
                     "claude_session_id": meta.get("claude_session_id", ""), "made_credential": False})


def sweep_expired() -> None:
    """End every sign-in whose lifetime has passed (any session's), as the SessionEnd hook does."""
    try:
        metas = list(root().glob("*.json"))
    except OSError:
        return
    for f in metas:
        doc = read_meta(f.stem)
        if doc is None or float(doc.get("expires_at") or 0) <= NOW():
            _end(f.stem, clear=True)


def _events(key: str) -> list:
    out = []
    try:
        for line in _paths(key)["out"].read_text(encoding="utf-8").splitlines():
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            if isinstance(doc, dict):
                out.append(doc)
    except OSError:
        pass
    return out


def _wait_event(key: str, name: str, seconds: float) -> Optional[dict]:
    deadline = NOW() + seconds
    while True:
        for ev in _events(key):
            if ev.get("event") == name:
                return ev
        meta = read_meta(key) or {}
        if not _alive(meta.get("pid")):
            # The process ended; one last look for the event it wrote on the way out.
            return next((ev for ev in _events(key) if ev.get("event") == name), None)
        if NOW() >= deadline:
            return None
        SLEEP(0.25)


def _clean_env() -> dict:
    """A provider's sign-in runs OUTSIDE any Claude Code session: no CLAUDE_* markers, no
    canopy turn identity, no tokens. Only what a CLI needs to find its own files."""
    keep = ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR", "USER", "XDG_RUNTIME_DIR")
    return {k: os.environ[k] for k in keep if k in os.environ}


def _provider(name: str) -> Provider:
    p = PROVIDERS.get(name)
    if p is None:
        raise click.ClickException(f"no such sign-in: {name} (known: {', '.join(PROVIDERS)})")
    return p


def _key() -> str:
    """The Claude session this runs in: one sign-in per session (the SessionEnd hook's key)."""
    sid = act_as_cli._claude_session()
    if not _KEY.match(sid):
        raise click.ClickException(
            "a sign-in belongs to a Claude session, and this process names none "
            "(no CLAUDE_CODE_SESSION_ID). Sign in yourself instead.")
    return sid


def _fmt_left(seconds: float) -> str:
    return f"{max(0, int(seconds // 60))} min"


# --- commands --------------------------------------------------------------------------------

@click.group("sign-in")
def sign_in_group():
    """Sign this session in to a third party with the person's own approval; gone when it ends."""


@sign_in_group.command("start")
@click.argument("provider_name", metavar="PROVIDER")
def start_cmd(provider_name):
    """Start PROVIDER's sign-in and print the link for the person to open."""
    prov = _provider(provider_name)
    if sys.platform != "linux":
        raise click.ClickException(
            f"{prov.what}: on this machine the credential is in the OS keychain, which "
            "`canopy sign-in` cannot take back out. Sign in yourself (`/design-login`).")
    if act_as_cli.person_present() is False:
        raise click.ClickException(
            "nobody is in this conversation to open the link (an emailed or scheduled turn). "
            "Say what you could not do instead.")
    binary = shutil.which("claude")
    if binary is None:
        raise click.ClickException("the `claude` CLI is not on PATH, so there is nothing to sign in with.")
    sweep_expired()
    key = _key()
    meta = read_meta(key)
    if meta and meta.get("provider") == prov.name and _alive(meta.get("pid")):
        link = next((ev.get(prov.link_field) for ev in _events(key) if ev.get("event") == prov.link_event), None)
        if link:
            _say_link(prov, link, again=True)
            return
    if meta:
        _end(key, clear=True)
    if prov.present():
        raise click.ClickException(
            f"{prov.what} is already signed in on this box (not by this session), so there is "
            "nothing to sign in; and it stays — `canopy sign-in` only removes what it made.")

    d = root()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    paths = _paths(key)
    for p in paths.values():
        p.unlink(missing_ok=True)
    os.mkfifo(paths["fifo"], 0o600)
    # O_RDWR on a FIFO never blocks and keeps a writer open, so the process never reads EOF
    # (which it takes for "cancelled") between this command and `code`.
    fifo_fd = os.open(paths["fifo"], os.O_RDWR)
    out_fd = os.open(paths["out"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        proc = subprocess.Popen(
            [binary, *prov.argv], stdin=fifo_fd, stdout=out_fd, stderr=subprocess.DEVNULL,
            env=_clean_env(), cwd=tempfile.gettempdir(), start_new_session=True)
    finally:
        os.close(fifo_fd)
        os.close(out_fd)
    write_meta(key, {"provider": prov.name, "pid": proc.pid, "phase": "waiting",
                     "started_at": NOW(), "expires_at": NOW() + 10 * 60,
                     "claude_session_id": act_as_cli._claude_session(), "made_credential": False})
    ev = _wait_event(key, prov.link_event, START_WAIT)
    link = ev.get(prov.link_field) if ev else None
    if not link:
        _end(key, clear=False)
        raise click.ClickException(f"{prov.what}: the sign-in did not give a link. "
                                   "Nothing was changed; try again.")
    _say_link(prov, link, again=False)


def _say_link(prov: Provider, link: str, *, again: bool) -> None:
    click.echo(("Still waiting. " if again else "") + f"Sign-in for {prov.what} is ready.")
    click.echo(f"Open this link: {link}")
    click.echo(f"Approve, then add the code the page shows as the chat secret {prov.secret} "
               "(chat menu → Secrets…) — not in the chat itself — within about 5 minutes.")
    click.echo(f"Once they have added it, run `canopy sign-in code {prov.name}` in the turn their "
               "next message starts (a chat's secrets can only be read inside a turn).")


def _chat_read(fn, *args):
    """A chat-secret read whose failure says what it means and carries no server text (a raw 401
    in Bash output reads, to credential_guard, as the agent's own login failing and aborts the turn)."""
    try:
        return fn(*args)
    except click.ClickException:
        raise click.ClickException(
            "could not read this chat's secrets from here (no valid chat key: run this inside a "
            "turn of the chat, not from a background process).") from None


def _deliver(prov: Provider, key: str, *, wait: bool) -> None:
    """Hand the chat secret to the waiting sign-in and record the result. Raises ClickException."""
    meta = read_meta(key)
    if not meta or meta.get("provider") != prov.name or not _alive(meta.get("pid")):
        raise click.ClickException(
            f"no {prov.name} sign-in is waiting for a code (it times out after 5 minutes). "
            f"Run `canopy sign-in start {prov.name}` again.")
    if meta.get("phase") == "signed_in":
        return
    chat = secret_cli.this_chat()
    deadline = NOW() + (SECRET_WAIT if wait else 0)
    # Only a secret nothing has read yet: the code is single-use, so one that was already used
    # (an earlier attempt's) is dead, and taking it would fail this sign-in for nothing.
    while prov.secret not in {r.get("name") for r in _chat_read(secret_cli.list_secrets, chat)
                              if not r.get("last_used_at")}:
        if NOW() >= deadline or not _alive(meta.get("pid")):
            raise click.ClickException(
                f"the secret {prov.secret} is not in this chat yet. Ask the person to add the code "
                f"the page showed under that name (chat menu → Secrets…)"
                + ".")
        SLEEP(3)
    code = _chat_read(secret_cli.fetch_value, chat, prov.secret).strip()
    if not _CODE.match(code):
        raise click.ClickException(
            f"{prov.secret} does not look like a sign-in code (it is `<code>#<state>`). "
            "Ask the person to add the whole thing the page showed.")
    meta["made_credential"] = not prov.present()
    try:
        fd = os.open(_paths(key)["fifo"], os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        raise click.ClickException("the sign-in process is no longer listening; start again.") from None
    try:
        os.write(fd, (json.dumps({"code": code}) + "\n").encode())
    finally:
        os.close(fd)
    ev = _wait_event(key, prov.done_event, CODE_WAIT)
    if ev is None or not ev.get("ok"):
        message = (ev or {}).get("message") or "no answer from the sign-in"
        _fail(key, message)
        raise click.ClickException(f"{prov.what}: {message} Run `canopy sign-in start {prov.name}` for a new link.")
    meta.update(phase="signed_in", expires_at=NOW() + LIFETIME, signed_in_at=NOW(),
                made_credential=meta["made_credential"] and prov.present())
    write_meta(key, meta)


@sign_in_group.command("code")
@click.argument("provider_name", metavar="PROVIDER")
@click.option("--wait/--no-wait", default=False,
              help="Wait for the person to add the secret (up to 4 minutes).")
def code_cmd(provider_name, wait):
    """Hand PROVIDER the code from this chat's secrets now."""
    prov = _provider(provider_name)
    _deliver(prov, _key(), wait=wait)
    click.echo(f"Signed in: {prov.what} for up to {_fmt_left(LIFETIME)}.")
    click.echo(f"`canopy sign-in stop {prov.name}` the moment you are done.")


@sign_in_group.command("status")
@click.argument("provider_name", metavar="PROVIDER", required=False)
@click.option("--json-output", "as_json", is_flag=True)
def status_cmd(provider_name, as_json):
    """This session's sign-in, if any."""
    sweep_expired()
    meta = read_meta(_key())
    if provider_name:
        _provider(provider_name)
    if not meta or (provider_name and meta.get("provider") != provider_name):
        doc = {"status": "none"}
    else:
        prov = PROVIDERS.get(meta.get("provider", ""))
        if prov is None or meta.get("phase") == "failed":
            doc = {"status": "failed", "provider": meta.get("provider", ""), "error": meta.get("error", ""),
                   "expires_in_seconds": 0, "credential_present": False}
            click.echo(json.dumps(doc) if as_json else f"sign-in failed: {doc['error']}")
            return
        left = float(meta.get("expires_at") or 0) - NOW()
        doc = {"status": meta.get("phase", "waiting"), "provider": prov.name,
               "expires_in_seconds": max(0, int(left)), "credential_present": prov.present()}
    click.echo(json.dumps(doc) if as_json else
               ("no sign-in for this session." if doc["status"] == "none" else
                f"{doc['provider']}: {doc['status']}, {_fmt_left(doc['expires_in_seconds'])} left."))


@sign_in_group.command("stop")
@click.argument("provider_name", metavar="PROVIDER", required=False)
def stop_cmd(provider_name):
    """Sign out now: remove what the sign-in wrote and end its process."""
    if provider_name:
        _provider(provider_name)
    key = _key()
    meta = read_meta(key)
    if not meta or (provider_name and meta.get("provider") != provider_name):
        click.echo("no sign-in for this session.")
        return
    cleared = _end(key, clear=True)
    click.echo("Signed out; the credential is removed from this box." if cleared
               else "Stopped; this sign-in had written no credential to remove.")
    click.echo("It stays valid at the provider until revoked (`/design-revoke`).")
