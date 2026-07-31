"""Backtest the stall classifier against history — before it ever nudges anyone.

Every transcript already carries its own answer key. At each point the agent
handed back to a human, the human's actual next message tells us whether an
automated nudge would have been welcome or would have talked over them: a
"keep going" says the handback was safe to auto-continue; a substantive reply
("no, the AWS deploy is still broken") says it was not. `collect_handbacks`
pairs every handback with the reply that followed it, so the rest of this
module can grade the classifier against reality instead of guessing.

The grading step makes that answer key honest by asking two questions from
*disjoint* inputs. `classify` sees only the agent's tail — what the classifier
would have seen in production, before any human has replied. `judge` sees
only the human's reply — the ground truth of whether a nudge would have been
welcome. If either function saw both sides, the backtest would be scoring a
classifier that cannot exist at decision time (the reply doesn't exist yet
when the real classifier has to decide), and every number that came out of it
would be fiction. This mirrors the same disjoint-input discipline enforced in
`stall_judge` itself — see that module's docstring.

Precision is the gate score, not recall, because the two errors are not
symmetric: a false positive (auto-sending when the human meant something
substantive) talks over a person mid-thought, which is the expensive
failure. A false negative (missing a safe nudge) just leaves a session
waiting a little longer, same as today. `score()` reports recall too, as a
measure of how much the auto-send envelope could grow, but precision is what
decides whether it's safe to ship at all.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from orchestrator.session_stall import hands_back_to_human
from orchestrator.stall_judge import AUTO_SEND_CLASSES, classify_tails, judge_replies

# Text prefixes that mark a `user` record as a harness-injected structural
# marker rather than something a human typed. This is the one place this
# module does prefix/string matching, and it is deliberately narrow: these
# are NOT semantic judgments about meaning (that's `judge_replies`' job),
# they are recognizing fixed structural markers the harness itself emits.
_HARNESS_PREFIXES = (
    "<task-notification",
    "<command-message",
    "<local-command",
    "Base directory for this skill",
    "Caveat:",
    "[Request interrupted",
    "[Image:",
)

# A real markdown heading line: 1-6 `#` followed by whitespace. Deliberately
# NOT `text.startswith("#")` (the original, over-broad version of this
# check) -- that matched ANY leading `#`, including things like "#1 issue:
# ..." or "#urgent" that a human could plausibly type, which is not what
# "starts with a markdown heading" is supposed to mean. This is still purely
# structural (markdown heading syntax), not a match on content.
_HEADING_RE = re.compile(r"^#{1,6}[ \t]")

# A fenced code block found this early in the text is the residue of a
# skill/command payload, not a human opening a reply with a snippet.
# Measured 2026-07-30: a skill body's YAML frontmatter (and any leading
# heading) is sometimes already stripped before the body lands in a `user`
# record as a plain `text` block -- e.g. the ace:labs-login skill body
# opens directly with "Run the labs walkthrough login script:\n\n```bash"
# and has NO heading anywhere in it, so a heading+fence check alone misses
# it. This is still a STRUCTURAL signal (the *position* of markdown syntax
# in the string), not a match on what the code says -- same family as the
# other checks in `_is_skill_or_command_payload`.
#
# Position alone is too broad, though: a genuine human reply that opens
# with a short pasted snippet or traceback ("try this:\n\n```\n...") would
# also have an early fence. Reviewed against the real corpus 2026-07-31:
# every genuine skill/command body this signal is meant to catch was well
# over 1,000 characters; nothing a real human replied was anywhere near
# that. `_EARLY_FENCE_MIN_LEN` requires the early fence AND enough total
# length before this signal fires, so a short human reply with a pasted
# snippet survives -- both conditions are structural (position, length),
# not content.
_EARLY_FENCE_WINDOW = 300
_EARLY_FENCE_MIN_LEN = 400


def _is_skill_or_command_payload(text: str) -> bool:
    """A skill/command markdown body, not something a human typed.

    Structural signals only, mirroring `_HARNESS_PREFIXES`'s narrow scope --
    none of these read what the text SAYS, only how it is SHAPED:
      - starts with `# /` (a command's own heading names itself this way)
      - an `ARGUMENTS:` line (a skill's argument spec)
      - starts with a real markdown heading (`_HEADING_RE`) and contains a
        fenced code block anywhere (e.g. the ddd-upload skill body:
        `# DDD Upload` ... ```bash)
      - a fenced code block within the first `_EARLY_FENCE_WINDOW` chars,
        AND the text is at least `_EARLY_FENCE_MIN_LEN` chars long (catches
        skill bodies with no heading at all, per the module note above --
        this is the one that's NOT in the original three-signal set filed
        for this bug, because the real corpus example it fixes has no
        heading to key off of; the length floor keeps it from also
        catching a short human reply that opens with a pasted snippet)
    """
    stripped = text.lstrip()
    if stripped.startswith("# /"):
        return True
    if any(line.startswith("ARGUMENTS:") for line in text.splitlines()):
        return True
    if _HEADING_RE.match(stripped) and "```" in text:
        return True
    fence_pos = text.find("```")
    if 0 <= fence_pos <= _EARLY_FENCE_WINDOW and len(text) >= _EARLY_FENCE_MIN_LEN:
        return True
    return False

# Per-item truncation applied at collection, before anything reaches a model.
# A week of real history runs ~1,800 tail chars per handback on average with
# a max over 19k — untruncated, a single batched call would carry hundreds of
# thousands of tokens of items alone. The two directions are NOT symmetric:
#   - Tail: keep the END. "Next I'll…" or the open question lives at the tail
#     of the agent's own message; the head is scene-setting and disposable.
#     Truncating the front, not the back, is what preserves the signal
#     `classify_tails` actually needs.
#   - Reply: keep the START. A human reply long enough to need truncating is
#     substantive by construction — trimming it can only ever discard MORE
#     evidence of substance, never manufacture the "mechanical" reading. That
#     keeps truncation from being able to push a verdict toward the dangerous
#     direction (see the module docstring on why false positives are the
#     expensive error).
TAIL_MAX_CHARS = 2000
REPLY_MAX_CHARS = 1000


@dataclass(frozen=True)
class Handback:
    tail: str
    reply: str


@dataclass(frozen=True)
class BacktestCase:
    klass: str
    would_send: bool
    human_was_mechanical: bool
    tail: str
    reply: str


def human_text(rec: dict) -> str | None:
    """The text of a human-typed `user` record, or None.

    Rejects: non-`user` records; a non-dict `message`; list content with no
    `text` blocks (tool results); empty/whitespace text; text that starts
    with a harness-injection marker (`_HARNESS_PREFIXES`); and text that is
    a skill/command payload (`_is_skill_or_command_payload`) — none of
    these are something a person typed, and counting them as replies would
    inflate (or corrupt) every number downstream. Bug measured 2026-07-30:
    ~2% of a 962-handback week had a skill/command body scored as if it
    were the human's answer, because it carried no `_HARNESS_PREFIXES`
    marker of its own (the harness's preamble line, e.g. "Base directory
    for this skill", lands in a separate record from the skill body).
    """
    if rec.get("type") != "user":
        return None
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")

    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        text = "\n".join(p for p in parts if p)
    else:
        return None

    if not text or not text.strip():
        return None
    if text.startswith(_HARNESS_PREFIXES):
        return None
    if _is_skill_or_command_payload(text):
        return None
    return text


def _tail_text(rec: dict) -> str:
    """The text blocks of THIS assistant record, newline-joined.

    Deliberately not `final_assistant_text(records)`, which returns the
    LAST assistant record's text for every call — using it here would give
    every handback in a transcript the same tail.
    """
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [b.get("text", "") for b in content
             if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(p for p in parts if p)


def collect_handbacks(records: list[dict]) -> list[Handback]:
    """Pair every handback with the human reply that followed it.

    Single forward pass, tracking at most one *pending* (unpaired) handback
    at a time. A record where the agent handed back to a human
    (`hands_back_to_human`) becomes the new pending handback — REPLACING
    whatever was pending before it, not queuing alongside it. The next
    record yielding a non-None `human_text` is paired with whatever is
    currently pending and clears it; if nothing is pending, that reply is
    irrelevant and ignored. A handback still pending when the transcript
    ends is dropped — there is no answer key for it yet.

    The replace-don't-queue rule is load-bearing. Bug measured 2026-07-30:
    an earlier forward-scan-per-handback version paired EVERY unanswered
    handback with the next reply it could find, so three consecutive
    `end_turn` records followed by one human message produced three
    `Handback`s all sharing that single reply — one human decision scored
    as up to six independent cases in the real corpus (337 of 962
    handbacks, 35%, shared a reply with another handback). A human can only
    have been responding to the LAST thing the agent said before they
    spoke, so only the most recent pending handback is answerable; earlier
    ones in the same unanswered run never got a reply of their own and must
    be dropped, not back-filled.

    A handback whose own tail is empty or whitespace-only is skipped
    entirely (never becomes pending). `hands_back_to_human` only inspects
    `stop_reason`, never content shape, so an `end_turn` record with no
    text blocks is not provably unreachable; grading it would silently hand
    `classify` a fabricated empty-string case. This mirrors how `human_text`
    already treats an empty reply as "not a real message." The emptiness
    check runs on the RAW tail, before truncation, so an all-whitespace
    tail is still caught.

    Tail and reply are then truncated to `TAIL_MAX_CHARS` / `REPLY_MAX_CHARS`
    (see the module-level comment above those constants for why the two
    directions differ) — this keeps a single transcript's worth of history
    from blowing up the size of every downstream `classify`/`judge` call.
    """
    out: list[Handback] = []
    pending_tail: str | None = None
    for rec in records:
        if hands_back_to_human(rec):
            tail = _tail_text(rec)
            if not tail.strip():
                continue
            if len(tail) > TAIL_MAX_CHARS:
                tail = tail[-TAIL_MAX_CHARS:]
            pending_tail = tail
            continue
        if pending_tail is None:
            continue
        reply = human_text(rec)
        if reply is None:
            continue
        if len(reply) > REPLY_MAX_CHARS:
            reply = reply[:REPLY_MAX_CHARS]
        out.append(Handback(tail=pending_tail, reply=reply))
        pending_tail = None
    return out


def _call_with_retries(fn, items: list[str], *, model: str, retries: int):
    """Call `fn(items, model=model)`, retrying on `ValueError` only.

    A `ValueError` out of `classify_tails`/`judge_replies` means the model
    returned a malformed or miscounted batch response (see
    `parse_batch_json`) — a transient flake worth one more try, not proof
    the model path is broken. Up to `retries` additional attempts are made
    (so `retries=2` means 3 attempts total); if every attempt still raises
    `ValueError`, this returns `None` rather than fabricating a result —
    the caller (`grade`) is what decides what "no result for this chunk"
    means (skip it).

    Any exception OTHER than `ValueError` (e.g. a `RuntimeError` from a
    broken `claude` subprocess) propagates immediately, on the first
    attempt, unretried — that signals the model path itself is unusable,
    which a retry cannot fix and silently swallowing would hide.
    """
    attempts = retries + 1
    for attempt in range(attempts):
        try:
            return fn(items, model=model)
        except ValueError:
            if attempt == attempts - 1:
                return None


def grade(handbacks: list[Handback], *, classify=classify_tails,
          judge=judge_replies, model: str = "haiku",
          batch_size: int = 30, retries: int = 2,
          stats: dict | None = None) -> list[BacktestCase]:
    """Grade each handback with two independent model calls, chunked.

    `classify` sees only tails; `judge` sees only replies. Neither call may
    see the other's input — that separation is what makes the resulting
    score a measurement of the classifier that can actually run in
    production. See the module docstring. This holds PER CHUNK, not just
    overall: handbacks are sliced into consecutive runs of `batch_size`, and
    each chunk still makes exactly two separate calls — one over that
    chunk's tails, one over that chunk's replies. A single unchunked call
    does not scale (a week of real history is ~1,262 handbacks and would
    carry ~571k tokens of items in one prompt); `classify_tails` and
    `judge_replies` stay single-shot and unchanged — chunking is `grade`'s
    job because `grade` is what owns the calls.

    Fail-loud must not mean fail-total. `classify_tails`/`judge_replies`
    correctly refuse to fabricate a verdict when the model drops an item
    from a batch (`ValueError`) — that contract must not weaken. But at 21+
    chunks in a real run, the odds of ONE chunk flaking approach certainty,
    and discarding ~40 successful model calls because of one short response
    is its own kind of dishonesty (silently wasting the money already
    spent, or worse, tempting a caller to catch-and-ignore the whole run).
    So each chunk's `classify` and `judge` calls are retried independently
    up to `retries` times via `_call_with_retries` — retrying only the pass
    that actually failed, never redoing one that already succeeded. If a
    chunk still fails after retries, that chunk (and only that chunk) is
    skipped: its handbacks contribute no `BacktestCase`, and the run
    continues with the rest. This is NOT the same as fail-soft: a skip is
    never silent (see `stats` below), and `RuntimeError`/other non-`ValueError`
    exceptions still abort the whole run immediately, same as before.

    `stats`, if a dict is passed, is populated with `chunks` (total chunks
    attempted), `chunks_failed` (chunks skipped after exhausting retries),
    and `handbacks_skipped` (handbacks belonging to a skipped chunk) — so a
    caller can refuse to present a measurement that silently dropped data.
    Left at the default `None`, nothing is written and every prior caller
    (including every existing test) is unaffected.

    Results accumulate in input order across chunk boundaries; a skipped
    chunk simply contributes nothing, so surrounding chunks' order among
    themselves is preserved.
    """
    if not handbacks:
        if stats is not None:
            stats["chunks"] = 0
            stats["chunks_failed"] = 0
            stats["handbacks_skipped"] = 0
        return []

    cases: list[BacktestCase] = []
    chunks_total = 0
    chunks_failed = 0
    handbacks_skipped = 0

    for start in range(0, len(handbacks), batch_size):
        chunk = handbacks[start:start + batch_size]
        chunks_total += 1

        judgments = _call_with_retries(
            classify, [h.tail for h in chunk], model=model, retries=retries)
        if judgments is None:
            chunks_failed += 1
            handbacks_skipped += len(chunk)
            continue

        mechanicals = _call_with_retries(
            judge, [h.reply for h in chunk], model=model, retries=retries)
        if mechanicals is None:
            chunks_failed += 1
            handbacks_skipped += len(chunk)
            continue

        cases.extend(
            BacktestCase(
                klass=j.klass,
                would_send=j.klass in AUTO_SEND_CLASSES,
                human_was_mechanical=mech,
                tail=h.tail,
                reply=h.reply,
            )
            for h, j, mech in zip(chunk, judgments, mechanicals)
        )

    if stats is not None:
        stats["chunks"] = chunks_total
        stats["chunks_failed"] = chunks_failed
        stats["handbacks_skipped"] = handbacks_skipped

    return cases


def score(cases: list[BacktestCase]) -> dict:
    """Precision per class, plus overall precision/recall.

    A case counts toward a class's `tp`/`fp` only when `would_send` is True
    (only auto-send decisions can be right or wrong about being sent).
    Overall `recall` is `tp / (number of cases whose human_was_mechanical is
    True)` — the fraction of genuinely safe moments the classifier actually
    caught. Both divisions are guarded; empty input returns zeros rather than
    raising.
    """
    per_class: dict[str, dict] = {}
    overall_tp = 0
    overall_fp = 0
    mechanical_total = 0

    for case in cases:
        bucket = per_class.setdefault(case.klass, {"tp": 0, "fp": 0, "n": 0})
        bucket["n"] += 1
        if case.human_was_mechanical:
            mechanical_total += 1
        if case.would_send:
            if case.human_was_mechanical:
                bucket["tp"] += 1
                overall_tp += 1
            else:
                bucket["fp"] += 1
                overall_fp += 1

    for bucket in per_class.values():
        denom = bucket["tp"] + bucket["fp"]
        bucket["precision"] = bucket["tp"] / denom if denom else 0.0

    overall_denom = overall_tp + overall_fp
    overall = {
        "n": len(cases),
        "tp": overall_tp,
        "fp": overall_fp,
        "precision": overall_tp / overall_denom if overall_denom else 0.0,
        "recall": overall_tp / mechanical_total if mechanical_total else 0.0,
    }

    return {"per_class": per_class, "overall": overall}
