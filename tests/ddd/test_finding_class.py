"""An accuracy finding must never reach a human gate.

Regression for ACE ``spark-facilitator/20260813-2126``: four iterations, ~2M
subagent tokens, never passed, and the final two blockers were escalated to the
operator as product decisions when both were ordinary accuracy defects —
narration saying "cost" over a panel titled FACILITATOR EARNINGS, and an n=1
uncontrolled pre/post framed as a causal coaching arc.
"""
from __future__ import annotations

from scripts.ddd.finding_class import (
    ACCURACY,
    CANONICAL_ACCURACY_FIX,
    STRATEGY,
    UNCLASSIFIED,
    classify,
    count_by_class,
    is_determinate,
    normalize_findings,
)

# The two findings that actually stopped the run.
COST_VS_EARNINGS = {
    "scene": "9",
    "dimension": "concept_clarity",
    "route": "CONCEPT",
    "fix_kind": "redesign",
    "detail": 'The narration says "cost" but the panel on screen is titled FACILITATOR EARNINGS.',
    "fix_recommendation": "Rethink whether the scene should be about cost or earnings.",
}

N_OF_ONE_CAUSAL_ARC = {
    "scene": "11",
    "dimension": "claim_reality_coherence",
    "route": "CONCEPT",
    "fix_kind": "options",
    "detail": (
        "The scene frames a causal coaching arc, but the underlying data is an n=1 "
        "uncontrolled pre/post with no baseline."
    ),
    "fix_recommendation": (
        "Either drop the causal framing, or add a cohort baseline so the comparison is real."
    ),
}

# A genuine strategy finding — the artifact, not the wording, is wrong.
THIN_USE_CASE = {
    "scene": "4",
    "dimension": "use_case_soundness",
    "route": "CONCEPT",
    "fix_kind": "redesign",
    "detail": "The AI coach is invoked on a one-line answer.",
    "fix_recommendation": "Redraft the narrative so the coach runs on a full multi-question application.",
}


class TestClassify:
    def test_narration_vs_on_screen_label_is_accuracy(self) -> None:
        cls, reason = classify(COST_VS_EARNINGS)
        assert cls == ACCURACY, reason

    def test_claim_exceeding_evidence_is_accuracy(self) -> None:
        cls, reason = classify(N_OF_ONE_CAUSAL_ARC)
        assert cls == ACCURACY, reason

    def test_use_case_soundness_is_always_strategy(self) -> None:
        cls, _ = classify(THIN_USE_CASE)
        assert cls == STRATEGY

    def test_claim_reality_coherence_is_definitionally_accuracy(self) -> None:
        cls, _ = classify({"dimension": "claim_reality_coherence", "detail": "", "fix_recommendation": ""})
        assert cls == ACCURACY

    def test_why_groundedness_is_definitionally_accuracy(self) -> None:
        cls, _ = classify({"dimension": "why_groundedness", "detail": "", "fix_recommendation": ""})
        assert cls == ACCURACY

    def test_an_artifact_side_defect_is_strategy(self) -> None:
        cls, reason = classify(
            {
                "dimension": "design_soundness",
                "detail": "The demo never shows the feature at the scale where its value would be felt.",
                "fix_recommendation": "Show a bigger cohort so the claim is supportable.",
            }
        )
        assert cls == STRATEGY, reason

    def test_the_detail_decides_not_the_judges_flinch(self) -> None:
        """A judge that FINDS an accuracy defect and then says 'rethink it' has flinched.

        Classifying off the recommendation would take the flinch as the answer —
        which is exactly the escalation this module exists to stop. This is the
        literal shape of the first spark-facilitator blocker.
        """
        cls, reason = classify(
            {
                "dimension": "concept_clarity",
                "detail": 'The narration says "cost" but the panel is titled FACILITATOR EARNINGS.',
                "fix_recommendation": "Rethink whether the scene should be about cost or earnings.",
            }
        )
        assert cls == ACCURACY, reason

    def test_an_artifact_side_detail_still_wins_over_an_assertion_word(self) -> None:
        cls, reason = classify(
            {
                "dimension": "design_soundness",
                "detail": "The narration overstates it, and the task shown would be as easy without the feature.",
                "fix_recommendation": "Redraft the narrative around a task that needs it.",
            }
        )
        assert cls == STRATEGY, reason

    def test_no_signal_is_unclassified_not_accuracy(self) -> None:
        """This module GRANTS autonomy on evidence; it never assumes it."""
        cls, _ = classify(
            {"dimension": "visual_polish", "detail": "Spacing is uneven.", "fix_recommendation": "Tighten it."}
        )
        assert cls == UNCLASSIFIED

    def test_explicit_class_wins(self) -> None:
        cls, reason = classify({**THIN_USE_CASE, "finding_class": "accuracy"})
        assert cls == ACCURACY
        assert "explicit" in reason


