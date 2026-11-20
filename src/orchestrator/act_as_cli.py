"""`canopy act-as` — an agent acts as the person who started its turn, with their yes.

Some things only the person who asked can see or do on canopy-web (one of their private
sessions, say): the agent's own login gets a 404/403. Instead of borrowing anyone's PAT,
the agent ASKS: canopy-web pushes the request to that person, they approve it with their
passkey, and the agent gets a short-lived credential that acts as them — for at most 30
minutes, never extended, and only while the turn is still running. canopy-web logs every
call made with it as "<agent> as <person>".

    canopy act-as request --reason "…" [--scope read|write|either] [--wait/--no-wait] [--timeout S]
                          [--open/--no-open]
    canopy act-as status [--wait] [--timeout S]
    canopy act-as get PATH [--param k=v …] [--workspace SLUG]
    canopy act-as call METHOD PATH [--json BODY] [--workspace SLUG]
    canopy act-as stop

Contract (canopy-web): ``POST /api/me/act-as-requests/`` {reason, scope} →
{id, status:"pending", approve_url, expires_at}; ``GET …/{id}`` → {id, status, scope,
expires_at} and, on the FIRST read after approval only, ``token``; ``POST …/{id}/stop``.
The request, status and stop calls go out with the TURN's own agent login
(:func:`canopy_web.resolve_token`, agent-strict — never a person's PAT, never the box's).

**The credential is explicit-only.** It is kept at ``~/.canopy/act-as/<claude session>.token``
(0600, dir 0700) and used ONLY by ``canopy act-as get|call``; ``canopy_web.resolve_token``,
the MCP headers helper and every other canopy call keep acting as the agent. It is never
printed. It is ignored (and deleted) once its ``expires_at`` passes, deleted by
``stop``, and deleted when the Claude session ends (``hooks/act_as_cleanup.py``).

An older canopy-web has no act-as routes: a 404 on the request says so and exits
``EXIT_NO_ACT_AS_API`` (3).

**Waiting for the person, and putting the page in front of them.** A command's output only
reaches the chat when the command ENDS, so a blocking ``request --wait`` hides the very link
the person has to tap. The sanctioned flow when the person is in the conversation is therefore
two calls in ONE turn: ``request --no-wait`` (prints the link and returns), the agent says the
link to the person as a message, then ``status --wait`` (polls up to 240 s, under the fleet's
270 s foreground cap) and carries on in the same turn the moment they approve. Whether the
person is there is read from the presence record canopy's caller hook keeps per session
(``~/.canopy/caller/presence/<claude session>.json``): recorded ABSENT (an emailed or scheduled
turn) makes ``--wait`` default to off, because nobody is watching to tap anything; unknown keeps
the old default. On a session running on the person's own computer ``--open`` (auto) also opens
the approval page in their browser, so approving is one tap and their passkey.
"""
from __future__ import annotations

import datetime as _dt
import http.client
import json
import os
import re
import sys
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Optional

import click

from orchestrator import canopy_web

API = "/api/me/act-as-requests/"
SCOPES = ("read", "write", "either")
EXIT_NO_ACT_AS_API = 3
EXIT_STILL_PENDING = 4
#: Seconds between polls while waiting for the person's answer.
POLL_SECONDS = 3.0
#: Default wait. Under the fleet's 270 s foreground-wait cap (sleep_cap_guard): a person
#: who has not answered by then is asked again with `canopy act-as status --wait`.
DEFAULT_TIMEOUT = 240
#: While waiting, say we are still waiting this often (stderr), so a long wait is visibly alive.
NUDGE_SECONDS = 30
FINAL = ("declined", "expired", "stopped")
_KEY = re.compile(r"^[A-Za-z0-9._-]{1,100}$")

# Test seams.
TRANSPORT = None
SLEEP = time.sleep
NOW = time.time
OPEN_URL = None  # webbrowser.open unless a test says otherwise


class NoActAsApi(click.ClickException):
    """canopy-web predates act-as — nothing was asked."""

    exit_code = EXIT_NO_ACT_AS_API


class StillPending(click.ClickException):
    exit_code = EXIT_STILL_PENDING


