"""DDD loop, round 3 part B — storyboard critique, inner-loop target, judge tiering.

Evidence (connect-labs ``supply-sophie-rutf-2026-09-26-001``): arc-level story
decisions (move a scene, data scale) surfaced only at iteration 4; every batch
paid build + CI 7–9 + deploy 8–11 min before a judge could look; every judge
round ran all three judges (~450k tokens: concept ~170k, user ~150k, arc
~130k) although most batches only changed a few scenes.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts.ddd import gap_walk, judge_gate, judge_scope, loop_config, storyboard, target
from scripts.ddd.loop_config import LoopConfig
from scripts.ddd.run_pipeline import compute_auto_iterate
from scripts.ddd.schemas.models import RunState, Verdict

REPO = Path(__file__).resolve().parents[2]


def _v(score: float, verdict: str = "fail") -> Verdict:
    return Verdict(
        schema_version=1, kind="concept", gate="gating", rubric_name="r", ran_at="t",
        dimensions={}, overall_score=score, overall_rule="lowest", verdict=verdict,
    )


def _state(**kw) -> RunState:
    return RunState(run_id="rutf-001", narrative_slug="supply-sophie-rutf", **kw)


def _mech(scene: int, n: int = 0) -> dict:
    return {
        "scene": str(scene), "dimension": "visual_polish", "route": "PRODUCT",
        "fix_kind": "mechanical", "detail": f"defect {n} on scene {scene}", "fix_recommendation": f"fix {n}",
    }


# ---------------------------------------------------------------------------
# Item 2 — pre-build storyboard critique
# ---------------------------------------------------------------------------

SCOPE = {
    "scenes": [6], "dimension": "earns_place", "kind": "scope",
    "detail": "Scene 6 interrupts the rise from ranking (5) to award (7).",
    "fix_recommendation": "Cut scene 6, or move it after scene 7.",
    "evidence": ["docs/walkthroughs/supply-sophie-rutf.yaml scene 6"],
}
SEED = {
    "scenes": [1], "dimension": "data_scale", "kind": "seed",
    "detail": "The overview shows three rows; the triage claim needs a program-sized backlog.",
    "fix_recommendation": "Seed 12 procurement rounds across 3 commodities.",
    "evidence": ["seed_data.py: 2 tenders, 1 order"],
}
RESTATE = {
    "scenes": [4], "dimension": "restatement", "kind": "restate",
    "detail": "Narration promises 'the one blocking fact' for each quote; two quotes show two.",
    "fix_recommendation": "Say 'what keeps each quote out of the ranking'.",
    "evidence": ["recipe scene 4 narrative"],
}


def _sb(*findings) -> dict:
    return {"narrative_slug": "supply-sophie-rutf", "one_sentence_story": "s", "findings": list(findings)}


def _gaps(scenes: int = 7, gaps: list | None = None) -> dict:
    gap_scenes = {g["scene"] for g in gaps or []}
    return {
        "covered": [{"scene": i, "evidence": ["views.py"]} for i in range(1, scenes + 1) if i not in gap_scenes],
        "gaps": gaps or [],
    }


class TestStoryboard:
    def test_a_well_formed_storyboard_validates(self) -> None:
        assert storyboard.validate(_sb(SCOPE, SEED, RESTATE), scene_count=7) == []
        assert storyboard.validate(_sb(), scene_count=7) == []

    @pytest.mark.parametrize(
        "bad,needle",
        [
            ({**SCOPE, "kind": "move"}, "`kind`"),
            ({**SCOPE, "scenes": []}, "`scenes`"),
            ({**SCOPE, "scenes": [9]}, "exceed"),
            ({**SCOPE, "evidence": []}, "`evidence`"),
            ({**SCOPE, "dimension": "vibes"}, "`dimension`"),
            ({**SCOPE, "fix_recommendation": ""}, "`fix_recommendation`"),
        ],
    )
    def test_invalid_findings_are_named(self, bad: dict, needle: str) -> None:
        problems = storyboard.validate(_sb(bad), scene_count=7)
        assert any(needle in p for p in problems), problems

    def test_route_buckets_and_asking_once(self) -> None:
        r = storyboard.route(_sb(SCOPE, SEED, RESTATE))
        assert [len(r[k]) for k in ("restate", "seed", "decide", "suppressed")] == [1, 1, 1, 0]
        again = storyboard.route(_sb(SCOPE, SEED, RESTATE), already_asked=True)
        assert again["decide"] == [] and len(again["suppressed"]) == 1

    def test_order_or_scope_opens_the_gate_with_the_gap_walk(self) -> None:
        out = gap_walk.decide(_gaps(), storyboard.route(_sb(SCOPE, RESTATE)))
        assert out["action"] == "decide"
        assert out["decisions"][0]["scenes"] == [6]
        assert any(r.get("why") == "storyboard restatement" for r in out["restate"])

    def test_seed_findings_join_the_build_batch(self) -> None:
        out = gap_walk.decide(_gaps(), storyboard.route(_sb(SEED)))
        assert out["action"] == "build" and out["seed"][0]["scenes"] == [1]

    def test_a_clean_storyboard_and_walk_render(self) -> None:
        assert gap_walk.decide(_gaps(), storyboard.route(_sb()))["action"] == "render"
        assert gap_walk.decide(_gaps())["action"] == "render"  # no storyboard: unchanged

    def test_cli_marks_the_storyboard_consumed_and_never_reasks(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("CANOPY_DDD_RUNS_DIR", str(tmp_path / "runs"))
        import scripts.ddd.runstate as rs

        ddd = tmp_path / "ddd"
        ddd.mkdir()
        monkeypatch.setattr(rs, "_resolve_ddd_dir", lambda *a, **k: ddd)
        rid = rs.new_run("supply-sophie-rutf", ddd_dir=ddd)
        run = rs.run_dir_for(rid, ddd_dir=ddd)
        (run / "gaps.json").write_text(json.dumps(_gaps()))
        (run / "storyboard.json").write_text(json.dumps(_sb(SCOPE)))
        args = ["check", str(run / "gaps.json"), "--run-id", rid, "--storyboard", str(run / "storyboard.json")]
        assert gap_walk._main(args) == 1  # decide: the scope question
        assert rs.load(rid, ddd_dir=ddd).storyboard is None  # not consumed until asked
        state = rs.load(rid, ddd_dir=ddd)
        storyboard.mark(state, "asked", review_id="rev-9")
        rs.save(state, ddd_dir=ddd)
        assert gap_walk._main(args) == 0  # asked once: the re-walk renders

    def test_cli_marks_a_restate_only_storyboard_applied(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("CANOPY_DDD_RUNS_DIR", str(tmp_path / "runs"))
        import scripts.ddd.runstate as rs

        ddd = tmp_path / "ddd"
        ddd.mkdir()
        monkeypatch.setattr(rs, "_resolve_ddd_dir", lambda *a, **k: ddd)
        rid = rs.new_run("supply-sophie-rutf", ddd_dir=ddd)
        run = rs.run_dir_for(rid, ddd_dir=ddd)
        (run / "gaps.json").write_text(json.dumps(_gaps()))
        (run / "storyboard.json").write_text(json.dumps(_sb(RESTATE)))
        assert gap_walk._main(["check", str(run / "gaps.json"), "--run-id", rid,
                               "--storyboard", str(run / "storyboard.json")]) == 1  # build (restate)
        assert rs.load(rid, ddd_dir=ddd).storyboard["status"] == "applied"


# ---------------------------------------------------------------------------
# Items 3 + 4 — target choice, checkpoint guard, tiering
# ---------------------------------------------------------------------------


def _cfg(inner: bool = True, tiering: str = "auto"):
    raw = {"loop": {"mode": "backlog", "judge_tiering": tiering}}
    if inner:
        raw["inner_loop"] = {"base_url": "http://localhost:8000/", "setup": "make serve-demo",
                             "health_url": "http://localhost:8000/health/"}
    return loop_config.parse(raw)


class TestConfig:
    def test_inner_loop_is_off_by_default_and_parsed_when_set(self) -> None:
        assert not loop_config.parse({}).inner_loop.enabled
        inner = _cfg().inner_loop
        assert inner.enabled and inner.base_url == "http://localhost:8000" and inner.setup == "make serve-demo"

    @pytest.mark.parametrize(
        "raw,mode,expected",
        [("auto", "backlog", True), ("auto", "polish", False), ("on", "polish", True),
         ("off", "backlog", False), (True, "polish", True), ("nonsense", "backlog", True)],
    )
    def test_judge_tiering(self, raw, mode, expected) -> None:
        assert loop_config.parse({"loop": {"judge_tiering": raw}}).loop.tiered(mode) is expected


class TestTargetChoice:
    def test_a_deciding_checkpoint_always_renders_the_deploy_target_with_every_judge(self) -> None:
        for action in ("checkpoint", "confirm_full"):
            st = _state(next_judge_full=True, loop_mode="backlog")
            st.auto_iterate_next_action = action
            out = target.choose(st, _cfg())
            assert (out["target"], out["judges"], out["checkpoint"]) == ("deploy", ["concept", "user", "arc"], True)

    def test_a_periodic_full_pass_runs_on_the_local_build(self) -> None:
        # canopy#787: deployed labs is for the pass that decides, not the regression net.
        out = target.choose(_state(next_judge_full=True, loop_mode="backlog"), _cfg())
        assert (out["target"], out["judges"], out["checkpoint"], out["full"]) == (
            "inner", ["concept", "user", "arc"], False, True
        )
        assert out["base_url"] == "http://localhost:8000"
        exp = target.expected_scope(_state(next_judge_full=True, loop_mode="backlog"), _cfg())
        assert exp["full"] is True and exp["tiered"] is False
        # ...and anything it would decide goes to a deploy checkpoint first.
        assert target.decision_needs_checkpoint(out["target"], out["judges"])

    def test_deploy_checkpoints_restores_every_full_pass_on_deploy(self) -> None:
        cfg = loop_config.parse({"loop": {"mode": "backlog"}, "inner_loop": {
            "base_url": "http://localhost:8000", "deploy_checkpoints": True}})
        out = target.choose(_state(next_judge_full=True, loop_mode="backlog"), cfg)
        assert (out["target"], out["checkpoint"]) == ("deploy", True)

    def test_between_checkpoints_the_inner_build_and_concept_judge_only(self) -> None:
        out = target.choose(_state(next_judge_full=False, loop_mode="backlog"), _cfg())
        assert (out["target"], out["base_url"], out["judges"]) == ("inner", "http://localhost:8000", ["concept"])

    def test_without_inner_loop_config_between_checkpoints_is_the_deploy_target(self) -> None:
        out = target.choose(_state(next_judge_full=False, loop_mode="backlog"), _cfg(inner=False))
        assert (out["target"], out["judges"]) == ("deploy", ["concept"])

    def test_tiering_off_keeps_every_judge(self) -> None:
        out = target.choose(_state(next_judge_full=False, loop_mode="backlog"), _cfg(tiering="off"))
        assert out["judges"] == ["concept", "user", "arc"]

    def test_after_a_checkpoint_action_the_next_pass_is_a_checkpoint(self) -> None:
        st = _state(next_judge_full=False, loop_mode="backlog", auto_iterate_next_action="checkpoint")
        assert target.choose(st, _cfg())["target"] == "deploy"

    def test_plan_flags(self) -> None:
        st = _state(next_judge_full=False, loop_mode="backlog", iteration=2)
        st.current_target = {**target.choose(st, _cfg()), "iteration": 2}
        assert target.plan_flags(st, _cfg()) == "--tiered"
        st2 = _state(next_judge_full=True, loop_mode="backlog", iteration=2)
        assert target.plan_flags(st2, _cfg()) == "--full"

    def test_a_stale_stamp_is_ignored(self) -> None:
        st = _state(iteration=3, current_target={"target": "inner", "iteration": 2})
        assert target.current(st) == {}


class TestOnlyACheckpointDecides:
    def test_convergence_on_the_inner_build_is_a_checkpoint(self) -> None:
        st = _state()
        action, reason = compute_auto_iterate(
            st, _v(4.5, "pass"), _v(4.5, "pass"), [], unattended=True,
            judge_full=False, target="inner", judges=["concept"],
        )
        assert action == "checkpoint", reason
        assert st.next_judge_full is True and st.terminal_status == "running"
        assert st.progress_history[-1]["target"] == "inner"

    def test_the_inner_build_cannot_decide_even_with_every_judge(self) -> None:
        st = _state()
        action, _ = compute_auto_iterate(
            st, _v(4.5, "pass"), _v(4.5, "pass"), [], unattended=True,
            judge_full=True, target="inner", judges=["concept", "user", "arc"],
        )
        assert action == "checkpoint"

    def test_a_stop_on_a_concept_only_pass_is_a_checkpoint(self) -> None:
        st = _state()
        for i in range(3):  # three flat passes -> would stall
            action, _ = compute_auto_iterate(
                st, _v(2.0), _v(2.0), [_mech(1, i)], unattended=True,
                distribution={"mean": 3.3, "capping_cells": []}, judge_full=False,
                target="deploy", judges=["concept"],
            )
        assert action == "checkpoint"

    def test_fixing_on_the_inner_build_just_continues(self) -> None:
        st = _state()
        action, _ = compute_auto_iterate(
            st, _v(2.0), _v(2.0), [_mech(1)], unattended=True, judge_full=False,
            target="inner", judges=["concept"],
        )
        assert action == "continue"

    def test_a_full_deploy_pass_with_every_judge_converges(self) -> None:
        st = _state()
        action, _ = compute_auto_iterate(
            st, _v(4.5, "pass"), _v(4.5, "pass"), [], unattended=True,
            judge_full=True, target="deploy", judges=["concept", "user", "arc"],
        )
        assert action == "stop_done"
        assert st.progress_history[-1]["target"] == "deploy"


def _write_run(run: Path, scenes: int = 3) -> Path:
    snaps = run / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)
    for i in range(1, scenes + 1):
        (snaps / f"scene_{i}.png").write_bytes(f"png-{i}".encode())
        (snaps / f"scene_{i}_page_text.json").write_text(json.dumps({"scene_index": str(i), "page_text": f"t{i}"}))
    spec = run / "spec.yaml"
    spec.write_text(yaml.safe_dump({"name": "t", "scenes": [{"title": f"S{i}", "narrative": f"N{i}."} for i in range(1, scenes + 1)]}))
    return spec


class TestJudgeTieringScope:
    def test_tiered_incremental_pass_runs_the_concept_judge_only_and_carries_the_rest(self, tmp_path: Path) -> None:
        spec = _write_run(tmp_path)
        judge_scope.plan(tmp_path, spec)
        (tmp_path / "verdict-user.yaml").write_text(yaml.safe_dump({"overall_score": 3, "findings": [{"scene": "2"}]}))
        judge_scope.record(tmp_path, spec, iteration=0)
        (tmp_path / "verdict-user.yaml").unlink()  # gone from the run dir
        (tmp_path / "snapshots" / "scene_2.png").write_bytes(b"fixed")
        scope = judge_scope.plan(tmp_path, spec, tiered=True)
        assert scope["judges"] == ["concept"] and scope["arc"] is False and scope["rejudge"] == [2]
        out = judge_scope.carry(tmp_path)
        assert out["user_carried"] is True
        assert yaml.safe_load((tmp_path / "verdict-user.yaml").read_text())["findings"] == [{"scene": "2"}]

    def test_a_full_pass_ignores_tiering(self, tmp_path: Path) -> None:
        spec = _write_run(tmp_path)
        scope = judge_scope.plan(tmp_path, spec, tiered=True)  # no ledger -> full
        assert scope["full"] and scope["judges"] == ["concept", "user", "arc"] and scope["arc"]

    def test_untiered_incremental_pass_keeps_every_judge(self, tmp_path: Path) -> None:
        spec = _write_run(tmp_path)
        judge_scope.plan(tmp_path, spec)
        judge_scope.record(tmp_path, spec, iteration=0)
        (tmp_path / "snapshots" / "scene_2.png").write_bytes(b"fixed")
        scope = judge_scope.plan(tmp_path, spec)
        assert scope["judges"] == ["concept", "user", "arc"] and scope["arc"] is True


class TestInnerGate:
    def test_an_inner_pass_skips_the_deploy_half_when_the_local_build_answers(self) -> None:
        cfg = _cfg().inner_loop
        out = judge_gate.inner_deploy_status(cfg, fetch=lambda url: 200)
        assert out["status"] == "skipped"
        assert judge_gate.decide(out, {})["judge"] is True

    def test_a_dead_local_build_waits(self) -> None:
        cfg = _cfg().inner_loop

        def down(url):
            raise ConnectionRefusedError

        out = judge_gate.inner_deploy_status(cfg, wait=False, fetch=down, sleep=lambda s: None)
        assert out["status"] == "not_ready"
        assert judge_gate.decide(out, {})["action"] == "wait_deploy"


def test_the_recorder_accepts_a_base_url_override() -> None:
    out = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "walkthrough" / "record_video.py"), "--help"],
        capture_output=True, text=True, cwd=REPO,
        env={**__import__("os").environ, "PYTHONPATH": str(REPO)},
    )
    import re

    assert re.search(r"--base-url\s+BASE_URL", out.stdout), out.stderr[-500:]
