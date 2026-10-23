"""Tests for orchestrator.cli module."""

import json
from pathlib import Path
from unittest import mock

import pytest
from click.testing import CliRunner

from orchestrator.cli import main


# ---------------------------------------------------------------------------
# canopy skills find
# ---------------------------------------------------------------------------


class TestSkillsFind:
    """Tests for `canopy skills find <query>`."""

    SAMPLE_CATALOG = [
        {
            "name": "test-audit",
            "qualified": "canopy:test-audit",
            "scope": "plugin",
            "source": "canopy",
            "kind": "skill",
            "description": "Audit a Python pytest test suite and prune dumb tests",
            "path": "/abs/canopy/skills/test-audit/SKILL.md",
        },
        {
            "name": "doctor",
            "qualified": "canopy:doctor",
            "scope": "plugin",
            "source": "canopy",
            "kind": "skill",
            "description": "Diagnose canopy plugin health",
            "path": "/abs/canopy/skills/doctor/SKILL.md",
        },
        {
            "name": "improve",
            "qualified": "canopy:improve",
            "scope": "plugin",
            "source": "canopy",
            "kind": "skill",
            "description": "Run a full canopy improvement cycle from session analysis",
            "path": "/abs/canopy/skills/improve/SKILL.md",
        },
    ]

    def test_audit_tests_returns_test_audit_first(self):
        runner = CliRunner()
        with mock.patch(
            "orchestrator.skill_catalog.build_catalog",
            return_value=list(self.SAMPLE_CATALOG),
        ):
            result = runner.invoke(main, ["skills", "find", "audit", "tests"])
        assert result.exit_code == 0, result.output
        assert "canopy:test-audit" in result.output
        # First match line should be test-audit (rank highest)
        first_match = [
            line for line in result.output.splitlines() if "canopy:" in line
        ][0]
        assert "canopy:test-audit" in first_match

    def test_no_matches_message(self):
        runner = CliRunner()
        with mock.patch(
            "orchestrator.skill_catalog.build_catalog",
            return_value=list(self.SAMPLE_CATALOG),
        ):
            result = runner.invoke(main, ["skills", "find", "zzznevermatchesxxx"])
        assert result.exit_code == 0
        assert "No skills match" in result.output

    def test_json_output(self):
        runner = CliRunner()
        with mock.patch(
            "orchestrator.skill_catalog.build_catalog",
            return_value=list(self.SAMPLE_CATALOG),
        ):
            result = runner.invoke(
                main, ["skills", "find", "audit", "--json-output"]
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert any(e["qualified"] == "canopy:test-audit" for e in data)

    def test_limit_respected(self):
        runner = CliRunner()
        with mock.patch(
            "orchestrator.skill_catalog.build_catalog",
            return_value=list(self.SAMPLE_CATALOG),
        ):
            # query 'canopy' matches all three by description
            result = runner.invoke(
                main, ["skills", "find", "canopy", "--limit", "1", "--json-output"]
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 1


# ---------------------------------------------------------------------------
# Top-level group help
# ---------------------------------------------------------------------------


class TestCliHelp:
    def test_main_help_exits_zero(self):
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0

    def test_sessions_help_exits_zero(self):
        runner = CliRunner()
        result = runner.invoke(main, ["sessions", "--help"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Unknown-subcommand skill hint (CLI vs Skill-tool surface confusion)
# ---------------------------------------------------------------------------


class TestUnknownCommandSkillHint:
    def test_unknown_command_that_is_a_skill_gets_a_hint(self):
        runner = CliRunner()
        fake_catalog = [{"name": "session-review", "qualified": "canopy:session-review"}]
        with mock.patch("orchestrator.skill_catalog.build_catalog", return_value=fake_catalog):
            result = runner.invoke(main, ["session-review"])
        assert result.exit_code != 0
        assert "No such command" in result.output
        assert "canopy:session-review" in result.output
        assert "Skill tool" in result.output

    def test_unknown_command_that_is_not_a_skill_keeps_plain_error(self):
        runner = CliRunner()
        with mock.patch("orchestrator.skill_catalog.build_catalog", return_value=[]):
            result = runner.invoke(main, ["definitely-not-a-thing"])
        assert result.exit_code != 0
        assert "No such command" in result.output
        assert "Skill tool" not in result.output

    def test_hint_survives_catalog_errors(self):
        runner = CliRunner()
        with mock.patch("orchestrator.skill_catalog.build_catalog", side_effect=OSError("boom")):
            result = runner.invoke(main, ["session-review"])
        assert result.exit_code != 0
        assert "No such command" in result.output
