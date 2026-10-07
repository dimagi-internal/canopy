"""canopy#790 — converge on a caller-supplied target rubric (scripts.ddd.target_rubric)."""
from __future__ import annotations

from pathlib import Path

import yaml

from scripts.ddd import loop_config
from scripts.ddd import target_rubric as tr
from scripts.ddd.run_pipeline import compute_auto_iterate
from scripts.ddd.schemas.models import RunState, Verdict


def _verdict(kind: str, dims: dict[str, float], overall: float | None = None) -> Verdict:
    return Verdict(
        kind=kind,
        gate="gating",
        live_state_verified=True,
        rubric_name="r",
        ran_at="t",
        dimensions={d: {"score": s, "weight": 0.2} for d, s in dims.items()},
        overall_score=overall if overall is not None else min(dims.values()),
        overall_rule="lowest",
        verdict="warn",
    )


def _write_per_scene(run_dir: Path, name: str, kind: str, per_scene: dict[str, list[float]]) -> None:
    """A concept-style verdict file whose justifications carry `per-scene [..]`."""
    dims = {
        d: {"score": min(v), "weight": 0.2, "justification": f"Weakest-link (per-scene {v}; lowest ...)"}
        for d, v in per_scene.items()
    }
    (run_dir / name).write_text(yaml.safe_dump({"kind": kind, "gate": "gating", "dimensions": dims}))


def _product_run(tmp_path: Path, clarity: list[float], trust: list[float], caps=None):
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    _write_per_scene(run_dir, "verdict-concept.yaml", "concept", {"design_soundness": [3, 3, 4, 3]})
    _write_per_scene(run_dir, "verdict-user.yaml", "user_artifact", {"clarity": clarity, "trust": trust})
    verdicts = {
        "concept": _verdict("concept", {"design_soundness": 3}),
        "user_artifact": _verdict("user_artifact", {"clarity": min(clarity), "trust": min(trust)}),
    }
    dist = {"capping_cells": caps or []}
    return run_dir, verdicts, dist


def test_parse_and_precedence():
    run = {"pass_score": 2.5, "outcomes": [{"id": "o1", "scenes": [1], "dimensions": ["Clarity"]}]}
    spec = {"pass_score": 3.5}
    r = tr.resolve(objective="product", run_rubric=run, spec_rubric=spec, config_rubric={"pass_score": 1})
    assert r.source == "run" and r.pass_score == 2.5
    assert r.outcomes[0].dimensions == ("clarity",) and r.outcomes[0].scenes == (1,)
    assert tr.resolve(objective="product", spec_rubric=spec).source == "spec"
    assert tr.resolve(objective="product", config_rubric={"draws": 5}).draws == 5
    assert tr.resolve(objective="demo").source == "default:demo"


def test_defaults_build_lighter_than_polish():
    v = {"concept": _verdict("concept", {"design_soundness": 3, "visual_polish": 3, "claim_reality_coherence": 1}),
         "user_artifact": _verdict("user_artifact", {"clarity": 3})}
    build, polish = tr.default_for("product", v), tr.default_for("demo", v)
    assert build.pass_score == 3 and set(build.blocking_dimensions) == {"design_soundness", "clarity"}
    assert polish.pass_score == 4 and "visual_polish" in polish.blocking_dimensions
    assert "claim_reality_coherence" not in polish.blocking_dimensions


def test_one_low_cell_no_longer_fails_the_criterion(tmp_path):
    # #492: the weakest-link min failed on a single +/-1 draw. The median does not.
    run_dir, verdicts, dist = _product_run(tmp_path, clarity=[4, 4, 2, 3], trust=[3, 4, 4, 3])
    r = tr.default_for("product", verdicts)
    crit = {c["id"]: c for c in tr.evaluate_pass(r, run_dir=run_dir, verdicts=verdicts, distribution=dist)}
    assert crit["dim:clarity"]["passed"] and crit["dim:clarity"]["cells"] == 4


def test_a_confirmed_break_still_fails_its_criterion(tmp_path):
    caps = [{"scene": "3", "dimension": "clarity", "draws": [2, 2, 3], "confirmed": 2}]
    run_dir, verdicts, dist = _product_run(tmp_path, clarity=[4, 4, 2, 3], trust=[3, 4, 4, 3], caps=caps)
    r = tr.default_for("product", verdicts)
    crit = {c["id"]: c for c in tr.evaluate_pass(r, run_dir=run_dir, verdicts=verdicts, distribution=dist)}
    assert not crit["dim:clarity"]["passed"] and "confirmed broken" in crit["dim:clarity"]["why"]