class Transient(click.ClickException):
    """canopy-web could not be reached (DNS, connection, timeout). Local state is kept."""

#: Back-off between retries of a transient network failure inside a wait.
RETRY_SECONDS = (2.0, 4.0, 8.0, 15.0, 30.0)


def act_as_dir() -> Path:
    return Path.home() / ".canopy" / "act-as"


# --- whose turn, which login --------------------------------------------------

def grant_key() -> str:
    """The Claude session this runs in (else its turn): one grant per key.

    Keyed by the SESSION, not the turn: in a canopy-web chat each message is a new turn
    (and CANOPY_TURN_ID may be empty), while CLAUDE_CODE_SESSION_ID stays put — the
    request one turn makes is collected by the next."""
    from orchestrator import provenance

    p = provenance.resolve_parent()
    key = p.get("claude_session_id") or p.get("turn_id") or ""
    if not _KEY.match(key):
        raise click.ClickException(
            "act-as belongs to a turn, and this session names none (no CANOPY_TURN_ID, caller "
            "envelope or Claude session id). Ask the person directly instead.")
    return key


def agent_login() -> str:
    """The turn's own agent login. Refuses a person's session and a local turn: those
    already act as the person, so there is nothing to ask for."""
    from orchestrator import private_runner_session

    if private_runner_session.in_local_turn():
        raise click.ClickException(
            "this is a local turn, which already acts as you — act-as is for an agent's turn.")
    if not canopy_web.agent_context_slug():
        raise click.ClickException(
            "act-as is for an agent's turn; this session belongs to no agent "
            "(set CANOPY_AGENT or run from the agent's repo).")
    try:
        return canopy_web.resolve_token(None, agent_strict=True)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from None


# --- the stored grant ---------------------------------------------------------

def _path(key: str) -> Path:
    return act_as_dir() / f"{key}.token"


def _expires(value) -> float:
    try:
        when = _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    return when.timestamp()


def _live(doc: dict) -> bool:
    return _expires(doc.get("expires_at")) > NOW()


def read_grant(key: str) -> Optional[dict]:
    """The stored grant for ``key``, or None — an expired one is deleted, never used."""
    path = _path(key)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or not _live(doc):
        _forget(key)
        return None
    return doc


def write_grant(key: str, doc: dict) -> None:
    d = act_as_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    tmp = _path(key).with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, _path(key))


def _forget(key: str) -> None:
    try:
        _path(key).unlink()
    except OSError:
        pass


def sweep_expired() -> None:
    """Delete every grant file whose expiry has passed (any turn's)."""
    try:
        files = list(act_as_dir().glob("*.token"))
    except OSError:
        return
    for f in files:
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = {}
        if not isinstance(doc, dict) or not _live(doc):
            try:
                f.unlink()
            except OSError:
                pass


# --- HTTP ---------------------------------------------------------------------

def _send(method: str, path: str, token: str, body=None, *, base: str) -> "tuple[int, str]":
    from orchestrator.provenance import provenance_headers

    transport = TRANSPORT or canopy_web.urllib_transport
    headers = {**provenance_headers(), "Authorization": f"Bearer {token}",
               "Content-Type": "application/json", "Accept": "application/json"}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    try:
        return transport(method, base + path, headers, data)
    except (OSError, http.client.HTTPException) as exc:  # URLError is an OSError
        reason = getattr(exc, "reason", None) or exc
        raise Transient(f"could not reach canopy-web ({type(exc).__name__}: {reason}) — "
                        "nothing changed locally; try again.") from None


def _json(text: str):
    try:
        return json.loads(text) if (text or "").strip() else {}
    except ValueError:
        return None


def _detail(text: str) -> str:
    """The server's own words: a JSON ``detail``/``error``/``reason``, else the body."""
    doc = _json(text)
    if isinstance(doc, dict):
        for k in ("detail", "reason", "error", "message"):
            if doc.get(k):
                return str(doc[k])
    return (text or "").strip()[:400]


