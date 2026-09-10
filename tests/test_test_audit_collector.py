"""Tests for orchestrator.test_audit.collector."""
from pathlib import Path

from orchestrator.test_audit.collector import collect


FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_suite"


def test_collect_finds_all_test_functions():
    items = collect(FIXTURE)
    names = sorted(it.name for it in items)
    assert names == sorted([
        "test_add_returns_sum",
        "test_add_with_negatives",
        "test_always_passes",
        "test_no_assertion",
        "test_env_fragile",
        "test_subtraction_works",
        "test_add_with_mock_of_cut",
    ])


def test_collect_nodeid_includes_relative_file_path():
    items = collect(FIXTURE)
    item = next(it for it in items if it.name == "test_add_returns_sum")
    assert item.nodeid.endswith("test_calculator.py::test_add_returns_sum")


def test_collect_skips_non_test_files(tmp_path):
    (tmp_path / "main.py").write_text("def foo(): pass\ndef test_x(): pass\n")
    (tmp_path / "test_real.py").write_text("def test_real(): assert True\n")
    items = collect(tmp_path)
    assert [it.name for it in items] == ["test_real"]


def test_collect_respects_pyproject_norecursedirs(tmp_path):
    """If pyproject.toml has [tool.pytest.ini_options].norecursedirs, the
    collector must skip those dirs — otherwise fixture suites with
    intentionally-bad tests pollute the audit corpus."""
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        'norecursedirs = ["tests/fixtures"]\n'
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_real.py").write_text("def test_real(): assert 1 == 1\n")
    fixtures = tmp_path / "tests" / "fixtures" / "synthetic"
    fixtures.mkdir(parents=True)
    (fixtures / "test_bad.py").write_text("def test_bad(): assert True\n")

    names = sorted(it.name for it in collect(tmp_path))
    assert names == ["test_real"]
    assert "test_bad" not in names


def test_collect_respects_pyproject_testpaths(tmp_path):
    """If pyproject.toml has testpaths, only walk those directories."""
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        'testpaths = ["tests"]\n'
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_in_scope.py").write_text("def test_a(): assert 1\n")
    (tmp_path / "scratch").mkdir()
    (tmp_path / "scratch" / "test_out_of_scope.py").write_text("def test_b(): assert 1\n")

    names = sorted(it.name for it in collect(tmp_path))
    assert names == ["test_a"]


def test_collect_works_when_no_pyproject(tmp_path):
    """No pyproject = walk everything (existing behavior preserved)."""
    (tmp_path / "test_a.py").write_text("def test_a(): assert 1\n")
    (tmp_path / "deep").mkdir()
    (tmp_path / "deep" / "test_b.py").write_text("def test_b(): assert 1\n")
    names = sorted(it.name for it in collect(tmp_path))
    assert names == ["test_a", "test_b"]


def test_detection_prefers_the_framework_with_more_tests(tmp_path):
    """A Django service with a little front-end tooling has vitest in
    package.json and thousands of pytest tests. Detection used to return vitest
    on sight of the dep, auditing the 143 JS tests and silently ignoring 6,012
    Python ones."""
    import json

    from orchestrator.test_audit.framework import detect_framework

    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (tmp_path / "package.json").write_text(json.dumps({"devDependencies": {"vitest": "^2"}}))

    (tmp_path / "app" / "tests").mkdir(parents=True)
    for i in range(12):
        (tmp_path / "app" / "tests" / f"test_m{i}.py").write_text(f"def test_{i}():\n    assert True\n")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "one.test.ts").write_text("import {it, expect} from 'vitest'\nit('a', () => expect(1).toBe(1))\n")

    assert detect_framework(tmp_path).name == "pytest"


def test_detection_still_picks_vitest_for_a_js_repo(tmp_path):
    import json

    from orchestrator.test_audit.framework import detect_framework

    (tmp_path / "package.json").write_text(json.dumps({"devDependencies": {"vitest": "^2"}}))
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.test.ts").write_text("import {it, expect} from 'vitest'\nit('a', () => expect(1).toBe(1))\n")

    assert detect_framework(tmp_path).name == "vitest"
