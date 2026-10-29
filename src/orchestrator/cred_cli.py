"""`canopy cred` — may this session act as agent X, and if so, give it X's env.

The contract is docs/architecture/session-identity.md (canopy#850, "Design
revision", 2026-10-09). In short:

* **A runner turn** (a turn id is known: ``$CANOPY_TURN_ID``, the caller
  envelope, or the laptop's by-task record) may act only as the agent it was
  dispatched for (``$CANOPY_AGENT_SLUG`` / ``$CANOPY_AGENT`` / the envelope's
  ``agent``). Any other ``--agent`` is refused.
* **A human session** may act as X when canopy-web says so (the Canopy vault is where every
  agent's secrets live): ``GET /api/agents/X/credentials/access`` says ``may_resolve``.
* Anything else acts as the human, and the refusal says what access to get.

Commands::

    canopy cred check  [--agent X] [--json]   exit 0 allowed, 3 refused
    canopy cred env    [--agent X] [--refresh] prints ~/.X/.env (resolved if missing)
    canopy cred env    --slug X [--runner] --out PATH    X's env, resolved in memory, to PATH (0600)
    canopy cred env    --slug X --runner --print-json    X's env as JSON on stdout (a runner's cache)
    canopy cred refresh [--agent X]           re-resolve ~/.X/.env
    canopy cred sync   [--agent X] [--ttl S]  re-inject ~/.X/.env iff its vault changed
    canopy cred exec   [--agent X] -- <cmd>   run <cmd> with X's env in the CHILD only (no file)
    canopy cred google-token [--agent X]      X's Google access token -> a 0600 file; prints its path
    canopy cred google-token --delegated A [--agent X]   the access token for Google account A a
                                              person delegated to X, as staged by the runner; its path
    canopy cred google-token --runner --delegated A --agent X   (the runner) stage that file
    canopy cred connection-token P [--refresh]  the person's own access token for their canopy-web
                                              connection to provider P (e.g. salesforce) ->
                                              ~/.canopy/connections/P.json; its path (--refresh: a new
                                              token, e.g. after the provider answered 401)
    canopy cred connection-token P --agent X  in a turn: the path of the P token the runner staged for X
    canopy cred connection-token P --runner --agent X --runner-id R --turn T [--min-life S] [--refresh]
                                              (the runner) stage ~/.canopy/connections/X/P.json; exit 3
                                              when X has no P connection
    canopy cred salesforce-token …            = connection-token salesforce …
    canopy cred whoami [--json]               who this session is, and why

Exit codes (stable — agent-identity MCP servers shell out to `check`):
    0 allowed · 1 error (resolve failed) · 2 usage (no agent named or detectable)
    3 refused (stderr: one actionable paragraph) · 4 undetermined (could not decide)

No command ever prints a secret value (google-token / connection-token print a path, not the token) — except
`cred env --print-json`, whose caller is a runner process keeping the values in memory.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

import click

from orchestrator import canopy_web

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_UNDETERMINED = 4

#: How long a canopy-web /access answer is trusted. An MCP server checks before every
#: action; asking canopy-web each time would be slow.
CACHE_TTL = 600
CACHE_DIR = Path.home() / ".canopy"
CACHE_FILE = CACHE_DIR / "cred-cache.json"

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$", re.I)
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

Runner = Callable[..., subprocess.CompletedProcess]


class CredError(click.ClickException):
    """A failure with an exit code of its own."""

    def __init__(self, message: str, code: int = EXIT_ERROR):
        super().__init__(message)
        self.exit_code = code


# ──────────────────────────────────────────────────────────────────────────────
# who is this session
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Session:
    kind: str                 # "agent-turn" | "human"
    agent: str = ""           # the turn's agent, or the human session's agent context
    turn_id: str = ""
    why: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _turn_id() -> tuple[str, str]:
    """(turn id, where it came from), or ("", "")."""
    env = os.environ.get("CANOPY_TURN_ID", "").strip()
    if env:
        return env, "$CANOPY_TURN_ID"
    try:
        from orchestrator import provenance
        turn = provenance.resolve_parent().get("turn_id") or ""
    except Exception:  # noqa: BLE001 — identity lookup must never crash
        turn = ""
    return (turn, "the turn's caller envelope / by-task record") if turn else ("", "")


def _turn_agent() -> tuple[str, str]:
    """(the agent a runner turn belongs to, where that came from)."""
    for var in ("CANOPY_AGENT_SLUG", "CANOPY_AGENT"):
        slug = os.environ.get(var, "").strip()
        if slug and _SLUG_RE.match(slug):
            return slug, f"${var}"
    slug = canopy_web._dispatched_agent_slug()
    return (slug, "the turn's caller envelope") if slug else ("", "")


def session_identity() -> Session:
    turn, turn_src = _turn_id()
    if turn:
        agent, agent_src = _turn_agent()
        if agent:
            why = (f"runner turn {turn} (from {turn_src}) dispatched for agent "
                   f"'{agent}' (from {agent_src}); it may act only as '{agent}'")
        else:
            why = (f"runner turn {turn} (from {turn_src}) names no agent — it may not act "
                   f"as any agent")
        return Session("agent-turn", agent, turn, why)
    agent = canopy_web.agent_context_slug()
    why = "no turn id ($CANOPY_TURN_ID unset, no dispatched-turn record), so this is a person's session"
    if agent:
        why += (f"; it is working in agent '{agent}'s context — whether it may act as "
                f"'{agent}' is `canopy cred check`'s call")
    return Session("human", agent, "", why)


def default_agent() -> str:
    """The session's agent, for a command run without --agent."""
    return canopy_web.agent_context_slug()


def _require_agent(agent: Optional[str]) -> str:
    slug = (agent or "").strip() or default_agent()
    if not slug:
        raise CredError(
            "this session is not an agent — no $CANOPY_AGENT_SLUG / $CANOPY_AGENT, no "
            "dispatched turn, and the cwd is not an agent repo. Pass --agent <slug>.",
            EXIT_USAGE)
    if not _SLUG_RE.match(slug):
        raise CredError(f"not an agent slug: {slug!r}", EXIT_USAGE)
    return slug


# ──────────────────────────────────────────────────────────────────────────────
# cache (~/.canopy/cred-cache.json) — verdicts and backend, never values
# ──────────────────────────────────────────────────────────────────────────────

def _cache_load() -> dict:
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _cache_get(key: str, now: Optional[float] = None) -> Optional[dict]:
    entry = _cache_load().get(key)
    if not isinstance(entry, dict):
        return None
    if (now or time.time()) - float(entry.get("at") or 0) > CACHE_TTL:
        return None
    return entry.get("value")


def _cache_put(key: str, value: dict) -> None:
    data = _cache_load()
    data[key] = {"at": time.time(), "value": value}
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        _write_private(CACHE_FILE, json.dumps(data, indent=1, sort_keys=True))
    except OSError:
        pass  # a cache that can't be written only costs a re-probe


def _cache_drop(slug: str) -> None:
    data = _cache_load()
    keep = {k: v for k, v in data.items() if k.rsplit("|", 1)[-1] != slug}
    if keep != data:
        try:
            _write_private(CACHE_FILE, json.dumps(keep, indent=1, sort_keys=True))
        except OSError:
            pass


