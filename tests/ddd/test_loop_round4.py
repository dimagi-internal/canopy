"""Loop round 4 — three inefficiencies measured on one real v1 run.

connect-labs ``supply-sophie-rutf-2026-09-26-001`` (canopy 0.2.543, backlog
mode, no ``inner_loop``), iterations 5-9, ~60 minutes per cycle:

1. every pass judged all 7 scenes with all three judges ("full pass
   requested") although the loop had asked for incremental passes — the
   orchestrator re-used a literal ``judge_scope plan ... --full``, and earlier
   rewrote run_state past a ``stop_max_iter``;
2. every batch, recipe-only ones included, went PR -> CI -> deploy (~35 of each
   60 minutes);
3. the stall detector never fired: open findings trickled 42 -> 39 -> 36 -> 35
   -> 34 while the mean cell sat at 3.60 and the floor flipped 2/3/3/2/3.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from scripts.ddd import decision, fix_scope, judge_gate, judge_scope, target
from scripts.ddd.loop_config import DDDConfig, InnerLoopConfig, LoopConfig
from scripts.ddd.run_pipeline import compute_auto_iterate
from scripts.ddd.schemas.models import RunState, Verdict

CFG = LoopConfig(mode="auto", backlog_min_findings=8, full_rejudge_every=3)


def _v(score: float, verdict: str = "fail") -> Verdict:
    return Verdict(
        schema_version=1,
        kind="concept",
        gate="gating",
        rubric_name="ddd-concept-eval",
        ran_at="2026-10-01T00:00:00Z",
        dimensions={},
        overall_score=score,
        overall_rule="lowest",
        verdict=verdict,
    )


def _state(**kw) -> RunState:
    return RunState(run_id="supply-sophie-rutf-2026-09-26-001", narrative_slug="supply-sophie-rutf", **kw)


def _mech(n: int, scene: int = 1, **kw) -> dict:
    return {
        "scene": str(scene),
        "dimension": "visual_polish",
        "route": "PRODUCT",
        "fix_kind": "mechanical",
        "detail": f"Mechanical defect #{n}.",
        "fix_recommendation": f"Fix defect #{n} in the comparison template.",
        **kw,
    }


def _findings(count: int, tag: int) -> list[dict]:
    return [_mech(tag * 1000 + i, scene=i % 7 + 1) for i in range(count)]


def _dist(mean: float, caps: int) -> dict:
    return {
        "mean": mean,
        "capping_cells": [
            {"scene": str(i + 1), "dimension": "visual_polish", "confirmed": 2.0} for i in range(caps)
        ],
    }


def _recipe(scene: int) -> dict:
    return {
        "scene": str(scene),
        "dimension": "motion_friction",
        "route": "PRODUCT",
        "fix_kind": "mechanical",
        "detail": "The narrated card is cut off at the bottom of the frame.",
        "fix_recommendation": "In the recipe, scroll_to the heading before the snapshot so the card is fully in frame.",
    }


# ---------------------------------------------------------------------------
# 3 — a trickle of open findings with everything else flat is a stall
# ---------------------------------------------------------------------------

# The run's own progress_history, iterations 5..9 (run_state.yaml).
SOPHIE = [
    # (iteration, score, open_findings, mean_cell, confirmed_caps)
    (5, 2.0, 42, 3.333, 4),
    (6, 3.0, 39, 3.595, 0),
    (7, 3.0, 36, 3.643, 0),
    (8, 2.0, 35, 3.619, 1),
    (9, 3.0, 34, 3.595, 0),
]


class TestTrickleStall:
    def test_replaying_the_sophie_history_stops_at_iteration_8_not_never(self) -> None:
        state = _state()
        actions: dict[int, tuple[str, str]] = {}
        for iteration, score, n, mean, caps in SOPHIE:
            state.iteration = iteration
            actions[iteration] = compute_auto_iterate(
                state, _v(score), _v(score), _findings(n, iteration), unattended=True,
                distribution=_dist(mean, caps), judge_full=True, loop_config=CFG,
            )
            if actions[iteration][0] != "continue":
                break
        # 6 is a real step (caps 4 -> 0, mean +0.26, floor 2 -> 3). 7 and 8 only
        # trickle (39 -> 36 -> 35, inside 15% of the backlog) with the mean inside
        # its band and no cap fixed — two flat passes, so 8 stops. Before: 9 too,
        # and the HARD_CAP after it.
        assert [actions[i][0] for i in (5, 6, 7)] == ["continue"] * 3
        action, reason = actions[8]
        assert action == "stop_max_iter", reason
        assert "Stalled" in reason
        assert 9 not in actions
        assert [p["open_findings"] for p in state.progress_history] == [42, 39, 36, 35]

    def test_a_backlog_falling_fast_with_a_flat_mean_keeps_going(self) -> None:
        """The floor-pinned v1 shape the progress signal exists for still continues."""
        state = _state()
        for i, n in enumerate([60, 44, 30, 20]):
            a, reason = compute_auto_iterate(
                state, _v(2.0), _v(2.0), _findings(n, i), unattended=True,
                distribution=_dist(3.4, 2), judge_full=True, loop_config=CFG,
            )
            assert a == "continue", (i, reason)


# ---------------------------------------------------------------------------
# 1a — judge_scope plan derives the scope; a contradicting --full needs a reason
# ---------------------------------------------------------------------------


def _write_run(run: Path, scenes: int = 3) -> Path:
    snaps = run / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)
    for i in range(1, scenes + 1):
        (snaps / f"scene_{i}.png").write_bytes(f"png-{i}".encode())
        (snaps / f"scene_{i}_page_text.json").write_text(
            json.dumps({"scene_index": str(i), "page_text": f"text {i}"})
        )
    spec = run / "spec.yaml"
    spec.write_text(
        yaml.safe_dump(
            {"name": "t", "scenes": [{"title": f"S{i}", "narrative": f"N{i}."} for i in range(1, scenes + 1)]}
        )
    )
    return spec


def _save(run: Path, state: RunState) -> None:
    (run / "run_state.yaml").write_text(yaml.safe_dump(state.model_dump()))


def _incremental_state() -> RunState:
    """What `assemble` left after iteration 7's ``continue``: next judge incremental."""
    st = _state(iteration=8, loop_mode="backlog", next_judge_full=False, batches_since_full=0)
    st.auto_iterate_next_action = "continue"
    decision.seal(st)
    return st


