"""Tests for upload.py's authored-spec resolution (canopy#620).

`upload_run` hardcoded `run_dir / "unified_spec.yaml"`. That is right for a run
whose spec was authored into the run dir and wrong for every run that was not —
which is every ACE-originated run, because ACE authors the spec at the demo root
under the narrative's own slug. Those runs pass every gate and then die at the
last step, after the render and judging are already paid for.

The shape that broke, from `bednet-check-2-visit/20260907-1126`:

    ~/.ace/demo/<opp>-<run>/bednet-followup-verification.yaml    <- authored
    ~/.ace/demo/<opp>-<run>/why_brief.yaml
    ~/.ace/demo/<opp>-<run>/.canopy/ddd/runs/<run_id>/           <- looked here

No network, no fixtures on disk beyond tmp_path.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ddd.upload import _authored_path_candidates, _resolve_authored_path


SLUG = "bednet-followup-verification"
RUN_ID = f"{SLUG}-2026-09-08-001"


def _ace_layout(tmp_path: Path) -> tuple[Path, Path]:
    """Return (demo_root, run_dir) in the real ACE shape."""
    demo_root = tmp_path / "demo" / "bednet-check-2-visit-20260907-1126"
    run_dir = demo_root / ".canopy" / "ddd" / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    return demo_root, run_dir


class TestRunDirStillWins:
    """A run that keeps its spec in the run dir must be completely unaffected."""

    def test_prefers_the_run_dir_copy(self, tmp_path: Path) -> None:
        _demo_root, run_dir = _ace_layout(tmp_path)
        (run_dir / "unified_spec.yaml").write_text("name: from-run-dir\n")

        resolved = _resolve_authored_path(run_dir, SLUG, "unified_spec.yaml")

        assert resolved == run_dir / "unified_spec.yaml"

    def test_run_dir_wins_even_when_the_demo_root_also_has_one(self, tmp_path: Path) -> None:
        # The divergent-copy situation the old workaround created. Preferring the
        # run dir keeps today's behaviour byte-for-byte for existing runs.
        demo_root, run_dir = _ace_layout(tmp_path)
        (run_dir / "unified_spec.yaml").write_text("name: from-run-dir\n")
        (demo_root / "unified_spec.yaml").write_text("name: from-demo-root\n")

        resolved = _resolve_authored_path(run_dir, SLUG, "unified_spec.yaml")

        assert resolved.read_text() == "name: from-run-dir\n"

    def test_run_dir_is_always_the_first_candidate(self, tmp_path: Path) -> None:
        _demo_root, run_dir = _ace_layout(tmp_path)
        candidates = _authored_path_candidates(run_dir, SLUG, "unified_spec.yaml")
        assert candidates[0] == run_dir / "unified_spec.yaml"


class TestAceLayout:
    """The defect: the spec is at the demo root, named for the narrative."""

    def test_finds_the_slug_named_spec_at_the_demo_root(self, tmp_path: Path) -> None:
        demo_root, run_dir = _ace_layout(tmp_path)
        authored = demo_root / f"{SLUG}.yaml"
        authored.write_text("name: authored-by-ace\n")

        resolved = _resolve_authored_path(run_dir, SLUG, "unified_spec.yaml")

        assert resolved == authored

    def test_finds_why_brief_at_the_demo_root_by_its_canonical_name(self, tmp_path: Path) -> None:
        demo_root, run_dir = _ace_layout(tmp_path)
        authored = demo_root / "why_brief.yaml"
        authored.write_text("problem: real\n")

        resolved = _resolve_authored_path(run_dir, SLUG, "why_brief.yaml")

        assert resolved == authored

    def test_finds_a_slug_named_spec_inside_the_run_dir(self, tmp_path: Path) -> None:
        _demo_root, run_dir = _ace_layout(tmp_path)
        authored = run_dir / f"{SLUG}.yaml"
        authored.write_text("name: slug-named-in-run-dir\n")

        resolved = _resolve_authored_path(run_dir, SLUG, "unified_spec.yaml")

        assert resolved == authored


class TestFailureIsLegible:
    """The old failure was a bare FileNotFoundError on a path nobody chose."""

    def test_raises_naming_every_path_it_tried(self, tmp_path: Path) -> None:
        demo_root, run_dir = _ace_layout(tmp_path)

        with pytest.raises(FileNotFoundError) as excinfo:
            _resolve_authored_path(run_dir, SLUG, "unified_spec.yaml")

        message = str(excinfo.value)
        assert "unified_spec.yaml" in message
        assert SLUG in message
        assert str(run_dir) in message
        assert str(demo_root) in message

    def test_warns_against_the_copy_workaround(self, tmp_path: Path) -> None:
        # The workaround is worse than the bug: two divergent copies, and a later
        # re-render silently reads the other one. The error says so.
        _demo_root, run_dir = _ace_layout(tmp_path)

        with pytest.raises(FileNotFoundError) as excinfo:
            _resolve_authored_path(run_dir, SLUG, "unified_spec.yaml")

        message = str(excinfo.value)
        assert "canopy#620" in message
        assert "divergent" in message


class TestCandidateHygiene:
    def test_candidates_are_unique(self, tmp_path: Path) -> None:
        _demo_root, run_dir = _ace_layout(tmp_path)
        candidates = _authored_path_candidates(run_dir, SLUG, "unified_spec.yaml")
        assert len(candidates) == len(set(candidates))

    def test_does_not_duplicate_when_filename_equals_the_slug_name(self, tmp_path: Path) -> None:
        _demo_root, run_dir = _ace_layout(tmp_path)
        candidates = _authored_path_candidates(run_dir, SLUG, f"{SLUG}.yaml")
        assert len(candidates) == len(set(candidates))

    def test_resolution_does_not_depend_on_the_cwd_being_a_git_repo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The git-toplevel candidate is best-effort; a non-repo cwd must not raise.
        demo_root, run_dir = _ace_layout(tmp_path)
        (demo_root / f"{SLUG}.yaml").write_text("name: ok\n")
        monkeypatch.chdir(tmp_path)

        assert _resolve_authored_path(run_dir, SLUG, "unified_spec.yaml").exists()
