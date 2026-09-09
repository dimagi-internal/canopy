"""Tests for orchestrator.test_audit.runner — junit-xml parsing and parametrize handling."""
import textwrap
from pathlib import Path

from orchestrator.test_audit.runner import _normalize_nodeid, _parse_junit


def test_normalize_nodeid_simple():
    assert _normalize_nodeid("tests.test_foo", "test_bar") == "tests/test_foo.py::test_bar"


def test_normalize_nodeid_with_class():
    nid = _normalize_nodeid("tests.test_foo.TestBar", "test_baz")
    assert nid == "tests/test_foo.py::TestBar::test_baz"


def test_normalize_nodeid_strips_parametrize_suffix():
    """junit-xml emits `name="test_bar[param1]"` for parametrized tests.
    The collector emits `nodeid=test_bar` (no suffix). The runner must
    strip the `[...]` so results match items by base nodeid."""
    nid = _normalize_nodeid("tests.test_foo", "test_bar[param1]")
    assert nid == "tests/test_foo.py::test_bar"


def test_normalize_nodeid_strips_complex_parametrize():
    nid = _normalize_nodeid("tests.test_foo", "test_x[1-True-bar]")
    assert nid == "tests/test_foo.py::test_x"


def test_parse_junit_aggregates_parametrize_results(tmp_path):
    """Multiple <testcase> entries differing only by [param] suffix should
    collapse to a single TestResult per base nodeid. If any param failed,
    the aggregate status is 'failed'; otherwise 'passed'."""
    xml = tmp_path / "junit.xml"
    xml.write_text(textwrap.dedent("""\
        <?xml version="1.0" encoding="utf-8"?>
        <testsuites>
          <testsuite name="pytest" tests="3">
            <testcase classname="tests.test_foo" name="test_x[a]" time="0.01"/>
            <testcase classname="tests.test_foo" name="test_x[b]" time="0.02"/>
            <testcase classname="tests.test_foo" name="test_x[c]" time="0.03">
              <failure message="boom"/>
            </testcase>
          </testsuite>
        </testsuites>
    """))
    results = _parse_junit(xml)
    assert "tests/test_foo.py::test_x" in results
    r = results["tests/test_foo.py::test_x"]
    assert r.status == "failed"
    # Sum of durations across all params.
    assert r.duration_ms >= 60


def test_parse_junit_passing_parametrize_aggregates_to_passed(tmp_path):
    xml = tmp_path / "junit.xml"
    xml.write_text(textwrap.dedent("""\
        <?xml version="1.0" encoding="utf-8"?>
        <testsuites>
          <testsuite name="pytest" tests="2">
            <testcase classname="tests.test_foo" name="test_x[a]" time="0.01"/>
            <testcase classname="tests.test_foo" name="test_x[b]" time="0.02"/>
          </testsuite>
        </testsuites>
    """))
    results = _parse_junit(xml)
    assert results["tests/test_foo.py::test_x"].status == "passed"


def test_a_pytest_run_that_never_starts_says_so(tmp_path, monkeypatch):
    """An empty JUnit XML used to surface as `ParseError: no element found`,
    which names neither pytest nor the environment that broke it."""
    import subprocess

    import pytest as _pytest

    from orchestrator.test_audit import runner

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 4, stdout="", stderr="ImproperlyConfigured: set DJANGO_SETTINGS_MODULE")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    with _pytest.raises(RuntimeError) as exc:
        runner.run_pytest(tmp_path)

    message = str(exc.value)
    assert "--no-run" in message
    assert "ImproperlyConfigured" in message


def test_missing_pytest_binary_names_the_venv(tmp_path, monkeypatch):
    import pytest as _pytest

    from orchestrator.test_audit import runner

    def boom(cmd, **kwargs):
        raise FileNotFoundError(2, "No such file or directory: 'pytest'")

    monkeypatch.setattr(runner.subprocess, "run", boom)

    with _pytest.raises(RuntimeError, match="virtualenv"):
        runner.run_pytest(tmp_path)
