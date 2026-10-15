"""`canopy cred` — may this session act as agent X, and if so, give it X's env.

The contract is docs/architecture/session-identity.md (canopy#850, "Design
revision", 2026-10-09). In short:

* **A runner turn** (a turn id is known: ``$CANOPY_TURN_ID``, the caller
  envelope, or the laptop's by-task record) may act only as the agent it was
  dispatched for (``$CANOPY_AGENT_SLUG`` / ``$CANOPY_AGENT`` / the envelope's
  ``agent``). Any other ``--agent`` is refused.
* **A human session** may act as X when X's credential backend says so:
  ``1password`` (the default) — the user's own ``op`` can read X's vault;
  ``canopy-web`` — ``GET /api/agents/X/credentials/access`` says
  ``may_resolve``. The backend is the agent record's ``credential_source``.
* Anything else acts as the human, and the refusal says what access to get.

Commands::

    canopy cred check  [--agent X] [--json]   exit 0 allowed, 3 refused
    canopy cred env    [--agent X] [--refresh] prints ~/.X/.env (resolved if missing)
    canopy cred refresh [--agent X]           re-resolve ~/.X/.env
    canopy cred sync   [--agent X] [--ttl S]  re-inject ~/.X/.env iff its vault changed
    canopy cred google-token [--agent X]      X's Google access token -> a 0600 file; prints its path
    canopy cred whoami [--json]               who this session is, and why

Exit codes (stable — agent-identity MCP servers shell out to `check`):
    0 allowed · 1 error (resolve failed) · 2 usage (no agent named or detectable)
    3 refused (stderr: one actionable paragraph) · 4 undetermined (could not decide)

No command ever prints a secret value (google-token prints a path, not the token).
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

SOURCES = ("1password", "canopy-web")
DEFAULT_SOURCE = "1password"

#: How long a canopy-web /access answer and a successful 1Password probe are
#: trusted. An MCP server checks before every action; re-probing `op` each time
#: would be slow and, through the desktop app, could prompt.
CACHE_TTL = 600
CACHE_DIR = Path.home() / ".canopy"
CACHE_FILE = CACHE_DIR / "cred-cache.json"

OP_INSTALL_URL = "https://developer.1password.com/docs/cli/get-started/"

#: stderr fragments meaning "op is not signed in / locked", not "no access".
OP_SIGNIN_FAILURES = ("not currently signed in", "no accounts configured", "session expired",
                      "authorization timeout", "authorization prompt", "connect to 1password",
                      "prompterror", "account is not signed in", "you are not currently signed in")

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
    """The person's own canopy-web PAT (the same operator rule `canopy agent op-token`
    and `agent bootstrap` use: explicit env, else the workbench-token)."""
    from orchestrator.agent_bootstrap import _operator_token
    return _operator_token()


def fetch_access(slug: str, *, refresh: bool = False) -> dict:
    """canopy-web's verdict for this caller on agent ``slug`` — cached CACHE_TTL.

    Returns the /access body plus ``error`` ("" on success). On any failure the
    backend falls back to the default (1password): the 1Password probe still
    decides, so an unreachable canopy-web never grants anything by itself."""
    key = f"access|{canopy_web.resolve_base_url(None)}|{slug}"
    if not refresh:
        hit = _cache_get(key)
        if hit is not None:
            return hit
    try:
        body = canopy_web.call("GET", f"/api/agents/{slug}/credentials/access",
                               token=_human_token()) or {}
    except Exception as e:  # noqa: BLE001 — degrade, don't traceback
        return {"agent": slug, "credential_source": DEFAULT_SOURCE, "may_resolve": False,
                "via": None, "reason": "",
                "error": str(e).splitlines()[0][:200] if str(e) else type(e).__name__}
    source = body.get("credential_source") or DEFAULT_SOURCE
    out = {"agent": slug,
           "credential_source": source if source in SOURCES else DEFAULT_SOURCE,
           "may_resolve": bool(body.get("may_resolve")),
           "via": body.get("via"), "reason": str(body.get("reason") or ""), "error": ""}
    _cache_put(key, out)
    return out


# ──────────────────────────────────────────────────────────────────────────────
# 1Password: can the user's `op` read X's vault?
# ──────────────────────────────────────────────────────────────────────────────

def agent_vault_name(slug: str) -> str:
    """X's 1Password vault: the instance this machine is bound to (`canopy agent env`'s
    ~/.X/canopy-instance.json), else the ``op_vault`` canopy-web records for X. Raises
    CredError when neither has one — no naming-convention fallback (a plugin is code; the
    vault belongs to the instance)."""
    from orchestrator.agent_instance import InstanceError, instance_vault
    try:
        return instance_vault(slug, token=_human_token())
    except InstanceError as e:
        raise CredError(e.message, EXIT_UNDETERMINED)


def _template_env(slug: str, tpl: Path, env: dict, vault: str = "") -> dict:
    """`env` plus AGENT_VAULT / AGENT_SLUG when the template references ${AGENT_VAULT}
    (templates stopped naming the vault; the instance's recorded one fills it in)."""
    from orchestrator.agent_instance import inject_env, uses_vault_var
    if not uses_vault_var(tpl.read_text(encoding="utf-8")):
        return env
    return inject_env(env, vault or agent_vault_name(slug), slug)


@dataclass
class OpProbe:
    ok: bool
    problem: str = ""        # "" | "missing" | "signin" | "no-access"
    mode: str = ""           # "user" | "service-account" — which identity read the vault
    detail: str = ""


def _op_env(mode: str) -> dict:
    env = dict(os.environ)
    if mode == "user":
        # The USER's own op: never the agent service-account key a hook may have
        # staged into this session (agent_op_env) — that key is the agent's, not yours.
        env.pop("OP_SERVICE_ACCOUNT_TOKEN", None)
    return env


def probe_op(vault: str, *, runner: Runner = subprocess.run,
             which: Callable[[str], Optional[str]] = shutil.which) -> OpProbe:
    """`op vault get <vault>` — cheap, prints no secret. Tries the user's own op
    first; if that can't read it and a service-account key is in the env, tries
    that key (an agent session's own key reads its own vault)."""
    if not which("op"):
        return OpProbe(False, "missing", detail="the 1Password CLI (`op`) is not installed")
    modes = ["user"] + (["service-account"] if os.environ.get("OP_SERVICE_ACCOUNT_TOKEN") else [])
    last = OpProbe(False, "no-access")
    for mode in modes:
        try:
            r = runner(["op", "vault", "get", vault, "--format", "json"], capture_output=True,
                       text=True, timeout=45, env=_op_env(mode))
        except FileNotFoundError:
            return OpProbe(False, "missing", detail="the 1Password CLI (`op`) is not installed")
        except subprocess.TimeoutExpired:
            last = OpProbe(False, "signin", mode=mode,
                           detail="`op` timed out — 1Password is locked or waiting on a prompt")
            continue
        if r.returncode == 0:
            return OpProbe(True, mode=mode)
        err = (r.stderr or "").strip()
        low = err.lower()
        if any(m in low for m in OP_SIGNIN_FAILURES):
            last = OpProbe(False, "signin", mode=mode, detail=(err.splitlines() or [""])[0][:200])
        elif mode == "user" or last.problem != "signin":
            last = OpProbe(False, "no-access", mode=mode, detail=(err.splitlines() or [""])[0][:200])
    return last


# ──────────────────────────────────────────────────────────────────────────────
# the decision
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Verdict:
    agent: str
    allowed: bool
    identity: str               # "agent-turn" | "human"
    source: str = ""            # credential backend consulted ("" for a turn)
    via: Optional[str] = None   # "runner" | "admin" | "1password" | "local-env" | "turn" | None
    reason: str = ""            # short machine-ish reason
    message: str = ""           # the one-paragraph human message (refusals)
    op_mode: str = ""           # which op identity read the vault (1password)
    vault: str = ""
    exit_code: int = EXIT_OK
    notes: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _agent_repo(slug: str) -> Optional[Path]:
    """X's repo on this machine: the cwd's agent repo when it IS X, else a local
    checkout (~/emdash/repositories/X and the other roots), else the installed plugin."""
    here = Path.cwd().resolve()
    for d in (here, *here.parents):
        if (d / ".claude-plugin" / "plugin.json").is_file():
            if canopy_web._agent_slug_for_cwd(d) == slug:
                return d
            break
    try:
        from orchestrator.agent_email import AgentEmailError, find_agent_repo
        return find_agent_repo(slug)
    except Exception:  # noqa: BLE001 — AgentEmailError or anything odd: no repo
        return None


def _refusal_1password(slug: str, vault: str, probe: OpProbe) -> str:
    lead = f"Acting as agent '{slug}' needs its credentials from 1Password (vault '{vault}'), "
    tail = (f" Until then this session acts as you, not as '{slug}'. Re-check with "
            f"`canopy cred check --agent {slug}`.")
    if probe.problem == "missing":
        return (lead + f"and the 1Password CLI is not installed. Install the 1Password CLI "
                f"({OP_INSTALL_URL}) and sign in (`op signin`), or ask the vault owner to "
                f"share '{vault}' with you." + tail)
    if probe.problem == "signin":
        return (lead + f"and the 1Password CLI is not signed in ({probe.detail or 'no session'}). "
                f"Sign in with `op signin` (or unlock the 1Password app and enable its CLI "
                f"integration); if you have no access to '{vault}', ask the vault owner to "
                f"share it with you." + tail)
    return (lead + f"and your 1Password account cannot read '{vault}'"
            + (f" ({probe.detail})" if probe.detail else "")
            + f". Ask the vault owner to share '{vault}' with you (or check you are signed "
            f"in to the right 1Password account: `op whoami`)." + tail)


def _refusal_canopy_web(slug: str, access: dict) -> str:
    why = access.get("reason") or "you are neither a runner operator for it nor its owner/admin"
    return (f"Acting as agent '{slug}' needs canopy-web to hand this session its credentials "
            f"(agent '{slug}' keeps them on canopy-web), and canopy-web says you may not: "
            f"{why}. Ask {slug}'s owner or an agent admin to make you an admin of '{slug}' "
            f"on canopy-web (/agents/{slug} → Settings), then run `canopy cred refresh "
            f"--agent {slug}`. Until then this session acts as you, not as '{slug}'.")


def decide(slug: str, *, refresh: bool = False, runner: Runner = subprocess.run,
           which: Callable[[str], Optional[str]] = shutil.which) -> Verdict:
    sess = session_identity()
    if sess.kind == "agent-turn":
        if sess.agent and sess.agent == slug:
            # A turn resolves through the runner-provided service-account key, never
            # through a desktop-app session on the box.
            mode = "service-account" if os.environ.get("OP_SERVICE_ACCOUNT_TOKEN") else "user"
            return Verdict(slug, True, "agent-turn", via="turn", op_mode=mode,
                           reason=f"runner turn {sess.turn_id} is agent '{slug}'s own")
        who = f"agent '{sess.agent}'s" if sess.agent else "a"
        msg = (f"This is {who} runner turn ({sess.turn_id}), so it may act only as "
               f"{repr(sess.agent) if sess.agent else 'no agent'} — not as '{slug}'. To get "
               f"work done as '{slug}', dispatch it or ask it (`canopy agent dispatch --slug "
               f"{slug} …`, or `{slug}:ask`); never resolve another agent's credentials.")
        return Verdict(slug, False, "agent-turn", via=None, reason="turn-is-another-agent",
                       message=msg, exit_code=EXIT_REFUSED)

    # A human who already resolved X's env on this machine (the one-time `op inject` /
    # `canopy cred env`) keeps acting as X without asking 1Password again — re-probing a
    # locked desktop app on every server start is the "constant op prompt" Jonathan ruled
    # out (2026-10-09). Only a private file counts; --refresh always re-proves access.
    # Runner turns never reach this branch: they stay limited to their own agent above.
    if not refresh and _private_env(env_path(slug)):
        return Verdict(slug, True, "human", source="local-env", via="local-env",
                       reason=f"{env_path(slug)} already resolved on this machine")

    access = fetch_access(slug, refresh=refresh)
    source = access.get("credential_source") or DEFAULT_SOURCE
    notes = []
    if access.get("error"):
        notes.append(f"canopy-web /access unavailable ({access['error']}); "
                     f"assuming the default backend '{DEFAULT_SOURCE}'")

    if source == "canopy-web":
        if access.get("may_resolve"):
            return Verdict(slug, True, "human", source=source, via=access.get("via"),
                           reason=access.get("reason") or "canopy-web: may_resolve", notes=notes)
        return Verdict(slug, False, "human", source=source, via=None,
                       reason=access.get("reason") or "canopy-web: may not resolve",
                       message=_refusal_canopy_web(slug, access), exit_code=EXIT_REFUSED,
                       notes=notes)

    try:
        vault = agent_vault_name(slug)
    except CredError as e:
        message = f"Cannot decide whether you may act as agent '{slug}': {e.message}"
        if " 404" in e.message or "-> 404" in e.message:
            # canopy-web answers 404 to a non-member of the agent's workspace.
            message += (f" canopy-web does not show you agent '{slug}' (not a member of its "
                        f"workspace) — ask a workspace admin to invite you first.")
        return Verdict(slug, False, "human", source=source, via=None, reason="no-vault",
                       message=message, exit_code=EXIT_UNDETERMINED, notes=notes)
    key = f"op|{vault}|{slug}"
    hit = None if refresh else _cache_get(key)
    if hit and hit.get("ok"):
        probe = OpProbe(True, mode=hit.get("mode") or "user")
    else:
        probe = probe_op(vault, runner=runner, which=which)
        if probe.ok:
            _cache_put(key, {"ok": True, "mode": probe.mode})
    if probe.ok:
        return Verdict(slug, True, "human", source=source, via="1password",
                       reason=f"1Password ({probe.mode}) can read vault '{vault}'",
                       op_mode=probe.mode, vault=vault, notes=notes)
    message = _refusal_1password(slug, vault, probe)
    if " 404" in (access.get("error") or "") or "-> 404" in (access.get("error") or ""):
        # canopy-web answers /access with 404 to a non-member of the agent's workspace.
        message += (f" canopy-web also does not show you agent '{slug}' (not a member of its "
                    f"workspace) — if '{slug}' keeps its credentials on canopy-web, ask a "
                    f"workspace admin to invite you first.")
    return Verdict(slug, False, "human", source=source, via=None,
                   reason=f"1password: {probe.problem}", vault=vault,
                   message=message, exit_code=EXIT_REFUSED, notes=notes)


# ──────────────────────────────────────────────────────────────────────────────
# resolving ~/.<slug>/.env
# ──────────────────────────────────────────────────────────────────────────────

def env_path(slug: str) -> Path:
    return Path.home() / f".{slug}" / ".env"


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


def resolve_1password(slug: str, out: Path, verdict: Verdict, *,
                      runner: Runner = subprocess.run) -> str:
    repo = _agent_repo(slug)
    if repo is None:
        raise CredError(
            f"no repo for agent '{slug}' on this machine — clone it (gh repo clone "
            f"dimagi-internal/{slug} ~/emdash/repositories/{slug}), install its plugin, or "
            f"run from inside it. Its .env.tpl says what ~/.{slug}/.env holds.")
    tpl = repo / ".env.tpl"
    if not tpl.is_file():
        raise CredError(f"agent '{slug}'s repo ({repo}) has no .env.tpl — nothing to resolve")
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=str(out.parent), prefix=".tmp-", suffix=".env")
    os.close(fd)
    os.chmod(tmp, 0o600)
    try:
        env = _template_env(slug, tpl, _op_env(verdict.op_mode or "user"), verdict.vault)
        r = runner(["op", "inject", "-f", "-i", str(tpl), "-o", tmp], capture_output=True,
                   text=True, timeout=180, env=env)
        if r.returncode != 0:
            # op names the reference it could not resolve, never a value.
            first = ((r.stderr or "").strip().splitlines() or ["no output"])[0][:300]
            raise CredError(f"op inject failed for '{slug}': {first}")
        os.chmod(tmp, 0o600)
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return f"1Password ({verdict.op_mode or 'user'}) via {tpl}"


