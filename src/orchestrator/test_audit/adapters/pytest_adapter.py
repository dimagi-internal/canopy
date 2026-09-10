"""Pytest adapter — thin wrapper around the existing pytest-specific modules.

The pytest implementation predates the adapter Protocol (it was the only
framework). This file exposes those functions through the Protocol surface
so `corpus.py` and `applier.py` can stay framework-agnostic. No behavior
change vs pre-Protocol code.
"""
from __future__ import annotations

from pathlib import Path

from orchestrator.test_audit.collector import TestItem, collect as _collect
from orchestrator.test_audit.parser import StaticAnalysis, analyze as _analyze
from orchestrator.test_audit.runner import TestResult, run_pytest as _run_pytest
from orchestrator.test_audit.architecture import ModuleInfo, module_inventory as _module_inventory


class PytestAdapter:
    name = "pytest"

    def collect(self, repo: Path) -> list[TestItem]:
        return _collect(repo)

    def analyze(self, item: TestItem) -> StaticAnalysis:
        return _analyze(item)

    def run(self, repo: Path, reruns: int = 0) -> dict[str, TestResult]:
        return _run_pytest(repo, reruns=reruns)

    def module_inventory(self, repo: Path,
                         source_roots: list[str] | None = None) -> list[ModuleInfo]:
        # Every root the caller passed, not just the first. Silently dropping
        # the rest is worse than not supporting them: the caller sees a module
        # inventory that looks complete and is missing three quarters of the
        # code (measured on a repo invoked with four roots).
        roots = list(source_roots) if source_roots else ["src"]
        seen: set[str] = set()
        inv: list = []
        for root in roots:
            for m in _module_inventory(repo, src_root=root):
                if m.src_path not in seen:
                    seen.add(m.src_path)
                    inv.append(m)
        return inv

    def apply_delete(self, file: Path, name: str) -> bool:
        from orchestrator.test_audit.applier import _delete_test
        return _delete_test(file, name)

    def apply_skip(self, file: Path, name: str, reason: str) -> bool:
        from orchestrator.test_audit.applier import _skip_mark_test
        return _skip_mark_test(file, name, reason)

    def supports_delete(self) -> bool:
        return True
