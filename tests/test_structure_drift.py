"""Tests for the `canopy structure-drift` self-audit.

Verifies:
  - the CURRENT clean repo reports no error-severity drift (the invariants
    canopy already enforces via pytest guards hold),
  - injected violations are detected per-invariant, and
  - --strict flips the exit code when any finding exists.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from orchestrator import structure_drift as sd
from orchestrator.cli import main

REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# Current clean repo
# --------------------------------------------------------------------------

def test_current_repo_has_no_error_drift():
    """On the real checkout, no error-severity invariant should fire.

    Warnings (e.g. an over-budget skill description) are allowed to surface
    without being treated as a hard failure here — they are advisory.
    """
    report = sd.run_structure_drift(repo_root=REPO_ROOT)
    errors = [f for f in report["findings"] if f["severity"] == "error"]
    assert errors == [], f"Unexpected error-severity drift: {errors}"


def test_report_shape():
    report = sd.run_structure_drift(repo_root=REPO_ROOT)
    assert set(report) >= {"ok", "findings", "by_invariant", "counts", "repo_root"}
    assert report["counts"]["total"] == len(report["findings"])
    for f in report["findings"]:
        assert set(f) >= {"invariant", "severity", "detail"}
        assert f["severity"] in {"error", "warning"}


# --------------------------------------------------------------------------
# Fixture: a minimal fake plugin tree we can mutate to inject violations
# --------------------------------------------------------------------------

def _make_repo(tmp_path: Path, version: str = "1.2.3") -> Path:
    """Build a minimal, clean canopy-shaped repo skeleton under tmp_path."""
    root = tmp_path
    plugin = root / "plugins" / "canopy"
    (plugin / "skills").mkdir(parents=True)
    (plugin / "commands").mkdir(parents=True)
    (plugin / "agents").mkdir(parents=True)
    (plugin / ".claude-plugin").mkdir(parents=True)

    # VERSION + plugin.json + marketplace.json all in sync
    (root / "VERSION").write_text(version + "\n")
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "canopy", "version": version}, indent=2)
    )
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "canopy",
                "metadata": {"version": version},
                "plugins": [{"name": "canopy", "version": version}],
            },
            indent=2,
        )
    )

    # One clean skill with a short description
    skill = plugin / "skills" / "widget"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: widget\ndescription: A short clean description.\n---\nBody\n"
    )
    return root


def test_clean_fixture_reports_ok(tmp_path):
    root = _make_repo(tmp_path)
    report = sd.run_structure_drift(repo_root=root)
    assert report["ok"], report["findings"]
    assert report["counts"]["total"] == 0


def test_pattern_b_violation_detected(tmp_path):
    root = _make_repo(tmp_path)
    plugin = root / "plugins" / "canopy"
    # Add a command colliding with the `widget` skill that does NOT follow Pattern B.
    (plugin / "commands" / "widget.md").write_text("Just improvise from memory.\n")

    report = sd.run_structure_drift(repo_root=root)
    assert not report["ok"]
    invs = {f["invariant"] for f in report["findings"]}
    assert sd.INVARIANT_PATTERN_B in invs

    # Now make the command follow Pattern B -> finding clears.
    (plugin / "commands" / "widget.md").write_text(
        "Read skills/widget/SKILL.md from disk and follow it.\n"
    )
    report2 = sd.run_structure_drift(repo_root=root)
    assert sd.INVARIANT_PATTERN_B not in {f["invariant"] for f in report2["findings"]}


def test_reserved_builtin_name_detected(tmp_path):
    root = _make_repo(tmp_path)
    plugin = root / "plugins" / "canopy"
    # A skill named after a built-in slash command.
    doctor = plugin / "skills" / "doctor"
    doctor.mkdir()
    (doctor / "SKILL.md").write_text(
        "---\nname: doctor\ndescription: collides with builtin.\n---\nBody\n"
    )

    report = sd.run_structure_drift(repo_root=root)
    assert not report["ok"]
    assert sd.INVARIANT_RESERVED_NAME in {f["invariant"] for f in report["findings"]}


def test_version_mismatch_detected(tmp_path):
    root = _make_repo(tmp_path)
    # Bump VERSION out of sync with plugin.json + marketplace.json.
    (root / "VERSION").write_text("9.9.9\n")

    report = sd.run_structure_drift(repo_root=root)
    assert not report["ok"]
    version_findings = [
        f for f in report["findings"] if f["invariant"] == sd.INVARIANT_VERSION_SYNC
    ]
    # plugin.json AND marketplace.json both disagree -> at least two findings.
    assert len(version_findings) >= 2


def test_skill_description_budget_detected(tmp_path):
    root = _make_repo(tmp_path)
    plugin = root / "plugins" / "canopy"
    fat = plugin / "skills" / "verbose"
    fat.mkdir()
    long_desc = "x" * 2000
    (fat / "SKILL.md").write_text(
        f"---\nname: verbose\ndescription: {long_desc}\n---\nBody\n"
    )

    report = sd.run_structure_drift(repo_root=root, per_skill_limit=1024)
    budget = [
        f for f in report["findings"]
        if f["invariant"] == sd.INVARIANT_SKILL_DESCRIPTION_BUDGET
    ]
    assert budget, "expected an over-budget description finding"
    assert budget[0]["severity"] == "warning"


# --------------------------------------------------------------------------
# CLI / --strict exit-code behavior
# --------------------------------------------------------------------------

def test_cli_clean_exits_zero(tmp_path):
    root = _make_repo(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["structure-drift", "--repo", str(root)])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_cli_strict_flips_exit_code_on_violation(tmp_path):
    root = _make_repo(tmp_path)
    plugin = root / "plugins" / "canopy"
    (plugin / "commands" / "widget.md").write_text("improvise\n")  # pattern B violation

    runner = CliRunner()

    # Without --strict: findings printed, exit 0.
    soft = runner.invoke(main, ["structure-drift", "--repo", str(root)])
    assert soft.exit_code == 0, soft.output
    assert "DRIFT" in soft.output

    # With --strict: same findings, non-zero exit.
    strict = runner.invoke(main, ["structure-drift", "--repo", str(root), "--strict"])
    assert strict.exit_code != 0, strict.output


def test_cli_json_output(tmp_path):
    root = _make_repo(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main, ["structure-drift", "--repo", str(root), "--json-output"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["counts"]["total"] == 0


# --------------------------------------------------------------------------
# Repo-root resolution (the uv-tool-install shape)
# --------------------------------------------------------------------------

def test_non_checkout_root_fails_loudly_instead_of_reporting_clean(tmp_path):
    """A root with no plugin tree must NOT come back clean.

    This is the `uv tool install` shape: the module ships from site-packages, so
    the shipped-from path is `.../lib/python3.x`. Every other check degrades to
    "directory missing -> no findings" there, so a silent pass would assert that
    invariants held when nothing was scanned.
    """
    bare = tmp_path / "lib" / "python3.14"
    bare.mkdir(parents=True)

    report = sd.run_structure_drift(repo_root=bare)

    assert not report["ok"]
    assert {f["invariant"] for f in report["findings"]} == {sd.INVARIANT_REPO_ROOT}
    assert report["counts"]["error"] == 1
    # It must not masquerade as a version-file problem — that framing is what
    # made the real bug read as "one small finding" rather than "audited nothing".
    detail = report["findings"][0]["detail"]
    assert "not a canopy checkout" in detail
    assert "--repo" in detail


def test_is_canopy_checkout(tmp_path):
    assert sd.is_canopy_checkout(_make_repo(tmp_path / "good"))

    bare = tmp_path / "bare"
    bare.mkdir()
    assert not sd.is_canopy_checkout(bare)

    # VERSION alone isn't enough — the plugin tree is what the checks scan.
    partial = tmp_path / "partial"
    partial.mkdir()
    (partial / "VERSION").write_text("1.2.3\n")
    assert not sd.is_canopy_checkout(partial)


def test_default_repo_root_prefers_a_real_checkout_over_shipped_from(tmp_path, monkeypatch):
    """When the shipped-from path isn't a checkout, fall back to CWD's checkout."""
    root = _make_repo(tmp_path / "repo")
    nested = root / "src" / "orchestrator"
    nested.mkdir(parents=True)

    monkeypatch.setattr(sd, "MARKETPLACE_CLONE", tmp_path / "no-such-clone")
    monkeypatch.chdir(nested)

    # Simulate site-packages: shipped-from resolves to a non-checkout dir.
    fake_module = tmp_path / "lib" / "python3.14" / "site-packages" / "orchestrator"
    fake_module.mkdir(parents=True)
    monkeypatch.setattr(sd, "__file__", str(fake_module / "structure_drift.py"))

    assert sd.default_repo_root() == root.resolve()


def test_default_repo_root_falls_back_to_marketplace_clone(tmp_path, monkeypatch):
    clone = _make_repo(tmp_path / "clone")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    monkeypatch.setattr(sd, "MARKETPLACE_CLONE", clone)
    monkeypatch.chdir(elsewhere)

    fake_module = tmp_path / "lib" / "python3.14" / "site-packages" / "orchestrator"
    fake_module.mkdir(parents=True)
    monkeypatch.setattr(sd, "__file__", str(fake_module / "structure_drift.py"))

    assert sd.default_repo_root() == clone


def test_default_repo_root_returns_shipped_from_when_nothing_resolves(tmp_path, monkeypatch):
    """Unresolvable -> return the shipped-from path so check_repo_root reports it."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(sd, "MARKETPLACE_CLONE", tmp_path / "no-such-clone")
    monkeypatch.chdir(elsewhere)

    fake_module = tmp_path / "lib" / "python3.14" / "site-packages" / "orchestrator"
    fake_module.mkdir(parents=True)
    monkeypatch.setattr(sd, "__file__", str(fake_module / "structure_drift.py"))

    resolved = sd.default_repo_root()
    assert not sd.is_canopy_checkout(resolved)
    report = sd.run_structure_drift(repo_root=resolved)
    assert {f["invariant"] for f in report["findings"]} == {sd.INVARIANT_REPO_ROOT}
