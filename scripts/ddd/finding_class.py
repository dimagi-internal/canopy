"""Accuracy vs strategy — the one classification that decides whether the DDD
loop can fix a finding by itself.

Why this module exists
----------------------
``fix_kind`` (mechanical | options | redesign) already tells the loop whether a
finding is auto-applicable, but the judge picks it from the *shape of its own
prose* ("does my recommendation contain the word 'or'?") and is explicitly told
"when in doubt, prefer ``options``".  In an ATTENDED run that bias is cheap — one
extra prompt.  In an UNATTENDED run it is the whole failure: a finding routed to a
human is a finding that is never fixed.

Measured case (ACE ``spark-facilitator/20260813-2126``, 4 iterations, ~2M tokens,
never passed).  The final two blockers were:

1. narration saying "cost" over a panel titled **FACILITATOR EARNINGS**;
2. an n=1 uncontrolled pre/post framed as a causal coaching arc.

Both were classified as concept decisions and escalated.  Neither needed a human.
The panel says what it says; the data supports what it supports.  In both cases
the *correct assertion is readable off the artifact*.

The discriminator
-----------------
Every finding is a mismatch between an ASSERTION (narration, scene title,
``concept_claim``) and an ARTIFACT (the rendered screen, the captured page text,
the underlying data).  The only question that matters is **which side has to
move**:

* ``accuracy`` — the ASSERTION is wrong relative to the artifact.  The artifact
  is the authority and it is right there, so the fix is determinate by
  construction: *restate the assertion at the strength the artifact supports.*
  Always autonomously fixable.  Never a human question.

* ``strategy`` — the ARTIFACT is wrong relative to the goal: wrong data, wrong
  scale, wrong story, wrong audience.  Fixing it means changing what is
  demonstrated, not what is said about it.  These are the findings a human
  genuinely owns.

* ``unclassified`` — no positive signal either way.  Deliberately NOT a default
  of "accuracy": this module only ever *grants* autonomy on evidence, it never
  assumes it.  An unclassified finding keeps whatever ``fix_kind`` the judge gave
  it, so behaviour is unchanged from before this module existed.

The load-bearing consequence, enforced in :func:`normalize_findings` and not in
prose: **``finding_class: accuracy`` implies ``fix_kind: mechanical``.**  A judge
that emitted ``options``/``redesign`` on an accuracy finding is overridden, and
the override is recorded on the finding so it is auditable.

Never the other direction: this module never downgrades a strategy finding into
something the loop may auto-apply.
"""
from __future__ import annotations

import re

ACCURACY = "accuracy"
STRATEGY = "strategy"
UNCLASSIFIED = "unclassified"

VALID_CLASSES = (ACCURACY, STRATEGY, UNCLASSIFIED)

# ---------------------------------------------------------------------------
# Dimension-level rules — these come straight from the rubric's own scope prose,
# so they are definitional, not heuristic.
# ---------------------------------------------------------------------------

# "Every narration claim is fully demonstrated in the captured footage" — the
# dimension IS the assertion-vs-artifact comparison.  Always accuracy.
_ACCURACY_DIMENSIONS = {
    "claim_reality_coherence",
    "why_groundedness",
}

# "Is the demonstrated USE substantive and load-bearing, or trivial?" — the
# dimension IS the question of whether the right thing is being shown at all.
# Always strategy.  (The rubric already routes it CONCEPT/redesign; this keeps
# that intact and makes it machine-checkable.)
_STRATEGY_DIMENSIONS = {
    "use_case_soundness",
}

# ---------------------------------------------------------------------------
# Phrase signals for the dimensions that can be either (concept_clarity,
# design_soundness, visual_polish, motion_friction).  Matched case-insensitively
# against detail + fix_recommendation.
# ---------------------------------------------------------------------------

