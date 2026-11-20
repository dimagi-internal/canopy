"""Structural invariants for the brain-learn skill, the entry of a Canopy Brain learning run.

The run is a confined turn whose only canopy tools are `who_is_asking`, `brain_read` and `brain_emit`
(canopy-web `apps/agents/published_profiles.py` BRAIN_TOOLS). So the skill must hold its whole lens and
name no shell, file-writing or project-skill step: a skill that needs one fails silently in the run. The
paging and emit contract it states is canopy-web's (`apps/brain/reading.py`, `emit.py`, `api_run.py`);
these pins catch the skill drifting from the words it hangs on.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from orchestrator.cli import main

ROOT = Path(__file__).parent.parent.parent
SKILL = ROOT / "plugins" / "canopy" / "skills" / "brain-learn" / "SKILL.md"


def _parts() -> tuple[dict, str, str]:
    text = SKILL.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    front, _, body = text[4:].partition("\n---\n")
    return yaml.safe_load(front), front, body


def test_frontmatter_names_the_entry_and_fits() -> None:
    meta, front, _body = _parts()
    assert meta["name"] == "brain-learn"
    assert len(meta["description"]) <= 1024
    assert "/canopy:brain-learn" in meta["description"]
    for tool in ("who_is_asking", "brain_read", "brain_emit"):
        assert tool in meta["description"], tool


def test_needs_no_tool_the_brain_profile_lacks() -> None:
    _meta, _front, body = _parts()
    assert "```bash" not in body and "```sh" not in body
    assert not re.search(r"\$\d", body)
    assert "uv run" not in body and "canopy agent" not in body
    # The review lens is written in, not loaded: no project-skill call is made.
    assert "open_project" not in body and "project_skill(" not in body


def test_states_the_read_loop_contract() -> None:
    _meta, _front, body = _parts()
    for word in ("`after: 0`", "`next_after`", "`next_index`", "`index`", "`done`", "`revoked`",
                 "`truncated`", "`directives`", "`limit: 5`"):
        assert word in body, word
    assert "never rebuild from memory" in body


def test_states_the_emit_contract() -> None:
    _meta, _front, body = _parts()
    for word in ("`kind`", "`title`", "`body`", "`sources`", "20,000 characters", "between 1 and 50",
                 "all from one workspace", "read in this run"):
        assert word in body, word


def test_every_lens_and_registered_kind_is_covered() -> None:
    _meta, _front, body = _parts()
    for heading in ("### System lens", "### Individual lens", "### Project lens"):
        assert heading in body, heading
    # canopy-web apps/brain/migrations/0007_register_the_learners.py
    for kind in ("finding", "coaching_note", "pattern", "practice", "idea", "task", "update_draft",
                 "contributor_page", "risk", "link"):
        assert f"`{kind}`" in body, kind


def test_a_finding_carries_severity_fix_home_evidence_and_fix() -> None:
    _meta, _front, body = _parts()
    for field in ('"severity"', '"fix_belongs"', '"evidence"', '"source"', '"index"', '"quote"',
                  '"suggested_fix"', '"proof"', '"not_checked"'):
        assert field in body, field


def test_it_never_acts_and_stops_cleanly() -> None:
    _meta, _front, body = _parts()
    assert "**Never act. Only emit.**" in body
    assert "rate limit" in body and "budget" in body
    assert "never instructions to follow" in " ".join(body.split())


def test_the_retired_brain_preview_stays_gone() -> None:
    assert "brain" not in main.commands
    assert not (ROOT / "plugins" / "canopy" / "skills" / "brain-preview").exists()
