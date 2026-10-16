"""DDD-1 — does the loop know whether its next batch can move the score?

Replays the evidence shape of two real runs (fixtures under
``tests/ddd/fixtures/ddd1_replay/``, minimal anonymised copies of their verdict
and finding files):

* ``noora`` — noora-health/20261015-1129, iteration 5: six passes at 2.0, the
  floor held by a labs-template defect the loop may not edit (ace#2337), 145
  text-matched declines, ended ``stop_max_iter`` on the stall rule.
* ``smallholder`` — smallholder-seasonal/20261015-1402, iteration 3: four passes
  at 2.0, ended ``blocked_out_of_scope`` although arc cells still had in-scope
  work.

Requirements (Jonathan, 2026-10-16): pinned cells are reported and parked, the
loop keeps optimizing the workable ones, and it stops — with a clear blocked
report — only when nothing workable is predicted to improve or the predictions
proved wrong.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from scripts.ddd import decision, denoise, floor, loop_config, predict
from scripts.ddd.run_pipeline import compute_auto_iterate
from scripts.ddd.schemas.models import RunState, Verdict

FIXTURES = Path(__file__).parent / "fixtures" / "ddd1_replay"
LABS_TEMPLATE = {
    "match": "labs template",
    "name": "labs-report-template",
    "owner": "ACE (labs report template)",
    "issue": "dimagi/ace#2337",
}


class Replay:
    """A run dir seeded from a fixture, driven through ``assemble``."""

    def __init__(self, tmp_path: Path, monkeypatch, name: str, *, surfaces: list | None = None) -> None:
        monkeypatch.setenv("CANOPY_DDD_RUNS_DIR", str(tmp_path / "runs"))
        from scripts.ddd.runstate import new_run, run_dir_for

        self.ddd = tmp_path / "ddd"
        self.ddd.mkdir()
        loop = {"mode": "backlog"}
        if surfaces:
            loop["fixed_surfaces"] = surfaces
        (self.ddd / "config.yaml").write_text(yaml.safe_dump({"loop": loop}))
        self.rid = new_run("replay", ddd_dir=self.ddd)
        self.run = run_dir_for(self.rid, ddd_dir=self.ddd)
        for p in (FIXTURES / name).iterdir():
            shutil.copy(p, self.run / p.name)

    def assemble(self) -> dict:
        from scripts.ddd import assemble

        return assemble.assemble(self.rid, ddd_dir=self.ddd)

    def state(self) -> RunState:
        from scripts.ddd.runstate import load

        return load(self.rid, ddd_dir=self.ddd)

    def save(self, st: RunState) -> None:
        from scripts.ddd.runstate import save

        save(st, ddd_dir=self.ddd)

    def declare(self, items: list[dict]) -> dict:
        st = self.state()
        entry = predict.declare(st, items, iteration=st.decision_seal["iteration"])
        if entry["decision"] == predict.STOP_NO_GAIN:
            decision.seal(st)
            predict.write_report(st, self.run)
        self.save(st)
        return entry

    def next_pass(self, **verdict_edits) -> None:
        """Bump the iteration (the batch landed) and mark the next judge pass full."""
        st = self.state()
        decision.bump(st)
        self.save(st)
        (self.run / "judge-scope.json").write_text(json.dumps({"full": True, "judges": ["concept", "user", "arc"]}))
        for fname, edit in verdict_edits.items():
            p = self.run / fname
            data = yaml.safe_load(p.read_text())
            edit(data)
            p.write_text(yaml.safe_dump(data))


def _item(st: RunState, fid: str, to: float) -> dict:
    f = next(f for f in st.findings if f["finding_id"] == fid)
    return {"finding_id": fid, "scene": f.get("scene"), "dimension": f.get("dimension"),
            "in_scope": f.get("edit_scope") == "in", "from": None, "expected_to": to}


# ---------------------------------------------------------------------------
# noora: pinned cells are parked, the loop keeps going on the workable ones
# ---------------------------------------------------------------------------


class TestNooraReplay:
    def test_pinned_cells_are_parked_and_the_workable_objective_continues(self, tmp_path, monkeypatch) -> None:
        r = Replay(tmp_path, monkeypatch, "noora")
        out = r.assemble()
        assert out["auto_iterate_next_action"] == "continue", out["auto_iterate_reason"]
        cf = r.state().cell_floor
        assert cf["true_score"] == 2.0 and cf["workable_score"] == 3.0
        # the labs-template floor: concept visual_polish s1 and user clarity s1/s3/s4
        for key in ("concept:1:visual_polish", "user:1:clarity", "user:3:clarity", "user:4:clarity"):
            assert key in cf["pinned"], key
        assert "user:2:clarity" in cf["workable"] and "concept:1:motion_friction" in cf["workable"]
        # The user verdict's `scenes:` mapping is read now (it was `scenes: []`).
        assert r.state().gating_floor["scenes"] == [1, 3, 4]
        # Declined findings are reported, never batched.
        st = r.state()
        assert all(f["edit_scope"] == "out" for f in st.findings if f.get("pinned"))
        rep = json.loads((r.run / predict.REPORT_FILE).read_text())
        assert rep["true_score"] == 2.0 and rep["workable_score"] == 3.0
        assert any("ace#2337" in i for o in rep["by_owner"] for i in o["issues"])

    def test_the_next_pass_refuses_until_the_batch_is_predicted(self, tmp_path, monkeypatch) -> None:
        r = Replay(tmp_path, monkeypatch, "noora")
        r.assemble()
        st = r.state()
        assert st.predictions_required is True
        out = decision.check(st)
        assert not out["ok"] and "predict declare" in out["problem"]
        # an override is still always allowed — it only has to be said
        decision.override(st, "test: proceeding without predictions")
        assert decision.check(st)["ok"]

    def test_a_batch_that_cannot_move_a_workable_cell_stops_with_a_report(self, tmp_path, monkeypatch) -> None:
        r = Replay(tmp_path, monkeypatch, "noora")
        r.assemble()
        st = r.state()
        # The recipe fix on user clarity scene 2 is in scope, but an out-of-scope
        # defect the judge scored 3 stays on that cell: 4 is clamped to 3.
        entry = r.declare([
            _item(st, "s2.clarity.2", 4.0),
            {**_item(st, "s1.visual_polish.1", 4.0), "in_scope": True},  # a wrong claim
        ])
        assert entry["decision"] == predict.STOP_NO_GAIN
        clamp = next(i for i in entry["items"] if i["finding_id"] == "s2.clarity.2")
        assert clamp["expected_to"] == 3.0 and any("clamped" in n for n in clamp["notes"])
        out_item = next(i for i in entry["items"] if i["finding_id"] == "s1.visual_polish.1")
        assert out_item["in_scope"] is False and any("but it is not" in n for n in out_item["notes"])
        st = r.state()
        assert st.auto_iterate_next_action == predict.STOP_NO_GAIN
        assert st.terminal_status == "stopped_no_predicted_gain"
        chk = decision.check(st)
        assert not chk["ok"] and predict.STOP_NO_GAIN in chk["problem"]
        rep = json.loads((r.run / predict.REPORT_FILE).read_text())
        assert rep["last_prediction"]["decision"] == predict.STOP_NO_GAIN
        assert any(line.startswith("  Blocked") for line in rep["summary"])

    def test_a_prediction_that_misses_every_target_stops_the_run(self, tmp_path, monkeypatch) -> None:
        r = Replay(tmp_path, monkeypatch, "noora")
        r.assemble()
        st = r.state()
        entry = r.declare([_item(st, "s1.motion_friction.1", 4.0)])
        assert entry["decision"] == "proceed"
        assert entry["gain_cells"] == {"concept:1:motion_friction": {"from": 3.0, "predicted_best": 4.0}}
        assert decision.check(r.state())["ok"]
        r.next_pass()  # the batch landed; the judges said the same thing
        out = r.assemble()
        assert out["auto_iterate_next_action"] == predict.STOP_FAILED, out["auto_iterate_reason"]
        assert out["terminal_status"] == "stopped_prediction_failed"
        st = r.state()
        row = st.prediction_calibration[-1]
        assert (row["cell"], row["from"], row["expected_to"], row["actual"], row["hit"]) == (
            "concept:1:motion_friction", 3.0, 4.0, 3.0, False,
        )
        assert predict.calibration_summary(st)["hit_rate"] == 0.0
        assert "0/1 predicted cell(s) improved" in "\n".join(out["blocked_report"]["summary"])
        assert predict.STOP_FAILED in decision.BLOCKING_STOPS


# ---------------------------------------------------------------------------
# smallholder: an out-of-scope floor no longer stops the run
# ---------------------------------------------------------------------------


class TestSmallholderReplay:
    def test_fixed_surfaces_pin_with_owner_and_the_arc_work_continues(self, tmp_path, monkeypatch) -> None:
        r = Replay(tmp_path, monkeypatch, "smallholder", surfaces=[LABS_TEMPLATE])
        out = r.assemble()
        # The real run ended blocked_out_of_scope here; the arc still had work.
        assert out["auto_iterate_next_action"] == "continue", out["auto_iterate_reason"]
        cf = r.state().cell_floor
        assert {"concept:2:visual_polish", "concept:4:visual_polish", "user:*:clarity"} <= set(cf["pinned"])
        assert {"arc:*:visual_variety", "arc:*:arc_shape"} <= set(cf["workable"])
        rep = json.loads((r.run / predict.REPORT_FILE).read_text())
        owners = {o["owner"]: o for o in rep["by_owner"]}
        assert owners["ACE (labs report template)"]["issues"] == ["dimagi/ace#2337"]
        assert "concept:2:visual_polish" in owners["ACE (labs report template)"]["cells"]

    def test_a_prediction_that_lands_keeps_the_loop_going_and_is_calibrated(self, tmp_path, monkeypatch) -> None:
        r = Replay(tmp_path, monkeypatch, "smallholder", surfaces=[LABS_TEMPLATE])
        r.assemble()
        st = r.state()
        entry = r.declare([_item(st, "sx.visual_variety.1", 4.0)])
        assert entry["decision"] == "proceed"

        def lift(data: dict) -> None:
            data["dimensions"]["visual_variety"]["score"] = 4.0

        r.next_pass(**{"verdict-arc.yaml": lift})
        out = r.assemble()
        assert out["auto_iterate_next_action"] not in (predict.STOP_FAILED, "stop_out_of_scope"), out["auto_iterate_reason"]
        row = r.state().prediction_calibration[-1]
        assert row["cell"] == "arc:*:visual_variety" and row["hit"] is True and row["met"] is True
        assert {"cell": "arc:*:visual_variety", "from": 3.0, "to": 4.0} in out_report(r)["improved"]

    def test_when_nothing_workable_is_left_the_run_stops_blocked(self, tmp_path, monkeypatch) -> None:
        r = Replay(tmp_path, monkeypatch, "smallholder", surfaces=[LABS_TEMPLATE])
        (r.run / "arc_findings.json").write_text("[]")
        (r.run / "lint_findings.json").write_text("[]")
        design = json.loads((r.run / "design_findings.json").read_text())
        (r.run / "design_findings.json").write_text(
            json.dumps([f for f in design if f.get("dimension") != "why_groundedness"])
        )
        out = r.assemble()
        assert out["auto_iterate_next_action"] == "stop_out_of_scope", out["auto_iterate_reason"]
        assert "ACE (labs report template)" in out["auto_iterate_reason"]
        assert Path(out["blocked_report"]["path"]).exists()


def out_report(r: Replay) -> dict:
    return json.loads((r.run / predict.REPORT_FILE).read_text()) if (r.run / predict.REPORT_FILE).exists() else predict.blocked_report(r.state())


# ---------------------------------------------------------------------------
# stable finding ids
# ---------------------------------------------------------------------------


def _v(score: float, kind: str = "concept", dims: dict | None = None) -> Verdict:
    return Verdict(
        schema_version=1, kind=kind, gate="gating", rubric_name="r", ran_at="t",
        dimensions={k: {"weight": 1.0, "score": s} for k, s in (dims or {}).items()},
        overall_score=score, overall_rule="lowest", verdict="fail" if score < 3 else "warn",
    )


FLOATING = {
    "scene": "1", "dimension": "visual_polish", "route": "PRODUCT", "fix_kind": "mechanical",
    "detail": "The floating 'Ask an agent' button occludes footer text and the table overflows.",
    "fix_recommendation": "Give the page a bottom inset so the floating agent button cannot overlap content.",
}
REWORDED = {
    **FLOATING,
    "detail": "'Ask an agent' floating button overlaps the sparkline; footer text hidden below it.",
    "fix_recommendation": "Dock the floating agent button outside the content area.",
}


class TestStableFindingIds:
    def test_a_reworded_finding_keeps_its_id(self) -> None:
        a = [dict(FLOATING)]
        ledger = floor.assign_ids(a, [], iteration=0)
        b = [dict(REWORDED), {**FLOATING, "scene": "2"}]
        floor.assign_ids(b, ledger, iteration=1)
        assert b[0]["finding_id"] == a[0]["finding_id"] == "s1.visual_polish.1"
        assert b[1]["finding_id"] == "s2.visual_polish.1"  # other cell, other id

    def test_an_unrelated_finding_on_the_same_cell_gets_a_new_id(self) -> None:
        ledger = floor.assign_ids([dict(FLOATING)], [], iteration=0)
        other = [{**FLOATING, "detail": "Axis labels collide at 1440px.", "fix_recommendation": "Thin the ticks."}]
        floor.assign_ids(other, ledger, iteration=1)
        assert other[0]["finding_id"] == "s1.visual_polish.2"

    def test_a_decline_stays_closed_when_the_judge_rewords_the_finding(self) -> None:
        st = RunState(run_id="r", narrative_slug="n", loop_mode="backlog")
        verdicts = (_v(2, dims={"visual_polish": 2}), _v(4, "user_artifact", dims={"clarity": 4}))
        declined = [{"match": "occludes footer text", "reason": "labs template (ace#2337)", "owner": "ACE"}]
        compute_auto_iterate(st, *verdicts, [dict(FLOATING)], converged=False, unattended=True,
                             edit_scope={"declined": declined})
        assert st.findings[0]["edit_scope"] == "out"
        assert st.out_of_scope_ids["s1.visual_polish.1"]["via"] == "decline"
        # next pass: re-worded — the text decline no longer matches, the id does
        st.iteration += 1
        compute_auto_iterate(st, *verdicts, [dict(REWORDED)], converged=False, unattended=True,
                             edit_scope={"declined": declined})
        f = st.findings[0]
        assert f["finding_id"] == "s1.visual_polish.1"
        assert f["edit_scope"] == "out" and f["edit_scope_via"] == "sticky" and f["owner"] == "ACE"

    def test_decline_by_finding_id(self) -> None:
        f = {**FLOATING, "finding_id": "s1.visual_polish.1"}
        d = floor.scope_detail(f, declined=[{"finding_id": "s1.visual_polish.1", "reason": "x", "issue": "o/r#1"}])
        assert d["edit_scope"] == "out" and d["via"] == "decline" and d["issue"] == "o/r#1"

    def test_decline_cli_records_id_owner_and_issue(self, tmp_path) -> None:
        rc = floor._main([
            "decline", str(tmp_path), "--finding-id", "s1.visual_polish.1",
            "--reason", "labs template, ACE-owned (dimagi/ace#2337)", "--owner", "ACE",
        ])
        assert rc == 0
        row = json.loads((tmp_path / floor.DECLINED_FILE).read_text())[0]
        assert row["finding_id"] == "s1.visual_polish.1" and row["owner"] == "ACE"
        assert row["issue"] == "dimagi/ace#2337"


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


class TestConfig:
    def test_fixed_surfaces_take_owner_and_issue(self) -> None:
        cfg = loop_config.parse({"loop": {"fixed_surfaces": ["registry", LABS_TEMPLATE, {"match": "(bad"}]}})
        assert cfg.loop.fixed_surfaces == ("registry", "labs template")
        s = cfg.loop.surfaces[1]
        assert (s.label, s.owner, s.issue) == ("labs-report-template", "ACE (labs report template)", "dimagi/ace#2337")

    def test_a_surface_names_its_owner_in_the_scope_reason(self) -> None:
        cfg = loop_config.parse({"loop": {"fixed_surfaces": [LABS_TEMPLATE]}})
        d = floor.scope_detail(
            {"fix_recommendation": "[UPSTREAM] labs template: dock the button"}, surfaces=cfg.loop.surfaces
        )
        assert d["via"] == "surface" and d["owner"] == "ACE (labs report template)"

    def test_judges_have_a_sensible_default_budget(self) -> None:
        t = loop_config.parse({}).timeouts
        assert t.for_step("judge:user") == 20.0 and t.for_step("fixer:B1") == 45.0
        assert loop_config.parse({"timeouts": {"judge_minutes": 30}}).timeouts.for_step("judge:arc") == 30.0


# ---------------------------------------------------------------------------
# k=3 confirmation only when it can change a decision
# ---------------------------------------------------------------------------


class TestPlanConfirmations:
    CELL_FLOOR = {
        "cells": [
            {"key": "concept:1:visual_polish", "judge": "concept", "scene": 1, "dimension": "visual_polish",
             "score": 2.0, "status": "pinned"},
            {"key": "concept:1:motion_friction", "judge": "concept", "scene": 1, "dimension": "motion_friction",
             "score": 3.0, "status": "workable"},
            {"key": "user:2:clarity", "judge": "user", "scene": 2, "dimension": "clarity",
             "score": 2.0, "status": "workable"},
        ]
    }
    CAPS = [{"scene": "1", "dimension": "visual_polish", "score": 2.0},
            {"scene": "1", "dimension": "motion_friction", "score": 2.0}]

    def test_a_pinned_cap_is_never_confirmed(self) -> None:
        out = denoise.plan_confirmations(self.CAPS, cell_floor=self.CELL_FLOOR, rejudged={"concept": None, "user": None})
        assert [c["dimension"] for c in out["confirm"]] == ["motion_friction"]
        assert "pinned" in out["skip"][0]["why"]

    def test_caps_are_skipped_while_a_carried_workable_cell_holds_the_floor(self) -> None:
        out = denoise.plan_confirmations(self.CAPS, cell_floor=self.CELL_FLOOR, rejudged={"concept": None})
        assert out["confirm"] == [] and "user:2:clarity" in out["skip"][1]["why"]

    def test_a_cap_the_last_batch_targeted_is_always_confirmed(self) -> None:
        out = denoise.plan_confirmations(
            self.CAPS, cell_floor=self.CELL_FLOOR, rejudged={"concept": None},
            targeted={"concept:1:motion_friction"},
        )
        assert [c["dimension"] for c in out["confirm"]] == ["motion_friction"]

    def test_without_a_cell_floor_every_cap_is_confirmed(self) -> None:
        assert len(denoise.plan_confirmations(self.CAPS, cell_floor=None)["confirm"]) == 2

    def test_a_skipped_confirmation_makes_the_caps_signal_incomparable(self) -> None:
        from scripts.ddd import progress

        assert progress._confirmed_caps({"confirmation": "skipped", "capping_cells": [{"score": 1}]}) is None
        assert progress._confirmed_caps(
            {"capping_cells": [{"scene": "1", "dimension": "v", "confirmed": 2.0}]}, {("1", "v")}
        ) == 0


# ---------------------------------------------------------------------------
# noise bands + stall on a partial pass
# ---------------------------------------------------------------------------


class TestProgressBands:
    def test_noora_caps_wobble_is_not_progress(self) -> None:
        from scripts.ddd import progress

        # noora's confirmed caps over six passes at a flat 2.0 / ~3.3 mean
        hist = [{"score": 2.0, "open_findings": 31, "mean_cell": 3.4, "confirmed_caps": 2},
                {"score": 2.0, "open_findings": 27, "mean_cell": 3.333, "confirmed_caps": 3},
                {"score": 2.0, "open_findings": 32, "mean_cell": 3.433, "confirmed_caps": 1}]
        assert progress.improved_signals(hist[:2], hist[2]) == []
        assert progress.stalled(hist)

    def test_workable_score_compares_only_under_the_same_pinned_set(self) -> None:
        from scripts.ddd import progress

        a = {"score": 2.0, "workable_score": 2.0, "pinned": ["x"]}
        assert progress.improved_signals([a], {**a, "workable_score": 3.0, "pinned": ["x", "y"]}) == []
        assert progress.improved_signals([a], {**a, "workable_score": 3.0}) == ["workable_score"]


# ---------------------------------------------------------------------------
# judge timeout
# ---------------------------------------------------------------------------


class TestJudgeTimeout:
    def test_a_hung_judge_is_carried_and_noted(self, tmp_path, monkeypatch) -> None:
        r = Replay(tmp_path, monkeypatch, "smallholder", surfaces=[LABS_TEMPLATE])
        (r.run / "arc_findings.json").write_text("[]")
        (r.run / "lint_findings.json").write_text("[]")
        design = json.loads((r.run / "design_findings.json").read_text())
        (r.run / "design_findings.json").write_text(
            json.dumps([f for f in design if f.get("dimension") != "why_groundedness"])
        )
        st = r.state()
        st.steps = {
            "judge:user": {"status": "running", "started_at": "2026-10-16T00:00:00+00:00",
                           "last_beat": "2026-10-16T00:00:00+00:00", "timeout_minutes": 20.0,
                           "heartbeat_minutes": 15.0},
        }
        r.save(st)
        out = r.assemble()
        # The user judge's cells are carried unchanged (its last verdict file),
        # it is dropped from this pass's judges, and the reason says so. A stop
        # that no checkpoint could change (nothing in scope) still stands.
        assert out["auto_iterate_next_action"] == "stop_out_of_scope", out["auto_iterate_reason"]
        assert out["auto_iterate_reason"].startswith("JUDGE TIMEOUT: user")
        assert out["judge_timeouts"][0]["judge"] == "user"
        assert out["judges"] == ["concept", "arc"]
        assert r.state().judge_timeouts[0]["judge"] == "user"

    def test_a_timed_out_pass_cannot_converge(self) -> None:
        # The fidelity rule the timeout relies on: a pass missing a judge hands
        # convergence to a checkpoint instead of declaring it.
        st = RunState(run_id="r", narrative_slug="n")
        action, _ = compute_auto_iterate(
            st, _v(4.5, dims={"visual_polish": 4.5}), _v(4.5, "user_artifact", dims={"clarity": 4.5}), [],
            unattended=True, judge_full=True, judges=["concept", "arc"],
        )
        assert action == "checkpoint"


@pytest.mark.parametrize("action", [predict.STOP_NO_GAIN, predict.STOP_FAILED])
def test_new_stops_have_their_own_terminal_status(action: str) -> None:
    from scripts.ddd.run_pipeline import classify_termination

    out = classify_termination(action, converged=False, score_history=[2.0, 2.0])
    assert out["terminal"] and out["status"] in ("stopped_no_predicted_gain", "stopped_prediction_failed")


# ---------------------------------------------------------------------------
# CLIs
# ---------------------------------------------------------------------------


class TestClis:
    def test_predict_declare_exit_codes(self, tmp_path, monkeypatch, capsys) -> None:
        r = Replay(tmp_path, monkeypatch, "noora")
        r.assemble()
        monkeypatch.setenv("DDD_DIR", str(r.ddd))
        st = r.state()
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps([_item(st, "s1.visual_polish.1", 4.0)]))
        assert predict._main(["declare", r.rid, "--batch", str(bad)]) == 3
        assert r.state().auto_iterate_next_action == predict.STOP_NO_GAIN
        assert "DDD blocked report" in capsys.readouterr().err
        assert predict._main(["report", r.rid]) == 0
        assert "Blocked" in capsys.readouterr().out

    def test_plan_confirm_reads_a_run_dir(self, tmp_path, monkeypatch, capsys) -> None:
        r = Replay(tmp_path, monkeypatch, "noora")
        r.assemble()
        caps = json.dumps([{"scene": "1", "dimension": "visual_polish", "score": 2.0},
                           {"scene": "1", "dimension": "motion_friction", "score": 2.0}])
        assert denoise._main(["plan-confirm", str(r.run), "--caps", caps]) == 0
        out = json.loads(capsys.readouterr().out)
        assert [c["dimension"] for c in out["confirm"]] == ["motion_friction"]
        assert out["skip"][0]["dimension"] == "visual_polish"
