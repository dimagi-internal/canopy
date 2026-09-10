"""Tests for orchestrator.test_audit.architecture — suite-level grist."""
from pathlib import Path

from orchestrator.test_audit.architecture import (
    module_inventory, mock_density_by_file, slow_tests,
    ModuleInfo, MockDensity,
)
from orchestrator.test_audit.collector import TestItem
from orchestrator.test_audit.parser import StaticAnalysis
from orchestrator.test_audit.runner import TestResult


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _stub_static(file_rel: str, asserts: int, mocks: list[str]) -> StaticAnalysis:
    return StaticAnalysis(
        nodeid=f"{file_rel}::test_x", name="test_x", body_source="",
        assertion_count=asserts, mock_targets=mocks,
    )


# ---------- module_inventory ----------

def test_module_inventory_pairs_src_modules_with_their_test_files(tmp_path):
    _write(tmp_path / "src" / "pkg" / "alpha.py",
           "def hello():\n    pass\n\ndef _private():\n    pass\n")
    _write(tmp_path / "src" / "pkg" / "beta.py", "def world(): pass\n")
    _write(tmp_path / "tests" / "test_alpha.py", "def test_a(): assert 1\n")
    # Note: no test_beta.py — beta should be flagged untested.

    inv = module_inventory(tmp_path, src_root="src/pkg", tests_root="tests")

    by_name = {m.module_name: m for m in inv}
    assert by_name["alpha"].has_test_file is True
    assert by_name["alpha"].test_file_path is not None
    assert by_name["beta"].has_test_file is False
    assert by_name["beta"].test_file_path is None


def test_module_inventory_counts_public_functions(tmp_path):
    _write(tmp_path / "src" / "pkg" / "x.py",
           "def public_one(): pass\n"
           "def _private(): pass\n"
           "def public_two(): pass\n"
           "class _PrivateClass: pass\n"
           "class PublicClass: pass\n")
    _write(tmp_path / "tests" / "test_x.py", "def test_a(): assert 1\n")
    inv = module_inventory(tmp_path, src_root="src/pkg", tests_root="tests")
    [info] = [m for m in inv if m.module_name == "x"]
    # public_one + public_two = 2 (private functions excluded). Classes optional.
    assert info.public_func_count == 2


def test_module_inventory_skips_dunder_and_init_files(tmp_path):
    _write(tmp_path / "src" / "pkg" / "__init__.py", "")
    _write(tmp_path / "src" / "pkg" / "real.py", "def f(): pass\n")
    inv = module_inventory(tmp_path, src_root="src/pkg", tests_root="tests")
    names = {m.module_name for m in inv}
    assert "real" in names
    assert "__init__" not in names


def test_module_inventory_handles_missing_src_root(tmp_path):
    inv = module_inventory(tmp_path, src_root="src/missing", tests_root="tests")
    assert inv == []


def test_module_inventory_recognizes_module_imported_by_test(tmp_path):
    """A module without a name-matched test_<name>.py file but imported by
    SOME test should be marked has_test_file=True. Pure name-matching
    misses nested layouts like test_test_audit_collector.py covering
    test_audit/collector.py."""
    _write(tmp_path / "src" / "orchestrator" / "test_audit" / "collector.py",
           "def collect(): pass\n")
    # The test file's name doesn't match `test_collector.py` — but it imports
    # the module. Old heuristic missed this; new one must catch it.
    _write(tmp_path / "tests" / "test_test_audit_collector.py",
           "from orchestrator.test_audit.collector import collect\n"
           "def test_a(): assert callable(collect)\n")

    inv = module_inventory(tmp_path,
                           src_root="src/orchestrator/test_audit",
                           tests_root="tests")
    [info] = [m for m in inv if m.module_name == "collector"]
    assert info.has_test_file is True


def test_module_inventory_marks_truly_uncovered_module(tmp_path):
    """No test_<name>.py and no test importing the module => has_test_file=False."""
    _write(tmp_path / "src" / "orchestrator" / "skill_runner.py",
           "def run_skill(): pass\n")
    _write(tmp_path / "tests" / "test_unrelated.py",
           "def test_a(): assert True\n")

    inv = module_inventory(tmp_path, src_root="src/orchestrator",
                           tests_root="tests")
    [info] = [m for m in inv if m.module_name == "skill_runner"]
    assert info.has_test_file is False


def test_module_inventory_recognizes_from_package_import(tmp_path):
    """`from orchestrator.test_audit import collector` (importing the
    submodule via parent package) should also count."""
    _write(tmp_path / "src" / "orchestrator" / "test_audit" / "collector.py",
           "def collect(): pass\n")
    _write(tmp_path / "tests" / "test_x.py",
           "from orchestrator.test_audit import collector\n"
           "def test_a(): assert callable(collector.collect)\n")

    inv = module_inventory(tmp_path,
                           src_root="src/orchestrator/test_audit",
                           tests_root="tests")
    [info] = [m for m in inv if m.module_name == "collector"]
    assert info.has_test_file is True


