"""Tests for scripts/ddd/run_pipeline.py (SP4.1 + SP4.2, TDD).

SP4.1 — assemble_run_state:
    given a RunState + two stub Verdicts + a findings list, the result has:
    - verdicts == {"concept": concept_path, "user_artifact": user_path}
    - findings == the provided list
    - phase == "judged"

SP4.2 — compute_convergence:
    - both verdicts overall_score >= 4.0 AND neither blocked → True
    - one verdict overall_score < 4.0 → False
    - both overall_score >= 4.0 but one verdict=="blocked" → False
    - both overall_score >= 4.5 → True

    Also: MAX_ITERATIONS constant exported at module level.
"""
from __future__ import annotations

import pytest

from scripts.ddd.schemas.models import Dimension, RunState, Verdict


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_verdict(
    overall_score: float,
    verdict_str: str = "pass",
    gate: str = "gating",
) -> Verdict:
    """Create a minimal Verdict for testing.

    ``gate`` is stamped explicitly (default ``gating``): these stubs stand in
    for the concept / user_artifact pair, which the skills stamp at write time
    and ``load_verdict`` stamps at load time. The MODEL default is ``advisory``
    — an unstamped legacy verdict must never gate.
    """
    return Verdict(
        schema_version=1,
        gate=gate,  # type: ignore[arg-type]
        dimensions={
            "concept_clarity": Dimension(score=overall_score, weight=1.0),
        },
        overall_score=overall_score,
        verdict=verdict_str,  # type: ignore[arg-type]
        blocking_reason=None,
        fix_recommendation=None,
    )


def _stub_state(narrative_slug: str = "test-narrative_slug") -> RunState:
    return RunState(run_id=f"{narrative_slug}-2026-01-01-001", narrative_slug=narrative_slug)


# ---------------------------------------------------------------------------
# SP4.1 — assemble_run_state
# ---------------------------------------------------------------------------


class TestAssembleRunState:
    def test_sets_verdict_paths(self) -> None:
        from scripts.ddd.run_pipeline import assemble_run_state

        state = _stub_state()
        concept_v = _stub_verdict(4.5)
        user_v = _stub_verdict(4.0)

        result = assemble_run_state(
            state,
            concept_v,
            user_v,
            findings=[],
            concept_path="verdict-concept.yaml",
            user_path="verdict-user.yaml",
        )

        assert result.verdicts == {
            "concept": "verdict-concept.yaml",
            "user_artifact": "verdict-user.yaml",
        }

    def test_sets_findings(self) -> None:
        from scripts.ddd.run_pipeline import assemble_run_state

        state = _stub_state()
        findings = [
            {"scene": "Scene 1", "dimension": "design_soundness", "severity": "high"},
            {"scene": "Scene 2", "dimension": "concept_clarity", "severity": "low"},
        ]

        result = assemble_run_state(
            state,
            _stub_verdict(4.0),
            _stub_verdict(4.0),
            findings=findings,
        )

        assert result.findings == findings

    def test_sets_phase_to_judged(self) -> None:
        from scripts.ddd.run_pipeline import assemble_run_state

        state = _stub_state()

        result = assemble_run_state(
            state,
            _stub_verdict(4.0),
            _stub_verdict(4.0),
            findings=[],
        )

        assert result.phase == "judged"

    def test_default_paths_used_when_not_specified(self) -> None:
        from scripts.ddd.run_pipeline import assemble_run_state

        state = _stub_state()

        result = assemble_run_state(
            state,
            _stub_verdict(4.0),
            _stub_verdict(4.0),
            findings=[],
        )

        # Default paths
        assert result.verdicts["concept"] == "verdict-concept.yaml"
        assert result.verdicts["user_artifact"] == "verdict-user.yaml"

    def test_returns_runstate(self) -> None:
        from scripts.ddd.run_pipeline import assemble_run_state

        state = _stub_state()
        result = assemble_run_state(
            state,
            _stub_verdict(5.0),
            _stub_verdict(5.0),
            findings=[],
        )
        assert isinstance(result, RunState)

    def test_preserves_run_id_and_feature(self) -> None:
        from scripts.ddd.run_pipeline import assemble_run_state

        state = _stub_state("my-narrative_slug")
        result = assemble_run_state(
            state,
            _stub_verdict(4.0),
            _stub_verdict(4.0),
            findings=[],
        )
        assert result.run_id == state.run_id
        assert result.narrative_slug == "my-narrative_slug"

    def test_custom_paths(self) -> None:
        from scripts.ddd.run_pipeline import assemble_run_state

        state = _stub_state()
        result = assemble_run_state(
            state,
            _stub_verdict(4.0),
            _stub_verdict(4.0),
            findings=[],
            concept_path="runs/abc/verdict-concept.yaml",
            user_path="runs/abc/verdict-user.yaml",
        )
        assert result.verdicts["concept"] == "runs/abc/verdict-concept.yaml"
        assert result.verdicts["user_artifact"] == "runs/abc/verdict-user.yaml"

    def test_scenes_run_and_filter_from_manifest(self) -> None:
        """When a render manifest is passed, scenes_run/scene_filter are carried
        from it onto the run state — the engine is the single source of truth for
        which scenes were rendered."""
        from scripts.ddd.run_pipeline import assemble_run_state

        state = _stub_state()
        result = assemble_run_state(
            state,
            _stub_verdict(4.0),
            _stub_verdict(4.0),
            findings=[],
            manifest={"scenes_run": [1, 2, 3], "scene_filter": None},
        )

        assert result.scenes_run == [1, 2, 3]
        assert result.scene_filter is None

    def test_partial_scene_filter_carried_from_manifest(self) -> None:
        from scripts.ddd.run_pipeline import assemble_run_state

        state = _stub_state()
        result = assemble_run_state(
            state,
            _stub_verdict(4.0),
            _stub_verdict(4.0),
            findings=[],
            manifest={"scenes_run": [2], "scene_filter": "2"},
        )

        assert result.scenes_run == [2]
        assert result.scene_filter == "2"

    def test_no_manifest_leaves_scene_fields_untouched(self) -> None:
        """Without a manifest, assemble_run_state does not touch scenes_run/
        scene_filter (backward-compatible default)."""
        from scripts.ddd.run_pipeline import assemble_run_state

        state = _stub_state()
        result = assemble_run_state(
            state,
            _stub_verdict(4.0),
            _stub_verdict(4.0),
            findings=[],
        )

        assert result.scenes_run is None
        assert result.scene_filter is None


