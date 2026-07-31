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
                       batch_size: int = 30, retries: int = 2,
                       stats: dict | None = None,
                       ) -> dict[str, StallVerdict]:
    """Classify many sessions at once. Working sessions never reach the model.

    `runner` is passed through to `stall_judge.classify_tails` only when it is
    not None, so that module's own default runner (`run_claude`) stays in
    force when the caller doesn't override it.

    This is the PRODUCTION classification path (`canopy sessions stalled`),
    and until this fix it made ONE unchunked, untruncated, unretried call
    over every stalled session's tail — at `--hours 168` that's ~102 items
    (~55k tokens), and any single dropped item (a `ValueError` from
    `parse_batch_json`) killed the WHOLE call, same flake class that killed
    the backtest's clean run before `stall_backtest.grade` was hardened.
    `session_stall` had inherited none of that hardening. It now shares the
    exact same `stall_judge.chunk_items` / `stall_judge.call_with_retries`
    machinery `grade` uses (one implementation, not a parallel copy) —
    `batch_size`-sized chunks, each retried up to `retries` times on
    `ValueError`, a chunk that still fails after retries SKIPPED rather than
    fabricating a verdict. A skipped chunk's sessions simply have no entry
    in the returned dict — never a made-up one. `stats`, if a dict is
    passed, is populated with `chunks`, `chunks_failed`, `sessions_skipped`,
    mirroring `grade`'s contract exactly. Left at the default `None`, every
    prior caller (including every existing test) is unaffected.

    A session whose tail is empty/whitespace-only after `final_assistant_text`
    is skipped before it ever reaches a chunk — one such session existed in
    the live corpus, and the old unchunked call sent it to the model
    regardless (`collect_handbacks` already had this discipline; production
    did not). Tails longer than `stall_judge.TAIL_MAX_CHARS` are truncated
    to the last `TAIL_MAX_CHARS` characters — the constant is IMPORTED from
    `stall_judge`, not redefined here, so this path and the backtest can't
    silently drift onto two different truncation lengths.
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
            continue
        tail = final_assistant_text(records)
        if not tail.strip():
            # No provable text to classify -- skip rather than fabricate.
            continue
        if len(tail) > stall_judge.TAIL_MAX_CHARS:
            tail = tail[-stall_judge.TAIL_MAX_CHARS:]
        stalled_ids.append(session_id)
        stalled_stop_reasons.append(stop_reason)
        tails.append(tail)

    if not stalled_ids:
        if stats is not None:
            stats["chunks"] = 0
            stats["chunks_failed"] = 0
            stats["sessions_skipped"] = 0
        return out

    def _classify(chunk_tails, model):
        if runner is not None:
            return stall_judge.classify_tails(chunk_tails, runner=runner, model=model)
        return stall_judge.classify_tails(chunk_tails, model=model)

    chunks_total = 0
    chunks_failed = 0
    sessions_skipped = 0

    combined = list(zip(stalled_ids, stalled_stop_reasons, tails))
    for chunk in stall_judge.chunk_items(combined, batch_size):
        chunks_total += 1
        chunk_ids = [c[0] for c in chunk]
        chunk_stops = [c[1] for c in chunk]
        chunk_tails = [c[2] for c in chunk]

        judgments = stall_judge.call_with_retries(
            _classify, chunk_tails, model=model, retries=retries)
        if judgments is None:
            chunks_failed += 1
            sessions_skipped += len(chunk_ids)
            continue

        for session_id, stop_reason, judgment in zip(chunk_ids, chunk_stops, judgments):
            out[session_id] = StallVerdict(
                "stalled", judgment.klass, judgment.confidence, judgment.reason, stop_reason)

    if stats is not None:
        stats["chunks"] = chunks_total
        stats["chunks_failed"] = chunks_failed
        stats["sessions_skipped"] = sessions_skipped

    return out