def _env_line(name: str, value: str) -> str:
    return f"{name}={value}"


def resolve_canopy_web(slug: str, out: Path) -> str:
    try:
        body = canopy_web.call("GET", f"/api/agents/{slug}/credentials/resolve",
                               token=_human_token()) or {}
    except Exception as e:  # noqa: BLE001 — report, don't traceback (never the body)
        raise CredError(f"could not resolve '{slug}'s credentials from canopy-web: "
                        f"{str(e).splitlines()[0][:200] if str(e) else type(e).__name__}")
    values = body.get("values") or {}
    if not isinstance(values, dict):
        raise CredError(f"canopy-web returned no credential values for '{slug}'")
    lines, skipped = [], []
    for name in sorted(values):
        val = values[name]
        val = "" if val is None else str(val)
        if not _ENV_NAME_RE.match(str(name)) or "\n" in val:
            skipped.append(str(name))  # not an env var (e.g. gog-token): staged elsewhere
            continue
        lines.append(_env_line(str(name), val))
    header = (f"# {slug} credentials, resolved from canopy-web by `canopy cred env` "
              f"— do not commit.\n")
    _write_private(out, header + "\n".join(lines) + ("\n" if lines else ""))
    note = f"canopy-web ({len(lines)} vars"
    if skipped:
        note += f"; {len(skipped)} non-env credential(s) not written: {', '.join(skipped)}"
    return note + ")"