# The assertion is wrong: a word, a number, a label, or a claim that the artifact
# itself contradicts or does not support.
_ASSERTION_SIDE = (
    r"narrat(?:ion|es|ed)\s+(?:says|claims|calls|describes|refers|uses|states)",
    r"\bsays\s+[\"'“]",
    r"\b(?:mis)?label(?:l)?ed\b",
    r"\btitled\b",
    r"\bheaded\b",
    r"\bheading\s+(?:says|reads)\b",
    r"\bdoes\s*n[o']?t\s+match\b",
    r"\bdoesn't\s+match\b",
    r"\bnot\s+visible\s+(?:anywhere\s+)?in\b",
    r"\bcontradicts?\b",
    r"\binconsistent\s+with\s+the\s+(?:screen|panel|page|label|data)\b",
    r"\bover(?:states?|claims?|sells?)\b",
    r"\bunsupported\s+by\b",
    r"\bexceeds?\s+(?:the\s+)?evidence\b",
    r"\bno\s+(?:control|baseline|comparison)\b",
    r"\bn\s*=\s*1\b",
    r"\bsingle\s+data\s+point\b",
    r"\bone\s+or\s+two\s+data\s+points\b",
    r"\bcausal\s+(?:claim|framing|language|arc)\b",
    r"\binaccurate\b",
    r"\bwrong\s+(?:word|term|number|figure|label|name)\b",
    r"\bterminology\s+mismatch\b",
)

# The artifact is wrong: the demo needs to show something else, or the idea needs
# rethinking.  These beat assertion-side signals when both appear.
_ARTIFACT_SIDE = (
    r"\bredraft\s+the\s+narrative\b",
    r"\bre-?conceive\b",
    r"\brethink\b",
    r"\bconsider\s+whether\b",
    r"\bmay\s+be\s+(?:the\s+)?wrong\b",
    r"\bis\s+this\s+the\s+right\b",
    r"\bdifferent\s+(?:demo|story|dataset|audience|use\s+case)\b",
    r"\bat\s+the\s+(?:scale|complexity)\s+where\b",
    r"\bas\s+easy\s+(?:or\s+easier\s+)?without\b",
    r"\bwould\s*n[o']?t\s+actually\s+need\b",
    r"\bshow\s+(?:a\s+)?(?:bigger|larger|fuller|richer)\b",
    r"\bgather\s+(?:more|additional)\s+data\b",
    r"\badd\s+a\s+(?:cohort|control|comparison)\s+(?:baseline|group|arm)\b",
    r"\bre-?record\b",
)

# A recommendation is DETERMINATE when it names exactly one concrete change.
# These are the judge's own "options" smell tests, reused here.
_NON_DETERMINATE = (
    r"\balternativ(?:e|ely)\b",
    r"\beither\b.*\bor\b",
    r"\bcould\s+also\b",
    r"\bconsider\s+\w+\s+or\b",
    r"\bor\s+(?:else|otherwise)\b",
    r"^\s*(?:should|would|could|is|are|do|does|can)\b.*\?\s*$",
)

# The canonical fix for an accuracy finding.  It is determinate BY CONSTRUCTION:
# whatever the artifact shows is the authority, so there is exactly one correct
# assertion and no choice to make.  Substituted only when the judge's own
# recommendation was non-determinate (the original is preserved alongside).
CANONICAL_ACCURACY_FIX = (
    "Restate the assertion at the strength the artifact supports: edit the "
    "scene's narration / title / concept_claim so it matches what is actually "
    "on screen and what the underlying data actually shows. Change the words, "
    "never the artifact — if the artifact itself needs to change, this is a "
    "strategy finding, not an accuracy one."
)


def _detail(finding: dict) -> str:
    return str(finding.get("detail") or "").lower()


def _recommendation(finding: dict) -> str:
    return str(finding.get("fix_recommendation") or "").lower()


def _matches(patterns: tuple[str, ...], text: str) -> str | None:
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
        if m:
            return pat
    return None


def is_determinate(fix_recommendation: str | None) -> bool:
    """True when the recommendation names exactly ONE concrete change.

    Mirrors the rubric's own ``options`` smell tests. An empty/absent
    recommendation is not determinate — there is nothing to apply.
    """
    text = (fix_recommendation or "").strip()
    if not text:
        return False
    return _matches(_NON_DETERMINATE, text) is None


