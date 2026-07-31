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
    `text` blocks (tool results); empty/whitespace text; and any text that
    starts with a harness-injection marker (`_HARNESS_PREFIXES`) — those are
    structural noise the harness emits, not something a person typed, and
    counting them as replies would inflate every number downstream.
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

    For each record where the agent handed back to a human
    (`hands_back_to_human`), scan forward for the first record yielding a
    non-None `human_text`. If none is found before the transcript ends, this
    handback is skipped — there is no answer key for it yet.
    """
    out: list[Handback] = []
    for i, rec in enumerate(records):
        if not hands_back_to_human(rec):
            continue
        for later in records[i + 1:]:
            reply = human_text(later)
            if reply is not None:
                out.append(Handback(tail=_tail_text(rec), reply=reply))
                break
    return out


def grade(handbacks: list[Handback], *, classify=classify_tails,
          judge=judge_replies, model: str = "haiku") -> list[BacktestCase]:
    """Grade each handback with two independent model calls.

    `classify` sees only tails; `judge` sees only replies. Neither call may
    see the other's input — that separation is what makes the resulting
    score a measurement of the classifier that can actually run in
    production. See the module docstring.
    """
    if not handbacks:
        return []

    judgments = classify([h.tail for h in handbacks], model=model)
    mechanicals = judge([h.reply for h in handbacks], model=model)

    return [
        BacktestCase(
            klass=j.klass,
            would_send=j.klass in AUTO_SEND_CLASSES,
            human_was_mechanical=mech,
            tail=h.tail,
            reply=h.reply,
        )
        for h, j, mech in zip(handbacks, judgments, mechanicals)
    ]


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
