#!/usr/bin/env python3
"""No ending a turn that holds an agent task until its result is accepted.

An agent task (canopy-web `/api/agent-tasks/`, docs/architecture/agent-tasks.md there) is a
page or backend calling an agent INLINE: a task, structured input, and a JSON Schema the
answer must match. It is done only when the agent calls `submit_agent_task_result` and
canopy-web accepts the result — never because the turn ended. Jonathan, designing it
(2026-10-27): "we will likely have you respond too soon." An agent that writes its answer as
a chat reply, or summarises and stops, leaves the page waiting on nothing.

So this Stop hook reads the turn's caller envelope (via the session's presence record, which
names the turn — `caller_context.py`), and when it carries `agent_task`, asks canopy-web for
the task's status. Queued or running → block the stop and say what to call. Done, failed or
cancelled → let it stop.

Bounded: at most MAX_BLOCKS per task per session, then it lets the stop through and
canopy-web's own close backstop (a follow-up nudge, then `failed`) takes over — a guard that
can block forever wedges the session. Anything it cannot read (no presence record, no
envelope, no task, canopy-web unreachable or not serving the route yet) → exit 0 silently:
the server is the authority, this only saves a round trip.
"""
import json
import os
import re
import subprocess
import sys
import urllib.request

CALLER_ROOT = os.path.expanduser("~/.canopy/caller")
BASE_URL = os.environ.get("CANOPY_WEB_URL", "https://canopy.dimagi.com").rstrip("/")
HEADERS_HELPER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "scripts", "canopy-web-mcp-headers.js")
MAX_BLOCKS = 3
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
OPEN = ("queued", "running")

REASON = (
    "canopy agent_task_guard: this turn holds agent task {name} ({id}), and canopy-web has no "
    "accepted result for it yet (status: {status}). Ending the turn now leaves the page that "
    "asked waiting on nothing. Finish the task, then call the `submit_agent_task_result` MCP "
    "tool with a result that matches the task's output_schema (re-read it with "
    "`get_agent_task`). A chat reply is not a result. If the result is refused, fix what its "
    "errors name and submit again."
)


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def agent_task(session_id: str, *, root=None):
    """The `agent_task` block of this session's current turn envelope, or None."""
    root = root or CALLER_ROOT
    if not session_id or not _ID.match(session_id):
        return None
    pres = _read_json(os.path.join(root, "presence", f"{session_id}.json")) or {}
    turn_id = str(pres.get("turn_id") or "")
    if not turn_id or not _ID.match(turn_id):
        return None
    task = (_read_json(os.path.join(root, f"{turn_id}.json")) or {}).get("agent_task")
    return task if isinstance(task, dict) and task.get("id") else None


def _auth_headers() -> dict:
    try:
        r = subprocess.run(["node", HEADERS_HELPER], capture_output=True, text=True, timeout=6)
        out = json.loads(r.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    return out if isinstance(out, dict) else {}


def task_status(task_id: str) -> str:
    """canopy-web's status for the task, or "" when it cannot be read."""
    if not _ID.match(task_id):
        return ""
    req = urllib.request.Request(f"{BASE_URL}/api/agent-tasks/{task_id}", headers=_auth_headers())
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return str(json.load(resp).get("status") or "")
    except Exception:  # noqa: BLE001 — unreachable / not deployed / refused: fail open
        return ""


def _count(session_id: str, task_id: str, *, root=None) -> int:
    """Bump and return how many times this session's stop was blocked for this task."""
    d = os.path.join(root or CALLER_ROOT, "agent-task-stops")
    path = os.path.join(d, f"{session_id}.json")
    rec = _read_json(path) or {}
    n = int(rec.get(task_id) or 0) + 1
    rec[task_id] = n
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
    except OSError:
        pass
    return n


def main() -> int:
    try:
        data = json.load(sys.stdin)
        sid = str(data.get("session_id") or "")
        task = agent_task(sid)
        if not task:
            return 0
        status = task_status(str(task["id"]))
        if status not in OPEN:
            return 0
        if _count(sid, str(task["id"])) > MAX_BLOCKS:
            return 0
        print(json.dumps({"decision": "block", "reason": REASON.format(
            name=task.get("name") or "", id=task["id"], status=status)}))
    except Exception:  # noqa: BLE001 — never wedge a session on a broken guard
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