def _agent_call(method: str, path: str, body=None) -> dict:
    """A call with the turn's agent login. 404 on the collection = no act-as here."""
    base = canopy_web.resolve_base_url(None)
    status, text = _send(method, path, agent_login(), body, base=base)
    if status == 404 and path == API:
        raise NoActAsApi("this canopy-web doesn't support act-as yet (no "
                         f"{API} route) — nothing was asked.")
    if status >= 500:
        raise Transient(f"canopy-web answered {status} for {method} {path} — nothing changed "
                        "locally; try again.")
    if not (200 <= status < 300):
        raise click.ClickException(f"{method} {path} -> {status}: {_detail(text)}")
    doc = _json(text)
    if isinstance(doc, list):
        return {"results": doc}
    return doc if isinstance(doc, dict) else {}


# --- display ------------------------------------------------------------------

def _left(doc: dict) -> str:
    secs = int(_expires(doc.get("expires_at")) - NOW())
    return f"{max(secs, 0) // 60}m{max(secs, 0) % 60:02d}s"


def _say(doc: dict) -> None:
    """One line about a grant — never its token."""
    has = " (credential stored)" if doc.get("token") else ""
    click.echo(f"act-as #{doc.get('id')}: {doc.get('status')}, scope {doc.get('scope')}, "
               f"expires {doc.get('expires_at')} ({_left(doc)} left){has}")


def _public(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k != "token"}


# --- recovery -----------------------------------------------------------------

class TokenGone(click.ClickException):
    """Approved, but the one-time credential was handed out and this machine lacks it."""


def _fresh_doc(key: str, got: dict) -> dict:
    return {"id": got["id"], "status": got.get("status") or "pending",
            "scope": got.get("scope") or "", "expires_at": got.get("expires_at"),
            "approve_url": got.get("approve_url") or "", "reason": got.get("reason") or "",
            "base_url": canopy_web.resolve_base_url(None),
            "agent": canopy_web.agent_context_slug(), "key": key,
            "claude_session_id": _claude_session()}


def _recover(key: str) -> Optional[dict]:
    """Rebuild lost local state from canopy-web's list of this agent's requests in this
    session (``GET /api/me/act-as-requests/`` with the turn login; never carries tokens).
    A pending one is stored; an approved one is collected (its token is handed out once)."""
    try:
        listed = _agent_call("GET", API)
    except Transient:
        raise
    except click.ClickException:
        return None  # an older canopy-web without the list, or a refusal: nothing to recover
    items = listed.get("results") or listed.get("requests") or listed.get("items") or []
    live = [i for i in items if isinstance(i, dict) and i.get("id")
            and i.get("status") in ("pending", "approved") and _live(i)]
    if not live:
        return None
    # An approved grant first (it is what the person last said yes to), then the newest.
    live.sort(key=lambda i: (i.get("status") == "approved", str(i.get("created_at") or ""),
                             str(i.get("id"))), reverse=True)
    doc = _fresh_doc(key, live[0])
    if doc["status"] == "approved":
        return _refresh(key, doc)  # may raise TokenGone
    write_grant(key, doc)
    click.echo(f"(recovered act-as #{doc['id']} from canopy-web)", err=True)
    return doc


def load_grant(key: str) -> Optional[dict]:
    """This session's grant: the local copy, else recovered from canopy-web."""
    return read_grant(key) or _recover(key)


# --- is the person here, and can we open the page for them --------------------

def person_present() -> Optional[bool]:
    """Whether the person is in the conversation now: True / False as canopy's caller hook
    last recorded for this Claude session, None when nothing is recorded (then assume the
    old behaviour). A chat message means present; email, a schedule or an API call means not."""
    sid = _claude_session()
    if not sid or not _KEY.match(sid):
        return None
    path = Path.home() / ".canopy" / "caller" / "presence" / f"{sid}.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(doc, dict) and "present" in doc:
        return bool(doc["present"])
    return None


def on_a_desktop() -> bool:
    """True when this process runs where a person could see a browser open: a laptop with a
    display, never a cloud runner (no screen, and the page would open on nobody's)."""
    if os.path.exists("/opt/canopy-runner"):
        return False
    return sys.platform == "darwin" or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def open_for_person(url: str) -> bool:
    """Open the approval page in the person's browser. Never raises; False = it did not open."""
    try:
        return bool((OPEN_URL or webbrowser.open)(url))
    except Exception:  # noqa: BLE001 — a browser that will not open must not fail the request
        return False


