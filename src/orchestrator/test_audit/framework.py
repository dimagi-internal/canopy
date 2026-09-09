"""Framework adapter Protocol + auto-detection.

A `FrameworkAdapter` exposes the seven operations test-audit needs to do:
test discovery, static analysis, runtime invocation, module inventory, and
two apply primitives (delete + skip-mark). Pytest and vitest each implement
this Protocol; `build_corpus` and `apply_verdicts` dispatch through it so the
orchestrator stays framework-agnostic.

Detection is purely structural — file presence + package.json deps. No
network, no shelling out. The `--framework` CLI flag overrides detection.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol, runtime_checkable

from orchestrator.test_audit.collector import TestItem
from orchestrator.test_audit.parser import StaticAnalysis
from orchestrator.test_audit.runner import TestResult
from orchestrator.test_audit.architecture import ModuleInfo


@runtime_checkable
class FrameworkAdapter(Protocol):
    """The seven operations test-audit needs from a framework backend."""

    name: str  # "pytest" | "vitest"

    def collect(self, repo: Path) -> list[TestItem]: ...
    def analyze(self, item: TestItem) -> StaticAnalysis: ...
    def run(self, repo: Path, reruns: int = 0) -> dict[str, TestResult]: ...
    def module_inventory(self, repo: Path,
                         source_roots: list[str] | None = None) -> list[ModuleInfo]: ...
    def apply_delete(self, file: Path, name: str) -> bool: ...
    def apply_skip(self, file: Path, name: str, reason: str) -> bool: ...
    def supports_delete(self) -> bool: ...


def _has_vitest(repo: Path) -> bool:
    """True iff the repo has vitest configured (config file or package.json dep)."""
    for cfg in ("vitest.config.ts", "vitest.config.js", "vitest.config.mjs",
                "vitest.config.cjs", "vitest.config.mts"):
        if (repo / cfg).exists():
            return True
    pkg = repo / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        for key in ("dependencies", "devDependencies", "peerDependencies"):
            if "vitest" in (data.get(key) or {}):
                return True
    return False


def _has_pytest(repo: Path) -> bool:
    """True iff the repo has pytest configured (pyproject/pytest.ini/setup.cfg)."""
    if (repo / "pyproject.toml").exists():
        return True
    if (repo / "pytest.ini").exists():
        return True
    if (repo / "setup.cfg").exists():
        return True
    if any(repo.glob("**/conftest.py")):
        return True
    return False


def detect_framework(repo: Path, override: str | None = None) -> FrameworkAdapter:
    """Pick the framework adapter for `repo`.

    Resolution order:
      1. explicit `override` ("pytest" | "vitest") — error if unknown
      2. BOTH configured: whichever backend collects more tests
      3. vitest config or package.json dep
      4. pytest config (pyproject/pytest.ini/conftest.py)
      5. fallback to pytest (preserves prior behavior)

    Imported lazily to avoid a circular import — adapters import this module
    for the Protocol.
    """
    repo = repo.resolve()

    if override:
        return _build_adapter(override)

    vitest, pytest_ = _has_vitest(repo), _has_pytest(repo)

    # Both present: decide by COUNTING, not by order. A Django service with a
    # little front-end tooling has vitest in package.json and 6,000 pytest
    # tests; returning vitest there produced an audit of 143 tests and silently
    # ignored the other 6,000. Whichever backend actually collects more tests
    # is the suite the user means.
    if vitest and pytest_:
        counts = {}
        for name in ("pytest", "vitest"):
            try:
                counts[name] = len(_build_adapter(name).collect(repo))
            except Exception:  # noqa: BLE001 - a backend that cannot collect loses
                counts[name] = 0
        if counts["pytest"] or counts["vitest"]:
            return _build_adapter(max(counts, key=lambda k: counts[k]))

    if vitest:
        return _build_adapter("vitest")
    return _build_adapter("pytest")


def _build_adapter(name: str) -> FrameworkAdapter:
    if name == "pytest":
        from orchestrator.test_audit.adapters.pytest_adapter import PytestAdapter
        return PytestAdapter()
    if name == "vitest":
        from orchestrator.test_audit.adapters.vitest_adapter import VitestAdapter
        return VitestAdapter()
    raise ValueError(f"unknown framework: {name!r} (expected 'pytest' or 'vitest')")
