"""A repo with a deploy gate must say how it renders between checkpoints (0.2.554).

On connect-labs every v1 fix batch paid PR -> CI -> deploy (~35 of every 50
minutes) before a frame could be judged, because nothing required the repo to
configure the inner loop that already existed. A backlog loop now refuses to
continue on such a repo unless `inner_loop:` is configured, or declared off with
a reason that run_state records and assemble prints.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest
import yaml
from scripts.ddd import loop_config, target
from scripts.ddd.run_pipeline import classify_termination, compute_auto_iterate
from scripts.ddd.schemas.models import RunState, Verdict

GATE = {"deploy_gate": {"health_url": "https://labs.example.invalid/health/"}}
INNER = {"inner_loop": {"base_url": "http://localhost:8000", "setup": "make serve-demo"}}


def _v(score: float, verdict: str = "fail") -> Verdict:
    return Verdict(
        schema_version=1, kind="concept", gate="gating", rubric_name="r", ran_at="t",
        dimensions={}, overall_score=score, overall_rule="lowest", verdict=verdict,
    )


def _mech(n: int) -> dict:
    return {
        "scene": "1", "dimension": "visual_polish", "route": "PRODUCT",
        "fix_kind": "mechanical", "detail": f"defect {n}", "fix_recommendation": f"fix {n}",
    }


def _policy(raw: dict) -> dict:
    return target.inner_loop_policy(loop_config.parse(raw))


class TestPolicy:
    def test_a_deploy_gate_without_an_inner_loop_is_missing(self) -> None:
        out = _policy(GATE)
        assert out["status"] == "missing" and "inner_loop" in out["reason"]

    def test_a_configured_inner_loop_passes(self) -> None:
        assert _policy({**GATE, **INNER})["status"] == "configured"

    def test_no_deploy_gate_means_nothing_to_require(self) -> None:
        assert _policy({})["status"] == "not_required"

    def test_off_with_a_reason_is_allowed_and_carries_it(self) -> None:
        # A bare `off` is YAML False — the form a person actually writes.
        raw = yaml.safe_load(
            "deploy_gate: {health_url: https://x.invalid/health/}\n"
            "inner_loop: off\n"
            "inner_loop_off_reason: no locally servable build yet\n"
        )
        assert raw["inner_loop"] is False
        assert _policy(raw) == {"status": "off", "reason": "no locally servable build yet"}

    def test_the_mapping_form_of_off(self) -> None:
        out = _policy({**GATE, "inner_loop": {"off": True, "reason": "prod-only data"}})
        assert out == {"status": "off", "reason": "prod-only data"}

    @pytest.mark.parametrize("raw", [False, "off", {"off": True}, {"off": True, "reason": "  "}])
    def test_off_without_a_reason_is_still_missing(self, raw) -> None:
        out = _policy({**GATE, "inner_loop": raw})
        assert out["status"] == "missing" and "needs a reason" in out["reason"]

    def test_off_wins_over_a_stale_base_url(self) -> None:
        cfg = loop_config.parse({**GATE, "inner_loop": {"off": True, "reason": "r", "base_url": "http://x"}})
        assert not cfg.inner_loop.enabled


def _backlog_pass(policy: dict | None) -> tuple[str, str, RunState]:
    st = RunState(run_id="r-001", narrative_slug="n")
    action, reason = compute_auto_iterate(
        st, _v(2.0), _v(2.0), [_mech(i) for i in range(10)], unattended=True,
        judge_full=True, loop_config=loop_config.LoopConfig(mode="backlog"),
        target="deploy", judges=["concept", "user", "arc"], inner_loop_policy=policy,
    )
    return action, reason, st


class TestBacklogLoopRefusal:
    def test_a_missing_inner_loop_refuses_the_backlog_loop(self) -> None:
        action, reason, st = _backlog_pass(_policy(GATE))
        assert st.loop_mode == "backlog"
        assert action == "stop_inner_loop_required", reason
        assert "inner_loop" in reason and "'continue'" in reason
        assert st.terminal_status == "needs_config"

    def test_the_refusal_keeps_the_scheduling_the_continue_would_have_left(self) -> None:
        # So a logged `decision override` resumes exactly the pass this would have been.
        _, _, refused = _backlog_pass(_policy(GATE))
        _, _, allowed = _backlog_pass(_policy({**GATE, **INNER}))
        assert refused.next_judge_full == allowed.next_judge_full
        assert refused.batch_plan == allowed.batch_plan

    @pytest.mark.parametrize(
        "policy",
        [
            {"status": "configured", "reason": "inner loop at http://localhost:8000"},
            {"status": "off", "reason": "no local build"},
            {"status": "not_required", "reason": "no deploy_gate configured"},
            None,
        ],
    )
    def test_anything_but_missing_continues(self, policy) -> None:
        action, reason, _ = _backlog_pass(policy)
        assert action == "continue", reason

    def test_a_polish_loop_is_not_refused(self) -> None:
        st = RunState(run_id="r-001", narrative_slug="n")
        action, _ = compute_auto_iterate(
            st, _v(2.0), _v(2.0), [_mech(1)], unattended=True, judge_full=True,
            loop_config=loop_config.LoopConfig(mode="polish"), inner_loop_policy=_policy(GATE),
        )
        assert action == "continue" and st.loop_mode == "polish"

    def test_it_is_a_terminal_blocking_stop(self) -> None:
        from scripts.ddd import decision

        assert classify_termination("stop_inner_loop_required", converged=False)["terminal"] is True
        assert "stop_inner_loop_required" in decision.BLOCKING_STOPS


class TestAssembleRecordsAndPrintsThePolicy:
    def _run(self, tmp_path: Path, monkeypatch, config: str) -> tuple[str, RunState]:
        import scripts.ddd.runstate as rs

        ddd = tmp_path / "ddd"
        ddd.mkdir()
        (ddd / "config.yaml").write_text(config)
        monkeypatch.setenv("CANOPY_DDD_RUNS_DIR", str(tmp_path / "runs"))
        monkeypatch.setattr(rs, "_resolve_ddd_dir", lambda *a, **k: ddd)
        rid = rs.new_run("n", ddd_dir=ddd)
        run = rs.run_dir_for(rid, ddd_dir=ddd)
        verdict = {
            "schema_version": 1, "kind": "concept", "gate": "gating", "rubric_name": "r",
            "ran_at": "t", "dimensions": {}, "overall_score": 2.0, "overall_rule": "lowest",
            "verdict": "fail",
        }
        (run / "verdict-concept.yaml").write_text(yaml.safe_dump(verdict))
        (run / "verdict-user.yaml").write_text(yaml.safe_dump({**verdict, "kind": "user_artifact"}))
        (run / "design_findings.json").write_text(json.dumps([_mech(i) for i in range(10)]))
        from scripts.ddd import assemble

        buf = io.StringIO()
        with redirect_stdout(buf):
            assert assemble._main([rid]) == 0
        return buf.getvalue(), rs.load(rid, ddd_dir=ddd)

    def test_off_reason_is_recorded_in_run_state_and_printed(self, tmp_path: Path, monkeypatch) -> None:
        out, st = self._run(
            tmp_path, monkeypatch,
            "loop: {mode: backlog}\ndeploy_gate: {health_url: https://x.invalid/health/}\n"
            "inner_loop: off\ninner_loop_off_reason: the fixture has no local build\n",
        )
        assert st.inner_loop_policy == {"status": "off", "reason": "the fixture has no local build"}
        assert "Inner loop:   OFF by config — the fixture has no local build" in out
        assert st.auto_iterate_next_action == "continue"

    def test_missing_is_refused_and_printed(self, tmp_path: Path, monkeypatch) -> None:
        out, st = self._run(
            tmp_path, monkeypatch,
            "loop: {mode: backlog}\ndeploy_gate: {health_url: https://x.invalid/health/}\n",
        )
        assert st.inner_loop_policy["status"] == "missing"
        assert st.auto_iterate_next_action == "stop_inner_loop_required"
        assert "Inner loop:   MISSING" in out and "stop_inner_loop_required" in out


@pytest.mark.parametrize(
    "base_url,expected",
    [("http://localhost:8010/", "http://localhost:8010"), (None, "https://deploy.example.invalid")],
)
def test_the_recorder_tells_setup_which_origin_it_films(tmp_path: Path, monkeypatch, base_url, expected) -> None:
    """An inner-loop render must reseed the LOCAL build, so setup sees the origin being filmed."""
    from scripts.walkthrough import record_video

    monkeypatch.setattr(record_video, "resolve_setup_cwd", lambda _p: tmp_path)
    monkeypatch.setenv("CANOPY_RENDER_BASE_URL", "stale")  # restored (removed) after the test
    seen = tmp_path / "seen.txt"
    spec = {"base_url": "https://deploy.example.invalid"}
    assert record_video.export_render_origin(spec, base_url) == expected
    record_video.run_setup({"command": f"printf '%s' \"$CANOPY_RENDER_BASE_URL\" > {seen}"}, tmp_path / "w.yaml")
    assert seen.read_text() == expected
