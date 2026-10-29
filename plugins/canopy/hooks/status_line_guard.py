#!/usr/bin/env python3
"""No closeout status line while the person is IN the conversation.

`agent-core/turn.md` has every turn end on a status line — "✅ Session complete — safe to
close." or "⏸ Session paused — waiting on you for: …". That line exists for an UNATTENDED
turn (an email, a schedule, a dispatch): nobody is watching, so the last line is the only
signal of whether the work finished. When the person is chatting with the agent, the line is
wrong twice over. They are right there, so it tells them nothing. And it is a claim of
completion that a live conversation cannot back: on 2026-10-27 an ada session that started
from Jonathan's email and continued in canopy-web chat ended three replies in a row on
"✅ Session complete — safe to close." with work it had just promised still undone. He asked:
"are you just auto-printing that out?" — it was, because the rule was keyed on the turn's
MODE (auto), which is fixed at claim, instead of on who is present, which changes with
every message.

Presence is the latest message's channel. `caller_context.py` records it on every prompt at
`~/.canopy/caller/presence/<claude_session_id>.json`; this Stop hook reads that record and,
when it says present, blocks a stop whose closing message carries the status line, once
(`stop_hook_active` → never loop). It matches only the two fixed tokens the procedure itself
mandates, so it is not a phrase classifier: the retired regex decide-guard taught that
judging free-form hand-backs by pattern is whack-a-mole (agent-core/decide_guard.py).

No presence record (a non-canopy session, or one that predates this hook), ABSENT, or any
error → exit 0 silently. Never blocks an unattended turn.
"""
import json
import os
import re
import sys

CALLER_ROOT = os.path.expanduser("~/.canopy/caller")
_SID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
STATUS_LINE = re.compile(r"Session (complete|paused|monitoring|incomplete)", re.IGNORECASE)

REASON = (
    "canopy status_line_guard: the person is in this conversation (presence: PRESENT — the "
    "latest message came by {origin}), so a closeout status line (\"Session complete\" / "
    "\"Session paused\") does not belong here. It is the unattended-turn signal, and here it "
    "reads as a completion claim. Rewrite your closing message without it: answer what was "
    "asked, and if something is still open, say plainly what it is and either do it now (if "
    "it is inside your authority) or ask the person for the one decision that is theirs."
)


def presence(session_id: str, *, root=None):
    if not session_id or not _SID.match(session_id):
        return None
    try:
        with open(os.path.join(root or CALLER_ROOT, "presence", f"{session_id}.json"),
                  encoding="utf-8") as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text") or "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def last_assistant_text(data: dict) -> str:
    """Claude Code passes `last_assistant_message`; older builds don't — read the transcript."""
    msg = data.get("last_assistant_message")
    if isinstance(msg, str) and msg:
        return msg
    path = data.get("transcript_path") or ""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()[-200:]
    except (OSError, TypeError):
        return ""
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        m = rec.get("message") if isinstance(rec, dict) else None
        if rec.get("type") == "assistant" and isinstance(m, dict):
            text = _text(m.get("content"))
            if text.strip():
                return text
    return ""


def main() -> int:
    try:
        data = json.load(sys.stdin)
        if data.get("stop_hook_active"):
            return 0
        rec = presence(str(data.get("session_id") or ""))
        if not rec or not rec.get("present"):
            return 0
        if not STATUS_LINE.search(last_assistant_text(data)):
            return 0
        print(json.dumps({"decision": "block",
                          "reason": REASON.format(origin=rec.get("origin") or "chat")}))
    except Exception:  # noqa: BLE001 — never wedge a session on a broken guard
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