def _write_private(path: Path, data: str) -> None:
    """Write `data` to `path` via a 0600 temp file in the same dir + atomic rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        if hasattr(os, "fchmod") and os.name != "nt":  # no file modes on Windows
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ──────────────────────────────────────────────────────────────────────────────
# canopy-web: GET /api/agents/{slug}/credentials/access
# ──────────────────────────────────────────────────────────────────────────────

def _human_token() -> str:
    """The person's own canopy-web PAT (the same operator rule `agent bootstrap` uses:
    explicit env, else the workbench-token)."""
    from orchestrator.agent_bootstrap import _operator_token
    return _operator_token()


def fetch_access(slug: str, *, refresh: bool = False,
                 transport: Optional[canopy_web.Transport] = None) -> dict:
    """canopy-web's verdict for this caller on agent ``slug`` — cached CACHE_TTL.

    Returns the /access body plus ``error`` ("" on success). On any failure
    ``may_resolve`` is False, so an unreachable canopy-web never grants anything by itself.

    Names the current turn (``X-Canopy-Turn-Id``) when there is one: a licence holder
    may resolve only during their own local turn, and canopy-web can say so only when
    it knows which turn is asking."""
    from orchestrator.cred_resolve import TURN_HEADER, current_turn_id
    turn = current_turn_id()
    key = (f"access|{canopy_web.resolve_base_url(None)}|" + (f"{turn}|" if turn else "") + slug)
    if not refresh:
        hit = _cache_get(key)
        if hit is not None:
            return hit
    try:
        body = canopy_web.call("GET", f"/api/agents/{slug}/credentials/access",
                               token=_human_token(), transport=transport,
                               headers={TURN_HEADER: turn} if turn else None) or {}
    except Exception as e:  # noqa: BLE001 — degrade, don't traceback
        return {"agent": slug, "may_resolve": False, "via": None, "reason": "",
                "error": str(e).splitlines()[0][:200] if str(e) else type(e).__name__}
    out = {"agent": slug,
           "may_resolve": bool(body.get("may_resolve")),
           "via": body.get("via"), "reason": str(body.get("reason") or ""), "error": "",
           # Credentials people delegated to the agent: never given to you; bring your own.
           "delegated_withheld": [w for w in body.get("delegated_withheld") or [] if isinstance(w, dict)]}
    _cache_put(key, out)
    return out


#: How this machine runs an agent (`machine_mode`): a runner box that serves it, an admin's
#: own laptop, a person's licence to use it locally — or not known (offline, an error).
MODES = ("runner", "admin", "license", "unknown")
#: The mode lookup sits on failure paths (a preflight's fix line): never wait long for it.
MODE_TIMEOUT = 5.0
_MODES: dict = {}


def _timeout_transport(timeout: float) -> "canopy_web.Transport":
    import urllib.error
    import urllib.request

    def send(method: str, url: str, headers: dict, body: Optional[bytes]):
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
    return send


def machine_mode(slug: str, *, transport: Optional["canopy_web.Transport"] = None) -> str:
    """How THIS machine runs agent ``slug``: "runner" | "admin" | "license" | "unknown".

    canopy-web's `/credentials/access` answer (`fetch_access`, cached CACHE_TTL) says as whom
    this caller reads the agent's secrets. Outside a turn a licence holder reads nothing, so
    `via` is None for them; then an ACTIVE licence of yours in `/licenses` still says
    "license". Any failure is "unknown" — this only picks a fix line, so it never raises
    and asks with a short timeout. Cached per process.

    A licence holder repairs the agent's Google login with `canopy agent use` /
    `canopy agent google-login`, never with `canopy agent bootstrap` or a mailbox
    re-connect (2026-10-28, failure investigation 17: ace on a licensee's laptop)."""
    if slug in _MODES:
        return _MODES[slug]
    mode = "unknown"
    try:
        tr = transport or _timeout_transport(MODE_TIMEOUT)
        access = fetch_access(slug, transport=tr)
        via = access.get("via")
        if via in ("runner", "admin", "license"):
            mode = via
        elif not access.get("error") and _holds_active_licence(slug, tr):
            mode = "license"
    except Exception:  # noqa: BLE001 — a fix line must never cost the preflight
        mode = "unknown"
    _MODES[slug] = mode
    return mode


def _holds_active_licence(slug: str, transport) -> bool:
    token = _human_token()
    me = canopy_web.call("GET", "/api/me/", token=token, transport=transport) or {}
    my_id = str(me.get("id") or me.get("user_id") or "")
    my_email = str(me.get("email") or "").lower()
    rows = canopy_web.call("GET", f"/api/agents/{slug}/licenses", token=token,
                           transport=transport) or []
    if isinstance(rows, dict):
        rows = rows.get("items") or rows.get("results") or []
    return any(isinstance(r, dict) and r.get("status") == "active" and (
        (my_id and str(r.get("user_id")) == my_id)
        or (my_email and str(r.get("email") or "").lower() == my_email)) for r in rows)


# ──────────────────────────────────────────────────────────────────────────────
# the decision
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Verdict:
    agent: str
    allowed: bool
    identity: str               # "agent-turn" | "human"
    via: Optional[str] = None   # "runner" | "admin" | "license" | "local-env" | "turn" | None
    reason: str = ""            # short machine-ish reason
    message: str = ""           # the one-paragraph human message (refusals)
    exit_code: int = EXIT_OK
    notes: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _refusal_canopy_web(slug: str, access: dict) -> str:
    err = access.get("error") or ""
    if " 404" in err or "-> 404" in err:
        # canopy-web answers /access with 404 to a non-member of the agent's workspace.
        return (f"Acting as agent '{slug}' needs canopy-web to hand this session its "
                f"credentials, and canopy-web does not show you agent '{slug}' (not a member "
                f"of its workspace) — ask a workspace admin to invite you first, then run "
                f"`canopy agent use {slug}`. Until then this session acts as you, not as "
                f"'{slug}'.")
    if err:
        return (f"Acting as agent '{slug}' needs canopy-web to hand this session its "
                f"credentials from canopy vault, and canopy-web could not be asked "
                f"({access['error']}). Check your connection and sign-in (`canopy agent use "
                f"{slug}` checks both), then run `canopy cred refresh --agent {slug}`. Until "
                f"then this session acts as you, not as '{slug}'.")
    why = access.get("reason") or "you are neither a runner operator for it nor its owner/admin"
    return (f"Acting as agent '{slug}' needs canopy-web to hand this session its credentials "
            f"(agent '{slug}' keeps them in canopy vault), and canopy-web says you may not: "
            f"{why}. To use '{slug}' yourself, run `canopy agent use {slug}` (it says what is "
            f"missing: your licence, this device's grant) and work inside a Claude Code "
            f"session of '{slug}'; to act as it from anywhere, ask {slug}'s owner to make you "
            f"an admin of '{slug}' (/agents/{slug} → Settings), then run `canopy cred refresh "
            f"--agent {slug}`. Until then this session acts as you, not as '{slug}'.")


def decide(slug: str, *, refresh: bool = False, local_env_counts: bool = True) -> Verdict:
    sess = session_identity()
    if sess.kind == "agent-turn":
        if sess.agent and sess.agent == slug:
            return Verdict(slug, True, "agent-turn", via="turn",
                           reason=f"runner turn {sess.turn_id} is agent '{slug}'s own")
        who = f"agent '{sess.agent}'s" if sess.agent else "a"
        msg = (f"This is {who} runner turn ({sess.turn_id}), so it may act only as "
               f"{repr(sess.agent) if sess.agent else 'no agent'} — not as '{slug}'. To get "
               f"work done as '{slug}', dispatch it or ask it (`canopy agent dispatch --slug "
               f"{slug} …`, or `{slug}:ask`); never resolve another agent's credentials.")
        return Verdict(slug, False, "agent-turn", via=None, reason="turn-is-another-agent",
                       message=msg, exit_code=EXIT_REFUSED)

    # A human who already resolved X's env on this machine (`canopy cred env`) keeps acting
    # as X without asking canopy-web again on every server start. Only a private file
    # counts; --refresh always re-proves access. Runner turns never reach this branch: they
    # stay limited to their own agent above.
    if local_env_counts and not refresh and _private_env(env_path(slug)):
        return Verdict(slug, True, "human", via="local-env",
                       reason=f"{env_path(slug)} already resolved on this machine")

    access = fetch_access(slug, refresh=refresh)
    notes = []
    if access.get("error"):
        notes.append(f"canopy-web /access unavailable ({access['error']})")
    if access.get("may_resolve"):
        return Verdict(slug, True, "human", via=access.get("via"),
                       reason=access.get("reason") or "canopy-web: may_resolve", notes=notes)
    return Verdict(slug, False, "human", via=None,
                   reason=access.get("reason") or "canopy-web: may not resolve",
                   message=_refusal_canopy_web(slug, access), exit_code=EXIT_REFUSED,
                   notes=notes)


# ──────────────────────────────────────────────────────────────────────────────
# resolving ~/.<slug>/.env
# ──────────────────────────────────────────────────────────────────────────────

def env_path(slug: str) -> Path:
    """The agent's env file to read: this turn's ``$CANOPY_AGENT_ENV_FILE`` when the turn is
    ``slug``'s and the file exists, else ``~/.<slug>/.env`` (`orchestrator.agent_env_file`)."""
    from orchestrator.agent_env_file import agent_env_path
    return agent_env_path(slug)


def legacy_env_path(slug: str) -> Path:
    """``~/.<slug>/.env`` — the only file the writers here ever write. A per-turn file
    (``$CANOPY_AGENT_ENV_FILE``) is the runner's to write and remove, never canopy's."""
    from orchestrator.agent_env_file import legacy_env_path as _legacy
    return _legacy(slug)


def turn_env_file(slug: str) -> Optional[Path]:
    from orchestrator.agent_env_file import turn_env_file as _turn
    return _turn(slug)


def _private_env(path: Path) -> bool:
    """A non-empty env file this user owns that nobody else can read (mode 0600-ish)."""
    try:
        st = path.stat()
    except OSError:
        return False
    return (path.is_file() and st.st_size > 0 and st.st_uid == os.getuid()
            and not st.st_mode & 0o077)


def _fresh(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _env_line(name: str, value: str) -> str:
    return f"{name}={value}"


def fetch_canopy_web_values(slug: str, refs: Optional[list] = None) -> dict:
    """canopy-web's values for ``slug`` (only ``refs`` when given), through the one
    resolve client: it adds the turn id and this machine's device proof when known."""
    from orchestrator import cred_resolve
    try:
        body = cred_resolve.resolve(slug, token=_human_token(), refs=refs)
    except Exception as e:  # noqa: BLE001 — report, don't traceback (never the body)
        raise CredError(f"could not resolve '{slug}'s credentials from canopy-web: "
                        f"{str(e).splitlines()[0][:300] if str(e) else type(e).__name__}")
    values = body.get("values") or {}
    if not isinstance(values, dict):
        raise CredError(f"canopy-web returned no credential values for '{slug}'")
    return values


def env_values(values: dict) -> tuple[dict, list]:
    """({NAME: value} usable as environment variables, [names that are not])."""
    usable, skipped = {}, []
    for name in sorted(values):
        val = values[name]
        val = "" if val is None else str(val)
        if not _ENV_NAME_RE.match(str(name)) or "\n" in val:
            skipped.append(str(name))  # not an env var (e.g. gog-token): staged elsewhere
            continue
        usable[str(name)] = val
    return usable, skipped


def write_env_from_values(slug: str, out: Path, values: dict) -> tuple[int, list]:
    """Write canopy-web's ``values`` to ``out`` (0600) as an env file: (vars written,
    names that are not env vars and were left out)."""
    usable, skipped = env_values(values)
    lines = [_env_line(n, v) for n, v in usable.items()]
    header = (f"# {slug} credentials, resolved from canopy-web by canopy "
              f"— do not commit.\n")
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write_private(out, header + "\n".join(lines) + ("\n" if lines else ""))
    return len(lines), skipped


def values_fingerprint(values: dict) -> str:
    """A hash over names and value hashes — says whether anything changed, reveals nothing."""
    import hashlib
    h = hashlib.sha256()
    for name in sorted(values):
        h.update(name.encode() + b"\0" + hashlib.sha256(str(values[name]).encode()).digest())
    return "canopy-web:" + h.hexdigest()


def resolve_canopy_web(slug: str, out: Path) -> str:
    n, skipped = write_env_from_values(slug, out, fetch_canopy_web_values(slug))
    note = f"canopy-web ({n} vars"
    if skipped:
        note += f"; {len(skipped)} non-env credential(s) not written: {', '.join(skipped)}"
    return note + ")"


def ensure_env(slug: str, *, refresh: bool = False) -> tuple[Path, str]:
    """(path, what happened). Raises CredError(EXIT_REFUSED) when the session may not
    act as ``slug``."""
    verdict = decide(slug, refresh=refresh)
    if not verdict.allowed:
        raise CredError(verdict.message, EXIT_REFUSED)
    turn_file = turn_env_file(slug)
    if turn_file is not None:
        # The runner wrote this turn's env and removes it when the turn ends; canopy never
        # writes it, and never writes a second, persistent copy beside it.
        return turn_file, "present ($CANOPY_AGENT_ENV_FILE, this turn's env)"
    out = legacy_env_path(slug)
    if _fresh(out) and not refresh:
        try:
            os.chmod(out, 0o600)
        except OSError:
            pass
        return out, "present"
    how = resolve_canopy_web(slug, out)
    return out, f"resolved from {how}"


# ──────────────────────────────────────────────────────────────────────────────
# env --out / --print-json: resolve in memory, hand over once (secrets off disk)
# ──────────────────────────────────────────────────────────────────────────────

#: The runner's tmpfs root for per-turn env files; `--out` must land inside it when set.
TURN_ENV_ROOT_VAR = "CANOPY_TURN_ENV_ROOT"


def confine_out(out: Path) -> Path:
    """``out`` made absolute, refused (EXIT_USAGE) when ``$CANOPY_TURN_ENV_ROOT`` is set and
    ``out`` (symlinks resolved) is not inside it — so a turn cannot aim the runner's write
    at a persistent path."""
    out = Path(out).expanduser()
    out = out if out.is_absolute() else Path.cwd() / out
    root = os.environ.get(TURN_ENV_ROOT_VAR, "").strip()
    if not root:
        return out
    root_real = os.path.realpath(root)
    out_real = os.path.realpath(out)
    if out_real == root_real or os.path.commonpath([root_real, out_real]) != root_real:
        raise CredError(f"refusing to write outside ${TURN_ENV_ROOT_VAR} ({root}): {out}",
                        EXIT_USAGE)
    return Path(out_real)


def mkdirs_private(path: Path) -> None:
    """Create ``path`` and every MISSING parent at 0700 (umask notwithstanding); existing
    directories are left as they are."""
    path = Path(path)
    missing = []
    while not path.exists():
        missing.append(path)
        if path.parent == path:
            break
        path = path.parent
    for d in reversed(missing):
        try:
            d.mkdir(mode=0o700)
        except FileExistsError:
            continue
        os.chmod(d, 0o700)


def write_env_out(out: Path, values: dict, slug: str = "") -> int:
    """Write ``values`` (env-var-shaped ones) to ``out``: parents 0700, the file 0600 via an
    atomic rename. Returns how many variables it wrote."""
    usable, _ = env_values(values)
    out = confine_out(out)
    mkdirs_private(out.parent)
    header = f"# {slug} credentials for one turn, written by canopy — removed when it ends.\n"
    _write_private(out, header + "".join(f"{_env_line(n, v)}\n" for n, v in usable.items()))
    os.chmod(out, 0o600)
    return len(usable)


def runner_env_values(slug: str, *, agent_root: Path, base_url: str = "", token: str = "",
                      runner: Runner = subprocess.run) -> tuple[dict, str]:
    """({NAME: value}, backend) — `runner_env_bundle` without the extras."""
    values, how, _ = runner_env_bundle(slug, agent_root=agent_root, base_url=base_url,
                                       token=token, runner=runner)
    return values, how


def runner_env_bundle(slug: str, *, agent_root: Path, base_url: str = "", token: str = "",
                      runner: Runner = subprocess.run) -> tuple[dict, str, dict]:
    """({NAME: value}, backend) as a RUNNER resolves them for one turn — the same
    credentials `canopy agent prepare --runner` uses (canopy-web `/credentials/resolve` with
    the runner's token), held in memory — plus the resolve's top-level `delegated`,
    when it returned one (`agent_prepare.RESOLVE_EXTRAS`), from
    the same resolve call (device proof and zero-knowledge unwrap included)."""
    from orchestrator.agent_prepare import RunnerPreparer
    base = base_url or os.environ.get("CANOPY_BASE_URL", "").strip() or \
        canopy_web.resolve_base_url(None)
    tok = token or os.environ.get("CANOPY_TOKEN", "").strip() or canopy_web.resolve_token(None)
    prep = RunnerPreparer(base_url=base, token=tok, agent_root=Path(agent_root), runner=runner,
                          no_secret_files=True, echo=lambda _m: None,
                          in_turn=bool(os.environ.get("CANOPY_TURN_ID", "").strip()))
    prep._slug = slug
    try:
        values, how = prep.env_values(slug, Path(agent_root) / slug)
    except RuntimeError as e:
        raise CredError(f"could not resolve '{slug}'s env: {e}")
    return values, how, dict(prep.creds(slug).extras)


# ──────────────────────────────────────────────────────────────────────────────
# exec: the agent's env in ONE child process — no file, no session env
# ──────────────────────────────────────────────────────────────────────────────

def exec_env(slug: str, *, refs: Optional[list] = None) -> tuple[dict, str]:
    """({NAME: value} for the child, which backend). The same rules as `cred check`
    decide — a runner turn only for its own agent — except that an already-resolved
    ~/.<slug>/.env is NOT a reason to allow: exec never touches that file."""
    verdict = decide(slug, local_env_counts=False)
    if not verdict.allowed:
        raise CredError(verdict.message, EXIT_REFUSED)
    values, _ = env_values(fetch_canopy_web_values(slug, refs=refs))
    how = "canopy-web"
    if refs:
        missing = [r for r in refs if r not in values]
        if missing:
            raise CredError(f"'{slug}' has no {', '.join(missing)} in {how}")
        values = {k: values[k] for k in refs}
    return values, how


# ──────────────────────────────────────────────────────────────────────────────
# keeping ~/.<slug>/.env current with the vault, cheaply
# ──────────────────────────────────────────────────────────────────────────────

#: Re-checked at most this often per agent. Shared across processes through the stamp,
#: because the caller is a SessionStart hook: a new process every session.
SYNC_TTL = 300


def stamp_path(slug: str) -> Path:
    """Beside the env file: the fingerprint last written. The cloud runner writes the
    same file in the same format (`cloud_runner._refresh_agent_env`)."""
    return legacy_env_path(slug).with_name(".env.stamp")


def sync_env(slug: str, *, ttl: float = SYNC_TTL, now: Optional[float] = None) -> str:
    """Bring ~/.<slug>/.env up to date with the agent's Canopy vault, without waste.
    Returns what happened: "skipped: …", "unchanged", or "re-injected from canopy-web".

    Throttled to once per `ttl` per agent via the stamp; when due, the values are fetched
    and the file re-written only when their fingerprint moved. A failure keeps the existing
    file and raises CredError with the reason.

    A turn whose runner handed it ``$CANOPY_AGENT_ENV_FILE`` is skipped: that file is the
    turn's env, written fresh at claim, and there is no persistent copy to keep current."""
    if turn_env_file(slug) is not None:
        return "skipped: this turn's env is $CANOPY_AGENT_ENV_FILE (the runner's)"
    out, stamp = legacy_env_path(slug), stamp_path(slug)
    now = time.time() if now is None else now
    try:
        prior = json.loads(stamp.read_text(encoding="utf-8"))
        prior = prior if isinstance(prior, dict) else {}
    except (OSError, ValueError):
        prior = {}
    if out.is_file() and now - float(prior.get("checked_at") or 0) < ttl:
        return "skipped: checked recently"
    access = fetch_access(slug)
    sess = session_identity()
    if sess.kind == "agent-turn" and sess.agent != slug:
        return f"skipped: {sess.why}"
    if not access.get("may_resolve"):
        return "skipped: canopy-web will not resolve this agent for this session"

    def record(**fields) -> None:
        try:
            out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _write_private(stamp, json.dumps({**prior, **fields, "checked_at": now}))
        except OSError:
            pass  # an unwritable stamp only costs a re-check

    try:
        values = fetch_canopy_web_values(slug)
    except CredError:
        record()
        raise
    fingerprint = values_fingerprint(values)
    if out.is_file() and fingerprint == prior.get("fingerprint"):
        record()
        return "unchanged"
    write_env_from_values(slug, out, values)
    record(fingerprint=fingerprint, injected_at=now)
    return "re-injected from canopy-web"


# ──────────────────────────────────────────────────────────────────────────────
# google-token: a short-lived access token for the agent's OWN Google login
# ──────────────────────────────────────────────────────────────────────────────

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
#: A token with less life left than this is minted again, so a caller that reads the
#: file never holds one that expires mid-call.
GOOGLE_TOKEN_MIN_LIFE = 300


def google_token_path(slug: str) -> Path:
    return CACHE_DIR / "google-token" / f"{slug}.json"


def _mint_google_token(account: str, client: str, *, runner: Runner) -> dict:
    """Exchange the agent's stored gog refresh token for an access token. The refresh
    token passes through a 0600 temp file that is deleted before this returns."""
    import urllib.error
    import urllib.parse
    import urllib.request

    from orchestrator.agent_email import GOG_CONFIG_DIR, gog_client_credentials

    creds_path = gog_client_credentials(GOG_CONFIG_DIR, client)
    if not creds_path:
        raise CredError(f"gog holds no OAuth client '{client}' on this machine — "
                        f"`canopy email preflight --account {account}` says how to set it up")
    tmpdir = tempfile.mkdtemp(prefix="canopy-gtok-")
    exported = os.path.join(tmpdir, "token.json")
    try:
        try:
            r = runner(["gog", "auth", "tokens", "export", account, "--client", client,
                        "--out", exported, "--overwrite", "--no-input"],
                       capture_output=True, text=True, timeout=45)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise CredError(f"could not run gog to read {account}'s Google login: {e}")
        if r.returncode != 0 or not os.path.isfile(exported):
            err = " ".join((r.stderr or r.stdout or "").split())[:300]
            raise CredError(f"{account} has no Google login under client '{client}' on this "
                            f"machine ({err or f'gog exited {r.returncode}'}) — "
                            f"sign it in: `gog login {account} --client {client}`")
        with open(exported, encoding="utf-8") as fh:
            refresh = (json.load(fh).get("refresh_token") or "").strip()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    if not refresh:
        raise CredError(f"gog's stored login for {account} has no refresh token")
    with open(creds_path, encoding="utf-8") as fh:
        c = json.load(fh)
    c = c.get("installed") or c.get("web") or c
    body = urllib.parse.urlencode({
        "client_id": c.get("client_id", ""), "client_secret": c.get("client_secret", ""),
        "refresh_token": refresh, "grant_type": "refresh_token",
    }).encode()
    try:
        with urllib.request.urlopen(GOOGLE_TOKEN_URL, body, timeout=30) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            reason = json.load(e).get("error_description") or ""
        except Exception:  # noqa: BLE001
            reason = ""
        raise CredError(f"Google refused {account}'s login ({e.code} {reason}) — sign it in "
                        f"again: `gog login {account} --client {client}`")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise CredError(f"could not reach Google to refresh {account}'s login: {e}")
    if not data.get("access_token"):
        raise CredError(f"Google returned no access token for {account}")
    return data


def google_token(slug: str, *, runner: Optional[Runner] = None,
                 now: Optional[float] = None) -> Path:
    """Write a fresh access token for agent ``slug``'s own Google login to
    ~/.canopy/google-token/<slug>.json (0600) and return the path.

    For a tool that should act on Google AS the agent rather than as a shared service
    account (chrome-sales' Drive tools). The login is the one the runner
    already keeps for the agent's mailbox — `gog`, under the client `canopy email client`
    picks — so there is no second credential to provision. A runner turn may only ask for
    its own agent's token; a person's session may ask for any agent whose login this
    machine holds, as it could already run gog as that agent. The token is reused while
    it has more than GOOGLE_TOKEN_MIN_LIFE seconds left."""
    from orchestrator.agent_email import (AgentEmailError, find_agent_repo,
                                          reconcile_client, resolve_email_identity)

    runner = runner or subprocess.run
    now = time.time() if now is None else now
    session = session_identity()
    if session.kind == "agent-turn" and session.agent != slug:
        raise CredError(f"refused: {session.why}", EXIT_REFUSED)
    out = google_token_path(slug)
    try:
        cached = json.loads(out.read_text(encoding="utf-8"))
        if float(cached.get("expires_at") or 0) - now > GOOGLE_TOKEN_MIN_LIFE:
            return out
    except (OSError, ValueError, AttributeError):
        pass
    try:
        ident = resolve_email_identity(find_agent_repo(slug))
    except AgentEmailError as e:
        raise CredError(f"no Google login known for agent '{slug}': {e}")
    rec = reconcile_client(ident, runner=runner, apply=False)
    data = _mint_google_token(ident.account, rec.client, runner=runner)
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write_private(out, json.dumps({
        "agent": slug, "account": ident.account,
        "access_token": data["access_token"],
        "expires_at": now + float(data.get("expires_in") or 3600),
        "scope": data.get("scope", ""),
    }))
    return out


# ──────────────────────────────────────────────────────────────────────────────
# google-token --delegated: a Google account a PERSON delegated to the agent
# ──────────────────────────────────────────────────────────────────────────────
#
# canopy-web's `/agents/<slug>/connect/google`: a person lends the agent their own Google
# account. It is a delegated vault entry, `google-delegated-<account>` (a gog token with a
# refresh token), sealed only to the machines that run the agent — a cloud runner, never
# another person's laptop. The RUNNER opens it with its own key and stages only a
# short-lived ACCESS token for the turn (`--runner`); the turn reads that file. The
# refresh token never reaches a file a turn can read.

DELEGATED_GOOGLE_PREFIX = "google-delegated-"
#: The shared vault item holding the OAuth client a delegated token was minted for.
DELEGATED_GOOGLE_CLIENT_ITEM = "gog-oauth-client-web"
#: A staged token with less life than this is not handed to a turn.
DELEGATED_GOOGLE_MIN_LIFE = 60


def delegated_google_token_path(slug: str, account: str) -> Path:
    """~/.canopy/delegated/<slug>/google/<account>.json — the cloud runner's staging path."""
    return CACHE_DIR / "delegated" / slug / "google" / f"{account.strip().lower()}.json"


def _client_pair(raw) -> tuple[str, str]:
    """(client_id, client_secret) out of a client file: Google's download or gog's flat one."""
    try:
        data = json.loads(raw or "")
    except ValueError:
        data = None
    if isinstance(data, dict):
        data = data.get("web") or data.get("installed") or data
    if not isinstance(data, dict) or not data.get("client_id") or not data.get("client_secret"):
        raise CredError(f"this machine cannot read the Google OAuth client ({DELEGATED_GOOGLE_CLIENT_ITEM}) "
                        "from the agent's vault — give it a key to the workspace's shared vault")
    return str(data["client_id"]), str(data["client_secret"])


def _refresh_google(client_id: str, secret: str, refresh: str, account: str, *, post=None) -> dict:
    import urllib.error
    import urllib.parse
    import urllib.request

    form = {"client_id": client_id, "client_secret": secret, "refresh_token": refresh,
            "grant_type": "refresh_token"}
    if post is not None:
        return post(GOOGLE_TOKEN_URL, form)
    try:
        with urllib.request.urlopen(GOOGLE_TOKEN_URL, urllib.parse.urlencode(form).encode(),
                                    timeout=30) as resp:  # noqa: S310 - fixed https endpoint
            return json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            reason = json.load(e).get("error_description") or ""
        except Exception:  # noqa: BLE001
            reason = ""
        raise CredError(f"Google refused {account}'s delegated login ({e.code} {reason}) — its "
                        "person connects it again at the agent's connect/google page")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise CredError(f"could not reach Google to refresh {account}'s delegated login: {e}")


def stage_delegated_google_token(slug: str, account: str, *, token: Optional[str] = None,
                                 base_url: Optional[str] = None, transport=None, ecdh=None,
                                 post=None, now: Optional[float] = None) -> Path:
    """The RUNNER's half: open the Google account `account` a person delegated to `slug`
    (this machine's own resolve, device proof included), refresh it at Google, and write
    only the access token to `delegated_google_token_path` (0600). A credential that is no
    longer delegated (withdrawn, its person no longer an admin) REMOVES the staged file."""
    from orchestrator import cred_resolve
    from orchestrator import vault_zk as zk

    account = account.strip().lower()
    now = time.time() if now is None else now
    out = delegated_google_token_path(slug, account)
    tok = token or os.environ.get("CANOPY_TOKEN", "").strip() or canopy_web.resolve_token(None)
    try:
        body = cred_resolve.resolve(slug, token=tok, refs=[DELEGATED_GOOGLE_CLIENT_ITEM], base_url=base_url,
                                    transport=transport, raw=True, fallback=False,
                                    purpose=cred_resolve.RUNNER_REFRESH) or {}
    except canopy_web.CanopyError as e:
        raise CredError(f"could not resolve '{slug}' as this runner: {str(e)[:300]}")
    agent_id = str(body.get("agent_id") or "")
    name = DELEGATED_GOOGLE_PREFIX + account
    opened, errors = cred_resolve.open_delegated(
        {k: v for k, v in (body.get("delegated") or {}).items() if k == name}, agent_id=agent_id, ecdh=ecdh)
    if name not in opened:
        out.unlink(missing_ok=True)
        why = errors.get(name) or "none is delegated to it that this machine may open"
        raise CredError(f"no Google account {account} delegated to '{slug}' on this machine ({why})", EXIT_REFUSED)
    try:
        gog = json.loads(opened.pop(name))
    except ValueError:
        raise CredError(f"the delegated Google account {account} is not a gog token")
    if isinstance(gog, dict) and gog.get("session"):
        # Lent for a session: the sealed value IS the access token, with Google's own end.
        # Nothing to refresh; past its end it is dead everywhere, so stage nothing.
        expires_at = float(gog.get("expires_at") or 0)
        if str(gog.get("email") or "").strip().lower() != account or not gog.get("access_token"):
            raise CredError(f"the delegated Google session for {account} is not for that account")
        if expires_at - now < DELEGATED_GOOGLE_MIN_LIFE:
            out.unlink(missing_ok=True)
            raise CredError(f"{account}'s Google session for '{slug}' has ended — its person starts a new "
                            "one from the page that asked for it", EXIT_REFUSED)
        out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _write_private(out, json.dumps({
            "agent": slug, "account": account, "delegated": True, "session": True,
            "access_token": gog["access_token"], "expires_at": expires_at,
            "scope": " ".join(gog.get("scopes") or []),
        }))
        gog = ""  # noqa: F841
        return out
    refresh = str(gog.get("refresh_token") or "")
    if not refresh or str(gog.get("email") or "").strip().lower() != account:
        raise CredError(f"the delegated Google credential for {account} has no refresh token for that account")
    try:
        layered = cred_resolve.merge_vault_layers(slug, body, ecdh=ecdh)
    except cred_resolve.ZkUnlockError as e:
        raise CredError(f"could not open '{slug}'s vault for the Google OAuth client: {e}")
    raw = (layered.get("values") or {}).get(DELEGATED_GOOGLE_CLIENT_ITEM)
    if zk.is_zk_value(raw) or zk.is_sealed_value(raw):
        try:
            raw = (cred_resolve.open_zk(slug, {**body, "vaults": [], "delegated": None}, agent_id=agent_id,
                                        ecdh=ecdh).get("values") or {}).get(DELEGATED_GOOGLE_CLIENT_ITEM)
        except cred_resolve.ZkUnlockError as e:
            raise CredError(f"could not open the Google OAuth client in '{slug}'s vault: {e}")
    client_id, secret = _client_pair(raw)
    data = _refresh_google(client_id, secret, refresh, account, post=post)
    refresh = gog = ""  # noqa: F841 — drop our references
    if not data.get("access_token"):
        raise CredError(f"Google returned no access token for {account}")
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write_private(out, json.dumps({
        "agent": slug, "account": account, "delegated": True,
        "access_token": data["access_token"],
        "expires_at": now + float(data.get("expires_in") or 3600),
        "scope": data.get("scope", ""),
    }))
    return out


def delegated_google_token(slug: str, account: str, *, now: Optional[float] = None) -> Path:
    """The TURN's half: the path of the access token the runner staged for `account`,
    refused when there is none or it is about to lapse (the runner keeps it fresh while a
    turn runs). A runner turn may ask only for its own agent."""
    now = time.time() if now is None else now
    session = session_identity()
    if session.kind == "agent-turn" and session.agent != slug:
        raise CredError(f"refused: {session.why}", EXIT_REFUSED)
    out = delegated_google_token_path(slug, account)
    try:
        staged = json.loads(out.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        staged = None
    if not isinstance(staged, dict) or not staged.get("access_token"):
        raise CredError(
            f"no access token for the Google account {account.strip().lower()} delegated to '{slug}' is "
            "staged on this machine. Only a cloud runner that runs the agent (or its person's own runner) "
            f"holds it, and stages it for each turn of '{slug}' ($CANOPY_DELEGATED_GOOGLE lists the "
            "accounts). Its person connects one at /w/<workspace>/agents/<slug>/connect/google.",
            EXIT_REFUSED)
    if float(staged.get("expires_at") or 0) - now < DELEGATED_GOOGLE_MIN_LIFE:
        raise CredError(f"the staged token for {account} has expired and the runner has not refreshed "
                        "it — retry in a minute; if it persists the runner says why in its log")
    return out


# ──────────────────────────────────────────────────────────────────────────────
# connection-token: a short-lived access token from one of canopy-web's connections
# (Salesforce today; any provider canopy-web lists). `salesforce-token` is its alias.
# ──────────────────────────────────────────────────────────────────────────────

#: A staged/cached connection token is not handed out inside this many seconds of expiry.
CONNECTION_MIN_LIFE = 300
#: A provider key as canopy-web names it (`salesforce`): it becomes a file name and a URL segment.
_PROVIDER_KEY = re.compile(r"[a-z][a-z0-9_-]{0,39}")


def _provider_key(provider: str) -> str:
    if not _PROVIDER_KEY.fullmatch(provider or ""):
        raise CredError(f"{provider!r} is not a provider key (e.g. salesforce)", EXIT_USAGE)
    return provider


def connection_token_path(provider: str, slug: str = "") -> Path:
    """~/.canopy/connections/<provider>.json (a person's), or .../<slug>/<provider>.json (a turn's)."""
    base = CACHE_DIR / "connections"
    name = f"{_provider_key(provider)}.json"
    return (base / slug / name) if slug else (base / name)


def _epoch(value) -> float:
    """`expires_at` as epoch seconds: canopy-web sends ISO 8601; a number passes through."""
    if isinstance(value, (int, float)):
        return float(value)
    from datetime import UTC, datetime
    dt = datetime.fromisoformat(str(value).strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _problem(e: Exception) -> dict:
    """The problem+json body inside a CanopyError ("POST /p -> 409: {...}"), or {}."""
    text = str(e)
    _, _, tail = text.partition(": ")
    try:
        body = json.loads(tail.strip())
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _status_of(e: Exception) -> int:
    m = re.search(r"-> (\d{3})", str(e))
    return int(m.group(1)) if m else 0


def _usable(path: Path, now: float) -> bool:
    try:
        staged = json.loads(path.read_text(encoding="utf-8"))
        return bool(staged.get("access_token")) and _epoch(staged["expires_at"]) - now >= CONNECTION_MIN_LIFE
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def _write_connection_token(path: Path, body: dict) -> Path:
    try:
        staged = {"access_token": body["access_token"], "instance_url": body.get("instance_url") or "",
                  "expires_at": _epoch(body["expires_at"]), "account_label": body.get("account_label", "")}
    except (KeyError, ValueError, TypeError):
        raise CredError("canopy-web answered with a token this CLI cannot read") from None
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path.parent, 0o700)
    _write_private(path, json.dumps(staged))
    return path


def _refusal(e: Exception, *, runner: bool) -> CredError:
    """A canopy-web refusal as a CredError: its detail, then where to connect when it says.
    Never a token. In runner mode `not_connected` is exit 3 (the runner stays silent)."""
    body = _problem(e)
    detail = str(body.get("detail") or body.get("title") or str(e)[:300])
    if runner and _status_of(e) == 409 and body.get("error") == "not_connected":
        return CredError(detail, EXIT_REFUSED)
    if body.get("connect_url"):
        detail += f"\nConnect it in canopy-web: {body['connect_url']}"
    return CredError(detail)


def connection_token(provider: str, *, now: Optional[float] = None, refresh: bool = False) -> Path:
    """A PERSON's own token: POST /api/me/connections/<provider>/token with their PAT, written 0600
    to ~/.canopy/connections/<provider>.json and reused until 5 minutes before it lapses.
    `refresh` skips the file and asks canopy-web for a new token (`?refresh=true`)."""
    provider = _provider_key(provider)
    now = time.time() if now is None else now
    session = session_identity()
    if session.kind == "agent-turn":
        raise CredError(f"refused: this is an agent turn, which never holds a person's {provider} token. "
                        f"Ask for the agent's own with `canopy cred connection-token {provider} --agent "
                        f"{session.agent or '<slug>'}`.", EXIT_REFUSED)
    out = connection_token_path(provider)
    if not refresh and _usable(out, now):
        return out
    path = f"/api/me/connections/{provider}/token" + ("?refresh=true" if refresh else "")
    try:
        body = canopy_web.call("POST", path, token=_human_token())
    except canopy_web.CanopyError as e:
        raise _refusal(e, runner=False) from None
    return _write_connection_token(out, body)


def stage_connection_token(provider: str, slug: str, runner_id: str, turn_id: str, *,
                           token: Optional[str] = None, min_life: Optional[int] = None,
                           refresh: bool = False) -> Path:
    """The RUNNER's half: with its own bearer (CANOPY_TOKEN), ask canopy-web for the token of the
    `provider` connection `slug` uses and write it 0600 to ~/.canopy/connections/<slug>/<provider>.json.
    `min_life` (seconds) is forwarded as `min_life_seconds`, so canopy-web refreshes rather than hand
    back a cached token that lapses before the runner's next re-stage; `refresh` forces a new one.
    Exit 3 when the agent has no such connection (the runner treats that as silent)."""
    provider = _provider_key(provider)
    tok = token or os.environ.get("CANOPY_TOKEN", "").strip()
    if not tok:
        raise CredError("--runner needs the runner's bearer in $CANOPY_TOKEN", EXIT_USAGE)
    if not runner_id or not turn_id:
        raise CredError("--runner needs --runner-id and --turn", EXIT_USAGE)
    req: dict = {"provider": provider}
    if min_life is not None:
        req["min_life_seconds"] = min_life
    if refresh:
        req["refresh"] = True
    try:
        body = canopy_web.call("POST", f"/api/harness/runners/{runner_id}/turns/{turn_id}/connection-token",
                               req, token=tok)
    except canopy_web.CanopyError as e:
        raise _refusal(e, runner=True) from None
    return _write_connection_token(connection_token_path(provider, slug), body)


def staged_connection_token(provider: str, slug: str, *, now: Optional[float] = None) -> Path:
    """The TURN's half: the path of the `provider` token the runner staged for `slug`. No network."""
    provider = _provider_key(provider)
    now = time.time() if now is None else now
    session = session_identity()
    if session.kind == "agent-turn" and session.agent != slug:
        raise CredError(f"refused: {session.why}", EXIT_REFUSED)
    out = connection_token_path(provider, slug)
    if not _usable(out, now):
        raise CredError(f"no {provider} token staged for {slug}: this agent has no {provider} connection, "
                        "or this runner does not stage one")
    return out


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

AGENT_OPT = click.option("--agent", "agent", default=None,
                         help="Agent slug. Default: this session's agent "
                              "($CANOPY_AGENT_SLUG / $CANOPY_AGENT, the dispatched turn, "
                              "or the cwd's agent repo).")


@click.group("cred")
def cred_group():
    """Session identity: may this session act as an agent, and its credentials if so.

    Exit codes: 0 allowed · 1 error · 2 no agent named/detectable · 3 refused ·
    4 undetermined. See docs/architecture/session-identity.md."""


@cred_group.command("check")
@AGENT_OPT
@click.option("--refresh", is_flag=True, help="Ignore the 10-minute verdict cache.")
@click.option("--json", "as_json", is_flag=True, help="Print the verdict as JSON on stdout.")
def check_cmd(agent, refresh, as_json):
    """Exit 0 if this session may act as the agent; else exit 3 with what access to get."""
    slug = _require_agent(agent)
    try:
        v = decide(slug, refresh=refresh)
    except CredError:
        raise
    except Exception as e:  # noqa: BLE001 — "can't tell" is never "yes"
        msg = f"could not decide whether this session may act as '{slug}': {e}"
        if as_json:
            click.echo(json.dumps({"agent": slug, "allowed": False, "exit_code": EXIT_UNDETERMINED,
                                   "message": msg}))
        raise CredError(msg, EXIT_UNDETERMINED)
    if as_json:
        click.echo(json.dumps(v.as_dict(), sort_keys=True))
    for n in v.notes:
        click.echo(f"[canopy cred] note: {n}", err=True)
    if not v.allowed:
        click.echo(v.message, err=True)
        sys.exit(EXIT_REFUSED)
    if not as_json:
        click.echo(f"ok: this session may act as '{slug}' — {v.reason}")


@cred_group.command("env")
@AGENT_OPT
@click.option("--slug", "slug_opt", default=None, help="Same as --agent.")
@click.option("--refresh", is_flag=True, help="Re-resolve even if ~/.<agent>/.env exists.")
@click.option("--out", "out", default=None, type=click.Path(dir_okay=False),
              help="Resolve in memory and write the env to THIS file (0600) instead of "
                   "~/.<agent>/.env; refused outside $CANOPY_TURN_ENV_ROOT when that is set. "
                   "Prints the path.")
@click.option("--print-json", "print_json", is_flag=True,
              help="With --runner: resolve in memory and print {agent, source, values} — plus "
                   "delegated when the resolve returned it — "
                   "as JSON on stdout, for a runner's in-memory cache. Writes nothing.")
@click.option("--runner", "runner_mode", is_flag=True,
              help="Resolve as a RUNNER (its token, CANOPY_TOKEN; the clone under "
                   "--agent-root), as `canopy agent prepare --runner` does.")
@click.option("--agent-root", default=lambda: os.environ.get("AGENT_ROOT", "/opt/agents"),
              show_default="$AGENT_ROOT or /opt/agents",
              help="Where agents are cloned (--runner).")
def env_cmd(agent, slug_opt, refresh, out, print_json, runner_mode, agent_root):
    """Ensure ~/.<agent>/.env exists (resolving it only when missing or --refresh) and
    print its path. Never prints a value.

    With --out or --print-json nothing persistent is written: the values are resolved in
    memory (a runner's per-turn env, `--runner`) and handed over once."""
    if agent and slug_opt and agent != slug_opt:
        raise CredError("--agent and --slug disagree", EXIT_USAGE)
    slug = _require_agent(agent or slug_opt)
    if out and print_json:
        raise CredError("pass --out or --print-json, not both", EXIT_USAGE)
    if not (out or print_json):
        if runner_mode:
            raise CredError("--runner needs --out or --print-json (a runner writes no "
                            "~/.<agent>/.env)", EXIT_USAGE)
        path, how = ensure_env(slug, refresh=refresh)
        click.echo(f"[canopy cred] {slug}: {how}", err=True)
        click.echo(str(path))
        return
    if print_json and not runner_mode:
        raise CredError("--print-json is for a runner's in-memory cache: it needs --runner "
                        "(a session runs commands with its env through `canopy cred exec`)",
                        EXIT_USAGE)
    if out:
        confine_out(Path(out))   # refuse before resolving anything
    if print_json:   # --runner is required above
        values, how, extras = runner_env_bundle(slug, agent_root=Path(agent_root))
        usable, _ = env_values(values)
        click.echo(json.dumps({**extras, "agent": slug, "source": how, "values": usable},
                              sort_keys=True))
        return
    if runner_mode:
        values, how = runner_env_values(slug, agent_root=Path(agent_root))
    else:
        values, how = exec_env(slug)
    n = write_env_out(Path(out), values, slug)
    click.echo(f"[canopy cred] {slug}: {n} var(s) from {how} → {out} (0600)", err=True)
    click.echo(str(confine_out(Path(out))))


@cred_group.command("refresh")
@AGENT_OPT
def refresh_cmd(agent):
    """Re-resolve ~/.<agent>/.env (after rotation, or when a caller hit an auth failure)."""
    slug = _require_agent(agent)
    _cache_drop(slug)
    path, how = ensure_env(slug, refresh=True)
    click.echo(f"[canopy cred] {slug}: {how}", err=True)
    click.echo(str(path))


@cred_group.command("sync")
@AGENT_OPT
@click.option("--ttl", type=float, default=SYNC_TTL, show_default=True,
              help="Seconds between checks for this agent.")
def sync_cmd(agent, ttl):
    """Re-write ~/.<agent>/.env from its Canopy vault IF the values changed.

    Cheap enough for every session start: throttled per agent; re-written from canopy-web
    when the values' fingerprint moved. Never prints a value; a failure keeps the existing file and exits 1 with the reason."""
    slug = _require_agent(agent)
    try:
        click.echo(f"[canopy cred] {slug}: {sync_env(slug, ttl=ttl)}", err=True)
    except CredError as e:
        # The reason on stderr and a non-zero exit, so a caller (a runner's claim) can say
        # why its turns run on the last good file.
        click.echo(f"[canopy cred] {slug}: {e}", err=True)
        raise SystemExit(1)


@cred_group.command("exec", context_settings={"ignore_unknown_options": True,
                                               "allow_interspersed_args": False})
@AGENT_OPT
@click.option("--ref", "refs", multiple=True,
              help="Only this variable (repeatable). Default: all of the agent's.")
@click.argument("cmd", nargs=-1, required=True, type=click.UNPROCESSED)
def exec_cmd(agent, refs, cmd):
    """Run CMD with the agent's credentials in ITS environment only.

    `canopy cred exec [--agent X] [--ref NAME …] -- <cmd> [args…]`. Resolves from the
    Canopy vault (canopy-web) under the same rules as `cred check` — a runner turn only as
    its own agent — and writes nothing:
    no ~/.X/.env, nothing into this session's environment. Exits with CMD's status."""
    slug = _require_agent(agent)
    values, how = exec_env(slug, refs=list(refs) or None)
    click.echo(f"[canopy cred] {slug}: {len(values)} var(s) from {how} → child only", err=True)
    try:
        rc = subprocess.call(list(cmd), env={**os.environ, **values})
    except FileNotFoundError:
        raise CredError(f"no such command: {cmd[0]}", 127)
    finally:
        values = {}  # noqa: F841 — drop our only reference
    sys.exit(rc)


@cred_group.command("google-token")
@AGENT_OPT
@click.option("--delegated", "delegated", default="", metavar="ACCOUNT",
              help="A Google account a PERSON delegated to the agent (you@gmail.com), instead of "
                   "the agent's own login: the path of the access token the runner staged for it.")
@click.option("--runner", "runner_mode", is_flag=True,
              help="With --delegated: as the RUNNER (its token, CANOPY_TOKEN, and device proof), "
                   "open the delegated account, refresh it at Google and stage the access token.")
def google_token_cmd(agent, delegated, runner_mode):
    """Write a short-lived access token for the agent's OWN Google login and print the
    file's path (never the token): ~/.canopy/google-token/<agent>.json, mode 0600, with
    `access_token`, `expires_at` (epoch seconds) and `account`.

    For tools that should act on Google as the agent instead of a shared service account.
    Reuses the file while it has 5+ minutes left. A runner turn may only ask for its own
    agent.

    `--delegated ACCOUNT`: a Google account a person lent the agent (canopy-web's
    connect/google page). In a turn on a runner that holds it, prints the path of the
    access token the runner staged — ~/.canopy/delegated/<agent>/google/<account>.json —
    for `gog --access-token "$(jq -r .access_token "$(canopy cred google-token --delegated
    ACCOUNT)")" …`. The refresh token stays with the runner."""
    slug = _require_agent(agent)
    if runner_mode and not delegated:
        raise CredError("--runner stages a delegated account: pass --delegated ACCOUNT", EXIT_USAGE)
    if delegated and runner_mode:
        click.echo(str(stage_delegated_google_token(slug, delegated)))
    elif delegated:
        click.echo(str(delegated_google_token(slug, delegated)))
    else:
        click.echo(str(google_token(slug)))


def _connection_token_options(f):
    for opt in reversed((
        click.option("--agent", "agent", default=None, help="Agent slug: read the token the runner staged for it."),
        click.option("--runner", "runner_mode", is_flag=True,
                     help="As the RUNNER (bearer in $CANOPY_TOKEN): stage the agent's token for one turn."),
        click.option("--runner-id", "runner_id", default="", help="With --runner: this runner's id."),
        click.option("--turn", "turn", default="", help="With --runner: the turn's id."),
        click.option("--min-life", "min_life", type=click.IntRange(0, 3600), default=None,
                     help="With --runner: seconds the token must still live, else canopy-web refreshes it."),
        click.option("--refresh", "refresh", is_flag=True,
                     help="Ignore any cached token and have canopy-web refresh (after the provider answered 401)."),
    )):
        f = opt(f)
    return f


def _connection_token_cmd(provider, agent, runner_mode, runner_id, turn, min_life, refresh) -> None:
    if runner_mode:
        click.echo(str(stage_connection_token(provider, _require_agent(agent), runner_id, turn,
                                              min_life=min_life, refresh=refresh)))
        return
    if min_life is not None:
        raise CredError("--min-life applies only with --runner", EXIT_USAGE)
    if agent:
        if refresh:
            raise CredError("--refresh does not apply with --agent: the runner re-stages that token", EXIT_USAGE)
        click.echo(str(staged_connection_token(provider, _require_agent(agent))))
    else:
        click.echo(str(connection_token(provider, refresh=refresh)))


@cred_group.command("connection-token")
@click.argument("provider")
@_connection_token_options
def connection_token_cmd(provider, agent, runner_mode, runner_id, turn, min_life, refresh):
    """Print the path of a short-lived access token file for a canopy-web connection (never the
    token): {access_token, instance_url, expires_at, account_label}, mode 0600. PROVIDER is the
    connection's key, e.g. `salesforce`.

    No flags: YOUR connection (connect it in canopy-web Settings) ->
    ~/.canopy/connections/PROVIDER.json, reused until 5 minutes before it lapses (`--refresh` skips
    the file). `--agent X` in a turn: the file the runner staged for X,
    ~/.canopy/connections/X/PROVIDER.json (no network). `--runner --agent X --runner-id R --turn T`:
    the runner stages it (`--min-life S` forwarded; `--refresh` forces a new token); exit 3 when X
    has no PROVIDER connection."""
    _connection_token_cmd(provider, agent, runner_mode, runner_id, turn, min_life, refresh)


@cred_group.command("salesforce-token")
@_connection_token_options
def salesforce_token_cmd(agent, runner_mode, runner_id, turn, min_life, refresh):
    """`connection-token salesforce`: the same command, by the name it first shipped under."""
    _connection_token_cmd("salesforce", agent, runner_mode, runner_id, turn, min_life, refresh)


@cred_group.command("whoami")
@click.option("--json", "as_json", is_flag=True)
def whoami_cmd(as_json):
    """Who this session is — a runner turn acting as its agent, or a person — and why."""
    s = session_identity()
    if as_json:
        click.echo(json.dumps(s.as_dict(), sort_keys=True))
        return
    if s.kind == "agent-turn":
        click.echo(f"identity: agent '{s.agent}'" if s.agent else "identity: runner turn (no agent)")
    else:
        click.echo("identity: human (you)")
    click.echo(f"why: {s.why}")