def ensure_env(slug: str, *, refresh: bool = False, runner: Runner = subprocess.run,
               which: Callable[[str], Optional[str]] = shutil.which) -> tuple[Path, str]:
    """(path, what happened). Raises CredError(EXIT_REFUSED) when the session may not
    act as ``slug``."""
    verdict = decide(slug, refresh=refresh, runner=runner, which=which)
    if not verdict.allowed:
        raise CredError(verdict.message, EXIT_REFUSED)
    out = env_path(slug)
    if _fresh(out) and not refresh:
        try:
            os.chmod(out, 0o600)
        except OSError:
            pass
        return out, "present"
    source = verdict.source or ""
    if verdict.identity == "agent-turn":
        # A turn may resolve its own agent; the backend still decides how.
        source = fetch_access(slug, refresh=refresh).get("credential_source") or DEFAULT_SOURCE
    if source == "canopy-web":
        how = resolve_canopy_web(slug, out)
    else:
        how = resolve_1password(slug, out, verdict, runner=runner)
    return out, f"resolved from {how}"


# ──────────────────────────────────────────────────────────────────────────────
# keeping ~/.<slug>/.env current with the vault, cheaply
# ──────────────────────────────────────────────────────────────────────────────

#: Re-checked at most this often per agent. Shared across processes through the stamp,
#: because the caller is a SessionStart hook: a new process every session.
SYNC_TTL = 300
_OP_REF_VAULT = re.compile(r"op://([^/\s]+)/")
#: Only the agent's key decides whose vault is read — never a personal session.
_OP_PERSONAL_ENV_PREFIXES = ("OP_SESSION_",)
_OP_PERSONAL_ENV_KEYS = ("OP_ACCOUNT", "OP_CONNECT_HOST", "OP_CONNECT_TOKEN")