def tell_the_person(url: str) -> None:
    """The one line to say to the person, as a message, BEFORE waiting."""
    click.echo("Say this to the person as a message NOW (a command's output only reaches them "
               "when it ends):")
    click.echo(f'  "Tap to approve — it asks for your passkey: {url}"')


# --- the wait -----------------------------------------------------------------

def _refresh(key: str, doc: dict) -> dict:
    """Read the request's state; store the token the first approved read carries."""
    got = _agent_call("GET", f"{API}{doc['id']}")
    merged = {**doc, **{k: v for k, v in got.items()
                        if k in ("status", "scope", "expires_at") and v}}
    if got.get("token"):
        merged["token"] = str(got["token"])
    status = merged.get("status")
    if status in FINAL:
        _forget(key)
    elif status == "approved" and not merged.get("token"):
        write_grant(key, merged)
        raise TokenGone(
            f"act-as #{doc['id']} is approved, but its one-time credential was already handed "
            "out and this machine does not hold it. Ask again (`canopy act-as request`).")
    else:
        write_grant(key, merged)
    return merged


def _wait(key: str, doc: dict, timeout: float) -> dict:
    started = NOW()
    deadline = started + max(timeout, 0)
    next_nudge = started + NUDGE_SECONDS
    misses = 0
    while True:
        try:
            doc = _refresh(key, doc)
            misses = 0
        except Transient as exc:
            if NOW() >= deadline:
                raise StillPending(
                    f"act-as #{doc['id']}: {exc.message} Still waiting on canopy-web — check "
                    "again with `canopy act-as status --wait`.") from None
            click.echo(f"(network hiccup, retrying: {exc.message})", err=True)
            SLEEP(RETRY_SECONDS[min(misses, len(RETRY_SECONDS) - 1)])
            misses += 1
            continue
        status = doc.get("status")
        if status == "approved":
            return doc
        if status in FINAL:
            raise click.ClickException(
                f"act-as #{doc['id']} was {status} — you do NOT act as them. Carry on with your "
                "own login, or tell the person what you could not do.")
        if NOW() >= deadline:
            raise StillPending(
                f"act-as #{doc['id']} is still pending (no answer in {int(timeout)}s). Check "
                "again with `canopy act-as status --wait`, or `canopy act-as stop` to withdraw it.")
        if NOW() >= next_nudge:
            gone = int(NOW() - started)
            click.echo(f"(still waiting for the person to approve — {gone // 60}m{gone % 60:02d}s so "
                       "far)", err=True)
            next_nudge = NOW() + NUDGE_SECONDS
        SLEEP(POLL_SECONDS)


# --- commands -----------------------------------------------------------------

@click.group("act-as")
def act_as_group():
    """Act as the person who started this turn — only after they approve (30 min max)."""


@act_as_group.command("request")
@click.option("--reason", required=True,
              help="What you will do as them and why — shown to the person verbatim.")
@click.option("--scope", type=click.Choice(SCOPES), default="read", show_default=True,
              help="The narrowest that does the job. read = GET only.")
@click.option("--wait/--no-wait", default=None,
              help="Poll until the person answers. Default: wait, unless the person is recorded as "
                   "not in the conversation (then nobody can tap the link).")
@click.option("--timeout", type=int, default=DEFAULT_TIMEOUT, show_default=True,
              help="Seconds to wait before giving up for now (still pending → exit 4).")
@click.option("--open/--no-open", "open_page", default=None,
              help="Open the approval page in the person's browser. Default: yes when this runs on "
                   "their own computer with a screen, never on a cloud runner.")