class TestPlanDerivesScope:
    def _ledgered(self, tmp_path: Path) -> Path:
        spec = _write_run(tmp_path)
        judge_scope.plan(tmp_path, spec)
        judge_scope.record(tmp_path, spec, iteration=7)
        (tmp_path / "snapshots" / "scene_2.png").write_bytes(b"fixed")
        return spec

    def test_a_literal_full_against_an_incremental_state_is_refused(self, tmp_path: Path) -> None:
        spec = self._ledgered(tmp_path)
        _save(tmp_path, _incremental_state())
        with pytest.raises(ValueError, match="contradicts run_state"):
            judge_scope.resolve_plan_args(tmp_path, full=True, cfg=DDDConfig())
        assert judge_scope._main(["plan", str(tmp_path), str(spec), "--full"]) == 2

    def test_no_flags_follows_the_state_and_reuses_unchanged_scenes(self, tmp_path: Path) -> None:
        spec = self._ledgered(tmp_path)
        _save(tmp_path, _incremental_state())
        kw = judge_scope.resolve_plan_args(tmp_path, cfg=DDDConfig())
        scope = judge_scope.plan(tmp_path, spec, **kw)
        assert (scope["full"], scope["rejudge"], scope["reuse"]) == (False, [2], [1, 3])
        assert scope["judges"] == ["concept"]  # tiering: auto = on in backlog mode

    def test_an_override_with_a_reason_is_recorded(self, tmp_path: Path) -> None:
        spec = self._ledgered(tmp_path)
        _save(tmp_path, _incremental_state())
        kw = judge_scope.resolve_plan_args(
            tmp_path, full=True, reason="narrative re-locked to v4", cfg=DDDConfig()
        )
        scope = judge_scope.plan(tmp_path, spec, **kw)
        on_disk = json.loads((tmp_path / judge_scope.SCOPE_FILE).read_text())
        assert scope["full"] and on_disk["override"]["reason"] == "narrative re-locked to v4"
        assert on_disk["reason"].startswith("full pass OVERRIDE: narrative re-locked to v4")
        assert "full pass requested" not in on_disk["reason"]

    def test_a_checkpoint_is_full_and_says_why(self, tmp_path: Path) -> None:
        spec = self._ledgered(tmp_path)
        st = _incremental_state()
        st.next_judge_full = True
        st.auto_iterate_next_action = "continue"
        decision.seal(st)
        _save(tmp_path, st)
        scope = judge_scope.plan(tmp_path, spec, **judge_scope.resolve_plan_args(tmp_path, cfg=DDDConfig()))
        assert scope["full"] and "checkpoint" in scope["reason"]

    def test_plan_refuses_a_state_rewritten_after_assemble(self, tmp_path: Path) -> None:
        spec = self._ledgered(tmp_path)
        st = _incremental_state()
        st.next_judge_full = True  # the hand-written bump script did exactly this
        _save(tmp_path, st)
        with pytest.raises(ValueError, match="rewritten after assemble"):
            judge_scope.resolve_plan_args(tmp_path, cfg=DDDConfig())
        assert judge_scope._main(["plan", str(tmp_path), str(spec)]) == 2


