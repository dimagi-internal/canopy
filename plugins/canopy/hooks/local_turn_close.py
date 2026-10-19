#!/usr/bin/env python3
"""SessionEnd: upload a LOCAL turn's transcript to canopy-web, then finish the turn.

A session a person opens on their private runner with an agent is a local turn
(canopy-web#1704; `agent_op_env.py` opened it, `caller_context.py` showed its envelope).
Without the runner app nothing else would send its transcript anywhere: the plugin
only keeps `~/.claude/canopy/session-log.jsonl`. This hook hands `transcript_path` to
`canopy local close`, which appends it to the turn in batches (each with a batch id,
so a retry is not appended twice) and finishes the turn `done`.

Inert unless `~/.canopy/local/<session id>.json` exists — every other session pays one
`stat`. Fail-open: never blocks the session from ending; one line when it fails.
stdlib only.
"""
import json
import os
import re
import shutil
import subprocess
import sys

LOCAL_ROOT = os.path.expanduser("~/.canopy/local")


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0
    session_id = str(data.get("session_id") or "")
    if not re.match(r"^[A-Za-z0-9._-]{1,100}$", session_id):
        return 0
    if not os.path.isfile(os.path.join(LOCAL_ROOT, f"{session_id}.json")):
        return 0
    canopy = shutil.which("canopy")
    if not canopy:
        return 0
    cmd = [canopy, "local", "close", "--session-id", session_id]
    transcript = str(data.get("transcript_path") or "")
    if transcript and os.path.isfile(transcript):
        cmd += ["--transcript", transcript]
    # DETACHED, never waited on: Claude Code cancels a SessionEnd hook as it exits
    # ("Hook cancelled" — measured on `claude -p`, 2026-10-19), so an upload run inline
    # never finished. The child gets its own session and outlives this process; what it
    # says goes to a log beside the session's state. A close that still never happens
    # (power loss) is caught up by the next `canopy local open` on this machine.
    log = os.path.join(LOCAL_ROOT, f"{session_id}.close.log")
    try:
        with os.fdopen(os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "ab") as out:
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                             start_new_session=True, close_fds=True)
    except OSError as e:
        print(f"canopy: could not close this session's local turn ({type(e).__name__})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
