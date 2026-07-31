"""Why an AI coding agent stopped — judged by an LLM, not pattern-matched.

An earlier version of this module used regex/keyword lists to guess at meaning:
"done", "next I'll", "?" and the like. It shipped and was reverted the same day
it met `"no, we're not good to close out yet, the AWS deploy is still broken"` —
a negation the lexicon read as "keep going", because the words "good to close
out" and "still" are exactly the vocabulary a continuation-detector is tuned to
reward. Meaning does not factor into keyword hits. There is no regex fix for
this class of bug; the fix is to stop pattern-matching and ask a model to read
the sentence. The one regex in this file (`parse_batch_json`) is not an
exception to that rule — it extracts a JSON array from the model's OWN output,
which is parsing, not judgment.

The module exposes two independent passes, and they must stay independent:
`build_classify_prompt` sees only the agent's final message (the "tail");
`build_reply_prompt` sees only the human's reply. In production, the
classifier that decides whether a stalled session is safe to auto-continue
runs BEFORE any human has replied — there is no reply yet to look at. A
classifier prompt that peeked at the reply during scoring would therefore be
measuring a classifier that cannot exist at decision time. Two functions, two
call sites, disjoint inputs, enforced by their signatures.

Fail loud, not soft: unlike `fleet_align.judge`, which has deterministic
findings to fall back on and so swallows LLM errors, this module has no
fallback verdict. A parse failure, a count mismatch, or an unknown class is
raised, never defaulted — a silently-defaulted classification would fabricate
the very measurement the rest of the stall pipeline is gated on.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass

# ── classes ─────────────────────────────────────────────────────────────────
# The seven things a stopped agent might be waiting on. Values equal their
# lowercase names so they read the same in code, prompts, and JSON.
AWAITING_CONTINUE = "awaiting_continue"
PLAN_PENDING = "plan_pending"
DONE_CLAIMED = "done_claimed"
QUESTION_OPEN = "question_open"
BLOCKED_HUMAN = "blocked_human"
GATE_OUTBOUND = "gate_outbound"
ERRORED = "errored"

_ALL_CLASSES = frozenset({
    AWAITING_CONTINUE, PLAN_PENDING, DONE_CLAIMED,
    QUESTION_OPEN, BLOCKED_HUMAN, GATE_OUTBOUND, ERRORED,
})

# The only classes where an automated "keep going" nudge is safe. Everything
# else means a human's judgment is the point, not an obstacle to route around.
AUTO_SEND_CLASSES: frozenset[str] = frozenset(
    {AWAITING_CONTINUE, PLAN_PENDING, DONE_CLAIMED})


@dataclass(frozen=True)
class Judgment:
    klass: str
    confidence: float
    reason: str


# ── the model call ──────────────────────────────────────────────────────────

def run_claude(prompt: str, model: str = "haiku", timeout: int = 300) -> str:
    """Invoke `claude -p` and return its stdout. Raises on a non-zero exit.

    Mirrors `fleet_align._run_claude` in shape. No import-time subprocess
    call — this only runs when a caller actually invokes it (or, in tests,
    is swapped out entirely via `runner=`).
    """
    proc = subprocess.run(
        ["claude", "-p", prompt, "--model", model, "--no-session-persistence"],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:200] or "claude -p failed")
    return proc.stdout


# ── prompts ─────────────────────────────────────────────────────────────────

def build_classify_prompt(tails: list[str]) -> str:
    """Batched prompt: classify what a stopped agent is waiting for.

    Sees ONLY the agent's final message per item — never a human reply. See
    the module docstring for why that separation is load-bearing.
    """
    items = [{"index": i, "text": t} for i, t in enumerate(tails)]
    return (
        "You are judging why an AI coding agent stopped and is now waiting. "
        "Each item below is the FINAL message that agent left in its "
        "transcript before it went silent. Decide, for each item, what the "
        "agent is now waiting for. Classify into exactly one of:\n\n"
        f"- {AWAITING_CONTINUE} — states what it did and/or what it will do "
        "next; asks the human nothing.\n"
        f"- {PLAN_PENDING} — lays out a plan or proposal and waits for a "
        "go-ahead.\n"
        f"- {DONE_CLAIMED} — claims the work is finished.\n"
        f"- {QUESTION_OPEN} — asks the human a real question with genuine "
        "alternatives.\n"
        f"- {BLOCKED_HUMAN} — needs something only a human can do (a login, "
        "a credential, a click).\n"
        f"- {GATE_OUTBOUND} — waits for approval to send/publish something "
        "outside the system (email, post, external message).\n"
        f"- {ERRORED} — hit a rate limit, crashed, or otherwise could not "
        "proceed.\n\n"
        f"When torn between {AWAITING_CONTINUE} and any of {QUESTION_OPEN}, "
        f"{BLOCKED_HUMAN}, or {GATE_OUTBOUND}, choose the latter — a wrong "
        "auto-continue costs more than a missed one.\n\n"
        "Return ONLY a JSON array, one object per item, of the form "
        '{"index": i, "class": <one of the classes above>, '
        '"confidence": <0-1>, "reason": <≤20 words>}. No prose, no markdown '
        "fences, just the array.\n\n"
        f"ITEMS:\n{json.dumps(items, indent=2)}\n"
    )


def build_reply_prompt(replies: list[str]) -> str:
    """Batched prompt: is a human's reply mechanical or substantive?

    Sees ONLY the human replies — never the agent tail that prompted them.
    See the module docstring for why.
    """
    items = [{"index": i, "text": r} for i, r in enumerate(replies)]
    return (
        "You are judging human replies sent to a stalled AI coding agent. "
        "For each reply, decide: did it carry information the agent did not "
        "already have, or was the human just restarting a stalled agent?\n\n"
        "MECHANICAL = pure continuation/approval/status-check, e.g. "
        '"keep going", "yes", "do the plan", "good to close out?".\n'
        "SUBSTANTIVE = anything adding direction, correction, disagreement, "
        "a new constraint, or a question.\n\n"
        "Two rules to apply strictly:\n"
        '- A reply that begins with a continuation cue but then adds '
        'direction ("keep going but focus on X", "merge it, then also '
        'update the release notes") is SUBSTANTIVE.\n'
        '- A negation ("no, we\'re not good to close out yet") is '
        "SUBSTANTIVE.\n\n"
        "When genuinely unsure, answer false — over-calling mechanical "
        "inflates the case for auto-nudging, which is the dangerous "
        "direction.\n\n"
        "Return ONLY a JSON array, one object per item, of the form "
        '{"index": i, "mechanical": <true|false>}. No prose, no markdown '
        "fences, just the array.\n\n"
        f"ITEMS:\n{json.dumps(items, indent=2)}\n"
    )


# ── parsing the model's own output ──────────────────────────────────────────

def parse_batch_json(raw: str, expected: int) -> list[dict]:
    """Extract the outermost JSON array from `raw` and validate its length.

    This regex operates on the model's OWN output, extracting structure from
    text it produced — parsing, not judgment. It is the one place this
    module uses a regex.
    """
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        raise ValueError(f"no JSON array found in model output: {raw[:200]!r}")
    try:
        result = json.loads(m.group(0))
    except ValueError as e:
        raise ValueError(f"could not parse JSON array from model output: {e}") from e
    if not isinstance(result, list):
        raise ValueError(f"parsed JSON is not a list: {result!r}")
    if len(result) != expected:
        raise ValueError(
            f"expected {expected} items in model output, got {len(result)}")
    return result


# ── batched calls ────────────────────────────────────────────────────────────

def _index_items(items: list[dict], count: int) -> dict[int, dict]:
    """Validate and re-key a batch of `{"index": i, ...}` items by index.

    Shared by `classify_tails` and `judge_replies`: both need the same three
    checks (in-range integer index, no duplicates, no gaps) before they can
    trust `by_index[i]` to mean item `i`. Raises `ValueError` on any
    violation — there is no fallback verdict for a malformed response.
    """
    by_index: dict[int, dict] = {}
    for item in items:
        idx = item.get("index")
        if not isinstance(idx, int) or not (0 <= idx < count):
            raise ValueError(f"invalid or out-of-range index in model output: {idx!r}")
        if idx in by_index:
            raise ValueError(f"duplicate index {idx} in model output")
        by_index[idx] = item

    missing = set(range(count)) - by_index.keys()
    if missing:
        raise ValueError(f"missing index/indices in model output: {sorted(missing)}")
    return by_index


def classify_tails(tails: list[str], *, runner=run_claude, model: str = "haiku",
                    ) -> list[Judgment]:
    """Classify each agent tail. Fails loud — no default verdict on error."""
    if not tails:
        return []
    raw = runner(build_classify_prompt(tails), model)
    items = parse_batch_json(raw, len(tails))
    by_index = _index_items(items, len(tails))

    out: list[Judgment] = []
    for i in range(len(tails)):
        item = by_index[i]
        klass = item.get("class")
        if klass not in _ALL_CLASSES:
            raise ValueError(f"unknown class in model output: {klass!r}")
        out.append(Judgment(
            klass=klass,
            confidence=float(item.get("confidence", 0.0)),
            reason=str(item.get("reason", "")),
        ))
    return out


def judge_replies(replies: list[str], *, runner=run_claude, model: str = "haiku",
                   ) -> list[bool]:
    """Judge each human reply as mechanical (True) or substantive (False)."""
    if not replies:
        return []
    raw = runner(build_reply_prompt(replies), model)
    items = parse_batch_json(raw, len(replies))
    by_index = _index_items(items, len(replies))

    out: list[bool] = []
    for i in range(len(replies)):
        item = by_index[i]
        mech = item.get("mechanical")
        if not isinstance(mech, bool):
            raise ValueError(f"non-boolean 'mechanical' value in model output: {mech!r}")
        out.append(mech)
    return out
