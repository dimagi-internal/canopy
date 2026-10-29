#!/usr/bin/env python3
"""An unattended turn may not say "safe to close" unless its recorded closeout verdict is CLOSED.

Sibling of `status_line_guard.py` (which handles the person being PRESENT). This one covers the
other half: an UNATTENDED, incident-style turn — an alarm, a page — that recorded its closeout
facts with `canopy closeout record` (orchestrator/closeout.py computes the verdict: CLOSED |
MONITORING | NEEDS_YOU | INCOMPLETE). Before this, "✅ Session complete — safe to close." was a
free-typed claim nothing checked: across Hal's CloudWatch alarm turns 2026-08-26..10-29, 36
ended "complete" and about 12 had the incident actually closed (alarm still firing, cause
unconfirmed, the next step owned by "someone with SSO").

The record lives at `~/.canopy/closeout/<claude_session_id>.json`. This Stop hook blocks, once
(`stop_hook_active` → never loop), a closing message that says "Session complete" / "safe to
close" while the latest verdict is not CLOSED, or that carries a `[closeout: X]` token other
than the recorded verdict — and hands back the status line the verdict allows.

Silent (exit 0) when: the person is PRESENT (status_line_guard's case), no record exists for the
session (a turn that is not incident-style never recorded one — the guard only engages on turns
that opted in), the record predates the latest prompt (a reused session's earlier turn), or on
any error. Never blocks on a guess.
"""
import json
import os
import re
import sys

CALLER_ROOT = os.path.expanduser("~/.canopy/caller")
CLOSEOUT_ROOT = os.path.expanduser("~/.canopy/closeout")
_SID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
COMPLETE_CLAIM = re.compile(r"Session complete|safe to close", re.IGNORECASE)
TOKEN = re.compile(r"\[closeout: (CLOSED|MONITORING|NEEDS_YOU|INCOMPLETE)\]")

REASON = (
    "canopy closeout_guard: this turn's recorded closeout verdict is {verdict}, not CLOSED, so it "
    "may not end on \"{claim}\". {problems}End on the status line the verdict allows, verbatim:\n"
    "  {status_line}\n"
    "If the facts changed (the alarm cleared, an owner was assigned, a step re-ran), record them "
    "again with `canopy closeout record` and end on the line it prints."
)
MISMATCH = (
    "canopy closeout_guard: the closing message carries [closeout: {said}] but the recorded "
    "verdict is {verdict}. End on the status line the verdict allows, verbatim:\n  {status_line}"
)


def _json(path):
    try:
        with open(path, encoding="utf-8") as fh:
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
    msg = data.get("last_assistant_message")
    if isinstance(msg, str) and msg:
        return msg
    try:
        with open(data.get("transcript_path") or "", encoding="utf-8") as fh:
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


def decide(data: dict, *, caller_root=None, closeout_root=None):
    """The block reason, or None to let the stop through."""
    if data.get("stop_hook_active"):
        return None
    sid = str(data.get("session_id") or "")
    if not _SID.match(sid):
        return None
    presence = _json(os.path.join(caller_root or CALLER_ROOT, "presence", f"{sid}.json")) or {}
    if presence.get("present"):
        return None
    doc = _json(os.path.join(closeout_root or CLOSEOUT_ROOT, f"{sid}.json"))
    if not doc or doc.get("verdict") not in ("CLOSED", "MONITORING", "NEEDS_YOU", "INCOMPLETE"):
        return None
    try:  # a record from an EARLIER turn of a reused session says nothing about this one
        if float(presence.get("updated_at") or 0) > float(doc.get("recorded_at") or 0):
            return None
    except (TypeError, ValueError):
        return None
    if presence.get("turn_id") and doc.get("turn_id") and presence["turn_id"] != doc["turn_id"]:
        return None
    text = last_assistant_text(data)
    verdict, line = doc["verdict"], doc.get("status_line") or ""
    said = TOKEN.findall(text)
    if said and said[-1] != verdict:
        return MISMATCH.format(said=said[-1], verdict=verdict, status_line=line)
    claim = COMPLETE_CLAIM.search(text)
    if verdict != "CLOSED" and claim:
        problems = doc.get("problems") or []
        return REASON.format(
            verdict=verdict, claim=claim.group(0), status_line=line,
            problems=("Open: " + "; ".join(problems) + ". ") if problems else "")
    return None


def main() -> int:
    try:
        reason = decide(json.load(sys.stdin))
        if reason:
            print(json.dumps({"decision": "block", "reason": reason}))
    except Exception:  # noqa: BLE001 — never wedge a session on a broken guard
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