def test_outcomes_read_their_scenes_or_the_judges_result(tmp_path):
    run_dir, verdicts, dist = _product_run(tmp_path, clarity=[2, 2, 4, 4], trust=[3, 4, 4, 3])
    r = tr.parse(
        {"outcomes": [{"id": "early", "scenes": [1, 2], "dimensions": ["clarity"]},
                      {"id": "late", "scenes": [3, 4], "dimensions": ["clarity"]},
                      {"id": "judged", "pass_when": "x"}],
         "blocking_dimensions": []},
        source="run", defaults=tr.default_for("product", verdicts),
    )
    raw = yaml.safe_load((run_dir / "verdict-concept.yaml").read_text())
    raw["target_outcomes"] = [{"id": "judged", "draws": [True, False, True]}]
    (run_dir / "verdict-concept.yaml").write_text(yaml.safe_dump(raw))
    crit = {c["id"]: c for c in tr.evaluate_pass(r, run_dir=run_dir, verdicts=verdicts, distribution=dist)}
    assert crit["early"]["passed"] is False and crit["late"]["passed"] is True
    assert crit["judged"]["passed"] is True  # 2 of 3 draws


def test_majority_over_full_passes_absorbs_a_flip():
    r = tr.Rubric(pass_score=3, blocking_dimensions=("clarity",))
    hist = [{"iteration": 1, "criteria": {"dim:clarity": True}},
            {"iteration": 2, "criteria": {"dim:clarity": True}},
            {"iteration": 3, "criteria": {"dim:clarity": False}}]
    s = tr.standing(r, [{"id": "dim:clarity", "kind": "dimension", "passed": False}], hist)
    assert s[0]["passing"] is True and s[0]["window"] == [True, True, False]
    # Two of three failing is a real failure.
    hist[1]["criteria"]["dim:clarity"] = False
    assert tr.standing(r, [{"id": "dim:clarity", "kind": "dimension", "passed": False}], hist)[0]["passing"] is False
    # A tie goes to the current pass.
    tie = [{"criteria": {"dim:clarity": False}}, {"criteria": {"dim:clarity": True}}]
    assert tr.standing(r, [{"id": "dim:clarity", "kind": "dimension", "passed": True}], tie)[0]["passing"] is True


def test_partition_out_of_rubric_findings_are_advisory_unless_high():
    r = tr.Rubric(pass_score=3, blocking_dimensions=("clarity",), block_severities=("high",))
    status = [{"id": "dim:clarity", "kind": "dimension", "passing": False}]
    fs = [
        {"dimension": "clarity", "severity": "medium", "route": "PRODUCT"},
        {"dimension": "visual_variety", "severity": "medium", "route": "PRODUCT"},
        {"dimension": "visual_variety", "severity": "high", "route": "PRODUCT"},
        {"dimension": "clarity", "severity": "low", "route": "PRODUCT"},
    ]
    out = tr.partition(fs, r, status)
    assert [f["target_role"] for f in out] == ["blocking", "advisory", "blocking", "advisory"]
    assert out[1]["route"] == "DEFER" and out[1]["deferred_by"] == "target_rubric"
    # Once the criterion passes, its own medium findings stop blocking too.
    passing = tr.partition(fs, r, [{**status[0], "passing": True}])
    assert passing[0]["target_role"] == "advisory"


def test_mechanical_advisory_rides_the_batch_and_a_decision_is_deferred():
    """The rubric decides what blocks, not what gets fixed (canopy#788)."""
    r = tr.Rubric(pass_score=3, blocking_dimensions=("clarity",), block_severities=("high",))
    status = [{"id": "dim:clarity", "kind": "dimension", "passing": True}]
    fs = [
        {"dimension": "visual_variety", "severity": "low", "route": "PRODUCT", "fix_kind": "mechanical"},
        {"dimension": "visual_variety", "severity": "low", "route": "PRODUCT", "fix_kind": "mechanical",
         "parked": True},
        {"dimension": "arc_shape", "severity": "medium", "route": "CONCEPT", "fix_kind": "options"},
    ]
    out = tr.partition(fs, r, status)
    assert [f["target_role"] for f in out] == ["advisory"] * 3
    assert out[0]["route"] == "PRODUCT" and "deferred_by" not in out[0]
    assert out[1]["route"] == "DEFER" and out[2]["route"] == "DEFER"
    assert out[2]["deferred_by"] == "target_rubric"
    # ...and the fix batch picks the mechanical one up.
    from scripts.ddd import fix_scope

    assert fix_scope.batch_plan(out)["findings"] == 1