# ---------------------------------------------------------------------------
# SP4.2 — compute_convergence
# ---------------------------------------------------------------------------


class TestComputeConvergence:
    def test_both_at_threshold_returns_true(self) -> None:
        from scripts.ddd.run_pipeline import compute_convergence

        assert compute_convergence(_stub_verdict(4.0), _stub_verdict(4.0)) is True

    def test_one_below_threshold_returns_false(self) -> None:
        from scripts.ddd.run_pipeline import compute_convergence

        assert compute_convergence(_stub_verdict(3.9), _stub_verdict(4.0)) is False
        assert compute_convergence(_stub_verdict(4.0), _stub_verdict(3.9)) is False

    def test_both_above_threshold_but_one_blocked_returns_false(self) -> None:
        from scripts.ddd.run_pipeline import compute_convergence

        blocked = _stub_verdict(4.5, verdict_str="blocked")
        passing = _stub_verdict(4.5, verdict_str="pass")

        assert compute_convergence(blocked, passing) is False
        assert compute_convergence(passing, blocked) is False

    def test_both_above_threshold_pass(self) -> None:
        from scripts.ddd.run_pipeline import compute_convergence

        assert compute_convergence(_stub_verdict(4.5), _stub_verdict(5.0)) is True

    def test_both_exactly_4_default_threshold(self) -> None:
        from scripts.ddd.run_pipeline import compute_convergence

        assert compute_convergence(_stub_verdict(4.0), _stub_verdict(4.0)) is True

    def test_custom_threshold(self) -> None:
        from scripts.ddd.run_pipeline import compute_convergence

        # With threshold=3.0, a score of 3.5 should pass
        assert compute_convergence(_stub_verdict(3.5), _stub_verdict(3.5), threshold=3.0) is True
        # But 2.9 should not
        assert compute_convergence(_stub_verdict(2.9), _stub_verdict(3.5), threshold=3.0) is False

    def test_both_warn_with_score_3(self) -> None:
        """warn verdict at score 3 is below the default threshold of 4.0."""
        from scripts.ddd.run_pipeline import compute_convergence

        warn_v = _stub_verdict(3.0, verdict_str="warn")
        assert compute_convergence(warn_v, warn_v) is False

    def test_both_blocked_returns_false(self) -> None:
        from scripts.ddd.run_pipeline import compute_convergence

        blocked = _stub_verdict(5.0, verdict_str="blocked")
        assert compute_convergence(blocked, blocked) is False


