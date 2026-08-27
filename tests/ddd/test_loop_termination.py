"""The DDD loop owns its own termination.

Two regressions locked in here:

1. **An accuracy finding must not open the concept gate.** ACE
   ``spark-facilitator/20260813-2126`` escalated "narration says cost over a panel
   titled FACILITATOR EARNINGS" and "an n=1 pre/post framed as causal" as product
   decisions. Both are fixable from the artifact; neither needed a human.
2. **The loop must say WHICH kind of ending this is.** An orchestrator inventing
   "hard stop after this pass" is the symptom of a loop with no stopping rule.
3. **A strategy finding must not preempt pending mechanical work.** ACE
   ``spark-facilitator/20260820-0817`` produced one strategy redesign alongside
   five accuracy findings on iteration 0. The accuracy findings were correctly
   normalized to mechanical — and ``stop_concept_change`` fired anyway, so none
   of them were ever applied. The run ended ``stopped_not_converged`` with a
   ``score_history`` of length 1, and its hero video filmed an artifact carrying
   five defects nobody disputed. The concept gate buys a human's taste judgment
   on DIRECTION; spending it over a knowingly-wrong artifact wastes it.
"""
from __future__ import annotations

from scripts.ddd.run_pipeline import classify_termination, compute_auto_iterate
from scripts.ddd.schemas.models import RunState, Verdict


def _v(score: float, verdict_str: str = "pass") -> Verdict:
    return Verdict(
        schema_version=1,
        kind="concept",
        gate="gating",
        rubric_name="ddd-concept-eval",
        ran_at="2026-08-14T00:00:00Z",
        dimensions={},
        overall_score=score,
        overall_rule="lowest",
        verdict=verdict_str,
    )


def _state(**kw) -> RunState:
    return RunState(run_id="run-1", narrative_slug="spark-facilitator", **kw)


ACCURACY_BLOCKER = {
    "scene": "9",
    "dimension": "concept_clarity",
    "route": "CONCEPT",
    "fix_kind": "redesign",
    "detail": 'The narration says "cost" but the panel is titled FACILITATOR EARNINGS.',
    "fix_recommendation": "Rethink the framing of the scene.",
}
STRATEGY_BLOCKER = {
    "scene": "4",
    "dimension": "use_case_soundness",
    "route": "CONCEPT",
    "fix_kind": "redesign",
    "detail": "The coach runs on a one-line answer.",
    "fix_recommendation": "Redraft the narrative so the coach runs on a full application.",
}


class TestAccuracyNeverEscalates:
    def test_an_accuracy_finding_does_not_open_the_concept_gate(self) -> None:
        state = _state()
        action, reason = compute_auto_iterate(
            state, _v(2.0, "fail"), _v(4.0), [ACCURACY_BLOCKER], unattended=True
        )
        assert action == "continue", reason
        assert state.findings[0]["fix_kind"] == "mechanical"
        assert state.findings[0]["finding_class"] == "accuracy"

    def test_a_strategy_finding_still_opens_the_concept_gate(self) -> None:
        state = _state()
        action, _ = compute_auto_iterate(
            state, _v(2.0, "fail"), _v(4.0), [STRATEGY_BLOCKER], unattended=True
        )
        assert action == "stop_concept_change"

    def test_mixed_findings_fix_the_accuracy_one_first(self) -> None:
        """A confident fix must never sit behind an uncertain one."""
        state = _state()
        action, _ = compute_auto_iterate(
            state,
            _v(2.0, "fail"),
            _v(4.0),
            [ACCURACY_BLOCKER, {**STRATEGY_BLOCKER, "fix_kind": "options"}],
            unattended=True,
        )
        assert action == "continue"

    def test_unattended_is_reported_in_the_reason(self) -> None:
        state = _state()
        _, reason = compute_auto_iterate(
            state, _v(2.0, "fail"), _v(4.0), [STRATEGY_BLOCKER], unattended=True
        )
        assert "reported, not waited on" in reason


class TestPlateauDetection:
    def _fp_findings(self) -> list[dict]:
        return [
            {
                "scene": "3",
                "dimension": "visual_polish",
                "route": "PRODUCT",
                "fix_kind": "mechanical",
                "detail": "Spacing is uneven in the header row.",
                "fix_recommendation": "Set the header row gap to 12px.",
            }
        ]

    def test_identical_findings_two_rounds_running_stop_the_loop(self) -> None:
        state = _state()
        f = self._fp_findings()
        a1, _ = compute_auto_iterate(state, _v(3.0, "warn"), _v(4.0), f, unattended=True)
        assert a1 == "continue"
        a2, reason = compute_auto_iterate(state, _v(3.0, "warn"), _v(4.0), f, unattended=True)
        assert a2 == "stop_max_iter"
        assert "plateau" in reason.lower()

    def test_changing_findings_keep_the_loop_going(self) -> None:
        state = _state()
        compute_auto_iterate(state, _v(3.0, "warn"), _v(4.0), self._fp_findings(), unattended=True)
        moved = [{**self._fp_findings()[0], "detail": "A different defect entirely."}]
        action, _ = compute_auto_iterate(state, _v(3.5, "warn"), _v(4.0), moved, unattended=True)
        assert action == "continue"

    def test_plateau_signal_survives_a_score_wobble(self) -> None:
        """The score wobbles +/-1 on identical frames; the defect it names does not."""
        state = _state()
        f = self._fp_findings()
        compute_auto_iterate(state, _v(3.0, "warn"), _v(4.0), f, unattended=True)
        action, _ = compute_auto_iterate(state, _v(2.0, "fail"), _v(4.0), f, unattended=True)
        assert action == "stop_max_iter"