def test_an_empty_rubric_never_passes_vacuously():
    v = {"concept": _verdict("concept", {"x": 2}, overall=2)}
    v["concept"].dimensions = {}
    r = tr.Rubric(pass_score=3)
    crit = tr.evaluate_pass(r, run_dir=None, verdicts=v, distribution=None)
    assert crit and crit[0]["id"] == "overall:concept" and not crit[0]["passed"]
    assert tr.converged([], [], v)[0] is False


def test_compute_auto_iterate_converges_on_the_target_not_the_min(tmp_path):
    # supply-sophie-sheets: product objective, the final full pass dropped to a 2
    # on one cell with nothing relevant changed — stopped_not_converged. On the
    # target rubric the criteria pass by median and the run is done.
    run_dir, verdicts, dist = _product_run(tmp_path, clarity=[4, 4, 2, 3], trust=[3, 4, 4, 3])
    st = RunState(run_id="r-001", narrative_slug="n", objective="product")
    findings = [{"dimension": "visual_variety", "severity": "medium", "route": "PRODUCT", "fix_kind": "mechanical",
                 "scene": "2", "detail": "same table twice", "fix_recommendation": "vary"}]
    action, reason = compute_auto_iterate(
        st, verdicts["concept"], verdicts["user_artifact"], findings,
        unattended=True, judge_full=True,
        loop_config=loop_config.LoopConfig(objective="product"),
        product_config=loop_config.ProductConfig(polish_pass=False),
        target_rubric={"run": None, "spec": None, "config": None},
        run_dir=run_dir,
    )
    assert action == "stop_done", reason
    assert st.target["converged"] and st.target["source"] == "default:product"
    assert st.target_history and st.target_history[-1]["criteria"]["dim:clarity"] is True
    # Advisory never holds the run open, but a mechanical one is not DEFERred (#788).
    assert st.findings[0]["route"] == "PRODUCT" and st.findings[0]["target_role"] == "advisory"


def test_compute_auto_iterate_keeps_going_while_a_criterion_fails(tmp_path):
    run_dir, verdicts, dist = _product_run(tmp_path, clarity=[2, 2, 3, 2], trust=[3, 4, 4, 3])
    st = RunState(run_id="r-001", narrative_slug="n", objective="product")
    findings = [{"dimension": "clarity", "severity": "medium", "route": "PRODUCT", "fix_kind": "mechanical",
                 "scene": "1", "detail": "status unclear", "fix_recommendation": "show a status chip"}]
    action, reason = compute_auto_iterate(
        st, verdicts["concept"], verdicts["user_artifact"], findings,
        unattended=True, judge_full=True,
        loop_config=loop_config.LoopConfig(objective="product"),
        target_rubric={"run": {"blocking_dimensions": ["clarity"]}},
        run_dir=run_dir,
    )
    assert action == "continue", reason
    assert st.target["source"] == "run" and not st.target["converged"]
    assert st.findings[0]["target_role"] == "blocking"


def test_incremental_pass_reads_provisionally_and_records_nothing(tmp_path):
    run_dir, verdicts, dist = _product_run(tmp_path, clarity=[4, 4, 4, 3], trust=[3, 4, 4, 3])
    st = RunState(run_id="r-001", narrative_slug="n", objective="product")
    r = tr.default_for("product", verdicts)
    out = tr.evaluate(r, st, [], run_dir=run_dir, verdicts=verdicts, distribution=dist, judge_full=False)
    assert out["converged"] is True and st.target_history == []


def test_spec_carries_a_target_rubric(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump({"target_rubric": {"pass_score": 3}}))
    assert tr.spec_rubric(p) == {"pass_score": 3}
    assert tr.spec_rubric(None) is None