def request_cmd(reason, scope, wait, timeout, open_page):
    """Ask the person to let this turn act as them; print the link they approve at."""
    reason = " ".join((reason or "").split())
    if len(reason) < 10:
        raise click.UsageError("--reason must say specifically what you will do as them.")
    present = person_present()
    if wait is None:
        wait = present is not False
        if not wait:
            click.echo("(nobody is in this conversation now, so not waiting: the request is pushed to "
                       "them and waits 30 minutes. End your turn after saying so.)", err=True)
    sweep_expired()
    key = grant_key()
    try:
        held = load_grant(key)
    except TokenGone:
        held = None  # approved elsewhere and already collected: ask afresh
    if held and held.get("status") == "approved" and held.get("token"):
        click.echo("already approved for this session — reusing it, nothing new was asked.")
        _say(held)
        return
    if held and held.get("status") == "pending":
        click.echo(f"act-as #{held['id']} is already waiting for the person's answer.")
        if held.get("approve_url"):
            click.echo(f"\n  Approve at: {held['approve_url']}\n")
        doc = held
    else:
        got = _agent_call("POST", API, {"reason": reason, "scope": scope})
        if not got.get("id"):
            raise click.ClickException(f"canopy-web answered without a request id: {_public(got)}")
        doc = _fresh_doc(key, got)
        doc["scope"] = doc["scope"] or scope
        doc["reason"] = reason
        doc["expires_at"] = doc["expires_at"] or _dt.datetime.fromtimestamp(
            NOW() + 1800, _dt.timezone.utc).isoformat()
        write_grant(key, doc)
        click.echo(f"Asked to act as the person who started this turn (act-as #{doc['id']}, "
                   f"scope {scope}).")
        click.echo("SHOW THIS LINK TO THE PERSON — they approve with their passkey:")
        click.echo(f"\n  Approve at: {doc['approve_url']}\n")
    url = doc.get("approve_url") or ""
    if url and present is not False:
        tell_the_person(url)
        if open_page is None:
            open_page = on_a_desktop()
        if open_page and open_for_person(url):
            click.echo("Opened the approval page in their browser — they only tap Approve and use "
                       "their passkey.")
    if not wait:
        if present is False:
            click.echo("Not waiting: nobody is here to tap the link. Say what you will do once they "
                       "approve and end your turn; on your next turn run `canopy act-as status`.")
        else:
            click.echo("Not waiting YET. Say the link to the person as a message, then, in this same "
                       "turn, run `canopy act-as status --wait` (it polls up to 240 s and returns the "
                       "moment they approve). Only if they are not here, end your turn instead.")
        return
    doc = _wait(key, doc, timeout)
    click.echo("Approved.")
    _say(doc)
    click.echo("Use `canopy act-as get|call …`; `canopy act-as stop` the moment you are done.")


def _claude_session() -> str:
    from orchestrator import provenance

    return provenance.resolve_parent().get("claude_session_id") or ""


@act_as_group.command("status")
@click.option("--wait", is_flag=True, help="Poll a pending request until the person answers.")
@click.option("--timeout", type=int, default=DEFAULT_TIMEOUT, show_default=True)
@click.option("--json-output", "as_json", is_flag=True)
def status_cmd(wait, timeout, as_json):
    """This turn's act-as grant, if any (refreshes a pending one)."""
    sweep_expired()
    key = grant_key()
    doc = load_grant(key)
    if not doc:
        if as_json:
            click.echo(json.dumps({"status": "none"}))
        else:
            click.echo("no act-as grant for this session — you act as yourself.")
        return
    if doc.get("status") == "pending":
        doc = _wait(key, doc, timeout) if wait else _refresh(key, doc)
    if as_json:
        click.echo(json.dumps({**_public(doc), "credential": bool(doc.get("token"))}))
    else:
        _say(doc)
        if doc.get("status") == "pending" and doc.get("approve_url"):
            click.echo(f"  Approve at: {doc['approve_url']}")