def classify(finding: dict) -> tuple[str, str]:
    """Return ``(finding_class, reason)`` for one design finding.

    Precedence, most authoritative first:

    1. an explicit, valid ``finding_class`` already on the finding (a judge or a
       human said so — believe them);
    2. the dimension, when the rubric's own scope makes it definitional;
    3. the DEFECT as described in ``detail`` — artifact-side first, then
       assertion-side;
    4. the judge's ``fix_recommendation``, same order;
    5. ``unclassified``.

    Detail before recommendation is load-bearing. ``detail`` describes the DEFECT;
    ``fix_recommendation`` is only one proposed remedy, and the judge is
    explicitly biased toward escalation ("when in doubt, prefer options"). A judge
    that writes "the narration says cost but the panel says EARNINGS" in the
    detail and "rethink the framing" in the recommendation has found an accuracy
    defect and then flinched. Classifying off the recommendation would take the
    flinch as the answer — which is exactly the escalation this module exists to
    stop. Whenever the assertion is wrong relative to the artifact there is always
    a determinate assertion-side fix available, whatever else the judge suggests.
    """
    explicit = finding.get("finding_class")
    if isinstance(explicit, str) and explicit.lower() in VALID_CLASSES:
        return explicit.lower(), "explicit finding_class on the finding"

    dim = str(finding.get("dimension") or "").strip().lower()
    if dim in _ACCURACY_DIMENSIONS:
        return ACCURACY, f"dimension {dim!r} compares an assertion against the artifact by definition"
    if dim in _STRATEGY_DIMENSIONS:
        return STRATEGY, f"dimension {dim!r} asks whether the right thing is demonstrated at all"

    for where, text in (("detail", _detail(finding)), ("fix_recommendation", _recommendation(finding))):
        if not text:
            continue
        hit = _matches(_ARTIFACT_SIDE, text)
        if hit:
            return STRATEGY, f"artifact-side signal {hit!r} in {where} — the fix changes what is demonstrated"
        hit = _matches(_ASSERTION_SIDE, text)
        if hit:
            return ACCURACY, f"assertion-side signal {hit!r} in {where} — the fix changes what is said"

    return UNCLASSIFIED, "no positive accuracy or strategy signal — fix_kind left as the judge set it"


def normalize_findings(findings: list[dict]) -> list[dict]:
    """Stamp ``finding_class`` on every finding and enforce the accuracy rule.

    Returns NEW dicts; the input list is not mutated.

    For every finding classified ``accuracy``:

    * ``fix_kind`` becomes ``mechanical`` (recording ``fix_kind_override`` when
      it changed), because the correct assertion is readable off the artifact —
      there is nothing for a human to decide;
    * a non-determinate ``fix_recommendation`` is replaced with
      :data:`CANONICAL_ACCURACY_FIX` (the original preserved as
      ``fix_recommendation_original``), because "restate the claim at the
      strength the evidence supports" is a single concrete change however the
      judge happened to phrase its own suggestion.

    ``strategy`` and ``unclassified`` findings keep their ``fix_kind`` untouched.
    This function only ever grants autonomy where the artifact is the authority;
    it never takes a human decision away.
    """
    out: list[dict] = []
    for raw in findings or []:
        f = dict(raw)
        cls, reason = classify(f)
        f["finding_class"] = cls
        f["finding_class_reason"] = reason

        if cls == ACCURACY:
            old_kind = f.get("fix_kind")
            if old_kind != "mechanical":
                f["fix_kind"] = "mechanical"
                f["fix_kind_override"] = {
                    "from": old_kind,
                    "to": "mechanical",
                    "why": (
                        "accuracy finding — the artifact is the authority and the "
                        "correct assertion is readable off it, so the fix is "
                        "determinate and needs no human pick"
                    ),
                }
            if not is_determinate(f.get("fix_recommendation")):
                original = f.get("fix_recommendation")
                if original:
                    f["fix_recommendation_original"] = original
                f["fix_recommendation"] = CANONICAL_ACCURACY_FIX
        out.append(f)
    return out


def count_by_class(findings: list[dict]) -> dict[str, int]:
    """``{accuracy: n, strategy: n, unclassified: n}`` over already-normalized findings."""
    counts = {ACCURACY: 0, STRATEGY: 0, UNCLASSIFIED: 0}
    for f in findings or []:
        cls, _ = classify(f)
        counts[cls] = counts.get(cls, 0) + 1
    return counts


__all__ = [
    "ACCURACY",
    "CANONICAL_ACCURACY_FIX",
    "STRATEGY",
    "UNCLASSIFIED",
    "VALID_CLASSES",
    "classify",
    "count_by_class",
    "is_determinate",
    "normalize_findings",
]
