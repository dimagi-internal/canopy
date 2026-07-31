"""The deterministic lenses added after the first narrative went through the loop.

Every case here is drawn from a defect that actually cost a render + a judge
round on oes-partner-pipeline, so the tests double as the record of why each
lens exists.
"""
from __future__ import annotations

import json

import pytest

from scripts.ddd.data_fidelity import data_fidelity
from scripts.ddd.narrated_numbers import narrated_values, page_values
from scripts.ddd.narrated_numbers import check as narrated_check
from scripts.ddd.regression_guard import record


# ---------------------------------------------------------------------------
# data_fidelity
# ---------------------------------------------------------------------------


def _table(rows: list[list[str]]) -> str:
    return "\n".join("\t".join(r) for r in rows)


def test_identical_derived_column_is_flagged():
    """Eleven calendar rows all reading 214 children / 214 cartons."""
    rows = [[f"Site {i}", "214", "214", "covered"] for i in range(11)]
    result = data_fidelity(_table(rows))
    kinds = {f["kind"] for f in result["findings"]}
    assert "identical_column" in kinds
    assert result["verdict"] == "warn"


def test_a_column_of_zeros_is_not_a_finding():
    """Eleven empty sites honestly reading 0 is real data, not a generator."""
    rows = [[f"Site {i}", "0", "uncovered"] for i in range(11)]
    result = data_fidelity(_table(rows))
    assert not [f for f in result["findings"] if f["kind"] == "identical_column"]


def test_a_repeated_category_is_not_a_finding():
    """A status column with one value is a fact about the world, not a tell."""
    rows = [[f"Site {i}", "covered", f"{100 + i * 7}"] for i in range(10)]
    result = data_fidelity(_table(rows))
    assert not [f for f in result["findings"] if f["kind"] == "identical_column"]


def test_a_short_table_is_left_alone():
    """Three identical values is a coincidence; the lens must not cry wolf."""
    rows = [[f"Site {i}", "214", "214"] for i in range(3)]
    assert data_fidelity(_table(rows))["findings"] == []


def test_clustered_ratios_are_flagged():
    """Outcome capture landing 4.0-4.7% on every row."""
    served = [246, 310, 139, 177, 310, 219, 185, 258]
    rows = [[f"Site {i}", str(s), str(round(s * 0.042))] for i, s in enumerate(served)]
    result = data_fidelity(_table(rows))
    assert any(f["kind"] == "ratio_cluster" for f in result["findings"])


def test_lumpy_ratios_pass():
    """Real capture varies by site; that must not read as a defect."""
    pairs = [(246, 4), (310, 22), (139, 4), (177, 4), (310, 22), (219, 7), (185, 12), (258, 5)]
    rows = [[f"Site {i}", str(a), str(b)] for i, (a, b) in enumerate(pairs)]
    result = data_fidelity(_table(rows))
    assert not [f for f in result["findings"] if f["kind"] == "ratio_cluster"]


def test_a_small_magnitude_ratio_is_judged_on_its_own_scale():
    """Stock-on-hand beside weeks-of-cover, from the real OES command centre.

    The ratio here spans 0.0019–0.0158 — an EIGHTFOLD variation — but its
    absolute spread is 0.0139, so an absolute floor of 0.02 flagged it as
    "near-constant" on every scene of every arc rendering this table. The
    question is how much a ratio varies relative to its own size, not whether it
    happens to be a small number.
    """
    pairs = [(38, 0.6), (375, 1.6), (900, 1.7), (282, 2.0), (274, 2.5), (469, 3.0), (14760, 5.3)]
    rows = [[f"Node {i}", str(a), str(b)] for i, (a, b) in enumerate(pairs)]
    result = data_fidelity(_table(rows))
    assert not [f for f in result["findings"] if f["kind"] == "ratio_cluster"]


def test_a_tight_small_magnitude_ratio_is_still_flagged():
    """The fix must not blind the lens to a genuinely flat small ratio.

    Same order of magnitude as the case above, but held to a fixed 0.4% of the
    first column — which is exactly the generator tell the check exists for.
    """
    stock = [38, 375, 900, 282, 274, 469, 14760]
    rows = [[f"Node {i}", str(s), f"{s * 0.004:.4f}"] for i, s in enumerate(stock)]
    result = data_fidelity(_table(rows))
    assert any(f["kind"] == "ratio_cluster" for f in result["findings"])