class TestNormalizeEnforcesTheRule:
    def test_accuracy_finding_is_forced_mechanical(self) -> None:
        out = normalize_findings([COST_VS_EARNINGS, N_OF_ONE_CAUSAL_ARC])
        assert [f["fix_kind"] for f in out] == ["mechanical", "mechanical"]
        assert all(f["finding_class"] == ACCURACY for f in out)

    def test_override_is_recorded_and_auditable(self) -> None:
        out = normalize_findings([COST_VS_EARNINGS])[0]
        assert out["fix_kind_override"]["from"] == "redesign"
        assert out["fix_kind_override"]["to"] == "mechanical"
        assert out["fix_kind_override"]["why"]

    def test_non_determinate_accuracy_fix_gets_the_canonical_one(self) -> None:
        """'Either X or Y' is not a choice when the artifact is the authority."""
        out = normalize_findings([N_OF_ONE_CAUSAL_ARC])[0]
        assert out["fix_recommendation"] == CANONICAL_ACCURACY_FIX
        assert out["fix_recommendation_original"] == N_OF_ONE_CAUSAL_ARC["fix_recommendation"]

    def test_determinate_accuracy_fix_is_left_alone(self) -> None:
        f = {
            "dimension": "claim_reality_coherence",
            "fix_kind": "options",
            "detail": "narration says cost",
            "fix_recommendation": 'Change scene 9 narration from "cost" to "earnings".',
        }
        out = normalize_findings([f])[0]
        assert out["fix_recommendation"] == f["fix_recommendation"]
        assert "fix_recommendation_original" not in out

    def test_strategy_finding_is_never_downgraded(self) -> None:
        out = normalize_findings([THIN_USE_CASE])[0]
        assert out["fix_kind"] == "redesign"
        assert out["finding_class"] == STRATEGY
        assert "fix_kind_override" not in out

    def test_unclassified_keeps_its_fix_kind(self) -> None:
        f = {"dimension": "visual_polish", "fix_kind": "options", "detail": "x", "fix_recommendation": "y"}
        out = normalize_findings([f])[0]
        assert out["fix_kind"] == "options"

    def test_input_is_not_mutated(self) -> None:
        src = dict(COST_VS_EARNINGS)
        normalize_findings([src])
        assert src == COST_VS_EARNINGS

    def test_empty_is_safe(self) -> None:
        assert normalize_findings([]) == []
        assert normalize_findings(None) == []


class TestDeterminacy:
    def test_alternatives_are_not_determinate(self) -> None:
        assert is_determinate("Rename the field. Alternative: drop the scene.") is False
        assert is_determinate("Either tighten the narration or split the scene.") is False

    def test_a_question_is_not_determinate(self) -> None:
        assert is_determinate("Is this the right framing for the audience?") is False

    def test_empty_is_not_determinate(self) -> None:
        assert is_determinate("") is False
        assert is_determinate(None) is False

    def test_one_concrete_change_is_determinate(self) -> None:
        assert is_determinate('Rename the scene title to "Dana confirms each ward".') is True


def test_count_by_class() -> None:
    counts = count_by_class([COST_VS_EARNINGS, N_OF_ONE_CAUSAL_ARC, THIN_USE_CASE])
    assert counts[ACCURACY] == 2
    assert counts[STRATEGY] == 1
