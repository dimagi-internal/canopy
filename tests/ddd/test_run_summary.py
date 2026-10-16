"""A finished run's summary is part of the RUN, not a learnings.md (2026-10-16).

``.canopy/ddd/learnings.md`` never reached the next run: four machines, ACE runs
writing to per-run home dirs nobody read, nine diverging repo copies. The summary
now lives on the run document (pushed to canopy-web), in the run dir, and owned
findings go to their owners.
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts.ddd import run_learnings, run_store, run_summary
from scripts.ddd.schemas.models import RunState


def _state(**kw) -> RunState:
    s = RunState(run_id="supply-001", narrative_slug="supply")
    s.auto_iterate_next_action = "stop_out_of_scope"
    s.auto_iterate_reason = "The gating floor is held only by a registry string."
    s.terminal_status = "blocked_out_of_scope"
    s.score_history = [2.0, 3.0, 3.0]
    s.findings = [
        {"finding_id": "s4.clarity.1", "scene": 4, "dimension": "clarity", "recurring": 5, "route": "PRODUCT"},
        {"finding_id": "s6.trust.1", "scene": 6, "dimension": "trust", "route": "PRODUCT"},
        {"finding_id": "s2.clarity.1", "scene": 2, "dimension": "clarity", "route": "PRODUCT",
         "product_clarity": "whether 'active' counts paused workers",
         "detail": "Judge could not tell if paused workers are active."},
    ]
    s.gating_floor = {"findings": [{"finding_id": "s5.clarity.1", "scene": 5, "dimension": "clarity",
                                    "edit_scope": "out", "edit_scope_reason": "fixed surface: registry",
                                    "fix_recommendation": "Replace the targets footnote.",
                                    "owner": "Labs (registry)", "issue": "dimagi/connect-labs#12"}]}
    s.pass_timings = [{"wall_minutes": 40, "rejudged": 9, "reused": 0, "capture": "full"},
                      {"wall_minutes": 20, "rejudged": 2, "reused": 7, "capture": "scenes"}]
    s.inner_loop_policy = {"status": "dropped", "reason": "started local; now off"}
    s.prediction_calibration = [
        {"planned_iteration": 1, "iteration": 2, "cell": "concept:1:motion_friction", "from": 3.0,
         "expected_to": 4.0, "actual": 3.0, "judged": True, "hit": False, "met": False},
        {"planned_iteration": 2, "iteration": 3, "cell": "arc:*:visual_variety", "from": 3.0,
         "expected_to": 4.0, "actual": 4.0, "judged": True, "hit": True, "met": True},
        {"planned_iteration": 2, "iteration": 3, "cell": "user:9:clarity", "judged": False},
    ]
    s.decision_overrides = [{"iteration": 2, "action": "stop_max_iter", "reason": "one more pass"}]
    s.judge_timeouts = [{"iteration": 2, "judge": "user", "minutes": 98, "reason": "timed out"}]
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_the_summary_keeps_what_the_learnings_block_said_and_adds_calibration():
    s = run_summary.build(_state())
    assert s["ending"]["action"] == "stop_out_of_scope" and s["score_history"] == [2.0, 3.0, 3.0]
    assert [c["cell"] for c in s["never_cleared"]] == ["4:clarity"]
    assert s["never_cleared"][0]["open_passes"] == 5 and s["never_cleared"][0]["finding_ids"] == ["s4.clarity.1"]
    (o,) = s["out_of_scope"]
    assert o["finding_id"] == "s5.clarity.1" and o["owner"] == "Labs (registry)"
    assert o["issue"] == "dimagi/connect-labs#12" and "targets footnote" in o["fix"]
    assert s["timings"] == {"passes": 2, "wall_minutes": 60.0, "rejudged": 11, "reused": 7,
                            "captures": {"full": 1, "scenes": 1}}
    assert s["inner_loop"]["status"] == "dropped"
    cal = s["calibration"]
    assert (cal["judged"], cal["hits"], cal["hit_rate"]) == (2, 1, 0.5)
    assert [r["cell"] for r in cal["rows"]] == ["concept:1:motion_friction", "arc:*:visual_variety"]
    assert s["product_clarity"][0]["ambiguity"] == "whether 'active' counts paused workers"
    kinds = [t["kind"] for t in s["canopy_tooling"]]
    assert kinds == ["inner_loop_dropped", "decision_override", "judge_timeout"]

    md = run_summary.markdown(s)
    assert "stop_out_of_scope" in md and "2, 3, 3" in md
    assert "4:clarity (open 5 passes)" in md and "6:trust" not in md
    assert "Replace the targets footnote" in md and "registry" in md
    assert "re-judged 11, reused 7" in md and "full×1" in md
    assert "Inner loop dropped" in md and "1/2 predicted cell(s) improved" in md


def test_pinned_cells_join_the_out_of_scope_list_with_their_owner():
    st = _state(gating_floor=None)
    st.findings.append({"finding_id": "s1.visual_polish.1", "scene": 1, "dimension": "visual_polish",
                        "surface": "labs-report-template", "owner": "ACE", "issue": "dimagi/ace#2337"})
    st.cell_floor = {"cells": [
        {"key": "concept:1:visual_polish", "status": "pinned",
         "blocked_by": [{"finding_id": "s1.visual_polish.1", "text": "Floating button covers text"}]},
        {"key": "user:1:clarity", "status": "pinned",
         "blocked_by": [{"finding_id": "s1.visual_polish.1", "text": "Floating button covers text"}]},
        {"key": "user:2:clarity", "status": "workable"},
    ]}
    (o,) = run_summary.build(st)["out_of_scope"]
    assert o["cells"] == ["concept:1:visual_polish", "user:1:clarity"]
    assert (o["surface"], o["owner"], o["issue"], o["scene"]) == ("labs-report-template", "ACE", "dimagi/ace#2337", 1)


def test_the_files_say_when_canopy_web_does_not_have_it(tmp_path):
    st = _state()
    run_summary.finalize(st, tmp_path)
    jp, mp = run_summary.write(st, tmp_path)
    data = json.loads(jp.read_text())
    assert data["delivery"]["uploaded"] is False and "local-only" in data["delivery"]["canopy_web"]
    assert data["routing"]["mode"] == "dry"  # pytest: nothing is sent
    assert "local-only" in mp.read_text()


def test_a_synced_run_says_it_is_in_the_run_record():
    st = _state(store={"version": 7, "pending": False})
    assert run_summary.delivery(st)["uploaded"] is True


def test_the_run_record_summary_carries_the_actionable_digest():
    st = _state()
    run_summary.finalize(st, Path("/nonexistent"))
    learned = run_store.summary(st.model_dump(mode="json"))["learned"]
    assert learned["ending"] == "stop_out_of_scope"
    assert learned["never_cleared"] == [{"cell": "4:clarity", "open_passes": 5}]
    assert learned["out_of_scope"][0]["issue"] == "dimagi/connect-labs#12"
    assert learned["calibration"]["hit_rate"] == 0.5 and learned["product_clarity"] == 1
    assert "judge_timeout" in learned["canopy_tooling"]
    # dry under pytest: the named issue is an INTENDED comment; the clarity
    # finding has no product repo (no config, no origin) so it is unrouted
    assert learned["filed"] == [{"kind": "out_of_scope", "finding_id": "s5.clarity.1", "status": "intended",
                                 "url": None, "repo": "dimagi/connect-labs", "issue": "dimagi/connect-labs#12"}]
    # a run that has not ended carries none
    assert run_store.summary(RunState(run_id="x", narrative_slug="x").model_dump(mode="json"))["learned"] is None


def test_finalize_never_raises_when_routing_breaks(monkeypatch, tmp_path):
    from scripts.ddd import owner_routing

    def boom(*a, **k):
        raise RuntimeError("gh exploded")

    monkeypatch.setattr(owner_routing, "route", boom)
    st = _state()
    s = run_summary.finalize(st, tmp_path)
    assert s["routing"]["mode"] == "error" and "gh exploded" in s["routing"]["why"]
    assert st.run_summary is s


def test_the_old_module_name_rewrites_the_summary_and_never_learnings(tmp_path, monkeypatch):
    monkeypatch.setenv("CANOPY_DDD_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("DDD_DIR", str(tmp_path / "ddd"))
    from scripts.ddd.runstate import load, new_run, run_dir_for, save

    ddd = tmp_path / "ddd"
    ddd.mkdir()
    rid = new_run("supply", ddd_dir=ddd)
    st = load(rid, ddd_dir=ddd)
    st.auto_iterate_next_action = "stop_done"
    st.score_history = [4.0]
    save(st, ddd_dir=ddd)

    assert run_learnings._main([rid]) == 0
    rd = run_dir_for(rid, ddd_dir=ddd)
    data = json.loads((rd / run_summary.SUMMARY_JSON).read_text())
    assert data["ending"]["action"] == "stop_done"
    assert load(rid, ddd_dir=ddd).run_summary["run_id"] == rid
    assert not (ddd / "learnings.md").exists()
    # the deprecated record() helper writes the same file, never learnings.md
    assert run_learnings.record(load(rid, ddd_dir=ddd), ddd).name == run_summary.SUMMARY_JSON
    assert not (ddd / "learnings.md").exists()
    assert "stop_done" in run_learnings.summarize(st)


def test_only_a_stop_ends_the_run():
    assert run_summary.ends_run("stop_done") and run_summary.ends_run("stop_max_iter")
    assert not run_summary.ends_run("continue") and not run_summary.ends_run("checkpoint")
    assert not run_summary.ends_run(None)
    assert run_learnings.ends_run is run_summary.ends_run


def test_a_run_ending_assemble_writes_the_summary_and_routes_the_owned_floor(tmp_path, monkeypatch):
    """Replays smallholder (DDD-1 fixture) to a stop: the summary is in the run
    dir and on the run state, the labs-template floor is an intended comment on
    its tracking issue, and nothing is written to the repo's ddd dir."""
    from tests.ddd.test_ddd1_decision import LABS_TEMPLATE, Replay

    r = Replay(tmp_path, monkeypatch, "smallholder", surfaces=[LABS_TEMPLATE])
    (r.run / "arc_findings.json").write_text("[]")
    (r.run / "lint_findings.json").write_text("[]")
    design = json.loads((r.run / "design_findings.json").read_text())
    (r.run / "design_findings.json").write_text(
        json.dumps([f for f in design if f.get("dimension") != "why_groundedness"])
    )
    out = r.assemble()
    assert out["auto_iterate_next_action"] == "stop_out_of_scope", out["auto_iterate_reason"]
    rs = out["run_summary"]
    assert Path(rs["path"]).exists() and rs["uploaded"] is False
    data = json.loads(Path(rs["path"]).read_text())
    owners = {o["owner"] for o in data["out_of_scope"]}
    assert "ACE (labs report template)" in owners
    intents = [x for x in data["routing"]["results"] if x["issue"] == "dimagi/ace#2337"]
    assert intents and {x["status"] for x in intents} == {"intended"}
    assert r.state().run_summary["ending"]["action"] == "stop_out_of_scope"
    assert not (r.ddd / "learnings.md").exists()


def test_a_continuing_pass_writes_no_summary(tmp_path, monkeypatch):
    from tests.ddd.test_ddd1_decision import Replay

    r = Replay(tmp_path, monkeypatch, "noora")
    out = r.assemble()
    assert out["auto_iterate_next_action"] == "continue"
    assert out["run_summary"] is None and r.state().run_summary is None
    assert not (r.run / run_summary.SUMMARY_JSON).exists()


def test_product_clarity_is_part_of_the_findings_contract():
    from scripts.ddd import validate

    ok, problems = validate._validate_findings([
        {"scene": "2", "dimension": "clarity", "route": "PRODUCT", "fix_kind": "mechanical",
         "severity": "medium", "detail": "d", "product_clarity": "whether 'active' counts paused workers"},
        {"scene": "3", "dimension": "clarity", "route": "PRODUCT", "fix_kind": "mechanical",
         "severity": "low", "detail": "d", "product_clarity": True},
    ])
    assert ok, problems
