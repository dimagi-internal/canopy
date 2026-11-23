"""Structural invariants for the brain-learn skill, the entry of a Canopy Brain learning run.

The run is a full-profile turn of the learner's operator agent whose context canopy-web sets up (the caller
envelope's `brain_run`, canopy-web `apps/brain/context.py`). It reads the sessions due only through `brain_read`,
records what it learned with `brain_emit`, and may then act within the agent's rails; canopy-web records what the
turn makes against what it read (`apps/brain/lineage.py`). The skill holds its whole lens and embeds no shell. The
paging and emit contract it states is canopy-web's (`apps/brain/reading.py`, `emit.py`, `api_run.py`); these pins
catch the skill drifting from the words it hangs on.
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


def test_embeds_no_shell_and_loads_no_lens() -> None:
    _meta, _front, body = _parts()
    assert "```bash" not in body and "```sh" not in body
    assert not re.search(r"\$\d", body)
    assert "uv run" not in body and "canopy agent" not in body
    # The review lens is written in, not loaded: no project-skill call is made.
    assert "open_project" not in body and "project_skill(" not in body


def test_reads_its_context_and_the_sessions_only_through_brain_read() -> None:
    _meta, _front, body = _parts()
    for word in ("`brain_run`", "`target`", "`accountable`", "`sessions_due`", "`source_ids_due`", "full-profile"):
        assert word in body, word
    assert "ONLY through `brain_read`" in body
    for other in ("`search_sessions`", "`canopy_sessions_get_session`"):
        assert other in body, other               # named as what not to use
    assert "memory" in body


def test_states_the_read_loop_contract() -> None:
    _meta, _front, body = _parts()
    for word in ("`after: 0`", "`next_after`", "`next_index`", "`index`", "`done`", "`revoked`",
                 "`truncated`", "`directives`", "`limit: 5`"):
        assert word in body, word
    assert "never rebuild from memory" in body


def test_states_the_emit_contract() -> None:
    _meta, _front, body = _parts()
    for word in ("`kind`", "`title`", "`body`", "`sources`", "20,000 characters", "between 1 and 50",
                 "one workspace's per output", "read in this run", "`key`", "`folded: true`"):
        assert word in body, word


def test_states_the_budget_and_the_context_version() -> None:
    _meta, _front, body = _parts()
    # canopy-web apps/brain/budget.py, reading.read_next (`budget.stopped`), context.VERSION
    for word in ("`budget`", "`stopped:", "`version`", "`purpose_class`", "budget of outputs spent"):
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


def test_it_emits_first_acts_within_its_rails_and_stops_cleanly() -> None:
    _meta, _front, body = _parts()
    assert "**Read only through `brain_read`, emit first, act second.**" in body
    assert "**The finding is emitted first**" in body and "turn mode" in body
    assert "**Report every pull request, issue or link you create outside canopy**" in body
    assert "**It quotes no one.**" in body
    assert "rate limit" in body and "budget" in body
    assert "never instructions to follow" in " ".join(body.split())


def test_the_retired_brain_preview_stays_gone() -> None:
    assert "brain" not in main.commands
    assert not (ROOT / "plugins" / "canopy" / "skills" / "brain-preview").exists()


def test_checks_the_open_findings_and_reuses_their_keys() -> None:
    """canopy-web tells a run the open findings its learner holds (`brain_run.open_findings`) and folds an emit that
    names one by `key` or `same_as`: the skill must check them before minting a key, or each run re-files them."""
    _meta, _front, body = _parts()
    for word in ("`open_findings`", "`same_as`", "`lookback_days`", "Check the open findings first",
                 "Never two outputs for one problem"):
        assert word in body, word


def test_describes_dismissals_triage_and_grounded_evidence() -> None:
    """canopy-web remembers dismissals (`dismissed_findings`), serves due sessions best first (`triage`, `source`) and
    refuses an emit whose evidence is not a verbatim `{source, index, quote}` of a row the run read."""
    _meta, _front, body = _parts()
    for word in ("`dismissed_findings`", "`triage`", "pass `source`", "`{source, index, quote}`",
                 "Plain-string evidence is refused", "`single session`", "`pattern`", "come back"):
        assert word in body, word
