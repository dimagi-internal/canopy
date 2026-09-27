"""DDD loop, round 3 — what the first live v1-mode run taught.

Evidence: connect-labs ``supply-sophie-rutf-2026-09-26-001`` (canopy 0.2.528),
five judged iterations. It stopped at iteration 4 as ``stop_concept_change`` /
``diverging`` from ONE recipe-caused cap while the mean moved 3.71 -> 3.60 and
findings 16 -> 20 (M17); a fixer stalled 4 h on a shared test DB with no
timeout; AWS SSO expired mid-run (M15); a failed render uploaded a stale deck
(M16); an unattended ``defer`` halted all seven scenes over a question about a
few; three canopy versions were in play at once (M7/M18). Each class pins one.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest
import yaml

from scripts.ddd import fix_scope, loop_config, parking, pin, preflight, progress, render_check, watchdog
from scripts.ddd.run_pipeline import classify_termination, compute_auto_iterate
from scripts.ddd.schemas.models import RunState, Verdict


def _v(score: float, verdict: str = "fail") -> Verdict:
    return Verdict(
        schema_version=1,
        kind="concept",
        gate="gating",
        rubric_name="ddd-concept-eval",
        ran_at="2026-09-27T00:00:00Z",
        dimensions={},
        overall_score=score,
        overall_rule="lowest",
        verdict=verdict,
    )


def _state(**kw) -> RunState:
    return RunState(run_id="supply-sophie-rutf-2026-09-26-001", narrative_slug="supply-sophie-rutf", **kw)


# The iteration-4 cap, verbatim from the run's design_findings.json.
RECIPE_CAP_FINDING = {
    "scene": "4",
    "dimension": "motion_friction",
    "route": "PRODUCT",
    "fix_kind": "mechanical",
    "severity": "medium",
    "detail": (
        "The narration is about all three blocked quote cards, which would fit in one "
        "viewport, yet the captured frame is scrolled to the page top so the Tyonex card "
        "is clipped (fits-but-mis-framed, max 2)."
    ),
    "fix_recommendation": (
        "Before the snapshot, scroll_to the 'Comparison —' heading aligned to the top of "
        "the viewport so all three cards are fully in frame."
    ),
}

WHOLE_ARC_STRATEGY = {
    "scene": "1-7 the whole story",
    "dimension": "arc_shape",
    "route": "CONCEPT",
    "fix_kind": "redesign",
    "finding_class": "strategy",
    "detail": "Scenes 2-3 prove the tagline on round 1; scenes 4-7 never use it.",
    "fix_recommendation": "Redraft the narrative so round 2 carries the provenance story.",
}


def _mech(scene: int, n: int = 0) -> dict:
    return {
        "scene": str(scene),
        "dimension": "visual_polish",
        "route": "PRODUCT",
        "fix_kind": "mechanical",
        "detail": f"Column header {n} is blank on scene {scene}.",
        "fix_recommendation": f"Label column {n}.",
    }


def _strategy(scene: str) -> dict:
    return {
        "scene": scene,
        "dimension": "use_case_soundness",
        "route": "CONCEPT",
        "fix_kind": "redesign",
        "detail": "The scale shown is three rows; value is never felt at that scale.",
        "fix_recommendation": "Redraft the narrative to seed a program-sized backlog.",
    }


def _dist(mean: float, caps: list[tuple[str, str, float]]) -> dict:
    return {
        "mean": mean,
        "capping_cells": [
            {"scene": s, "dimension": d, "confirmed": c, "draws": [c, c, c]} for s, d, c in caps
        ],
    }


def _iteration4_state() -> RunState:
    """run_state as it stood before iteration 4's assemble (from the run's ledger)."""
    return _state(
        iteration=4,
        score_history=[2.0, 2.0, 3.0, 3.0],
        concept_gate_deferred=3,
        progress_history=[
            {"score": 2.0, "open_findings": 32, "mean_cell": 3.333, "confirmed_caps": 2, "full": True},
            {"score": 2.0, "open_findings": 24, "mean_cell": 3.5, "confirmed_caps": 2, "full": False},
            {"score": 3.0, "open_findings": 20, "mean_cell": 3.619, "confirmed_caps": 0, "full": False},
            {"score": 3.0, "open_findings": 16, "mean_cell": 3.71, "confirmed_caps": 0, "full": True},
        ],
        loop_mode="backlog",
    )


def _iteration4_findings(n_mech: int = 17) -> list[dict]:
    # 20 open findings: the recipe cap, the whole-arc strategy finding, and
    # mechanical work on the SAME scenes the strategy finding names.
    return [RECIPE_CAP_FINDING, WHOLE_ARC_STRATEGY] + [_mech(i % 7 + 1, i) for i in range(n_mech)] + [
        {**_mech(2, 99), "route": "DEFER"}
    ]


# ---------------------------------------------------------------------------
# Item 1 — M17: recipe caps are fixed and re-judged before any gate/diverging
# ---------------------------------------------------------------------------


class TestFixScope:
    def test_the_iteration4_cap_is_a_recipe_fix(self) -> None:
        assert fix_scope.classify(RECIPE_CAP_FINDING) == fix_scope.RECIPE

    def test_explicit_fix_scope_wins(self) -> None:
        assert fix_scope.classify({**RECIPE_CAP_FINDING, "fix_scope": "product"}) == "product"
        assert fix_scope.classify({**_mech(1), "fix_scope": "recipe"}) == "recipe"

    def test_scripting_tag_marks_a_recipe_fix_on_any_dimension(self) -> None:
        f = {**_mech(5), "fix_recommendation": "Park the hover on blank margin. [SCRIPTING, mechanical]"}
        assert fix_scope.classify(f) == "recipe"

    def test_a_product_change_is_never_a_recipe_fix(self) -> None:
        f = {
            **RECIPE_CAP_FINDING,
            "fix_recommendation": "Add a button above the fold so the cards are in frame before the snapshot.",
        }
        assert fix_scope.classify(f) == "product"

    def test_no_evidence_is_unknown_not_recipe(self) -> None:
        assert fix_scope.classify(_mech(3)) == "unknown"
        assert fix_scope.classify({**RECIPE_CAP_FINDING, "fix_recommendation": "Make it faster."}) == "unknown"

    def test_recipe_caps_needs_a_confirmed_cap_with_only_recipe_findings(self) -> None:
        dist = _dist(3.6, [("4", "motion_friction", 2.0)])
        assert [c["scene"] for c in fix_scope.recipe_caps(dist, [RECIPE_CAP_FINDING])] == ["4"]
        # "4: title" scene form matches the bare cell scene
        titled = {**RECIPE_CAP_FINDING, "scene": "4: Sophie reads the one thing each quote is missing"}
        assert fix_scope.recipe_caps(dist, [titled])
        # a cell that also carries a product finding is not lifted by a recipe edit
        product = {**RECIPE_CAP_FINDING, "fix_recommendation": "The page should show all cards.", "detail": "x"}
        assert fix_scope.recipe_caps(dist, [RECIPE_CAP_FINDING, product]) == []
        # a cell that did not reproduce (confirmed 3) is not a cap
        assert fix_scope.recipe_caps(_dist(3.6, [("4", "motion_friction", 3.0)]), [RECIPE_CAP_FINDING]) == []
        # a cap with no finding attached is not claimed
        assert fix_scope.recipe_caps(_dist(3.6, [("5", "motion_friction", 2.0)]), [RECIPE_CAP_FINDING]) == []


class TestRecipeCapPreemptsTheGate:
    def test_iteration4_replayed_rejudges_scene_4_instead_of_opening_the_gate(self) -> None:
        state = _iteration4_state()
        dist = _dist(3.595, [("4", "motion_friction", 2.0)])
        action, reason = compute_auto_iterate(
            state, _v(2.0), _v(3.0), _iteration4_findings(), unattended=True, distribution=dist
        )
        assert action == "rejudge_scenes", reason
        assert state.recipe_rejudge["scenes"] == [4]
        assert state.recipe_rejudge["status"] == "pending"
        assert state.recipe_rejudge["deferred_action"] == "stop_concept_change"
        assert state.terminal_status == "running"
        # the same iteration is re-assessed: no history point is left behind
        assert state.score_history == [2.0, 2.0, 3.0, 3.0]
        assert len(state.progress_history) == 4
        assert state.concept_gate_deferred == 3

    def test_the_reassessment_decides_once_the_scene_was_rejudged(self) -> None:
        state = _iteration4_state()
        dist = _dist(3.595, [("4", "motion_friction", 2.0)])
        compute_auto_iterate(state, _v(2.0), _v(3.0), _iteration4_findings(), unattended=True, distribution=dist)
        # Re-render + re-judge came back with the cap STILL there: no second detour.
        action, _ = compute_auto_iterate(
            state, _v(2.0), _v(3.0), _iteration4_findings(), unattended=True, distribution=dist
        )
        assert action == "stop_concept_change"
        assert state.recipe_rejudge["status"] == "done"
        assert len(state.progress_history) == 5

    def test_a_non_terminal_decision_is_not_detoured(self) -> None:
        """A `continue` batch carries the recipe fix anyway — no extra re-judge."""
        state = _state()
        dist = _dist(3.3, [("4", "motion_friction", 2.0)])
        action, _ = compute_auto_iterate(
            state, _v(2.0), _v(2.0), [RECIPE_CAP_FINDING, _mech(2)], unattended=True, distribution=dist
        )
        assert action == "continue"
        assert state.recipe_rejudge is None

    def test_a_product_cap_still_opens_the_gate(self) -> None:
        state = _iteration4_state()
        product_cap = {**RECIPE_CAP_FINDING, "fix_recommendation": "The page should keep all cards above the fold."}
        dist = _dist(3.595, [("4", "motion_friction", 2.0)])
        action, _ = compute_auto_iterate(
            state, _v(2.0), _v(3.0), [product_cap, WHOLE_ARC_STRATEGY] + [_mech(i % 7 + 1, i) for i in range(17)],
            unattended=True, distribution=dist,
        )
        assert action == "stop_concept_change"


class TestDivergingNeedsAMultiSignalDecline:
    def test_iteration4_is_not_diverging(self) -> None:
        """Floor 3 -> 2 from one cap, mean -0.11 (inside the band): not a decline."""
        state = _iteration4_state()
        state.recipe_rejudge = {"iteration": 4, "scenes": [4], "status": "done"}
        dist = _dist(3.595, [("4", "motion_friction", 2.0)])
        action, _ = compute_auto_iterate(
            state, _v(2.0), _v(3.0), _iteration4_findings(), unattended=True, distribution=dist
        )
        assert action == "stop_concept_change"
        assert state.terminal_status == "stopped_not_converged"

    def test_floor_mean_and_findings_all_falling_is_diverging(self) -> None:
        hist = [
            {"score": 3.0, "open_findings": 16, "mean_cell": 3.71},
            {"score": 2.0, "open_findings": 25, "mean_cell": 3.40},
        ]
        assert progress.declined(hist)
        out = classify_termination(
            "stop_max_iter", converged=False, score_history=[3.0, 2.0], progress_history=hist
        )
        assert out["status"] == "diverging"

    @pytest.mark.parametrize(
        "point",
        [
            {"score": 2.0, "open_findings": 25, "mean_cell": 3.60},  # mean inside band
            {"score": 2.0, "open_findings": 18, "mean_cell": 3.40},  # findings inside band
            {"score": 2.6, "open_findings": 25, "mean_cell": 3.40},  # floor inside band
        ],
    )
    def test_any_signal_inside_its_band_is_not_diverging(self, point: dict) -> None:
        hist = [{"score": 3.0, "open_findings": 16, "mean_cell": 3.71}, point]
        assert not progress.declined(hist)
        out = classify_termination(
            "stop_max_iter", converged=False, score_history=[3.0, point["score"]], progress_history=hist
        )
        assert out["status"] == "stopped_not_converged"

    def test_legacy_runs_without_progress_fall_back_to_the_score_trend(self) -> None:
        out = classify_termination("stop_max_iter", converged=False, score_history=[4.0, 3.0, 2.0])
        assert out["status"] == "diverging"


# ---------------------------------------------------------------------------
# Item 6 — a pending decision parks only the scenes it affects
# ---------------------------------------------------------------------------


class TestParking:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("4", {4}),
            (4, {4}),
            ("4: Sophie reads 3 quotes", {4}),
            ("2-3 round 1 audit vs 4-7 round 2 award", {2, 3, 4, 5, 6, 7}),
            ("1/3/7 overview; 4/5 comparison", {1, 3, 4, 5, 7}),
            ("1 opening vs 7 close", {1, 7}),
            ("5 A supplier answers, and the quote joins the ranking", {5}),
            ("", set()),
        ],
    )
    def test_scene_refs(self, raw, expected) -> None:
        assert parking.scene_refs(raw) == expected

    def test_a_decision_on_one_scene_parks_it_and_the_loop_keeps_going(self) -> None:
        state = _state(concept_gate_deferred=1, score_history=[2.0])  # deferral spent
        action, reason = compute_auto_iterate(
            state, _v(2.0), _v(2.0), [_strategy("6"), _mech(2), _mech(3)], unattended=True
        )
        assert action == "park_and_continue", reason
        assert state.park_request["scenes"] == [6]
        assert state.terminal_status == "running"

    def test_a_decision_about_every_scene_still_stops(self) -> None:
        state = _state(concept_gate_deferred=1, score_history=[2.0])
        action, _ = compute_auto_iterate(
            state, _v(2.0), _v(2.0), [_strategy("1-3 all of it"), _mech(2), _mech(3)], unattended=True
        )
        assert action == "stop_concept_change"
        assert state.park_request is None

    def test_parked_scenes_findings_are_withheld_and_the_rest_continue(self) -> None:
        state = _state(concept_gate_deferred=1, score_history=[2.0])
        compute_auto_iterate(state, _v(2.0), _v(2.0), [_strategy("6"), _mech(2)], unattended=True)
        entry = parking.park(state, review_id="rev-1", review_url="/review/rev-1/")
        assert entry["scenes"] == [6] and entry["status"] == "pending"
        assert state.park_request is None
        # next pass: the strategy question is still open, scene 6 has a mechanical
        # finding too, and scene 2 has fresh work -> keep going, scene 6 withheld
        action, reason = compute_auto_iterate(
            state, _v(3.0), _v(3.0), [_strategy("6"), _mech(6, 1), _mech(2, 2)], unattended=True
        )
        assert action == "continue", reason
        assert "PARKED" in reason
        by_scene = {f["scene"]: f for f in state.findings if f["fix_kind"] == "mechanical"}
        assert by_scene["6"].get("parked") is True
        assert "parked" not in by_scene["2"]

    def test_only_parked_work_left_stops(self) -> None:
        state = _state(concept_gate_deferred=1, score_history=[2.0])
        parking.park(state, review_id="rev-1", scenes=[6])
        action, _ = compute_auto_iterate(
            state, _v(3.0), _v(3.0), [_strategy("6"), _mech(6, 1)], unattended=True
        )
        assert action == "stop_concept_change"

    def test_poll_unparks_a_resolved_decision_and_leaves_pending_ones(self) -> None:
        state = _state()
        parking.park(state, review_id="a", scenes=[6])
        parking.park(state, review_id="b", scenes=[2])
        answers = {"a": {"status": "resolved", "response_json": {"decision": "redraft"}}, "b": {"status": "pending"}}
        resolved = parking.poll(state, lambda rid: answers[rid])
        assert [r["review_id"] for r in resolved] == ["a"]
        assert parking.parked_scenes(state) == {2}

    def test_a_fetch_error_never_unparks(self) -> None:
        state = _state()
        parking.park(state, review_id="a", scenes=[6])

        def boom(_rid):
            raise OSError("canopy-web down")

        assert parking.poll(state, boom) == []
        assert parking.parked_scenes(state) == {6}

    def test_convergence_still_counts_parked_scenes(self) -> None:
        """Parked scenes are never excluded: a passing verdict converges as usual,
        a failing one (whatever scene fails) does not."""
        failing = _state()
        parking.park(failing, review_id="a", scenes=[6])
        action, _ = compute_auto_iterate(failing, _v(2.0), _v(4.5, "pass"), [_mech(2)], unattended=True)
        assert action == "continue"
        passing = _state()
        parking.park(passing, review_id="a", scenes=[6])
        action, _ = compute_auto_iterate(passing, _v(4.5, "pass"), _v(4.5, "pass"), [], unattended=True)
        assert action == "stop_done"


# ---------------------------------------------------------------------------
# Item 5a — watchdog
# ---------------------------------------------------------------------------


class TestWatchdog:
    NOW = 1_800_000_000.0

    def _entry(self, started_ago_min: float, beat_ago_min: float, timeout=45.0, heartbeat=15.0) -> dict:
        state = _state()
        watchdog.start(state, "fixer:B3", timeout_minutes=timeout, heartbeat_minutes=heartbeat,
                       now=self.NOW - started_ago_min * 60)
        entry = state.steps["fixer:B3"]
        entry["last_beat"] = watchdog._iso(self.NOW - beat_ago_min * 60)
        return entry

    def test_running_within_budget_and_heartbeat(self) -> None:
        assert watchdog.evaluate(self._entry(20, 5), now=self.NOW)["status"] == "running"

    def test_total_budget_spent_is_timed_out(self) -> None:
        out = watchdog.evaluate(self._entry(50, 1), now=self.NOW)
        assert out["status"] == "timed_out" and "budget" in out["reason"]

    def test_silence_past_the_heartbeat_limit_is_timed_out(self) -> None:
        """B3: the fixer went idle on a locked test DB — 4 hours with no signal."""
        out = watchdog.evaluate(self._entry(20, 16), now=self.NOW)
        assert out["status"] == "timed_out" and "heartbeat" in out["reason"]

    def test_a_touched_heartbeat_file_keeps_it_alive(self) -> None:
        entry = self._entry(20, 16)
        assert watchdog.evaluate(entry, now=self.NOW, heartbeat_mtime=self.NOW - 60)["status"] == "running"

    def test_finished_steps_report_as_recorded_and_finish_validates(self) -> None:
        state = _state()
        watchdog.start(state, "render", timeout_minutes=20, heartbeat_minutes=15)
        watchdog.finish(state, "render", "ok")
        assert watchdog.check(state, "render")["status"] == "ok"
        with pytest.raises(ValueError):
            watchdog.finish(state, "render", "running")

    def test_run_command_kills_a_hung_child(self) -> None:
        t0 = time.monotonic()
        out = watchdog.run_command([sys.executable, "-c", "import time; time.sleep(30)"], timeout_s=0.5, poll_s=0.1)
        assert out["status"] == "timed_out"
        assert out["exit_code"] == watchdog.TIMED_OUT_EXIT
        assert time.monotonic() - t0 < 15

    def test_run_command_reports_ok_and_failed(self) -> None:
        assert watchdog.run_command([sys.executable, "-c", "pass"], timeout_s=30, poll_s=0.05)["status"] == "ok"
        bad = watchdog.run_command([sys.executable, "-c", "raise SystemExit(3)"], timeout_s=30, poll_s=0.05)
        assert bad["status"] == "failed" and bad["exit_code"] == 3

    def test_timeouts_config(self) -> None:
        cfg = loop_config.parse(
            {"timeouts": {"default_minutes": 30, "heartbeat_minutes": 10, "fixer_minutes": 60, "bogus": 5}}
        ).timeouts
        assert cfg.for_step("fixer:B3") == 60
        assert cfg.for_step("render") == 30
        assert cfg.heartbeat_minutes == 10
        assert loop_config.parse({}).timeouts.for_step("anything") == 45


# ---------------------------------------------------------------------------
# Item 5b — auth preflight
# ---------------------------------------------------------------------------


class TestAuthPreflight:
    def _cfg(self) -> loop_config.AuthPreflightConfig:
        return loop_config.parse(
            {
                "auth_preflight": {
                    "commands": [
                        {"name": "aws-labs", "run": "aws sts get-caller-identity --profile labs"},
                        "gh auth status",
                    ]
                }
            }
        ).auth_preflight

    def test_no_commands_is_skipped(self) -> None:
        assert preflight.check(loop_config.parse({}).auth_preflight)["status"] == "skipped"

    def test_the_first_dead_credential_is_named_and_stops_the_check(self) -> None:
        ran: list[str] = []

        def runner(cmd: str, timeout: float):
            ran.append(cmd)
            return (255, "Token has expired and refresh failed") if "aws" in cmd else (0, "")

        out = preflight.check(self._cfg(), runner=runner)
        assert out["status"] == "failed"
        assert out["credential"] == "aws-labs"
        assert "Token has expired" in out["reason"]
        assert ran == ["aws sts get-caller-identity --profile labs"]  # gh never ran

    def test_all_live_is_ok_and_string_commands_get_a_name(self) -> None:
        cfg = self._cfg()
        assert cfg.commands[1].name == "gh auth status"
        assert preflight.check(cfg, runner=lambda c, t: (0, ""))["status"] == "ok"

    def test_a_missing_binary_is_a_failure_not_a_crash(self) -> None:
        cfg = loop_config.parse({"auth_preflight": {"commands": ["definitely-not-a-binary-xyz --v"]}}).auth_preflight
        out = preflight.check(cfg)
        assert out["status"] == "failed" and out["checked"][0]["exit_code"] == 127


# ---------------------------------------------------------------------------
# Item 5c — never upload from a failed or stale render (M16)
# ---------------------------------------------------------------------------


def _fake_render(run_dir: Path, scenes=(1, 2)) -> None:
    (run_dir / "snapshots").mkdir(parents=True, exist_ok=True)
    (run_dir / "walkthrough-run-data.json").write_text(json.dumps({"scenes_run": list(scenes)}))
    (run_dir / "run-report.json").write_text("{}")
    for n in scenes:
        (run_dir / "snapshots" / f"scene_{n}.png").write_bytes(b"png")


class TestRenderCheck:
    def test_fresh_successful_render_may_upload(self, tmp_path: Path) -> None:
        since = time.time() - 1
        _fake_render(tmp_path)
        assert render_check.check(tmp_path, since=since, exit_code=0)["ok"]

    def test_a_failed_recorder_blocks_the_upload_even_with_fresh_files(self, tmp_path: Path) -> None:
        since = time.time() - 1
        _fake_render(tmp_path)
        out = render_check.check(tmp_path, since=since, exit_code=1)
        assert not out["ok"] and "exited 1" in out["reason"]

    def test_the_previous_iterations_frames_are_stale(self, tmp_path: Path) -> None:
        """M16: the wrapper said 0, the recorder had failed, the files were last iteration's."""
        _fake_render(tmp_path)
        old = time.time() - 3600
        os.utime(tmp_path / "snapshots" / "scene_2.png", (old, old))
        out = render_check.check(tmp_path, since=time.time() - 60, exit_code=0)
        assert not out["ok"] and "scene_2.png is stale" in out["reason"]

    def test_a_missing_manifest_blocks(self, tmp_path: Path) -> None:
        out = render_check.check(tmp_path, since=time.time() - 60)
        assert not out["ok"] and "walkthrough-run-data.json missing" in out["reason"]

    def test_upload_refuses_a_run_whose_current_render_failed(self, tmp_path: Path, monkeypatch) -> None:
        import scripts.ddd.runstate as rs
        from scripts.ddd import upload

        monkeypatch.setattr(rs, "_resolve_ddd_dir", lambda: tmp_path)
        monkeypatch.delenv("DDD_ALLOW_STALE_RENDER", raising=False)
        run_id = "smart-routing-2026-01-01-001"
        run_dir = tmp_path / "runs" / run_id
        run_dir.mkdir(parents=True)
        state = RunState(
            run_id=run_id, narrative_slug="smart-routing", iteration=3,
            narrative_review_id="11111111-1111-1111-1111-111111111111",
            steps={"render_check": {"iteration": 3, "ok": False, "reason": "render exited 1"}},
        )
        (run_dir / "run_state.yaml").write_text(yaml.safe_dump(state.model_dump()))
        with pytest.raises(upload.StaleRenderError):
            upload.upload_run(run_id, video_path="/dev/null", release=False,
                              _upload=lambda *a, **k: "u", _narrative_check=lambda *a, **k: True)


