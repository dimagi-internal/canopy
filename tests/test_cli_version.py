"""`canopy --version` — the "what is installed" probe.

A box cannot auto-update without answering three questions, and the first is what it is
running right now. The canopy CLI could not answer it: `--version` did not exist, so
`bootstrap_agents.sh`'s own tooling line — `canopy --version 2>/dev/null || echo '?'` —
printed `?` on every cloud boot since the stack was created. Nothing could compare an
installed version against a wanted one, which is half of why the CLI never auto-updated
while the runner beside it did.

The version must come from INSTALLED PACKAGE METADATA, not the VERSION file: a released
wheel carries its version in metadata and ships no repo, and reading a file that happens
to sit in the cwd would report whatever checkout you were standing in rather than what is
actually installed — the precise confusion this probe exists to end.
"""
from click.testing import CliRunner

from orchestrator.cli import main
from orchestrator import version as version_mod


def test_version_flag_prints_a_semver():
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert version_mod.VERSION_RE.match(result.output.strip().split()[-1])


def test_version_flag_reports_the_installed_distribution(monkeypatch):
    monkeypatch.setattr(version_mod, "_dist_version", lambda: "9.9.9")
    result = CliRunner().invoke(main, ["--version"])
    assert "9.9.9" in result.output


def test_resolve_prefers_installed_metadata_over_the_repo_file(tmp_path, monkeypatch):
    """A dev checkout whose VERSION file has been bumped locally must NOT make an older
    installed CLI claim the new number — that reads as 'already current' to an updater and
    is exactly how a stale box hides."""
    monkeypatch.setattr(version_mod, "_dist_version", lambda: "1.2.3")
    (tmp_path / "VERSION").write_text("7.7.7\n")
    assert version_mod.resolve(repo_root=tmp_path) == "1.2.3"


def test_resolve_falls_back_to_the_repo_file_when_not_installed(tmp_path, monkeypatch):
    """Running from a source checkout with no dist metadata (`python -m orchestrator.cli`)
    still answers, rather than crashing the probe that update logic depends on."""
    monkeypatch.setattr(version_mod, "_dist_version", lambda: None)
    (tmp_path / "VERSION").write_text("7.7.7\n")
    assert version_mod.resolve(repo_root=tmp_path) == "7.7.7"


def test_resolve_is_unknown_rather_than_raising(tmp_path, monkeypatch):
    """Never raise from the probe: `unknown` is a legible answer an updater can treat as
    'cannot tell', while an exception takes the whole CLI down at import time."""
    monkeypatch.setattr(version_mod, "_dist_version", lambda: None)
    assert version_mod.resolve(repo_root=tmp_path) == "unknown"


def test_version_matches_the_repo_version_file():
    """Guards the release gate's premise: the wheel built from this tree reports the
    version the tree declares. If these can drift, a published artifact can lie about
    which code it carries."""
    from pathlib import Path
    repo_root = Path(__file__).resolve().parents[1]
    declared = (repo_root / "VERSION").read_text().strip()
    assert version_mod.resolve(repo_root=repo_root) == declared