def test_duplicate_rows_are_flagged():
    """Two different sites, both '310 served / 22 recorded'."""
    rows = [
        ["Biu", "310", "22"],
        ["Gwoza", "310", "22"],
        ["Damboa", "139", "6"],
        ["Dikwa", "177", "7"],
        ["Konduga", "219", "9"],
        ["Mafa", "258", "10"],
    ]
    result = data_fidelity(_table(rows))
    assert any(f["kind"] == "duplicate_row" for f in result["findings"])


def test_round_robin_identifiers_are_flagged():
    """Batch ids cycling through a short repeating set down the column."""
    lots = ["LOT2600A", "LOT2601A", "LOT2602A"] * 4
    rows = [[f"Site {i}", lot, str(100 + i * 13)] for i, lot in enumerate(lots)]
    result = data_fidelity(_table(rows))
    assert any(f["kind"] == "round_robin_ids" for f in result["findings"])


# ---------------------------------------------------------------------------
# narrated_numbers
# ---------------------------------------------------------------------------


def test_spelled_out_numbers_are_read():
    """A narration written for the ear spells its numbers out."""
    values = dict(narrated_values("Kukawa is eleven days out."))
    assert 11 in values.values()


def test_compound_word_numbers_are_read():
    values = [v for _s, v in narrated_values("twelve thousand children by September")]
    assert 12000 in values


def test_small_numbers_are_ignored():
    """'two stages' is prose, not a data claim."""
    assert not narrated_values("This is how that becomes two stages.")


def test_an_unsupported_narrated_number_is_caught(tmp_path):
    """The exact defect that survived three iterations."""
    run = tmp_path / "run"
    (run / "snapshots").mkdir(parents=True)
    (run / "snapshots" / "scene_1_page_text.json").write_text(
        json.dumps({"page_text": "Kukawa Nutrition Centre\t1.4\tAug 5, 2026\t9 days"})
    )
    spec = tmp_path / "demo.yaml"
    spec.write_text(
        "name: demo\nnarrative: x\nbase_url: http://x\npersonas: {}\n"
        "scenes:\n"
        "- id: s1\n  persona: p\n  provenance: S0\n  title: A scene\n"
        "  concept_claim: A claim that is specific and observable here.\n"
        "  show: something\n"
        "  narrative: Kukawa is eleven days out.\n"
    )
    result = narrated_check(run, spec)
    assert result["verdict"] == "fail"
    assert result["findings"][0]["value"] == 11


def test_a_rounded_narration_passes(tmp_path):
    """'about twelve thousand' over 12,058 is honest rounding."""
    run = tmp_path / "run"
    (run / "snapshots").mkdir(parents=True)
    (run / "snapshots" / "scene_1_page_text.json").write_text(
        json.dumps({"page_text": "Borno\t12,058\tIPC 5"})
    )
    spec = tmp_path / "demo.yaml"
    spec.write_text(
        "name: demo\nnarrative: x\nbase_url: http://x\npersonas: {}\n"
        "scenes:\n"
        "- id: s1\n  persona: p\n  provenance: S0\n  title: A scene\n"
        "  concept_claim: A claim that is specific and observable here.\n"
        "  show: something\n"
        "  narrative: about twelve thousand children are expected this month.\n"
    )
    assert narrated_check(run, spec)["verdict"] == "pass"


def test_page_values_reads_thousands_separators():
    assert 12058.0 in page_values("Borno 12,058 children")


# ---------------------------------------------------------------------------
# regression_guard
# ---------------------------------------------------------------------------


def _write_report(run, actions):
    (run / "run-report.json").write_text(json.dumps({"actions": actions}))


