"""Tests for orchestrator.test_audit.applier — planning, source surgery, and
verdicts.yaml round-trip via apply_from_dir (without git/gh).
"""
import shutil
from pathlib import Path

import yaml

from orchestrator.test_audit.collector import TestItem, collect
from orchestrator.test_audit.applier import (
    Verdict, plan, _delete_test, _skip_mark_test,
    _parse_verdicts_yaml, apply_from_dir, _materialize_pr_body,
)


FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_suite"


def _items_by_id(*nodeids, file: Path):
    return {
        nid: TestItem(nodeid=nid, file=file, name=nid.split("::")[-1], line=1)
        for nid in nodeids
    }


def test_plan_skip_marks_env_fragile_regardless_of_score(tmp_path):
    f = tmp_path / "test_x.py"
    f.write_text("def test_a(): pass\n")
    items = _items_by_id("test_x.py::test_a", file=f)
    verdicts = {
        "test_x.py::test_a": Verdict("test_x.py::test_a", 8, "prune",
                                     "env-fragile", "missing module"),
    }
    changes = plan(items, verdicts)
    assert len(changes) == 1
    assert changes[0].action == "skip"


def test_plan_deletes_only_low_score_prunes_by_default(tmp_path):
    f = tmp_path / "test_x.py"
    f.write_text("def test_a(): pass\ndef test_b(): pass\n")
    items = _items_by_id("test_x.py::test_a", "test_x.py::test_b", file=f)
    verdicts = {
        "test_x.py::test_a": Verdict("test_x.py::test_a", 2, "prune", "tautology"),
        "test_x.py::test_b": Verdict("test_x.py::test_b", 5, "prune", "weak"),
    }
    changes = plan(items, verdicts)
    assert [c.action for c in changes] == ["delete"]
    assert changes[0].nodeid.endswith("test_a")


def test_plan_aggressive_includes_mid_score_prunes(tmp_path):
    f = tmp_path / "test_x.py"
    f.write_text("def test_a(): pass\ndef test_b(): pass\n")
    items = _items_by_id("test_x.py::test_a", "test_x.py::test_b", file=f)
    verdicts = {
        "test_x.py::test_a": Verdict("test_x.py::test_a", 2, "prune", "tautology"),
        "test_x.py::test_b": Verdict("test_x.py::test_b", 5, "prune", "weak"),
    }
    changes = plan(items, verdicts, aggressive=True)
    assert sorted(c.nodeid.split("::")[-1] for c in changes) == ["test_a", "test_b"]


def test_plan_never_applies_refactor_or_investigate(tmp_path):
    f = tmp_path / "test_x.py"
    f.write_text("def test_a(): pass\ndef test_b(): pass\n")
    items = _items_by_id("test_x.py::test_a", "test_x.py::test_b", file=f)
    verdicts = {
        "test_x.py::test_a": Verdict("test_x.py::test_a", 3, "refactor", "unclear"),
        "test_x.py::test_b": Verdict("test_x.py::test_b", 2, "investigate", "failing"),
    }
    assert plan(items, verdicts, aggressive=True) == []


def test_delete_test_removes_function(tmp_path):
    f = tmp_path / "test_x.py"
    f.write_text(
        "def test_keep():\n    assert True\n\n"
        "def test_drop():\n    assert False\n\n"
        "def test_other():\n    assert 1 == 1\n"
    )
    assert _delete_test(f, "test_drop")
    src = f.read_text()
    assert "test_keep" in src and "test_other" in src and "test_drop" not in src


def test_skip_mark_inserts_decorator_and_import(tmp_path):
    f = tmp_path / "test_x.py"
    f.write_text("def test_a():\n    assert 1 == 1\n")
    assert _skip_mark_test(f, "test_a", "missing module foo")
    src = f.read_text()
    assert "import pytest" in src
    assert "@pytest.mark.skip" in src
    assert "missing module foo" in src


def test_skip_mark_idempotent_when_already_skipped(tmp_path):
    f = tmp_path / "test_x.py"
    f.write_text(
        "import pytest\n\n"
        "@pytest.mark.skip(reason='already')\n"
        "def test_a():\n    assert 1 == 1\n"
    )
    before = f.read_text()
    assert _skip_mark_test(f, "test_a", "new reason") is False
    assert f.read_text() == before