@act_as_group.command("stop")
def stop_cmd():
    """End this turn's grant now (server and local copy)."""
    key = grant_key()
    try:
        doc = json.loads(_path(key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = None
    if not isinstance(doc, dict) or not doc.get("id"):
        try:
            doc = _recover(key)
        except TokenGone:
            doc = read_grant(key)
    if not isinstance(doc, dict) or not doc.get("id"):
        _forget(key)
        click.echo("no act-as grant for this session — nothing to stop.")
        return
    try:
        _agent_call("POST", f"{API}{doc['id']}/stop", {})
        click.echo(f"stopped act-as #{doc['id']} — you act as yourself again.")
    except click.ClickException as exc:
        click.echo(f"warning: canopy-web did not confirm the stop ({exc.message}); the local "
                   "credential is deleted anyway and the grant still ends at its expiry.",
                   err=True)
    finally:
        _forget(key)


def _held_token() -> "tuple[str, dict]":
    key = grant_key()
    doc = read_grant(key)
    if doc and doc.get("status") == "pending":
        doc = _refresh(key, doc)  # approved since? collect the credential now
    elif not doc:
        doc = _recover(key)
    if not doc or not doc.get("token"):
        if doc and doc.get("status") == "pending":
            raise click.ClickException(
                f"act-as #{doc['id']} is still waiting for the person's approval — "
                "`canopy act-as status --wait`.")
        raise click.ClickException(
            "no live act-as grant for this session (none asked, declined, stopped or expired). "
            "Ask with `canopy act-as request --reason …` — or use your own login.")
    return key, doc


def _clean_path(path: str) -> str:
    path = (path or "").strip()
    if not path.startswith("/") or path.startswith("//") or "://" in path:
        raise click.BadParameter("a canopy-web path starting with / (e.g. /api/sessions/123/) "
                                 "— never a URL: the credential only ever goes to canopy-web.",
                                 param_hint="PATH")
    return path


def _as_person(method: str, path: str, body, workspace: Optional[str]) -> None:
    key, doc = _held_token()
    method = method.upper()
    if doc.get("scope") == "read" and method != "GET":
        raise click.ClickException(
            f"this grant is read-only (scope read) — {method} is not allowed. Ask again with "
            "--scope write if the person agreed to that.")
    path = canopy_web.scoped_api_path(_clean_path(path), workspace)
    base = doc.get("base_url") or canopy_web.resolve_base_url(None)
    status, text = _send(method, path, doc["token"], body, base=base)
    if status == 401:
        _forget(key)
        raise click.ClickException(
            f"canopy-web no longer accepts this act-as credential ({_detail(text)}) — the grant "
            "ended (stopped, expired, or the turn finished). Deleted the local copy.")
    if status == 403:
        raise click.ClickException(f"refused as the person: {_detail(text)}")
    if not (200 <= status < 300):
        raise click.ClickException(f"{method} {path} -> {status}: {_detail(text)}")
    parsed = _json(text)
    click.echo(json.dumps(parsed, indent=2) if parsed is not None else text)


@act_as_group.command("get")
@click.argument("path")
@click.option("--param", "params", multiple=True, help="Query parameter k=v (repeatable).")
@click.option("--workspace", default=None, help="Workspace slug (default: $CANOPY_WEB_WORKSPACE).")
def get_cmd(path, params, workspace):
    """GET a canopy-web path AS THE PERSON (needs an approved grant)."""
    pairs = []
    for p in params:
        if "=" not in p:
            raise click.BadParameter(f"{p!r}: use k=v", param_hint="--param")
        pairs.append(tuple(p.split("=", 1)))
    if pairs:
        path = f"{path}{'&' if '?' in path else '?'}{urllib.parse.urlencode(pairs)}"
    _as_person("GET", path, None, workspace)


@act_as_group.command("call")
@click.argument("method", type=click.Choice(["GET", "POST", "PUT", "PATCH", "DELETE"],
                                             case_sensitive=False))
@click.argument("path")
@click.option("--json", "body", default=None, help="JSON request body.")
@click.option("--workspace", default=None, help="Workspace slug (default: $CANOPY_WEB_WORKSPACE).")
def call_cmd(method, path, body, workspace):
    """Make any canopy-web request AS THE PERSON (writes need a write-scope grant)."""
    parsed = None
    if body is not None:
        try:
            parsed = json.loads(body)
        except ValueError as exc:
            raise click.BadParameter(f"not JSON: {exc}", param_hint="--json") from None
    _as_person(method, path, parsed, workspace)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(act_as_group())
