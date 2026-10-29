#!/usr/bin/env python3
"""SessionEnd: delete this session's act-as credential (and any expired one).

`canopy act-as request` stores a credential that acts as a PERSON at
``~/.canopy/act-as/<claude session>.token`` (see ``src/orchestrator/act_as_cli.py``). canopy-web
already refuses it once the turn stops executing or 30 minutes pass; this removes the
file itself when the Claude session that asked for it ends, so it does not outlive the
turn on disk. Also sweeps every file whose ``expires_at`` has passed.

Inert when the directory does not exist — every other session pays one `stat`.
Fail-open: never blocks the session from ending. stdlib only.
"""
import datetime as dt
import json
import os
import sys
import time

ROOT = os.path.expanduser("~/.canopy/act-as")


def _expired(doc: dict, now: float) -> bool:
    try:
        when = dt.datetime.fromisoformat(str(doc.get("expires_at")).replace("Z", "+00:00"))
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return when.timestamp() <= now


def main() -> int:
    if not os.path.isdir(ROOT):
        return 0
    try:
        data = json.load(sys.stdin)
    except (OSError, ValueError):
        data = {}
    session_id = str((data or {}).get("session_id") or "") if isinstance(data, dict) else ""
    now = time.time()
    for name in os.listdir(ROOT):
        if not name.endswith(".token"):
            continue
        path = os.path.join(ROOT, name)
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            doc = {}
        if not isinstance(doc, dict):
            doc = {}
        mine = bool(session_id) and (doc.get("claude_session_id") == session_id
                                     or name == f"{session_id}.token")
        if mine or _expired(doc, now):
            try:
                os.unlink(path)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 — never block a session from ending
        sys.exit(0)
