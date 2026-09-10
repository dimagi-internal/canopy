"""Suite-level architectural grist: module inventory, mock density, slow tests.

The agent reads this alongside per-test data to write architecture-review.md
covering coverage gaps, over-mocked files, slow-test hot list, and design
feedback (e.g., "this CUT is hard to test, look at how heavily its tests
mock"). Pure structural data — no judgement. The judgement is the agent's job.
"""
from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.test_audit.collector import TestItem
from orchestrator.test_audit.parser import StaticAnalysis
from orchestrator.test_audit.runner import TestResult


@dataclass
class ModuleInfo:
    module_name: str  # bare name, no .py
    src_path: str  # rel path from repo
    src_lines: int
    public_func_count: int
    has_test_file: bool
    test_file_path: str | None  # rel path from repo, or None


@dataclass
class MockDensity:
    file: str
    total_mocks: int
    total_assertions: int
    test_count: int

    @property
    def is_overmocked(self) -> bool:
        # Heuristic: more mocks than assertions, with at least 2 mocks total.
        # Single-test files with 4+ mocks per assertion also flagged.
        if self.total_assertions == 0:
            return self.total_mocks >= 2
        return self.total_mocks > self.total_assertions and self.total_mocks >= 2


@dataclass
class SlowTest:
    nodeid: str
    duration_ms: int


_IGNORED_DIR_PARTS = frozenset({
    ".git", ".venv", "venv", "node_modules", "site-packages", "__pycache__",
    ".tox", ".mypy_cache", ".pytest_cache", "build", "dist", ".canopy",
})


def _is_ignored_path(path: Path) -> bool:
    """Keep the repo-wide test scan out of vendored and build trees."""
    return any(part in _IGNORED_DIR_PARTS for part in path.parts)


def _is_test_file_for(test_path: Path, module_name: str) -> bool:
    name = test_path.stem  # e.g., "test_alpha" or "alpha_test"
    return name == f"test_{module_name}" or name == f"{module_name}_test"


def _count_public_funcs(src: str) -> int:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return 0
    n = 0
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
            n += 1
        elif isinstance(node, ast.AsyncFunctionDef) and not node.name.startswith("_"):
            n += 1
    return n


def _scan_test_imports(tests_dir: Path) -> set[str]:
    """Collect every dotted module path imported anywhere in `tests_dir`.

    Two forms are tracked:
      * `from a.b.c import x`     -> 'a.b.c' AND 'a.b.c.x'
      * `import a.b.c`            -> 'a.b.c'
    The 'a.b.c.x' form lets us detect submodule imports — `from a.b import c`
    means a test exercises `a.b.c`.
    """
    out: set[str] = set()
    if not tests_dir.exists():
        return out
    for tp in tests_dir.rglob("*.py"):
        try:
            tree = ast.parse(tp.read_text(encoding="utf-8"))
        except (SyntaxError, OSError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                out.add(node.module)
                for alias in node.names:
                    out.add(f"{node.module}.{alias.name}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    out.add(alias.name)
    return out


def _import_matches_module(imported: str, module_name: str) -> bool:
    """True if a dotted import path appears to reference `module_name`.

    Matches when the import's leaf segment equals `module_name`:
      * `orchestrator.foo` matches module 'foo'
      * `orchestrator.test_audit.collector` matches module 'collector'
      * `orchestrator.test_audit` matches NEITHER 'collector' nor 'parser'
    Cheap and effective for typical Python projects; false positives only
    arise when two source modules share a leaf name.
    """
    if imported == module_name:
        return True
    return imported.endswith(f".{module_name}")


def module_inventory(repo: Path, src_root: str = "src",
                     tests_root: str = "tests") -> list[ModuleInfo]:
    """Walk `<repo>/<src_root>` and pair each module with its test file (if any).

    Returns one entry per source `.py` (excluding `__init__.py` and dunders).
    """
    repo = repo.resolve()
    src_dir = repo / src_root
    tests_dir = repo / tests_root
    if not src_dir.exists():
        return []

    # Tests are found wherever they live, not only under `<repo>/<tests_root>`.
    # A top-level `tests/` is one convention; Django and many packages instead
    # co-locate `<package>/<app>/tests/test_*.py`, and for those the old lookup
    # matched nothing and reported EVERY module untested — a 100%-untested
    # figure that is always noise and never a finding.
    search_dirs = [tests_dir] if tests_dir.exists() else []
    test_paths_by_name: dict[str, Path] = {}
    for tp in repo.rglob("test_*.py"):
        if _is_ignored_path(tp):
            continue
        test_paths_by_name.setdefault(tp.stem, tp)
        if tests_dir not in tp.parents and tp.parent not in search_dirs:
            search_dirs.append(tp.parent)
    for tp in repo.rglob("*_test.py"):
        if not _is_ignored_path(tp):
            test_paths_by_name.setdefault(tp.stem, tp)

    test_imports: list[str] = []
    for d in search_dirs:
        test_imports.extend(_scan_test_imports(d))

    inv: list[ModuleInfo] = []
    for src in sorted(src_dir.rglob("*.py")):
        if src.name.startswith("_"):
            continue
        name = src.stem
        try:
            text = src.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        # Primary: explicit name-matched test file (test_<name>.py / <name>_test.py).
        tp = test_paths_by_name.get(f"test_{name}") or test_paths_by_name.get(f"{name}_test")
        # Fallback: any test file imports a path whose leaf is this module.
        imported = any(_import_matches_module(i, name) for i in test_imports)
        inv.append(ModuleInfo(
            module_name=name,
            src_path=str(src.relative_to(repo)),
            src_lines=text.count("\n") + 1,
            public_func_count=_count_public_funcs(text),
            has_test_file=tp is not None or imported,
            test_file_path=str(tp.relative_to(repo)) if tp else None,
        ))
    return inv


def mock_density_by_file(items: list[TestItem],
                         statics: dict[str, StaticAnalysis]) -> list[MockDensity]:
    """Aggregate mock count + assertion count per test file."""
    by_file: dict[str, dict] = defaultdict(lambda: {"mocks": 0, "asserts": 0, "n": 0})
    for it in items:
        st = statics.get(it.nodeid)
        if st is None:
            continue
        # Use the file str from nodeid (rel path) for stable keys.
        rel = it.nodeid.split("::")[0]
        by_file[rel]["mocks"] += len(st.mock_targets)
        by_file[rel]["asserts"] += st.assertion_count
        by_file[rel]["n"] += 1

    return [
        MockDensity(file=f, total_mocks=d["mocks"], total_assertions=d["asserts"],
                    test_count=d["n"])
        for f, d in sorted(by_file.items())
    ]


def slow_tests(runtimes: dict[str, TestResult], threshold_ms: int = 1000,
               limit: int = 20) -> list[SlowTest]:
    """Top tests above `threshold_ms`, sorted descending by duration."""
    above = [
        SlowTest(nodeid=r.nodeid, duration_ms=r.duration_ms)
        for r in runtimes.values()
        if r.duration_ms >= threshold_ms
    ]
    above.sort(key=lambda s: -s.duration_ms)
    return above[:limit]
