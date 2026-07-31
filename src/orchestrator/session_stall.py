"""Which sessions have stopped and are waiting on a human — and why.

Completion is STRUCTURAL, read off the transcript's own end-of-turn marker, never
off elapsed time. Claude Code stamps every assistant record with the API's
`stop_reason`: "tool_use" means "I am calling a tool and will continue after its
result"; every other terminal value means the model stopped and a person is next.
That distinction is the only completion signal immune to how long a tool takes,
which is exactly what makes it the right one — an agent turn is silent for as long
as its longest tool call.

This mirrors `canopy_runner.chat_bridge.hands_back_to_human` in canopy-web, which
was idle-based until 2026-07-26; a 3-second quiet window meant the first Bash call
ended the turn, and eleven consecutive labs turns "finished" having bridged only
their preambles. Do not reintroduce a time-based test here.

Pure logic: records in, verdicts out. No file or clock access, so the rules
unit-test without fixtures.
"""
from __future__ import annotations

import re


def hands_back_to_human(rec: dict) -> bool:
    """True when this assistant record ended the turn and left a person to act.

    A missing/None stop_reason is NOT an ending — a writer that omits the field
    must not be read as ending the turn on every record.
    """
    if rec.get("type") != "assistant":
        return False
    msg = rec.get("message")
    reason = msg.get("stop_reason") if isinstance(msg, dict) else None
    return isinstance(reason, str) and reason != "tool_use"


def last_assistant_record(records: list[dict]) -> dict | None:
    """The most recent assistant record, or None if there is none."""
    for rec in reversed(records):
        if rec.get("type") == "assistant":
            return rec
    return None


def final_assistant_text(records: list[dict]) -> str:
    """The text blocks of the last assistant record, newline-joined.

    Tool-use blocks are dropped: the classifier reasons about what the agent SAID,
    not what it called.
    """
    rec = last_assistant_record(records)
    if rec is None:
        return ""
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [b.get("text", "") for b in content
             if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(p for p in parts if p)


def is_working(records: list[dict]) -> bool:
    """True while a tool call is outstanding. An empty transcript is not working."""
    rec = last_assistant_record(records)
    if rec is None:
        return False
    return not hands_back_to_human(rec)


# A human prompt is MECHANICAL when it carries no information the agent did not
# already have — the human is acting as a clock, not a decision-maker. Every
# pattern here is a verbatim prompt from the 2026-07-30 census (spec §1).
#
# This is the backtest's ANSWER KEY, so it is deliberately conservative: a
# false "mechanical" inflates the measured value of auto-nudging, which is the
# one error that would talk us into shipping something harmful.
_MECHANICAL_PATTERNS = [
    r"^keep going\b",
    r"^try again\b",
    r"^(yes|yep|yeah|ok|okay|sure)\b[\s,.!]*$",
    r"^(yeah|yes|ok|okay)?\s*go (ahead|for it)\b",
    r"^do the plan\b",
    r"^(okay |ok )?merge it\b",
    r"^are you still working",
    r"\bgood to close\s?o?\s?ut\b",
    r"\bclose out this session\b",
    r"\bready to close out\b",
    r"\bclose this out\b",
    r"^continue\b",
    r"^proceed\b",
]

# Above this, a prompt is carrying real content even if it opens with a cue word
# ("yes, but first rewrite..."). Tuned so every census mechanical prompt fits:
# the longest is 71 chars.
_MECHANICAL_MAX_CHARS = 90

_MECHANICAL_RE = re.compile("|".join(_MECHANICAL_PATTERNS), re.IGNORECASE)


def is_mechanical(text: str) -> bool:
    """True when a human prompt carried no information — the clock-work class.

    Ground truth for the backtest: if the human's next message was mechanical,
    an auto-nudge at that point would have been RIGHT.
    """
    stripped = (text or "").strip()
    if not stripped or len(stripped) > _MECHANICAL_MAX_CHARS:
        return False
    return bool(_MECHANICAL_RE.search(stripped))
