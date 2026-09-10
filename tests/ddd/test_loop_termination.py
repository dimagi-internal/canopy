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

import pytest

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

    The gate is deferred WHILE THE ARTIFACT IS STILL GETTING CLEANER, and opens the
    moment it stops. "Clean" is a property of the artifact, not a count of passes
    (canopy#588): a count of 1 cut two consecutive clean runs off with 29 and 14
    mechanical fixes pending while the score was demonstrably still climbing
    (`[2.0] -> [2.0, 3.0]`, +1.0 on every judge from exactly the deferred work), and
    a crashed pass spent the same budget on nothing. So the bound is exhaustion:
    the first deferral is free; every further one must be paid for by the previous
    pass moving the score outside the noise band. A flat pass — whether it applied
    nothing or applied fixes that changed nothing — buys no more waiting, and a
    stall, a plateau, or the hard cap ends it regardless.
    """

    MECHANICAL = {
        "scene": "5",
        "dimension": "claim_reality_coherence",
        "route": "PRODUCT",
        "fix_kind": "mechanical",
        "detail": "The drill prints the capped [1,2,3,3] field, not the true ordinal.",
        "fix_recommendation": "Bind the column to the uncapped ordinal.",
    }

    @classmethod
    def _fresh_mechanical(cls, n: int) -> dict:
        """A mechanical finding with a distinct detail, so passes never plateau."""
        return {**cls.MECHANICAL, "detail": f"Mechanical defect #{n} in the drill."}

    def test_a_strategy_finding_does_not_preempt_pending_mechanical_fixes(self) -> None:
        """The live failure: five fixable defects were skipped for one taste question."""
        state = _state()
        action, reason = compute_auto_iterate(
            state, _v(2.0, "fail"), _v(2.0, "fail"), [self.MECHANICAL, STRATEGY_BLOCKER],
            unattended=True,
        )
        assert action == "continue", reason
        assert state.concept_gate_deferred == 1
        assert "first deferral" in reason.lower(), reason
        assert "/1" not in reason, reason  # no count-of-N bound is printed any more

    def test_the_gate_is_deferred_while_improving_and_opens_when_it_stalls(self) -> None:
        """The new invariant, end to end: [2.0] defer -> [2.0, 3.0] defer again (the
        pass paid for itself) -> [2.0, 3.0, 3.0] flat, the gate opens."""
        state = _state()
        a1, _ = compute_auto_iterate(
            state, _v(2.0, "fail"), _v(2.0, "fail"),
            [self._fresh_mechanical(1), STRATEGY_BLOCKER], unattended=True,
        )
        assert a1 == "continue"
        a2, r2 = compute_auto_iterate(
            state, _v(3.0, "warn"), _v(3.0, "warn"),
            [self._fresh_mechanical(2), STRATEGY_BLOCKER], unattended=True,
        )
        assert a2 == "continue", r2
        assert state.concept_gate_deferred == 2
        a3, r3 = compute_auto_iterate(
            state, _v(3.0, "warn"), _v(3.0, "warn"),
            [self._fresh_mechanical(3), STRATEGY_BLOCKER], unattended=True,
        )
        assert a3 == "stop_concept_change", r3
        assert state.concept_gate_deferred == 2  # opening the gate is not a deferral
        assert state.score_history == [2.0, 3.0, 3.0]

    def test_a_second_deferral_is_granted_when_the_score_really_climbed(self) -> None:
        """canopy#588 clean case: spark-fcap-facilitation-2026-09-09-002 stopped
        `stop_concept_change` at history=[2.0, 3.0] with 14 mechanical pending. A
        +1.0 move is outside the +/-0.5 noise band — the deferred pass paid for
        itself, so the gate keeps waiting."""
        state = _state()
        a1, _ = compute_auto_iterate(
            state, _v(2.0, "fail"), _v(2.0, "fail"),
            [self._fresh_mechanical(1), STRATEGY_BLOCKER], unattended=True,
        )
        assert a1 == "continue"
        a2, reason = compute_auto_iterate(
            state, _v(3.0, "warn"), _v(3.0, "warn"),
            [self._fresh_mechanical(2), STRATEGY_BLOCKER], unattended=True,
        )
        assert a2 == "continue", reason
        assert state.concept_gate_deferred == 2
        assert "2.0" in reason and "3.0" in reason, reason  # says WHY it was granted
        assert "/1" not in reason, reason

    @pytest.mark.parametrize("second", [2.0, 2.4])
    def test_a_second_deferral_is_refused_when_the_score_is_flat(self, second: float) -> None:
        """canopy#588 clean case, run -001: history=[2.0, 2.0]. Flat, or a move
        inside the noise band (2.4 - 2.0 < NOISE_BAND), is not evidence the fixes
        are cleaning anything — the gate opens with the strategy finding standing."""
        from scripts.ddd import denoise

        assert abs(second - 2.0) < denoise.NOISE_BAND or second == 2.0
        state = _state()
        a1, _ = compute_auto_iterate(
            state, _v(2.0, "fail"), _v(2.0, "fail"),
            [self._fresh_mechanical(1), STRATEGY_BLOCKER], unattended=True,
        )
        assert a1 == "continue"
        a2, reason = compute_auto_iterate(
            state, _v(second, "fail"), _v(second, "fail"),
            [self._fresh_mechanical(2), STRATEGY_BLOCKER], unattended=True,
        )
        assert a2 == "stop_concept_change", reason
        assert state.concept_gate_deferred == 1

    def test_a_crashed_deferred_pass_does_not_spend_the_budget(self) -> None:
        """canopy#588 crash case: hh-poverty-targeting-census-sweep-2026-09-01-001
        deferred (counter=1 persisted to disk), the pass was killed before applying
        anything, and the resumed pass found the gate already spent. Under the
        exhaustion rule the persisted counter is a record, not a budget: the
        resumed pass is judged on whether it moved the score."""
        # (a) the resumed pass applied nothing -> flat -> the gate opens, honestly.
        state = _state(score_history=[2.0], concept_gate_deferred=1)
        a, reason = compute_auto_iterate(
            state, _v(2.0, "fail"), _v(2.0, "fail"),
            [self._fresh_mechanical(1), STRATEGY_BLOCKER], unattended=True,
        )
        assert a == "stop_concept_change", reason
        assert state.concept_gate_deferred == 1
        # (b) the resumed pass applied the fixes and the score climbed -> the
        # deferral continues, exactly as if the crash had never happened.
        state = _state(score_history=[2.0], concept_gate_deferred=1)
        a, reason = compute_auto_iterate(
            state, _v(3.0, "warn"), _v(3.0, "warn"),
            [self._fresh_mechanical(1), STRATEGY_BLOCKER], unattended=True,
        )
        assert a == "continue", reason
        assert state.concept_gate_deferred == 2

    def test_a_stall_suppresses_the_deferral_even_if_the_last_step_improved(self) -> None:
        """history=[4.0, 2.0, 3.0]: the last step climbed, but the last two
        iterations are both below the prior best — a stall, which the loop
        already hands to a human. The gate opens rather than waiting on fixes
        that are not converging. Holds for a first deferral too."""
        for deferred in (0, 1):
            state = _state(score_history=[4.0, 2.0], concept_gate_deferred=deferred)
            a, reason = compute_auto_iterate(
                state, _v(3.0, "warn"), _v(3.0, "warn"),
                [self._fresh_mechanical(1), STRATEGY_BLOCKER], unattended=True,
            )
            assert a == "stop_concept_change", (deferred, reason)
            assert state.concept_gate_deferred == deferred

    def test_the_hard_cap_still_ends_a_run_that_keeps_climbing_by_real_steps(self) -> None:
        """HARD_CAP is the runaway backstop. A run that improves by a real step
        every pass with a strategy finding standing must still terminate at the
        cap — it must not defer forever."""
        from scripts.ddd.run_pipeline import TERMINAL_ACTIONS

        cap = 5
        # +0.6 per pass: outside the +/-0.5 noise band, never reaching the 4.0
        # convergence threshold, so nothing but the cap can end this run.
        steps = [1.0 + 0.6 * k for k in range(cap)]
        state = _state()
        for i, sc in enumerate(steps[:-1], start=1):
            a, reason = compute_auto_iterate(
                state, _v(sc, "fail"), _v(sc, "fail"),
                [self._fresh_mechanical(i), STRATEGY_BLOCKER],
                unattended=True, hard_cap=cap,
            )
            assert a == "continue", (i, reason)
        assert state.concept_gate_deferred == cap - 1
        a, reason = compute_auto_iterate(
            state, _v(steps[-1], "fail"), _v(steps[-1], "fail"),
            [self._fresh_mechanical(cap), STRATEGY_BLOCKER],
            unattended=True, hard_cap=cap,
        )
        assert a in TERMINAL_ACTIONS and a != "continue", reason
        assert state.concept_gate_deferred == cap - 1  # the cap pass was not a deferral
        assert len(state.score_history) == cap

    def test_the_hard_cap_also_ends_a_mechanical_only_run_that_keeps_climbing(self) -> None:
        """Same backstop, same class, no strategy finding: pending mechanical work
        must not `continue` past the cap either."""
        cap = 5
        steps = [1.0 + 0.6 * k for k in range(cap)]
        state = _state()
        for i, sc in enumerate(steps[:-1], start=1):
            a, _ = compute_auto_iterate(
                state, _v(sc, "fail"), _v(sc, "fail"),
                [self._fresh_mechanical(i)], unattended=True, hard_cap=cap,
            )
            assert a == "continue"
        a, reason = compute_auto_iterate(
            state, _v(steps[-1], "fail"), _v(steps[-1], "fail"),
            [self._fresh_mechanical(cap)], unattended=True, hard_cap=cap,
        )
        assert a == "stop_max_iter", reason
        assert "backstop" in reason

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
        state.concept_gate_deferred = 0  # isolate the plateau from the exhaustion rule
        a2, _ = compute_auto_iterate(
            state, _v(3.0, "warn"), _v(3.0, "warn"), findings, unattended=True
        )
        assert a2 == "stop_concept_change"
        assert state.concept_gate_deferred == 0
