"""Thin orchestrator: collect (build corpus) + apply (consume verdicts).

The judging happens in the calling agent's context — see
`plugins/canopy/skills/test-audit/SKILL.md`. This module is plumbing only.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from orchestrator.test_audit.applier import (
    apply_from_dir, ApplyResult,
)
from orchestrator.test_audit.corpus import write_corpus
from orchestrator.test_audit.framework import detect_framework


@dataclass
class CollectResult:
    stamp_dir: Path
    corpus_path: Path
    test_count: int
    ran_tests: bool  # true if any test runner ran (not only pytest)
    framework: str = "pytest"


def collect_corpus(repo: Path, run_tests: bool = True, reruns: int = 0,
                   framework: str | None = None,
                   source_roots: list[str] | None = None) -> CollectResult:
    """Build the audit corpus and write it to `<repo>/.canopy/test-audits/<stamp>/corpus.yaml`.

    `source_roots` overrides the adapter's default source-root discovery
    when building the module inventory — pass a list of repo-relative
    directories for non-conventional layouts.
    """
    repo = repo.resolve()
    adapter = detect_framework(repo, override=framework)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = repo / ".canopy" / "test-audits" / stamp
    path, corpus = write_corpus(repo, out_dir, run_tests=run_tests, reruns=reruns,
                                adapter=adapter, source_roots=source_roots)
    return CollectResult(
        stamp_dir=out_dir,
        corpus_path=path,
        test_count=corpus["test_count"],
        ran_tests=run_tests,
        framework=adapter.name,
    )


def apply_audit(stamp_dir: Path, repo: Path | None = None,
                aggressive: bool = False, dry_run: bool = False,
                framework: str | None = None) -> ApplyResult:
    """Apply a verdicts.yaml that the agent wrote into `stamp_dir`."""
    return apply_from_dir(stamp_dir, repo=repo, aggressive=aggressive,
                          dry_run=dry_run, framework=framework)