def stamp_path(slug: str) -> Path:
    """Beside the env file: the fingerprint last injected. The cloud runner writes the
    same file in the same format (`cloud_runner._refresh_agent_env`)."""
    return env_path(slug).with_name(".env.stamp")


def op_key_account(token: str) -> str:
    """The 1Password account a service-account key signs in to, from the key's public
    header — never a secret. A key minted in the wrong account fails every call with
    "Signin credentials are not compatible…", which names nothing (ACE, 2026-10-12)."""
    import base64
    try:
        raw = token.strip().removeprefix("ops_")
        body = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        return str(body.get("signInAddress") or "").removeprefix("https://").rstrip("/")
    except Exception:  # noqa: BLE001
        return ""


def _sync_op_err(r: subprocess.CompletedProcess, what: str, token: str) -> str:
    # op names the reference or vault it could not read, never a value.
    err = " ".join((r.stderr or "").split())[:300] or f"{what} exited {r.returncode}"
    if "not compatible" in err or "signin" in err.lower().replace(" ", ""):
        account = op_key_account(token)
        if account:
            err += f" — this key signs in to {account}; re-mint it in the account that holds the vault"
    return err


def vault_fingerprint(tpl: Path, env: dict, token: str, *, runner: Runner) -> str:
    """The template's hash plus each vault's `content_version` — which moves on any item
    change. One small `op vault get` per vault the template names; no secret is read."""
    import hashlib

    from orchestrator.agent_instance import expand_vars
    text = tpl.read_text(encoding="utf-8")
    parts = [hashlib.sha256(text.encode()).hexdigest()]
    # ${AGENT_VAULT} refs name the vault op inject will read once the variable is set.
    for vault in sorted(set(_OP_REF_VAULT.findall(expand_vars(text, env)))):
        r = runner(["op", "vault", "get", vault, "--format", "json"], capture_output=True,
                   text=True, timeout=45, env=env)
        if r.returncode != 0:
            raise CredError(_sync_op_err(r, f"op vault get {vault}", token))
        try:
            version = json.loads(r.stdout or "{}").get("content_version")
        except ValueError:
            version = None
        if version is None:
            raise CredError(f"op vault get {vault} returned no content_version")
        parts.append(f"{vault}={version}")
    return "|".join(parts)