# ---------------------------------------------------------------------------
# SP4.2 — MAX_ITERATIONS constant
# ---------------------------------------------------------------------------


class TestMaxIterations:
    def test_hard_cap_is_backstop_not_a_low_count(self) -> None:
        # The loop is progress-aware: there is no raw 3-iteration cap.
        # HARD_CAP is a runaway backstop only, and MAX_ITERATIONS is a
        # back-compat alias for it (not 3).
        from scripts.ddd.run_pipeline import HARD_CAP, MAX_ITERATIONS

        assert HARD_CAP >= 8  # generous backstop, not the normal stop
        assert MAX_ITERATIONS == HARD_CAP

    def test_constants_are_int(self) -> None:
        from scripts.ddd.run_pipeline import HARD_CAP, MAX_ITERATIONS

        assert isinstance(HARD_CAP, int) and isinstance(MAX_ITERATIONS, int)


# ---------------------------------------------------------------------------
# Progress-aware auto-iterate
# ---------------------------------------------------------------------------


class TestComputeAutoIterate:
    """The loop continues while mechanical + improving; stops on stall/gate."""

    def test_continue_while_improving(self):
        from scripts.ddd.run_pipeline import compute_auto_iterate

        s = _stub_state("vm")
        mech = [{"route": "PRODUCT", "fix_kind": "mechanical"}]
        a, _ = compute_auto_iterate(s, _stub_verdict(2.0, "fail"), _stub_verdict(2.0, "fail"), mech)
        assert a == "continue"
        a, _ = compute_auto_iterate(s, _stub_verdict(3.0, "warn"), _stub_verdict(3.0, "warn"), mech)
        assert a == "continue"  # still improving — keep going past iteration 2
        assert s.score_history == [2.0, 3.0]

    def test_stop_done_on_converged(self):
        from scripts.ddd.run_pipeline import compute_auto_iterate

        s = _stub_state("vm")
        a, _ = compute_auto_iterate(s, _stub_verdict(4.0, "pass"), _stub_verdict(4.5, "pass"), [])
        assert a == "stop_done"

    def test_stop_unclear_on_options_finding(self):
        from scripts.ddd.run_pipeline import compute_auto_iterate

        s = _stub_state("vm")
        a, _ = compute_auto_iterate(
            s, _stub_verdict(2.0, "fail"), _stub_verdict(3.0, "warn"),
            [{"route": "PRODUCT", "fix_kind": "options"}],
        )
        assert a == "stop_unclear"

    def test_mechanical_applied_before_options_surfaced(self):
        """Mixed iteration: apply the CONFIDENT (mechanical) fixes first; do NOT
        surface a review just because some OTHER finding was uncertain. Only once
        no mechanical fixes remain do the options get surfaced. (The bug: a single
        options finding stopped the loop and folded the mechanical ones into the
        human review instead of auto-applying them.)"""
        from scripts.ddd.run_pipeline import compute_auto_iterate

        # iteration 1: 4 mechanical + 1 options, score improving → CONTINUE (apply mechanical)
        s = _stub_state("vm")
        mixed = [
            {"route": "PRODUCT", "fix_kind": "mechanical"},
            {"route": "PRODUCT", "fix_kind": "mechanical"},
            {"route": "CONCEPT", "fix_kind": "mechanical"},
            {"route": "PRODUCT", "fix_kind": "mechanical"},
            {"route": "PRODUCT", "fix_kind": "options"},
        ]
        a, _ = compute_auto_iterate(s, _stub_verdict(2.0, "fail"), _stub_verdict(3.0, "warn"), mixed)
        assert a == "continue"

        # next iteration: mechanical exhausted, only the options finding remains → STOP and surface
        a, _ = compute_auto_iterate(
            s, _stub_verdict(3.0, "warn"), _stub_verdict(3.0, "warn"),
            [{"route": "PRODUCT", "fix_kind": "options"}],
        )
        assert a == "stop_unclear"

    def test_stop_max_iter_on_stall_not_count(self):
        from scripts.ddd.run_pipeline import compute_auto_iterate

        s = _stub_state("vm")
        mech = [{"route": "PRODUCT", "fix_kind": "mechanical"}]
        # Three iterations at the same score → stalled (no new best), not a raw count.
        compute_auto_iterate(s, _stub_verdict(3.0, "warn"), _stub_verdict(3.0, "warn"), mech)
        compute_auto_iterate(s, _stub_verdict(3.0, "warn"), _stub_verdict(3.0, "warn"), mech)
        a, reason = compute_auto_iterate(s, _stub_verdict(3.0, "warn"), _stub_verdict(3.0, "warn"), mech)
        assert a == "stop_max_iter"
        assert "stall" in reason.lower()

    def test_many_iterations_ok_while_climbing(self):
        """A long but monotonically-improving run does NOT stop at 3 (the old bug)."""
        from scripts.ddd.run_pipeline import compute_auto_iterate

        s = _stub_state("vm")
        mech = [{"route": "PRODUCT", "fix_kind": "mechanical"}]
        for sc in (1.0, 2.0, 3.0, 3.5):  # 4 iterations, each better
            a, _ = compute_auto_iterate(s, _stub_verdict(sc, "warn"), _stub_verdict(sc, "warn"), mech)
            assert a == "continue"