class TestNoiseBandStall:
    def test_a_sub_band_wobble_is_a_stall_not_progress(self) -> None:
        state = _state(score_history=[3.4, 3.45])
        action, reason = compute_auto_iterate(
            state, _v(3.42, "warn"), _v(4.0), [], unattended=True
        )
        assert action == "stop_max_iter"
        assert "noise band" in reason

    def test_a_real_climb_is_not_a_stall(self) -> None:
        state = _state(score_history=[2.0, 3.0])
        action, _ = compute_auto_iterate(state, _v(4.0), _v(4.0), [], unattended=True)
        assert action == "stop_done"


class TestTerminationStatus:
    def test_converged_clean(self) -> None:
        state = _state()
        compute_auto_iterate(state, _v(4.5), _v(4.5), [], unattended=True)
        assert state.terminal_status == "converged_clean"

    def test_converged_with_open_questions(self) -> None:
        state = _state()
        compute_auto_iterate(
            state, _v(4.5), _v(4.5), [{**STRATEGY_BLOCKER, "route": "CONCEPT"}], unattended=True
        )
        assert state.terminal_status == "converged_with_open_questions"

    def test_stopped_not_converged_is_distinct_from_converged(self) -> None:
        state = _state(score_history=[2.0, 2.0])
        compute_auto_iterate(state, _v(2.0, "fail"), _v(2.0, "fail"), [], unattended=True)
        assert state.terminal_status == "stopped_not_converged"

    def test_diverging_is_distinct_from_stopped(self) -> None:
        out = classify_termination(
            "stop_max_iter", converged=False, score_history=[4.0, 3.0, 2.0], findings=[]
        )
        assert out["status"] == "diverging"
        assert out["trend"] == "regressing"

    def test_running_is_not_terminal(self) -> None:
        out = classify_termination("continue", converged=False, score_history=[2.0, 3.0], findings=[])
        assert out["status"] == "running"
        assert out["terminal"] is False

    def test_every_status_has_a_human_summary(self) -> None:
        for action, converged in (
            ("stop_done", True),
            ("stop_max_iter", False),
            ("continue", False),
        ):
            out = classify_termination(action, converged=converged, score_history=[3.0, 3.0], findings=[])
            assert out["summary"]
            assert out["status"] in {
                "converged_clean",
                "converged_with_open_questions",
                "stopped_not_converged",
                "diverging",
                "running",
            }


class TestConceptGateWaitsForMechanicalWork:
    """A confident fix must never sit behind an uncertain one — redesign included.

    The gate is deferred EXACTLY ONCE. It buys a human's judgment on direction,
    and it must not be starved by a mechanical backlog that keeps regenerating.
    """

    MECHANICAL = {
        "scene": "5",
        "dimension": "claim_reality_coherence",
        "route": "PRODUCT",
        "fix_kind": "mechanical",
        "detail": "The drill prints the capped [1,2,3,3] field, not the true ordinal.",
        "fix_recommendation": "Bind the column to the uncapped ordinal.",
    }

    def test_a_strategy_finding_does_not_preempt_pending_mechanical_fixes(self) -> None:
        """The live failure: five fixable defects were skipped for one taste question."""
        state = _state()
        action, reason = compute_auto_iterate(
            state, _v(2.0, "fail"), _v(2.0, "fail"), [self.MECHANICAL, STRATEGY_BLOCKER],
            unattended=True,
        )
        assert action == "continue", reason
        assert state.concept_gate_deferred == 1

    def test_the_gate_is_deferred_once_not_indefinitely(self) -> None:
        """Second pass with the strategy finding still standing: the gate opens."""
        state = _state()
        findings = [self.MECHANICAL, STRATEGY_BLOCKER]
        a1, _ = compute_auto_iterate(
            state, _v(2.0, "fail"), _v(2.0, "fail"), findings, unattended=True
        )
        assert a1 == "continue"
        moved = [{**self.MECHANICAL, "detail": "A different mechanical defect."}]
        a2, reason = compute_auto_iterate(
            state, _v(3.0, "warn"), _v(3.0, "warn"), moved + [STRATEGY_BLOCKER],
            unattended=True,
        )
        assert a2 == "stop_concept_change", reason
        assert state.concept_gate_deferred == 1

    def test_a_strategy_finding_alone_still_stops_immediately(self) -> None:
        """Regression guard: nothing mechanical pending means nothing to wait for."""
        state = _state()
        action, _ = compute_auto_iterate(
            state, _v(2.0, "fail"), _v(4.0), [STRATEGY_BLOCKER], unattended=True
        )
        assert action == "stop_concept_change"
        assert state.concept_gate_deferred == 0

    def test_a_plateau_still_suppresses_the_deferral(self) -> None:
        """Re-applying the same mechanical fix is not progress worth the gate's wait."""
        state = _state()
        findings = [self.MECHANICAL, STRATEGY_BLOCKER]
        state.finding_fingerprints = []
        a1, _ = compute_auto_iterate(
            state, _v(3.0, "warn"), _v(3.0, "warn"), findings, unattended=True
        )
        assert a1 == "continue"
        state.concept_gate_deferred = 0  # isolate the plateau from the deferral bound
        a2, _ = compute_auto_iterate(
            state, _v(3.0, "warn"), _v(3.0, "warn"), findings, unattended=True
        )
        assert a2 == "stop_concept_change"
        assert state.concept_gate_deferred == 0
