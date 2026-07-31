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

from dataclasses import dataclass

from orchestrator import stall_judge


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


# ── batched classification ──────────────────────────────────────────────────
# Joins the structural detector above with the LLM judgment layer in
# `stall_judge`. One entry point for both the CLI and the backtest: every
# caller has many sessions at once, and a working session's text must never
# reach the model — that's a cost-discipline property, not just an
# optimization, so it's enforced here rather than left to callers.

WORKING = "working"


@dataclass(frozen=True)
class StallVerdict:
    state: str
    klass: str
    confidence: float
    reason: str
    stop_reason: str


def _stop_reason(rec: dict | None) -> str:
    """The last assistant record's stop_reason, or "" when absent/malformed."""
    msg = rec.get("message") if rec else None
    reason = msg.get("stop_reason") if isinstance(msg, dict) else None
    return reason if isinstance(reason, str) else ""


def classify_sessions(records_by_id: list[tuple[str, list[dict]]], *,
                       runner=None, model: str = "haiku",
                       ) -> dict[str, StallVerdict]:
    """Classify many sessions at once. Working sessions never reach the model.

    `runner` is passed through to `stall_judge.classify_tails` only when it is
    not None, so that module's own default runner (`run_claude`) stays in
    force when the caller doesn't override it.
    """
    out: dict[str, StallVerdict] = {}
    stalled_ids: list[str] = []
    stalled_stop_reasons: list[str] = []
    tails: list[str] = []

    for session_id, records in records_by_id:
        rec = last_assistant_record(records)
        stop_reason = _stop_reason(rec)
        if is_working(records):
            out[session_id] = StallVerdict("working", WORKING, 1.0, "", stop_reason)
        else:
            stalled_ids.append(session_id)
            stalled_stop_reasons.append(stop_reason)
            tails.append(final_assistant_text(records))

    if not stalled_ids:
        return out

    kwargs = {"model": model}
    if runner is not None:
        kwargs["runner"] = runner
    judgments = stall_judge.classify_tails(tails, **kwargs)

    for session_id, stop_reason, judgment in zip(stalled_ids, stalled_stop_reasons, judgments):
        out[session_id] = StallVerdict(
            "stalled", judgment.klass, judgment.confidence, judgment.reason, stop_reason)

    return out
