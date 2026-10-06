"""Request provenance — every call canopy makes to canopy-web says WHAT made it and,
from inside a runner-launched Claude session, WHICH turn/session it came from.

Why: a scratch script in one Claude session posted chat messages to agent hal, and
nothing on canopy-web could say what had created those sessions. The bearer token
names an identity (hal), not a caller — every turn hal runs uses the same PAT. So the
client now attaches, beside the bearer:

  X-Canopy-Client          <tool>/<version>          e.g. canopy-cli/0.2.590
  X-Canopy-Parent-Turn     canopy-web turn uuid       the turn this session is running
  X-Canopy-Parent-Session  canopy-web session uuid    that turn's chat session
  X-Canopy-Parent-Task     emdash task name           laptop sessions
  X-Canopy-Parent-Host     hostname
  X-Canopy-Claude-Session  Claude Code session id
  User-Agent               canopy-cli/<version> (python/<x.y>; <platform>)

canopy-web (hal/turn-provenance) persists and logs them; it tolerates any of them
missing, so a header that cannot be computed is simply left off.

WHERE THE PARENT COMES FROM — first source with a value wins, per field:
  1. env `CANOPY_TURN_ID` / `CANOPY_SESSION_ID` / `CANOPY_EMDASH_TASK` — the cloud
     runner exports these into every session it launches.
  2. the caller envelope `CANOPY_CALLER` names (`turn_id`, `conversation.session_id`)
     — the cloud runner's older contract, still set beside the above.
  3. `~/.canopy/caller/by-task/<task>.json` — the laptop path. emdash launches laptop
     sessions, so the runner cannot set env vars; the plugin's UserPromptSubmit hook
     (`plugins/canopy/hooks/caller_context.py`) claims the runner's one-shot caller
     pointer and leaves this durable record keyed by emdash task. The task is read off
     the worktree path (`…/worktrees/<repo>/emdash-<task>-<suffix>/…`).
The Claude session id is `CLAUDE_CODE_SESSION_ID` (what Claude Code exports to its
tools), `CLAUDE_SESSION_ID`, then the by-task record's `claude_session_id` (the hook
input's `session_id`).

NEVER FAILS A REQUEST. Every lookup is best-effort; `provenance_headers()` swallows
everything and at worst returns only the client headers. Stdlib only — the canopy
plugin has no third-party HTTP deps, and this runs on every call.
"""
from __future__ import annotations

import json
import os
import platform
import re
import socket
import sys
from functools import lru_cache
from pathlib import Path
from typing import Optional

CALLER_ROOT = Path.home() / ".canopy" / "caller"
DEFAULT_CLIENT = "canopy-cli"

