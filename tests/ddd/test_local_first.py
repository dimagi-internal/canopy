"""canopy#787 — the inner loop stays on; deployed labs is for the pass that decides.

Measured: ``supply-sophie-sheets-2026-10-06-001`` rendered iterations 1-2 on
``localhost:8000``, then a new scene filmed a clone "whose data exists only on
labs", the inner loop was switched off for the run, and every later fix paid
merge + ``deploy-labs.yml`` (7 deploys, ~3 of 9.6 hours waiting). And with the
inner loop on, every third (full) pass still went through PR, CI and deploy.
"""
from __future__ import annotations

from types import SimpleNamespace

from scripts.ddd import loop_config, target
from scripts.ddd.run_pipeline import compute_auto_iterate
from scripts.ddd.schemas.models import Dimension, RunState, Verdict

GATE = {"deploy_gate": {"health_url": "https://labs.example/health/"}}
INNER = {"inner_loop": {"base_url": "http://localhost:8000"}}
OFF = {"inner_loop": "off", "inner_loop_off_reason": "scene 8's clone exists only on labs"}


def _cfg(**blocks):
    return loop_config.parse({**GATE, **blocks})


class TestDropped:
    def test_a_run_that_started_local_and_lost_it_is_dropped(self):
        start = target.inner_loop_policy(_cfg(**INNER))
        assert start["status"] == "configured"
        now = target.inner_loop_policy(_cfg(**OFF), prior=start)
        assert now["status"] == "dropped"
        assert "build gap" in now["reason"] and "accept-remote" in now["reason"]
        # sticky: the next pass still sees the drop
        assert target.inner_loop_policy(_cfg(**OFF), prior=now)["status"] == "dropped"
        # restoring the inner loop clears it
        assert target.inner_loop_policy(_cfg(**INNER), prior=now)["status"] == "configured"

    def test_off_from_the_start_with_a_reason_is_still_allowed(self):
        assert target.inner_loop_policy(_cfg(**OFF))["status"] == "off"

    def test_accept_remote_records_why(self):
        dropped = target.inner_loop_policy(_cfg(**OFF), prior=target.inner_loop_policy(_cfg(**INNER)))
        accepted = target.inner_loop_policy(_cfg(**OFF), prior={**dropped, "accepted": "labs-only clone, ok for 1 run"})
        assert accepted["status"] == "off" and "labs-only clone" in accepted["reason"]

    def test_a_dropped_loop_stops_a_backlog_continue(self):
        s = RunState(run_id="r", narrative_slug="n", loop_mode="backlog")
        v = Verdict(schema_version=1, kind="concept", gate="gating", rubric_name="c", ran_at="2026-10-07T00:00:00Z",
                    dimensions={"clarity": Dimension(score=2, weight=1.0)}, overall_score=2,
                    overall_rule="lowest", verdict="fail")
        finding = {"scene": 3, "dimension": "clarity", "detail": "d", "route": "PRODUCT", "fix_kind": "mechanical"}
        policy = {"status": "dropped", "reason": "started local; now off"}
        action, reason = compute_auto_iterate(
            s, v, v, [finding], converged=False, unattended=True, judge_full=True,
            inner_loop_policy=policy, loop_config=loop_config.LoopConfig(mode="backlog"),
        )
        assert action == "stop_inner_loop_required" and "started local" in reason

    def test_accept_remote_cli(self, monkeypatch):
        state = SimpleNamespace(inner_loop_policy={"status": "dropped", "reason": "x"}, iteration=3)
        saved = {}
        monkeypatch.setattr("scripts.ddd.runstate.load", lambda rid: state)
        monkeypatch.setattr("scripts.ddd.runstate.save", lambda st: saved.setdefault("s", st))
        monkeypatch.setattr("scripts.ddd.loop_config.load", lambda *a, **k: _cfg(**OFF))
        assert target._main(["accept-remote", "r", "--reason", "clone is labs-only this week"]) == 0
        assert saved["s"].inner_loop_policy["accepted"] == "clone is labs-only this week"