def test_first_iteration_has_nothing_to_compare(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    _write_report(run, [{"scene_index": 1, "kind": "click", "target": "a", "ok": True}])
    result = record(run)
    assert result["verdict"] == "pass"
    assert result["previous_actions_ok"] is None


def test_an_action_that_stops_working_is_a_regression(tmp_path):
    """The fix that removed the button the next scene clicked."""
    run = tmp_path / "run"
    run.mkdir()
    _write_report(run, [{"scene_index": 5, "kind": "click", "target": "btn", "ok": True}])
    record(run)

    _write_report(run, [{"scene_index": 5, "kind": "click", "target": "btn", "ok": False}])
    result = record(run)
    assert result["verdict"] == "fail"
    assert result["regressions"][0]["kind"] == "action_regression"


def test_a_newly_passing_action_is_not_a_regression(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    _write_report(run, [{"scene_index": 1, "kind": "click", "target": "a", "ok": False}])
    record(run)
    _write_report(run, [{"scene_index": 1, "kind": "click", "target": "a", "ok": True}])
    assert record(run)["verdict"] == "pass"


def test_score_moves_are_reported_but_do_not_fail(tmp_path):
    """A judge is not deterministic; a dip is information, not a gate."""
    import yaml

    run = tmp_path / "run"
    run.mkdir()
    _write_report(run, [{"scene_index": 1, "kind": "click", "target": "a", "ok": True}])
    (run / "verdict-concept.yaml").write_text(yaml.dump({"dimensions": {"clarity": {"score": 4}}}))
    record(run)

    (run / "verdict-concept.yaml").write_text(yaml.dump({"dimensions": {"clarity": {"score": 3}}}))
    result = record(run)
    assert result["verdict"] == "pass"
    assert result["score_moves"][0] == {"dimension": "clarity", "from": 4.0, "to": 3.0, "direction": "down"}


# ---------------------------------------------------------------------------
# viewport-accurate capture for DDD runs
# ---------------------------------------------------------------------------


class _Args:
    def __init__(self, ddd=False, full_page=False):
        self.ddd_orchestrated = ddd
        self.full_page_snapshots = full_page


def test_a_ddd_run_captures_the_viewport_by_default():
    """A full-page strip is not what a user sees.

    Every judge in the first four-way dispatch opened by discounting the strip
    by hand before scoring — attention spent on an artifact of our capture, on
    every scene, every round.
    """
    from scripts.walkthrough._lib.capture_mode import snapshot_full_page

    assert snapshot_full_page({}, _Args(ddd=True)) is False


def test_a_non_ddd_run_keeps_the_historical_default():
    """Existing narratives must be untouched by this change."""
    from scripts.walkthrough._lib.capture_mode import snapshot_full_page

    assert snapshot_full_page({}, _Args()) is None


def test_an_explicit_scene_setting_always_wins():
    """A map+table scene that asks for the viewport still gets it, and a scene
    that genuinely wants the strip can say so even inside a DDD run."""
    from scripts.walkthrough._lib.capture_mode import snapshot_full_page

    assert snapshot_full_page({"full_page": True}, _Args(ddd=True)) is True
    assert snapshot_full_page({"full_page": False}, _Args()) is False


def test_the_escape_hatch_restores_the_strip():
    from scripts.walkthrough._lib.capture_mode import snapshot_full_page

    assert snapshot_full_page({}, _Args(ddd=True, full_page=True)) is True


# ---------------------------------------------------------------------------
# preflight must not walk a world its own previous run mutated
# ---------------------------------------------------------------------------


def test_preflight_reads_a_pydantic_setup_block(tmp_path):
    """The reseed silently never ran.

    preflight APPLIES state-changing actions so a scene that depends on an
    earlier click is checked against the screen it will really face — which
    makes it a mutator. Walking a recipe that awards two lots leaves them
    awarded, so the next run finds the controls gone. It reseeds via the spec's
    own setup command to undo that, and reading SetupBlock as a plain dict
    returned None for the parsed-spec case, so it never did.
    """
    from scripts.ddd.recipe_preflight import _setup_command

    class _Block:
        def model_dump(self):
            return {"command": "python seed.py", "timeout_seconds": 42}

    assert _setup_command(_Block()) == ("python seed.py", 42)
    assert _setup_command({"command": "x"}) == ("x", 600)
    assert _setup_command(None) == (None, 600)