def test_parse_verdicts_yaml_accepts_top_level_list():
    raw = [
        {"nodeid": "a::t1", "score": 1, "verdict": "prune", "reason_code": "tautology"},
        {"nodeid": "a::t2", "score": 8, "verdict": "keep", "reason_code": "ok"},
    ]
    verdicts = _parse_verdicts_yaml(raw)
    assert set(verdicts.keys()) == {"a::t1", "a::t2"}
    assert verdicts["a::t1"].verdict == "prune"


def test_parse_verdicts_yaml_accepts_dict_wrapper():
    raw = {"verdicts": [
        {"nodeid": "a::t1", "score": 4, "verdict": "refactor", "reason_code": "unclear"},
    ]}
    verdicts = _parse_verdicts_yaml(raw)
    assert verdicts["a::t1"].score == 4


def test_parse_verdicts_yaml_skips_invalid_entries():
    raw = [
        {"nodeid": "ok::t", "score": 1, "verdict": "prune", "reason_code": "x"},
        "not-a-dict",
        {"score": 5},  # missing nodeid
    ]
    verdicts = _parse_verdicts_yaml(raw)
    assert list(verdicts.keys()) == ["ok::t"]


def test_materialize_pr_body_combines_audit_and_architecture(tmp_path):
    (tmp_path / "audit-report.md").write_text("# Audit\n\nbody.\n")
    (tmp_path / "architecture-review.md").write_text("# Arch\n\narch body.\n")
    out = _materialize_pr_body(tmp_path)
    assert out == tmp_path / "pr-body.md"
    text = out.read_text()
    assert "# Audit" in text
    assert "# Arch" in text
    assert "---" in text  # separator


def test_materialize_pr_body_returns_audit_only_when_arch_missing(tmp_path):
    audit = tmp_path / "audit-report.md"
    audit.write_text("# Audit\n")
    out = _materialize_pr_body(tmp_path)
    assert out == audit  # no combined file written


def test_materialize_pr_body_returns_none_when_both_missing(tmp_path):
    assert _materialize_pr_body(tmp_path) is None


def test_apply_from_dir_dry_run_against_synthetic_suite(tmp_path):
    """End-to-end: write verdicts.yaml, apply with dry_run=True, assert plan."""
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copytree(FIXTURE, repo / "tests")

    # Find real nodeids from the synthetic fixture.
    items = collect(repo)
    by_name = {it.name: it for it in items}

    stamp = repo / ".canopy" / "test-audits" / "20260428-000000"
    stamp.mkdir(parents=True)
    verdicts_payload = {
        "verdicts": [
            {"nodeid": by_name["test_always_passes"].nodeid, "score": 1,
             "verdict": "prune", "reason_code": "tautology", "reason": "no-op"},
            {"nodeid": by_name["test_env_fragile"].nodeid, "score": 6,
             "verdict": "prune", "reason_code": "env-fragile",
             "reason": "missing module"},
            {"nodeid": by_name["test_subtraction_works"].nodeid, "score": 4,
             "verdict": "refactor", "reason_code": "name-mismatch",
             "reason": "rename"},
            {"nodeid": by_name["test_add_returns_sum"].nodeid, "score": 8,
             "verdict": "keep", "reason_code": "ok", "reason": ""},
        ]
    }
    (stamp / "verdicts.yaml").write_text(yaml.safe_dump(verdicts_payload))

    result = apply_from_dir(stamp, repo=repo, dry_run=True)
    actions = sorted((c.nodeid.split("::")[-1], c.action) for c in result.changes)
    assert actions == [("test_always_passes", "delete"),
                       ("test_env_fragile", "skip")]


def test_verdicts_with_a_null_score_parse_rather_than_crash(tmp_path):
    """SKILL.md tells the judge to write `score: null` for not-sampled tests on
    a large suite, so the applier has to accept it. `int(None)` raised, which
    took down a 6,012-test audit at the apply step after all the judging was
    already paid for."""
    from orchestrator.test_audit.applier import _parse_verdicts_yaml

    parsed = _parse_verdicts_yaml(
        {
            "verdicts": [
                {"nodeid": "t.py::a", "score": None, "verdict": "keep", "reason_code": "not-sampled"},
                {"nodeid": "t.py::b", "score": 2, "verdict": "prune", "reason_code": "tautology"},
            ]
        }
    )

    assert parsed["t.py::a"].score == 0
    assert parsed["t.py::a"].verdict == "keep"
    assert parsed["t.py::b"].score == 2
