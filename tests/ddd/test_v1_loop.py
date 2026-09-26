"""The v1 / backlog DDD loop: progress signal, judge scope, score reuse, gates.

Measured on five freshly-built connect-labs supply narratives
(``supply-*-2026-09-23-001``): 19 of 23 judged iterations scored 2 while the
mean cell rose 3.14 -> 3.68 and open findings fell 61 -> 21; stall detection
could never fire while mechanical findings existed; every pass re-judged every
scene; judges ran against undeployed fixes; the narrated video was rendered at a
gating score of 2. Each class below pins one fix.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from scripts.ddd import gap_walk, judge_gate, judge_scope, loop_config, progress, video_gate
from scripts.ddd.loop_config import DeployGateConfig, LoopConfig
from scripts.ddd.passes import manifest, seal
from scripts.ddd.run_pipeline import compute_auto_iterate
from scripts.ddd.schemas.models import RunState, Verdict


def _v(score: float, verdict: str = "fail") -> Verdict:
    return Verdict(
        schema_version=1,
        kind="concept",
        gate="gating",
        rubric_name="ddd-concept-eval",
        ran_at="2026-09-26T00:00:00Z",
        dimensions={},
        overall_score=score,
        overall_rule="lowest",
        verdict=verdict,
    )


def _state(**kw) -> RunState:
    return RunState(run_id="run-1", narrative_slug="supply-test-kits", **kw)


def _mech(n: int, scene: int = 1) -> dict:
    return {
        "scene": str(scene),
        "dimension": "visual_polish",
        "route": "PRODUCT",
        "fix_kind": "mechanical",
        "detail": f"Mechanical defect #{n} on the award page.",
        "fix_recommendation": f"Fix defect #{n}.",
    }


def _findings(count: int, tag: int) -> list[dict]:
    """``count`` distinct mechanical findings (tag keeps passes from plateauing)."""
    return [_mech(tag * 1000 + i, scene=i % 5 + 1) for i in range(count)]


def _dist(mean: float, caps: int) -> dict:
    return {
        "mean": mean,
        "capping_cells": [
            {"scene": str(i), "dimension": "visual_polish", "confirmed": 2.0} for i in range(caps)
        ],
    }


# ---------------------------------------------------------------------------
# D — progress signal, stall ordering
# ---------------------------------------------------------------------------


class TestProgressSignal:
    def test_measure_counts_open_findings_mean_and_confirmed_caps(self) -> None:
        dist = {
            "mean": 3.14159,
            "capping_cells": [
                {"scene": "1", "dimension": "d", "confirmed": 2.0},
                {"scene": "2", "dimension": "d", "confirmed": 3.0},  # did not reproduce
                {"scene": "3", "dimension": "d", "score": 1.0},
            ],
        }
        findings = [_mech(1), {**_mech(2), "route": "DEFER"}]
        p = progress.measure(findings, dist, 2.0)
        assert p == {
            "score": 2.0,
            "open_findings": 1,
            "mean_cell": 3.1416,
            "confirmed_caps": 2,
            "full": True,
        }

    def test_mean_move_inside_the_band_is_not_progress(self) -> None:
        a = {"score": 2.0, "open_findings": 10, "mean_cell": 3.50, "confirmed_caps": 3}
        b = {**a, "mean_cell": 3.50 + progress.MEAN_BAND / 2}
        c = {**a, "mean_cell": 3.50 + progress.MEAN_BAND * 2}
        assert progress.improved_signals([a], b) == []
        assert progress.improved_signals([a], c) == ["mean_cell"]

    def test_fewer_caps_or_findings_is_progress(self) -> None:
        a = {"score": 2.0, "open_findings": 10, "mean_cell": 3.5, "confirmed_caps": 3}
        assert progress.improved_signals([a], {**a, "confirmed_caps": 2}) == ["confirmed_caps"]
        assert progress.improved_signals([a], {**a, "open_findings": 9}) == ["open_findings"]

    def test_stall_needs_all_signals_flat_across_two_iterations(self) -> None:
        base = {"score": 2.0, "open_findings": 20, "mean_cell": 3.2, "confirmed_caps": 5}
        assert not progress.stalled([base, base])  # needs three points
        assert progress.stalled([base, base, base])
        assert not progress.stalled([base, base, {**base, "open_findings": 19}])

    @pytest.mark.parametrize(
        "configured,n,expected",
        [("auto", 8, "backlog"), ("auto", 7, "polish"), ("polish", 50, "polish"), ("backlog", 0, "backlog")],
    )
    def test_select_mode(self, configured: str, n: int, expected: str) -> None:
        assert progress.select_mode(configured, n, backlog_min_findings=8) == expected


class TestStallFiresWithMechanicalWork:
    def test_a_pinned_floor_with_a_shrinking_backlog_keeps_going(self) -> None:
        """The measured supply shape: floor 2 every pass, findings and caps falling."""
        state = _state()
        points = [(61, 3.14, 13), (40, 3.52, 3), (21, 3.68, 1)]
        for i, (n, mean, caps) in enumerate(points):
            a, reason = compute_auto_iterate(
                state, _v(2.0), _v(2.0), _findings(n, i), unattended=True,
                distribution=_dist(mean, caps),
            )
            assert a == "continue", (i, reason)
        assert [p["open_findings"] for p in state.progress_history] == [61, 40, 21]

    def test_stall_fires_even_while_mechanical_findings_remain(self) -> None:
        """Before: ``mechanical -> continue`` preceded the stall check, so a v1 run
        with fresh mechanical findings every pass could only end at HARD_CAP."""
        state = _state()
        for i in range(2):
            a, _ = compute_auto_iterate(
                state, _v(2.0), _v(2.0), _findings(20, i), unattended=True,
                distribution=_dist(3.3, 4),
            )
            assert a == "continue"
        a, reason = compute_auto_iterate(
            state, _v(2.0), _v(2.0), _findings(20, 2), unattended=True,
            distribution=_dist(3.3, 4),
        )
        assert a == "stop_max_iter", reason
        assert "stalled" in reason.lower()

    def test_progress_pays_for_a_second_concept_gate_deferral(self) -> None:
        """Score flat, but the backlog halved — the deferred pass was not wasted."""
        strategy = {
            "scene": "4",
            "dimension": "use_case_soundness",
            "route": "CONCEPT",
            "fix_kind": "redesign",
            "detail": "The coach runs on a one-line answer.",
            "fix_recommendation": "Redraft the narrative so the coach runs on a full application.",
        }
        state = _state()
        a1, _ = compute_auto_iterate(
            state, _v(2.0), _v(2.0), _findings(20, 0) + [strategy], unattended=True,
            distribution=_dist(3.1, 9),
        )
        assert a1 == "continue"
        a2, reason = compute_auto_iterate(
            state, _v(2.0), _v(2.0), _findings(10, 1) + [strategy], unattended=True,
            distribution=_dist(3.1, 9),
        )
        assert a2 == "continue", reason
        assert state.concept_gate_deferred == 2
        assert "open_findings" in reason


# ---------------------------------------------------------------------------
# B — backlog mode + judge scope cadence; convergence only on a full pass
# ---------------------------------------------------------------------------


class TestBacklogMode:
    def test_a_big_first_backlog_selects_backlog_mode_and_an_incremental_next_pass(self) -> None:
        state = _state()
        a, reason = compute_auto_iterate(
            state, _v(2.0), _v(2.0), _findings(30, 0), unattended=True, judge_full=True,
        )
        assert a == "continue"
        assert state.loop_mode == "backlog"
        assert state.next_judge_full is False
        assert "ONE batch" in reason

    def test_a_small_backlog_stays_in_polish_mode_with_full_passes(self) -> None:
        state = _state()
        compute_auto_iterate(state, _v(3.0, "warn"), _v(3.0, "warn"), _findings(3, 0), unattended=True)
        assert state.loop_mode == "polish"
        assert state.next_judge_full is True

    def test_every_nth_batch_is_judged_in_full(self) -> None:
        cfg = LoopConfig(mode="backlog", full_rejudge_every=3)
        state = _state()
        fulls = []
        judge_full = True
        for i, n in enumerate([40, 30, 20, 15, 10]):
            compute_auto_iterate(
                state, _v(2.0), _v(2.0), _findings(n, i), unattended=True,
                judge_full=judge_full, loop_config=cfg,
            )
            fulls.append(state.next_judge_full)
            judge_full = state.next_judge_full
        # after full(0): inc, inc, FULL, then inc, inc ...
        assert fulls == [False, False, True, False, False]

    def test_mode_is_only_reread_on_a_full_pass(self) -> None:
        cfg = LoopConfig(mode="auto", backlog_min_findings=8)
        state = _state()
        compute_auto_iterate(state, _v(2.0), _v(2.0), _findings(30, 0), unattended=True, loop_config=cfg)
        assert state.loop_mode == "backlog"
        compute_auto_iterate(
            state, _v(2.0), _v(2.0), _findings(3, 1), unattended=True,
            judge_full=False, loop_config=cfg,
        )
        assert state.loop_mode == "backlog"  # incremental count is not a fresh read

    def test_an_incremental_pass_can_never_declare_convergence(self) -> None:
        state = _state()
        a, reason = compute_auto_iterate(
            state, _v(4.0, "pass"), _v(4.5, "pass"), [], unattended=True, judge_full=False,
        )
        assert a == "confirm_full", reason
        assert state.next_judge_full is True
        assert state.terminal_status == "running"

    def test_a_full_pass_that_passes_is_done(self) -> None:
        state = _state()
        a, _ = compute_auto_iterate(
            state, _v(4.0, "pass"), _v(4.5, "pass"), [], unattended=True, judge_full=True,
        )
        assert a == "stop_done"


# ---------------------------------------------------------------------------
# B + C — fingerprints, scope, carry, ledger, user merge
# ---------------------------------------------------------------------------


def _write_run(run: Path, scenes: int = 3, *, render_id: str = "r1") -> Path:
    snaps = run / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)
    for i in range(1, scenes + 1):
        (snaps / f"scene_{i}.png").write_bytes(f"png-{i}".encode())
        (snaps / f"scene_{i}_page_text.json").write_text(
            json.dumps({"scene_index": str(i), "page_text": f"text {i}", "render_id": render_id})
        )
    spec = run / "spec.yaml"
    spec.write_text(
        yaml.safe_dump(
            {
                "name": "t",
                "scenes": [
                    {"title": f"Scene {i}", "narrative": f"Narration {i}."}
                    for i in range(1, scenes + 1)
                ],
            }
        )
    )
    return spec


def _judge_scene(run: Path, scene: int, *, score: int = 4, suffix: str = "") -> None:
    d = run / "passes" / "concept"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"scene_{scene}{suffix}.json"
    p.write_text(json.dumps({"scene": scene, "dimensions": {"visual_polish": {"score": score}}}))
    seal(p)


class TestFingerprints:
    def test_a_new_render_stamp_alone_does_not_change_a_scene(self, tmp_path: Path) -> None:
        spec = _write_run(tmp_path, render_id="r1")
        a = judge_scope.fingerprints(tmp_path, judge_scope.spec_scenes(spec))
        _write_run(tmp_path, render_id="r2")
        b = judge_scope.fingerprints(tmp_path, judge_scope.spec_scenes(spec))
        assert a == b

    def test_frame_bytes_page_text_and_narration_each_change_a_scene(self, tmp_path: Path) -> None:
        spec = _write_run(tmp_path)
        base = judge_scope.fingerprints(tmp_path, judge_scope.spec_scenes(spec))
        (tmp_path / "snapshots" / "scene_1.png").write_bytes(b"different")
        pt = tmp_path / "snapshots" / "scene_2_page_text.json"
        pt.write_text(json.dumps({"scene_index": "2", "page_text": "new", "render_id": "r1"}))
        raw = yaml.safe_load(spec.read_text())
        raw["scenes"][2]["narrative"] = "Reworded narration."
        spec.write_text(yaml.safe_dump(raw))
        now = judge_scope.fingerprints(tmp_path, judge_scope.spec_scenes(spec))
        assert [s for s in base if base[s] != now[s]] == ["1", "2", "3"]

    def test_run_wide_context_change_rejudges_everything(self, tmp_path: Path) -> None:
        spec = _write_run(tmp_path)
        a = judge_scope.fingerprints(tmp_path, judge_scope.spec_scenes(spec), salt="a")
        b = judge_scope.fingerprints(tmp_path, judge_scope.spec_scenes(spec), salt="b")
        assert all(a[s] != b[s] for s in a)


class TestDecideScope:
    def test_no_ledger_is_a_full_pass(self) -> None:
        s = judge_scope.decide_scope({"1": "a", "2": "b"}, None, force_full=False)
        assert s["full"] and s["rejudge"] == [1, 2] and s["reuse"] == [] and s["arc"]

    def test_only_changed_scenes_are_rejudged(self) -> None:
        ledger = {"iteration": 2, "fingerprints": {"1": "a", "2": "b", "3": "c"}}
        s = judge_scope.decide_scope({"1": "a", "2": "B", "3": "c"}, ledger, force_full=False)
        assert (s["full"], s["rejudge"], s["reuse"], s["arc"]) == (False, [2], [1, 3], True)

    def test_nothing_changed_skips_the_arc_and_says_so(self) -> None:
        ledger = {"iteration": 2, "fingerprints": {"1": "a"}}
        s = judge_scope.decide_scope({"1": "a"}, ledger, force_full=False)
        assert s["rejudge"] == [] and s["arc"] is False
        assert "NOTHING changed" in s["reason"]

    def test_force_full_overrides_the_ledger(self) -> None:
        ledger = {"iteration": 2, "fingerprints": {"1": "a"}}
        s = judge_scope.decide_scope({"1": "a"}, ledger, force_full=True)
        assert s["full"] and s["rejudge"] == [1]


class TestReuseEndToEnd:
    def test_unchanged_scenes_carry_their_sealed_passes_into_the_next_judge(
        self, tmp_path: Path
    ) -> None:
        spec = _write_run(tmp_path)
        # iteration 0: full judge of three scenes (scene 2 capped + confirmed)
        judge_scope.plan(tmp_path, spec)
        for i in (1, 2, 3):
            _judge_scene(tmp_path, i)
        _judge_scene(tmp_path, 2, score=2, suffix="_r2")
        judge_scope.record(tmp_path, spec, iteration=0)

        # iteration 1: only scene 2's frame changed
        (tmp_path / "snapshots" / "scene_2.png").write_bytes(b"fixed")
        scope = judge_scope.plan(tmp_path, spec)
        assert (scope["full"], scope["rejudge"], scope["reuse"]) == (False, [2], [1, 3])

        carried = judge_scope.carry(tmp_path)
        concept = tmp_path / "passes" / "concept"
        # scene 2's stale passes (incl. its r2 confirmation) were archived, not left
        # to collide with the new judge's seals
        assert not (concept / "scene_2.json").exists()
        assert not (concept / "scene_2_r2.json").exists()
        assert (tmp_path / "iter0-archive" / "passes" / "concept" / "scene_2_r2.json").exists()
        assert carried["reuse"] == [1, 3] and carried["rejudge"] == [2]

        _judge_scene(tmp_path, 2, score=4)  # the re-judge seals normally
        entries = manifest(tmp_path, "concept", expect=3)
        assert sorted(e["pass_id"] for e in entries) == ["scene_1", "scene_2", "scene_3"]

    def test_reused_scenes_are_restored_after_the_passes_dir_was_archived(
        self, tmp_path: Path
    ) -> None:
        spec = _write_run(tmp_path)
        judge_scope.plan(tmp_path, spec)
        for i in (1, 2, 3):
            _judge_scene(tmp_path, i)
        judge_scope.record(tmp_path, spec, iteration=0)
        import shutil

        shutil.rmtree(tmp_path / "passes")  # an orchestrator archived by moving
        (tmp_path / "snapshots" / "scene_3.png").write_bytes(b"changed")
        judge_scope.plan(tmp_path, spec)
        out = judge_scope.carry(tmp_path)
        assert sorted(out["restored"]) == [
            "scene_1.json", "scene_1.json.seal.json", "scene_2.json", "scene_2.json.seal.json",
        ]
        assert out["reuse_missing_cache"] == []
        _judge_scene(tmp_path, 3)
        assert len(manifest(tmp_path, "concept", expect=3)) == 3

    def test_a_full_pass_archives_every_prior_pass(self, tmp_path: Path) -> None:
        spec = _write_run(tmp_path)
        judge_scope.plan(tmp_path, spec)
        for i in (1, 2, 3):
            _judge_scene(tmp_path, i)
        judge_scope.record(tmp_path, spec, iteration=0)
        judge_scope.plan(tmp_path, spec, force_full=True)
        judge_scope.carry(tmp_path)
        assert list((tmp_path / "passes" / "concept").iterdir()) == []


class TestMergeUser:
    PRIOR = {
        "kind": "user_artifact",
        "dimensions": {
            "clarity": {"score": 2, "justification": "scene 3 confuses", "fix_kind": "mechanical"},
            "trust": {"score": 4, "justification": "fine"},
        },
        "per_scene": {1: {"clarity": 4, "trust": 4}, 2: {"clarity": 3, "trust": 4}, 3: {"clarity": 2, "trust": 4}},
        "overall_score": 2,
        "verdict": "fail",
    }

    def test_the_floor_on_a_reused_scene_keeps_its_own_justification(self) -> None:
        partial = {
            "kind": "user_artifact",
            "dimensions": {"clarity": {"score": 4, "justification": "scene 2 now clear"}, "trust": {"score": 4}},
            "per_scene": {2: {"clarity": 4, "trust": 4}},
        }
        merged = judge_scope.merge_user(self.PRIOR, partial, reuse=[1, 3])
        assert set(merged["per_scene"]) == {1, 2, 3}
        assert merged["dimensions"]["clarity"]["score"] == 2
        assert merged["dimensions"]["clarity"]["justification"] == "scene 3 confuses"
        assert merged["overall_score"] == 2 and merged["verdict"] == "fail"

    def test_the_floor_on_a_rejudged_scene_uses_the_new_verdict(self) -> None:
        partial = {
            "dimensions": {"clarity": {"score": 1, "justification": "scene 3 broke"}, "trust": {"score": 4}},
            "per_scene": {3: {"clarity": 1, "trust": 4}},
        }
        merged = judge_scope.merge_user(self.PRIOR, partial, reuse=[1, 2])
        assert merged["dimensions"]["clarity"] == {"score": 1, "justification": "scene 3 broke"}

    def test_a_reused_scene_without_a_prior_row_is_refused(self) -> None:
        with pytest.raises(ValueError, match="reused scene 9"):
            judge_scope.merge_user(self.PRIOR, {"per_scene": {}}, reuse=[9])


# ---------------------------------------------------------------------------
# E — deploy readiness + deterministic hard-fail gate
# ---------------------------------------------------------------------------


class TestDeployGate:
    CFG = DeployGateConfig(
        health_url="https://labs.example/health/", sha_field="build.git_sha",
        samples=3, interval_seconds=0, attempts=2, retry_seconds=0,
    )

    @staticmethod
    def _fetcher(shas: list[str]):
        it = iter(shas)
        return lambda url: {"build": {"git_sha": next(it)}}

    def test_ready_only_when_every_sample_reports_the_fix_sha(self) -> None:
        out = judge_gate.check_deploy(
            self.CFG, "abcdef1234", fetch=self._fetcher(["abcdef1"] * 3), sleep=lambda s: None
        )
        assert out["status"] == "ready"

    def test_one_old_task_mid_rollout_is_not_ready(self) -> None:
        shas = ["abcdef1", "0ld0ld0", "abcdef1"] * 2
        out = judge_gate.check_deploy(
            self.CFG, "abcdef1234", fetch=self._fetcher(shas), sleep=lambda s: None
        )
        assert out["status"] == "not_ready"
        assert len(out["rounds"]) == 2

    def test_a_retry_round_can_turn_ready(self) -> None:
        shas = ["0ld0ld0", "abcdef1", "abcdef1", "abcdef1", "abcdef1", "abcdef1"]
        out = judge_gate.check_deploy(
            self.CFG, "abcdef1234", fetch=self._fetcher(shas), sleep=lambda s: None
        )
        assert out["status"] == "ready"

    def test_network_errors_are_not_ready_not_a_crash(self) -> None:
        def boom(url):
            raise OSError("down")

        out = judge_gate.check_deploy(self.CFG, "abcdef1", fetch=boom, sleep=lambda s: None)
        assert out["status"] == "not_ready"

    def test_unconfigured_or_no_sha_is_skipped(self) -> None:
        assert judge_gate.check_deploy(DeployGateConfig(), "abc")["status"] == "skipped"
        assert judge_gate.check_deploy(self.CFG, None)["status"] == "skipped"

    @pytest.mark.parametrize(
        "reported,expected,ok",
        [("abcdef1", "abcdef1234", True), ("abcdef1234", "abcdef1", True), ("abc", "abcdef1", False), ("1234567", "abcdef1", False)],
    )
    def test_sha_prefix_match(self, reported: str, expected: str, ok: bool) -> None:
        assert judge_gate.sha_matches(reported, expected) is ok


class TestJudgeGateDecision:
    def test_not_deployed_waits(self) -> None:
        out = judge_gate.decide({"status": "not_ready", "reason": "old"}, {})
        assert (out["judge"], out["action"]) == (False, "wait_deploy")

    @pytest.mark.parametrize(
        "lens,verdict",
        [("regression_guard", "fail"), ("visual_geometry", "fail"), ("render_pacing_audit", "recording_bug")],
    )
    def test_a_deterministic_hard_fail_skips_the_judges(self, lens: str, verdict: str) -> None:
        out = judge_gate.decide({"status": "ready"}, {lens: verdict, "data_fidelity": "warn"})
        assert (out["judge"], out["action"]) == (False, "fix_render")
        assert out["hard_fails"] == [f"{lens}={verdict}"]

    def test_warnings_and_a_ready_deploy_judge(self) -> None:
        out = judge_gate.decide(
            {"status": "ready"}, {"regression_guard": "warn", "visual_geometry": "pass"}
        )
        assert (out["judge"], out["action"]) == (True, "judge")


# ---------------------------------------------------------------------------
# A — gap walk
# ---------------------------------------------------------------------------


class TestGapWalk:
    def _doc(self, gaps: list[dict], covered: list[int]) -> dict:
        return {
            "covered": [{"scene": s, "evidence": [f"views.py:{s}"]} for s in covered],
            "gaps": gaps,
        }

    def _gap(self, scene: int, kind: str = "build") -> dict:
        return {
            "scene": scene,
            "claim": "Hauwa awards the quote",
            "missing_capability": "no award route",
            "evidence": ["urls.py"],
            "kind": kind,
        }

    def test_no_gaps_renders(self) -> None:
        doc = self._doc([], [1, 2])
        assert gap_walk.validate(doc, scene_count=2) == []
        assert gap_walk.decide(doc)["action"] == "render"

    def test_a_buildable_gap_builds_instead_of_rendering(self) -> None:
        doc = self._doc([self._gap(2)], [1])
        assert gap_walk.validate(doc, scene_count=2) == []
        out = gap_walk.decide(doc)
        assert (out["action"], out["open_gaps"], out["scenes"]) == ("build", 1, [2])

    def test_a_decision_gap_opens_the_concept_gate(self) -> None:
        doc = self._doc([self._gap(2), self._gap(3, "decision")], [1])
        assert gap_walk.decide(doc)["action"] == "decide"

    def test_a_walk_that_skips_a_scene_is_invalid(self) -> None:
        problems = gap_walk.validate(self._doc([], [1]), scene_count=3)
        assert any("[2, 3]" in p for p in problems)

    def test_a_gap_without_evidence_is_invalid(self) -> None:
        gap = {**self._gap(1), "evidence": []}
        assert any("evidence" in p for p in gap_walk.validate(self._doc([gap], []), scene_count=1))

    def test_an_unknown_gap_kind_is_invalid(self) -> None:
        gap = self._gap(1, "maybe")
        assert any("kind" in p for p in gap_walk.validate(self._doc([gap], []), scene_count=1))

    def test_cli_exit_codes(self, tmp_path: Path, capsys) -> None:
        p = tmp_path / "gaps.json"
        p.write_text(json.dumps(self._doc([], [1])))
        assert gap_walk._main(["check", str(p)]) == 0
        p.write_text(json.dumps(self._doc([self._gap(1)], [])))
        assert gap_walk._main(["check", str(p)]) == 1
        p.write_text(json.dumps({"gaps": "nope"}))
        assert gap_walk._main(["check", str(p)]) == 2


# ---------------------------------------------------------------------------
# F — video only after convergence
# ---------------------------------------------------------------------------


class TestVideoGate:
    def test_an_unconverged_run_is_refused(self) -> None:
        state = _state(auto_iterate_next_action="continue", score_history=[2.0, 2.0])
        out = video_gate.check(state)
        assert out["allowed"] is False
        assert "--allow-unconverged" in out["reason"]

    def test_the_override_is_explicit_and_labelled(self) -> None:
        out = video_gate.check(_state(), allow_unconverged=True)
        assert out["allowed"] and out["override"]
        assert out["reason"].startswith("OVERRIDE")

    @pytest.mark.parametrize(
        "kw",
        [
            {"auto_iterate_next_action": "stop_done"},
            {"terminal_status": "converged_clean"},
            {"phase": "uploaded"},
        ],
    )
    def test_a_converged_run_is_allowed(self, kw: dict) -> None:
        assert video_gate.check(_state(**kw))["allowed"] is True

    def test_partial_or_incremental_convergence_is_not_convergence(self) -> None:
        assert not video_gate.check(
            _state(auto_iterate_next_action="stop_done", scene_filter="2")
        )["allowed"]
        assert not video_gate.check(
            _state(terminal_status="converged_clean", last_judge_full=False)
        )["allowed"]

    def test_no_run_is_refused(self) -> None:
        assert video_gate.check(None)["allowed"] is False

    def test_newest_run_for_slug(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("CANOPY_DDD_RUNS_DIR", str(tmp_path / "runs"))
        from scripts.ddd.runstate import new_run, save, load

        ddd = tmp_path / "ddd"
        ddd.mkdir()
        rid = new_run("chlorine", ddd_dir=ddd)
        st = load(rid, ddd_dir=ddd)
        st.auto_iterate_next_action = "stop_done"
        save(st, ddd_dir=ddd)
        found = video_gate.newest_run_for_slug("chlorine", ddd_dir=ddd)
        assert found is not None and found.run_id == rid
        assert video_gate.newest_run_for_slug("other", ddd_dir=ddd) is None


# ---------------------------------------------------------------------------
# config + one-command assembly
# ---------------------------------------------------------------------------


class TestConfig:
    def test_defaults_and_parsing(self, tmp_path: Path) -> None:
        assert loop_config.load(tmp_path) == loop_config.DDDConfig()
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "workspace": "connect",
                    "loop": {"mode": "backlog", "full_rejudge_every": 2},
                    "deploy_gate": {"health_url": "https://x/health/", "samples": 4},
                }
            )
        )
        cfg = loop_config.load(tmp_path)
        assert cfg.loop == LoopConfig(mode="backlog", backlog_min_findings=8, full_rejudge_every=2)
        assert cfg.deploy_gate.enabled and cfg.deploy_gate.samples == 4

    def test_malformed_values_fall_back(self) -> None:
        cfg = loop_config.parse(
            {"loop": {"mode": "yolo", "full_rejudge_every": 0}, "deploy_gate": {"samples": "x"}}
        )
        assert cfg.loop == LoopConfig()
        assert not cfg.deploy_gate.enabled and cfg.deploy_gate.samples == 5


class TestAssemble:
    def test_one_command_assembles_decides_and_records_the_ledger(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("CANOPY_DDD_RUNS_DIR", str(tmp_path / "runs"))
        from scripts.ddd import assemble
        from scripts.ddd.runstate import load, new_run, run_dir_for

        ddd = tmp_path / "ddd"
        ddd.mkdir()
        (ddd / "config.yaml").write_text(yaml.safe_dump({"loop": {"mode": "backlog"}}))
        rid = new_run("kits", ddd_dir=ddd)
        run = run_dir_for(rid, ddd_dir=ddd)
        spec = _write_run(run)
        judge_scope.plan(run, spec)
        base = {"schema_version": 1, "overall_rule": "lowest", "ran_at": "t", "rubric_name": "r"}
        (run / "verdict-concept.yaml").write_text(
            yaml.safe_dump(
                {
                    **base,
                    "kind": "concept",
                    "dimensions": {"visual_polish": {"score": 2, "weight": 1.0}},
                    "overall_score": 2,
                    "verdict": "fail",
                    "distribution": _dist(3.4, 2),
                }
            )
        )
        (run / "verdict-user.yaml").write_text(
            yaml.safe_dump(
                {
                    **base,
                    "kind": "user_artifact",
                    "dimensions": {"clarity": {"score": 3, "weight": 1.0}},
                    "overall_score": 3,
                    "verdict": "warn",
                }
            )
        )
        (run / "design_findings.json").write_text(json.dumps(_findings(5, 0)))
        (run / "arc_findings.json").write_text(json.dumps(_findings(4, 1)))

        out = assemble.assemble(rid, spec=str(spec), ddd_dir=ddd)
        assert out["auto_iterate_next_action"] == "continue"
        assert out["open_findings"] == 9  # arc findings reach the decision
        assert out["loop_mode"] == "backlog" and out["next_judge_full"] is False
        assert out["progress"]["confirmed_caps"] == 2
        state = load(rid, ddd_dir=ddd)
        assert state.phase == "judged" and state.progress_history
        assert (run / "judge-cache" / "index.json").exists()