# ---------------------------------------------------------------------------
# Item 7 — version pinning (M7 / M18)
# ---------------------------------------------------------------------------


class TestVersionPin:
    def test_new_runs_are_pinned_to_the_runtime_that_started_them(self, tmp_path: Path) -> None:
        from scripts.ddd import runstate

        run_id = runstate.new_run("smart-routing", ddd_dir=tmp_path)
        state = runstate.load(run_id, ddd_dir=tmp_path)
        assert state.plugin_version == pin.runtime_version(pin.current_root())
        assert state.runtime_root == str(pin.current_root())

    def test_ensure_is_idempotent(self, tmp_path: Path) -> None:
        state = _state(plugin_version="0.2.528", runtime_root=str(tmp_path))
        assert pin.ensure(state)["fresh"] is False
        assert state.plugin_version == "0.2.528"

    def test_skill_text_from_another_version_is_a_loud_warning(self) -> None:
        state = _state(plugin_version="0.2.528", runtime_root=str(pin.current_root()))
        msgs = pin.skew(
            state,
            runtime_root=pin.current_root(),
            skill_dirs=["/Users/x/.claude/plugins/cache/canopy/canopy/0.2.524/skills/ddd-arc-eval"],
        )
        assert any("0.2.524" in m and "0.2.528" in m for m in msgs)
        pin.warn(state, msgs[-1])
        pin.warn(state, msgs[-1])
        assert state.version_warnings == [msgs[-1]]  # recorded once, for the digest

    def test_a_runtime_on_another_version_is_skew(self, tmp_path: Path) -> None:
        other = tmp_path / "rt"
        other.mkdir()
        (other / "VERSION").write_text("0.2.530\n")
        state = _state(plugin_version="0.2.528", runtime_root="/nowhere")
        assert any("0.2.530" in m for m in pin.skew(state, runtime_root=other))
        assert pin.skew(_state(), runtime_root=other) == []  # unpinned: nothing to compare

    def test_a_pruned_pin_falls_back_with_a_warning(self, tmp_path: Path) -> None:
        state = _state(plugin_version="0.2.528", runtime_root=str(tmp_path / "gone"))
        out = pin.resolve_root(state)
        assert out["runtime_root"] == str(pin.current_root())
        assert out["warning"] and "no longer exists" in out["warning"]
        live = _state(plugin_version="0.2.528", runtime_root=str(pin.current_root()))
        assert pin.resolve_root(live) == {"runtime_root": str(pin.current_root()), "pinned": True, "warning": None}


# ---------------------------------------------------------------------------
# Compatibility — a run_state.yaml written before this round still loads
# ---------------------------------------------------------------------------


def test_a_pre_round3_run_state_loads_with_defaults() -> None:
    legacy = {
        "schema_version": 1,
        "run_id": "supply-sophie-rutf-2026-09-26-001",
        "narrative_slug": "supply-sophie-rutf",
        "phase": "judged",
        "iteration": 4,
        "auto_iterate_next_action": "stop_concept_change",
        "terminal_status": "diverging",
        "concept_gate_deferred": 3,
        "score_history": [2.0, 2.0, 3.0, 3.0, 2.0],
        "progress_history": [{"score": 2.0, "open_findings": 32, "mean_cell": 3.333, "confirmed_caps": 2, "full": True}],
        "loop_mode": "backlog",
        "batches_since_full": 1,
        "last_fix_sha": "b01c5e5",
        "open_gaps": 0,
    }
    state = RunState.model_validate(legacy)
    assert state.parked == [] and state.steps == {} and state.recipe_rejudge is None
    assert state.plugin_version is None and state.version_warnings == []