# ---------------------------------------------------------------------------
# generic aggregation + convergence over N verdicts
# ---------------------------------------------------------------------------


class TestAssembleExtraVerdicts:
    def test_extra_verdict_paths_recorded(self) -> None:
        from scripts.ddd.run_pipeline import assemble_run_state

        state = _stub_state()
        result = assemble_run_state(
            state,
            _stub_verdict(4.5),
            _stub_verdict(4.0),
            findings=[],
            extra_verdict_paths={
                "timing": "verdict-timing.json",
                "why": "verdict-why.yaml",
            },
        )
        assert result.verdicts == {
            "concept": "verdict-concept.yaml",
            "user_artifact": "verdict-user.yaml",
            "timing": "verdict-timing.json",
            "why": "verdict-why.yaml",
        }

    def test_extra_verdicts_cannot_shadow_the_gating_pair(self) -> None:
        from scripts.ddd.run_pipeline import assemble_run_state

        state = _stub_state()
        result = assemble_run_state(
            state,
            _stub_verdict(4.5),
            _stub_verdict(4.0),
            findings=[],
            extra_verdict_paths={"concept": "evil-override.yaml"},
        )
        assert result.verdicts["concept"] == "verdict-concept.yaml"


class TestComputeConvergenceAll:
    def test_all_gating_above_threshold_converges(self) -> None:
        from scripts.ddd.run_pipeline import compute_convergence_all

        assert compute_convergence_all(
            {"concept": _stub_verdict(4.5), "user_artifact": _stub_verdict(4.0)}
        )

    def test_low_advisory_verdict_does_not_block(self) -> None:
        from scripts.ddd.run_pipeline import compute_convergence_all

        timing = _stub_verdict(2.0)
        timing.gate = "advisory"
        assert compute_convergence_all(
            {
                "concept": _stub_verdict(4.5),
                "user_artifact": _stub_verdict(4.0),
                "timing": timing,
            }
        )

    def test_low_gating_extra_blocks(self) -> None:
        from scripts.ddd.run_pipeline import compute_convergence_all

        assert not compute_convergence_all(
            {
                "concept": _stub_verdict(4.5),
                "user_artifact": _stub_verdict(4.0),
                "extra_gate": _stub_verdict(3.0),
            }
        )

    def test_blocked_gating_verdict_blocks(self) -> None:
        from scripts.ddd.run_pipeline import compute_convergence_all

        assert not compute_convergence_all(
            {"concept": _stub_verdict(4.5, "blocked"), "user_artifact": _stub_verdict(4.0)}
        )

    def test_unverified_gating_verdict_blocks(self) -> None:
        # item 3: a gating verdict whose anchor never touched live state must
        # not be able to converge a run, whatever its score says
        from scripts.ddd.run_pipeline import compute_convergence_all

        unverified = _stub_verdict(4.5)
        unverified.live_state_verified = False
        assert not compute_convergence_all(
            {"concept": unverified, "user_artifact": _stub_verdict(4.0)}
        )

    def test_no_gating_verdicts_never_converges(self) -> None:
        from scripts.ddd.run_pipeline import compute_convergence_all

        advisory = _stub_verdict(5.0)
        advisory.gate = "advisory"
        assert not compute_convergence_all({"timing": advisory})

    def test_two_verdict_compute_convergence_delegates(self) -> None:
        # the documented two-arg call keeps working and honors gating extras
        from scripts.ddd.run_pipeline import compute_convergence

        assert compute_convergence(_stub_verdict(4.5), _stub_verdict(4.0))
        assert not compute_convergence(
            _stub_verdict(4.5),
            _stub_verdict(4.0),
            extra={"other": _stub_verdict(2.0)},
        )

    def test_capped_advisory_with_passing_gating_pair_converges(self) -> None:
        """A capped advisory verdict (live_state_verified
        False, uncapped score above the 4.0 cap) rides along with a passing
        gating pair — the cap fires, the extra stays advisory, and convergence
        is still True."""
        from scripts.ddd.run_pipeline import compute_convergence

        capped = Verdict(
            kind="actionability",
            gate="advisory",
            live_state_verified=False,
            dimensions={"coverage": Dimension(score=4.8, weight=1.0)},
            overall_score=4.8,
            verdict="pass",
        )
        # The schema-layer cap fired and recorded the pre-cap score.
        assert capped.overall_score == 4.0
        assert capped.uncapped_overall_score == 4.8

        assert (
            compute_convergence(
                _stub_verdict(4.5),
                _stub_verdict(4.0),
                extra={"actionability": capped},
            )
            is True
        )

    def test_legacy_unstamped_verdict_is_advisory_not_gating(self) -> None:
        """Verdict.gate defaults to 'advisory', so a legacy
        verdict loaded via bare model_validate cannot hard-block convergence —
        and alone it cannot converge a run either (no gating verdict present →
        False, demonstrated not defaulted)."""
        from scripts.ddd.run_pipeline import compute_convergence_all

        legacy = Verdict.model_validate(
            {
                "dimensions": {"d": {"score": 2.0, "weight": 1.0}},
                "overall_score": 2.0,
                "verdict": "fail",
            }
        )
        assert legacy.gate == "advisory"
        # A low legacy verdict does not block a passing gating pair...
        assert compute_convergence_all(
            {
                "concept": _stub_verdict(4.5),
                "user_artifact": _stub_verdict(4.0),
                "legacy": legacy,
            }
        )
        # ...and by itself it can never demonstrate convergence.
        assert not compute_convergence_all({"legacy": legacy})