def sync_env(slug: str, *, ttl: float = SYNC_TTL, runner: Optional[Runner] = None,
             now: Optional[float] = None, repo: Optional[Path] = None) -> str:
    """Bring ~/.<slug>/.env up to date with the agent's vault, without waste. Returns
    what happened: "skipped: …", "unchanged", or "re-injected".

    Runs only with the agent's OWN service-account key in the environment (the
    SessionStart hook stages it): that key never prompts, which is what makes checking on
    every session safe — the user's desktop app is never asked (the "constant op prompt"
    ruled out on 2026-10-09). Throttled to once per `ttl` per agent via the stamp; when
    due, one `op vault get` per vault decides; the full `op inject` runs only when the
    fingerprint moved or the file is missing. A failure keeps the existing file and raises
    CredError with op's reason."""
    runner = runner or subprocess.run
    token = os.environ.get("OP_SERVICE_ACCOUNT_TOKEN", "").strip()
    if not token:
        return "skipped: no agent 1Password key in this session"
    repo = repo or _agent_repo(slug)
    tpl = repo / ".env.tpl" if repo else None
    if tpl is None or not tpl.is_file():
        return f"skipped: no .env.tpl for '{slug}' on this machine"
    out, stamp = env_path(slug), stamp_path(slug)
    now = time.time() if now is None else now
    try:
        prior = json.loads(stamp.read_text(encoding="utf-8"))
        prior = prior if isinstance(prior, dict) else {}
    except (OSError, ValueError):
        prior = {}
    if out.is_file() and now - float(prior.get("checked_at") or 0) < ttl:
        return "skipped: checked recently"
    env = {k: v for k, v in os.environ.items()
           if k not in _OP_PERSONAL_ENV_KEYS and not k.startswith(_OP_PERSONAL_ENV_PREFIXES)}
    # The vault a RUNNER already knows (it staged this agent's key with it, from canopy-web)
    # is the answer — asking canopy-web again needs a person's token, which a box has none
    # of. That is what let cloud_runner keep its own copy of this function; it calls this
    # one now (agent-setup-lifecycle.md phase 2).
    staged = env.get("AGENT_VAULT", "").strip() if env.get("AGENT_SLUG", "") == slug else ""
    env = _template_env(slug, tpl, env, staged)

    def record(**fields) -> None:
        try:
            out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _write_private(stamp, json.dumps({**prior, **fields, "checked_at": now}))
        except OSError:
            pass  # an unwritable stamp only costs a re-check

    try:
        fingerprint = vault_fingerprint(tpl, env, token, runner=runner)
    except CredError:
        record()  # throttle the retry too: an unreachable 1Password costs one probe per ttl
        raise
    except subprocess.TimeoutExpired:
        record()
        raise CredError("1Password did not answer within 45s")
    except OSError as e:  # FileNotFoundError: no `op` on this machine
        record()
        raise CredError(f"could not run the 1Password CLI (`op`): {e.strerror or e} — {OP_INSTALL_URL}")
    if out.is_file() and fingerprint == prior.get("fingerprint"):
        record()
        return "unchanged"
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=str(out.parent), prefix=".tmp-", suffix=".env")
    os.close(fd)
    os.chmod(tmp, 0o600)
    try:
        try:
            r = runner(["op", "inject", "-f", "-i", str(tpl), "-o", tmp], capture_output=True,
                       text=True, timeout=180, env=env)
        except (OSError, subprocess.TimeoutExpired) as e:
            record()
            raise CredError(f"op inject did not complete for '{slug}': {e}")
        if r.returncode != 0:
            record()
            raise CredError(f"op inject failed for '{slug}': {_sync_op_err(r, 'op inject', token)}")
        os.chmod(tmp, 0o600)
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    record(fingerprint=fingerprint, injected_at=now)
    return "re-injected"


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
@click.option("--refresh", is_flag=True, help="Re-resolve even if ~/.<agent>/.env exists.")
def env_cmd(agent, refresh):
    """Ensure ~/.<agent>/.env exists (resolving it only when missing or --refresh) and
    print its path. Never prints a value."""
    slug = _require_agent(agent)
    path, how = ensure_env(slug, refresh=refresh)
    click.echo(f"[canopy cred] {slug}: {how}", err=True)
    click.echo(str(path))


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
    """Re-inject ~/.<agent>/.env from its vault IF the vault or .env.tpl changed.

    Cheap enough for every session start: throttled per agent, one `op vault get` per
    vault when due, the full `op inject` only on a change. Needs the agent's own
    1Password key in $OP_SERVICE_ACCOUNT_TOKEN (never the desktop app). Never prints a
    value; a failure keeps the existing file and exits 1 with op's reason."""
    slug = _require_agent(agent)
    try:
        click.echo(f"[canopy cred] {slug}: {sync_env(slug, ttl=ttl)}", err=True)
    except CredError as e:
        # The reason on stderr and a non-zero exit, so a caller (a runner's claim) can say
        # why its turns run on the last good file.
        click.echo(f"[canopy cred] {slug}: {e}", err=True)
        raise SystemExit(1)


@cred_group.command("google-token")
@AGENT_OPT
def google_token_cmd(agent):
    """Write a short-lived access token for the agent's OWN Google login and print the
    file's path (never the token): ~/.canopy/google-token/<agent>.json, mode 0600, with
    `access_token`, `expires_at` (epoch seconds) and `account`.

    For tools that should act on Google as the agent instead of a shared service account.
    Reuses the file while it has 5+ minutes left. A runner turn may only ask for its own
    agent."""
    slug = _require_agent(agent)
    click.echo(str(google_token(slug)))


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