_UUID = re.compile(r"^[0-9a-fA-F-]{8,64}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
# Header values must be one printable line; anything else is dropped, not escaped.
_HEADER_SAFE = re.compile(r"^[\x20-\x7e]{1,256}$")

HEADER_CLIENT = "X-Canopy-Client"
HEADER_TURN = "X-Canopy-Parent-Turn"
HEADER_SESSION = "X-Canopy-Parent-Session"
HEADER_TASK = "X-Canopy-Parent-Task"
HEADER_HOST = "X-Canopy-Parent-Host"
HEADER_CLAUDE = "X-Canopy-Claude-Session"


def caller_root() -> Path:
    """Read at call time so a test that monkeypatches `CALLER_ROOT` moves this too."""
    return CALLER_ROOT


def by_task_dir() -> Path:
    return caller_root() / "by-task"


@lru_cache(maxsize=1)
def _version() -> str:
    try:
        from orchestrator import version as _v

        return _v.resolve()
    except Exception:  # noqa: BLE001 — a version probe must never break a request
        return "unknown"


def client_id(client: str = DEFAULT_CLIENT) -> str:
    """`<tool>/<version>`, e.g. `canopy-cli/0.2.590`."""
    return f"{client}/{_version()}"


def user_agent(client: str = DEFAULT_CLIENT) -> str:
    py = f"{sys.version_info.major}.{sys.version_info.minor}"
    return f"{client_id(client)} (python/{py}; {platform.system().lower() or 'unknown'})"


def task_candidates(cwd: Optional[str] = None) -> list:
    """emdash task names this cwd could belong to, most specific first.

    emdash starts a session in `…/worktrees/<repo>-<hash>/emdash-<task>-<suffix>`; the
    random `-<suffix>` makes the bare stem the likelier task name, so both are tried
    (the same rule `caller_context.task_candidates` applies to the transcript path).
    """
    try:
        parts = Path(cwd or os.getcwd()).resolve().parts
    except Exception:  # noqa: BLE001
        return []
    out: list = []
    for i, part in enumerate(parts):
        if part != "worktrees":
            continue
        for leaf_part in parts[i + 1:i + 3]:
            if not leaf_part.startswith("emdash-"):
                continue
            leaf = leaf_part[len("emdash-"):]
            stem = leaf.rpartition("-")[0]
            for name in (stem, leaf):
                if name and _NAME.match(name) and name not in out:
                    out.append(name)
    return out


def _read_json(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return doc if isinstance(doc, dict) else {}


def _from_caller_env() -> dict:
    """turn/session ids from the envelope `CANOPY_CALLER` names, or {}."""
    path = os.environ.get("CANOPY_CALLER", "").strip()
    if not path:
        return {}
    env = _read_json(Path(path))
    conv = env.get("conversation") if isinstance(env.get("conversation"), dict) else {}
    return {"turn_id": env.get("turn_id"), "session_id": (conv or {}).get("session_id")}


def _from_by_task(names: list) -> "tuple[str, dict]":
    """(task, record) for the first candidate with a by-task record, or ("", {})."""
    root = by_task_dir()
    for name in names:
        if not name or not _NAME.match(name):
            continue
        rec = _read_json(root / f"{name}.json")
        if rec:
            return name, rec
    return "", {}


def _clean(value, pattern=None) -> str:
    s = str(value or "").strip()
    if not s or not _HEADER_SAFE.match(s):
        return ""
    if pattern is not None and not pattern.match(s):
        return ""
    return s


def resolve_parent(cwd: Optional[str] = None) -> dict:
    """The originating turn/session/task/host/claude-session, as far as it is knowable.

    Keys: turn_id, session_id, task, host, claude_session_id — each "" when unknown.
    """
    env = os.environ
    turn = _clean(env.get("CANOPY_TURN_ID"), _UUID)
    session = _clean(env.get("CANOPY_SESSION_ID"), _UUID)
    task = _clean(env.get("CANOPY_EMDASH_TASK"), _NAME)
    claude = _clean(env.get("CLAUDE_CODE_SESSION_ID") or env.get("CLAUDE_SESSION_ID"), _UUID)

    if not (turn and session):
        caller = _from_caller_env()
        turn = turn or _clean(caller.get("turn_id"), _UUID)
        session = session or _clean(caller.get("session_id"), _UUID)

    candidates = [task] if task else task_candidates(cwd)
    if not (turn and session and claude) or not task:
        found, rec = _from_by_task(candidates)
        if found:
            task = task or found
            turn = turn or _clean(rec.get("turn_id"), _UUID)
            session = session or _clean(rec.get("session_id"), _UUID)
            claude = claude or _clean(rec.get("claude_session_id"), _UUID)
    if not task and candidates:
        task = candidates[0]

    try:
        host = _clean(socket.gethostname())
    except Exception:  # noqa: BLE001
        host = ""
    return {"turn_id": turn, "session_id": session, "task": task,
            "host": host, "claude_session_id": claude}


def provenance_parent(cwd: Optional[str] = None) -> dict:
    """The optional `parent` payload object canopy-web accepts — only known fields."""
    try:
        return {k: v for k, v in resolve_parent(cwd).items() if v}
    except Exception:  # noqa: BLE001
        return {}


def provenance_headers(client: str = DEFAULT_CLIENT, *, cwd: Optional[str] = None) -> dict:
    """Headers identifying this client and, when knowable, its originating session.

    Always returns at least `X-Canopy-Client` and `User-Agent`; never raises.
    """
    headers: dict = {}
    try:
        headers[HEADER_CLIENT] = client_id(client)
        headers["User-Agent"] = user_agent(client)
    except Exception:  # noqa: BLE001
        headers = {HEADER_CLIENT: f"{client}/unknown", "User-Agent": f"{client}/unknown"}
    try:
        p = resolve_parent(cwd)
    except Exception:  # noqa: BLE001 — provenance is best-effort, the request is not
        return headers
    for key, header in (("turn_id", HEADER_TURN), ("session_id", HEADER_SESSION),
                        ("task", HEADER_TASK), ("host", HEADER_HOST),
                        ("claude_session_id", HEADER_CLAUDE)):
        if p.get(key):
            headers[header] = p[key]
    return headers


def with_provenance(headers: Optional[dict], client: str = DEFAULT_CLIENT) -> dict:
    """`headers` plus provenance; the caller's own keys win (e.g. a custom UA)."""
    return {**provenance_headers(client), **(headers or {})}