# ---------------------------------------------------------------------------
# cap-visible verdict report lines
# ---------------------------------------------------------------------------


class TestFormatVerdictLine:
    def test_uncapped_verdict_renders_plain(self) -> None:
        from scripts.ddd.run_pipeline import format_verdict_line

        assert format_verdict_line(_stub_verdict(4.5)) == "4.5/5 (pass)"

    def test_capped_verdict_names_the_cap(self) -> None:
        from scripts.ddd.run_pipeline import format_verdict_line

        capped = Verdict(
            kind="why",
            gate="advisory",
            live_state_verified=False,
            dimensions={"d": Dimension(score=4.8, weight=1.0)},
            overall_score=4.8,
            verdict="pass",
        )
        line = format_verdict_line(capped)
        assert line == "4.0/5 (pass — capped from 4.8, not live-state verified)"

    def test_unverified_below_cap_renders_plain(self) -> None:
        # the cap never fired (score already <= 4.0) — nothing to annotate
        from scripts.ddd.run_pipeline import format_verdict_line

        v = Verdict(
            gate="advisory",
            live_state_verified=False,
            dimensions={"d": Dimension(score=3.5, weight=1.0)},
            overall_score=3.5,
            verdict="warn",
        )
        assert format_verdict_line(v) == "3.5/5 (warn)"