# ---------------------------------------------------------------------------
# 1b — a sealed decision cannot be quietly overruled
# ---------------------------------------------------------------------------


class TestDecisionSeal:
    def _stopped(self) -> RunState:
        st = _state(iteration=5)
        st.progress_history = [{"score": 2.0}, {"score": 3.0}, {"score": 3.0}]
        st.auto_iterate_next_action = "stop_max_iter"
        decision.seal(st)
        return st

    def test_a_new_pass_past_a_stop_is_refused(self) -> None:
        st = self._stopped()
        out = decision.check(st)
        assert not out["ok"] and "stop_max_iter" in out["problem"]

    def test_resetting_the_history_is_detected(self) -> None:
        st = self._stopped()
        st.progress_history = st.progress_history[-1:]  # the "v4 baseline" script
        out = decision.check(st)
        assert not out["ok"] and "rewritten after assemble" in out["problem"]

    def test_a_logged_override_lets_the_run_continue_and_is_kept(self) -> None:
        st = self._stopped()
        st.progress_history = st.progress_history[-1:]
        entry = decision.override(st, "series mixes narrative v3 and v4; rebased on v4")
        assert entry["action"] == "stop_max_iter" and entry["state_rewritten"] is True
        assert decision.check(st)["ok"]
        assert st.decision_overrides[-1]["reason"].startswith("series mixes")
        with pytest.raises(ValueError):
            decision.override(st, "  ")

    def test_bumping_the_iteration_is_not_a_rewrite(self) -> None:
        st = _incremental_state()
        decision.bump(st)
        assert st.iteration == 9 and decision.check(st)["ok"]

    def test_stop_partial_does_not_block_the_full_refire(self) -> None:
        st = _state()
        st.auto_iterate_next_action = "stop_partial"
        decision.seal(st)
        assert decision.check(st)["ok"]


# ---------------------------------------------------------------------------
# 2 — a recipe-only batch skips the deploy gate and re-judges only its scenes
# ---------------------------------------------------------------------------