# ---------- mock_density_by_file ----------

def test_mock_density_aggregates_per_test_file():
    items = [
        TestItem(nodeid="tests/test_a.py::t1", file=Path("/x/tests/test_a.py"), name="t1", line=1),
        TestItem(nodeid="tests/test_a.py::t2", file=Path("/x/tests/test_a.py"), name="t2", line=5),
        TestItem(nodeid="tests/test_b.py::t1", file=Path("/x/tests/test_b.py"), name="t1", line=1),
    ]
    statics = {
        "tests/test_a.py::t1": _stub_static("tests/test_a.py", asserts=1, mocks=["a", "b"]),
        "tests/test_a.py::t2": _stub_static("tests/test_a.py", asserts=2, mocks=["c"]),
        "tests/test_b.py::t1": _stub_static("tests/test_b.py", asserts=4, mocks=[]),
    }
    densities = mock_density_by_file(items, statics)
    by_file = {d.file: d for d in densities}
    assert by_file["tests/test_a.py"].total_mocks == 3
    assert by_file["tests/test_a.py"].total_assertions == 3
    assert by_file["tests/test_b.py"].total_mocks == 0
    assert by_file["tests/test_b.py"].total_assertions == 4


def test_mock_density_flags_overmocked_files():
    items = [
        TestItem(nodeid="tests/test_x.py::t1", file=Path("/x/tests/test_x.py"), name="t1", line=1),
    ]
    statics = {
        "tests/test_x.py::t1": _stub_static("tests/test_x.py", asserts=1, mocks=["a", "b", "c", "d"]),
    }
    densities = mock_density_by_file(items, statics)
    [d] = densities
    # 4 mocks for 1 assertion = mock-heavy.
    assert d.is_overmocked is True


# ---------- slow_tests ----------

def test_slow_tests_filters_by_threshold():
    runtimes = {
        "tests/x.py::fast": TestResult(nodeid="tests/x.py::fast", status="passed", duration_ms=10),
        "tests/x.py::medium": TestResult(nodeid="tests/x.py::medium", status="passed", duration_ms=500),
        "tests/x.py::slow": TestResult(nodeid="tests/x.py::slow", status="passed", duration_ms=2500),
    }
    slow = slow_tests(runtimes, threshold_ms=1000)
    assert [s.nodeid for s in slow] == ["tests/x.py::slow"]


def test_slow_tests_sorts_descending_by_duration():
    runtimes = {
        "a": TestResult(nodeid="a", status="passed", duration_ms=1500),
        "b": TestResult(nodeid="b", status="passed", duration_ms=3000),
        "c": TestResult(nodeid="c", status="passed", duration_ms=2000),
    }
    slow = slow_tests(runtimes, threshold_ms=1000)
    assert [s.nodeid for s in slow] == ["b", "c", "a"]


def test_module_inventory_finds_tests_that_live_beside_their_app(tmp_path):
    """Django and many packages co-locate `<app>/tests/test_*.py` instead of a
    top-level `tests/`. The old lookup only searched `<repo>/tests`, so those
    repos reported EVERY module untested — a 100% figure that is always noise.
    """
    from orchestrator.test_audit.architecture import module_inventory

    (tmp_path / "pkg" / "billing" / "tests").mkdir(parents=True)
    (tmp_path / "pkg" / "billing" / "invoices.py").write_text("def total():\n    return 1\n")
    (tmp_path / "pkg" / "billing" / "refunds.py").write_text("def refund():\n    return 1\n")
    (tmp_path / "pkg" / "billing" / "tests" / "test_invoices.py").write_text("def test_total():\n    pass\n")

    inv = {m.module_name: m for m in module_inventory(tmp_path, src_root="pkg")}

    assert inv["invoices"].has_test_file is True
    assert inv["refunds"].has_test_file is False


def test_module_inventory_ignores_vendored_trees(tmp_path):
    """The repo-wide scan must not credit a module because something inside
    node_modules or .venv happens to share its name."""
    from orchestrator.test_audit.architecture import module_inventory

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "invoices.py").write_text("def total():\n    return 1\n")
    (tmp_path / "node_modules" / "x" / "tests").mkdir(parents=True)
    (tmp_path / "node_modules" / "x" / "tests" / "test_invoices.py").write_text("def test_x():\n    pass\n")

    inv = {m.module_name: m for m in module_inventory(tmp_path, src_root="pkg")}

    assert inv["invoices"].has_test_file is False
