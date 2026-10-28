#!/usr/bin/env python3
"""Stop / PostToolUse: stream a LIVE local turn's transcript up to canopy-web.

Before this, a local turn's transcript went up only at SessionEnd (`local_turn_close.py`),
so a session that ran for hours — and dispatched work, and acted as its agent — could not
be read on canopy-web until it ended. This hook hands `transcript_path` to
`canopy local sync`, which appends only the lines added since the last upload.

After every reply (Stop) it syncs; between tool calls (PostToolUse) at most every
SYNC_EVERY seconds, so a long autonomous stretch still shows up. Off with
CANOPY_LOCAL_LIVE_TRANSCRIPT=0 (the close still uploads everything).

Inert unless `~/.canopy/local/<session id>.json` names an open turn — every other session
pays one failed open. Detached and fail-open: never makes the session wait. stdlib only.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time

LOCAL_ROOT = os.path.expanduser("~/.canopy/local")
#: Between tool calls, sync at most this often (a reply's end always syncs).
SYNC_EVERY = 60


def main() -> int:
    if os.environ.get("CANOPY_LOCAL_LIVE_TRANSCRIPT", "").strip() == "0":
        return 0
    try:
        data = json.load(sys.stdin)
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0
    session_id = str(data.get("session_id") or "")
    if not re.match(r"^[A-Za-z0-9._-]{1,100}$", session_id):
        return 0
    try:
        with open(os.path.join(LOCAL_ROOT, f"{session_id}.json"), encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return 0
    if not isinstance(state, dict) or state.get("closed_at") or not state.get("turn_id"):
        return 0
    if data.get("hook_event_name") != "Stop" and \
            time.time() - float(state.get("synced_at") or 0) < SYNC_EVERY:
        return 0
    canopy = shutil.which("canopy")
    if not canopy:
        return 0
    cmd = [canopy, "local", "sync", "--session-id", session_id]
    transcript = str(data.get("transcript_path") or "")
    if transcript and os.path.isfile(transcript):
        cmd += ["--transcript", transcript]
    log = os.path.join(LOCAL_ROOT, f"{session_id}.sync.log")
    try:
        with os.fdopen(os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "ab") as out:
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                             start_new_session=True, close_fds=True)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