class TestRecipeOnlyBatch:
    def test_batch_plan_scopes(self) -> None:
        assert fix_scope.batch_plan([_recipe(5), _recipe(6)])["scope"] == "recipe"
        assert fix_scope.batch_plan([_recipe(5), _recipe(6)])["judge_scenes"] == [5, 6]
        narration = {**_mech(1, scene=3), "route": "CONCEPT"}
        assert fix_scope.batch_plan([_recipe(5), narration])["scope"] == "recipe"
        mixed = fix_scope.batch_plan([_recipe(5), _mech(2, scene=1)])
        assert (mixed["scope"], mixed["deploy"], mixed["judge_scenes"]) == ("product", True, None)
        assert fix_scope.batch_plan([{**_mech(1), "route": "DEFER"}]) is None

    def test_continue_on_a_recipe_only_backlog_skips_pr_ci_and_deploy(self) -> None:
        state = _state(iteration=8, loop_mode="backlog")
        findings = [_recipe(4), _recipe(5), _recipe(6)] + [
            {**_recipe(s), "detail": f"another framing nit {s}"} for s in (4, 5, 6, 7, 4, 5)
        ]
        a, reason = compute_auto_iterate(
            state, _v(3.0), _v(3.0), findings, unattended=True,
            distribution=_dist(3.6, 0), judge_full=True, loop_config=CFG,
        )
        assert a == "continue", reason
        assert "RECIPE-ONLY" in reason and "do NOT open a product PR" in reason
        assert state.batch_plan["for_iteration"] == 9

        decision.bump(state)
        chosen = target.choose(state, DDDConfig(loop=CFG))
        assert chosen["deploy_gate"] == "skip" and chosen["target"] == target.DEPLOY
        assert chosen["judge_scenes"] == [4, 5, 6, 7]
        assert target.expected_scope(state, DDDConfig(loop=CFG))["judge_scenes"] == [4, 5, 6, 7]

    def test_a_checkpoint_never_takes_the_recipe_shortcut(self) -> None:
        state = _state(iteration=9, next_judge_full=True)
        state.batch_plan = {"scope": "recipe", "for_iteration": 9, "judge_scenes": [5]}
        chosen = target.choose(state, DDDConfig(loop=CFG))
        assert chosen["checkpoint"] and chosen["deploy_gate"] == "check"
        assert chosen["judge_scenes"] is None

    def test_a_stale_plan_is_ignored(self) -> None:
        state = _state(iteration=12, next_judge_full=False, loop_mode="backlog")
        state.batch_plan = {"scope": "recipe", "for_iteration": 9, "judge_scenes": [5]}
        assert target.choose(state, DDDConfig(loop=CFG))["deploy_gate"] == "check"

    def test_scenes_the_batch_did_not_edit_are_held_not_rejudged(self, tmp_path: Path) -> None:
        spec = _write_run(tmp_path)
        judge_scope.plan(tmp_path, spec)
        judge_scope.record(tmp_path, spec, iteration=8)
        before = json.loads((tmp_path / "judge-cache" / "index.json").read_text())["fingerprints"]
        for i in (1, 2, 3):  # a reseed moved every frame
            (tmp_path / "snapshots" / f"scene_{i}.png").write_bytes(f"reseeded-{i}".encode())
        scope = judge_scope.plan(tmp_path, spec, judge_scenes=[2])
        assert (scope["rejudge"], scope["reuse"], scope["held"]) == ([2], [1, 3], [1, 3])
        judge_scope.record(tmp_path, spec, iteration=9)
        after = json.loads((tmp_path / "judge-cache" / "index.json").read_text())["fingerprints"]
        # held scenes keep the fingerprint their cells were judged on
        assert after["1"] == before["1"] and after["3"] == before["3"]
        assert after["2"] != before["2"]

    def test_a_pass_that_held_scenes_cannot_decide(self) -> None:
        state = _state()
        a, reason = compute_auto_iterate(
            state, _v(4.0, "pass"), _v(4.5, "pass"), [], unattended=True,
            judge_full=True, held=[1, 3],
        )
        assert a == "checkpoint", reason

    def test_judge_gate_skips_the_deploy_half_for_a_recipe_pass(self) -> None:
        state = _state(iteration=9, next_judge_full=False, loop_mode="backlog")
        state.batch_plan = {"scope": "recipe", "for_iteration": 9, "judge_scenes": [5]}
        state.current_target = {**target.choose(state, DDDConfig(loop=CFG)), "iteration": 9}
        assert target.current(state)["deploy_gate"] == "skip"


class TestInnerLoopHint:
    def test_a_long_wait_without_an_inner_loop_prints_a_recommendation(self) -> None:
        now = datetime(2026, 10, 2, 1, 0, tzinfo=timezone.utc)
        state = _state()
        state.steps = {
            "fixer:B9": {"finished_at": (now - timedelta(minutes=50)).isoformat()},
            "fixer:B10": {"finished_at": (now - timedelta(minutes=35)).isoformat()},
            "render:iter9": {"finished_at": (now - timedelta(minutes=1)).isoformat()},
        }
        waited = judge_gate.batch_wait_minutes(state, now=now)
        assert waited == pytest.approx(35.0)
        hint = judge_gate.inner_loop_hint(waited, inner_enabled=False, threshold=15.0)
        assert hint and "inner_loop" in hint and "35 min" in hint
        assert judge_gate.inner_loop_hint(waited, inner_enabled=True, threshold=15.0) is None
        assert judge_gate.inner_loop_hint(10.0, inner_enabled=False, threshold=15.0) is None
        assert judge_gate.inner_loop_hint(waited, inner_enabled=False, threshold=0.0) is None

    def test_the_threshold_is_configurable(self) -> None:
        from scripts.ddd.loop_config import parse

        assert parse({"loop": {"inner_loop_hint_minutes": 30}}).loop.inner_loop_hint_minutes == 30.0
        assert parse({}).loop.inner_loop_hint_minutes == 15.0
        assert not InnerLoopConfig().enabled
