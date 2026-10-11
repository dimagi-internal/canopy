"""Structural invariants for the share-to-slack skill.

Pins the strings the skill's behaviour hangs on: the two canopy-web tools it
calls by name (renaming either on canopy-web breaks the skill silently), and the
`unbind` mode's triggers in the description (skills are matched on it).
"""
from __future__ import annotations

from pathlib import Path

SKILL = Path(__file__).parent.parent.parent / "plugins" / "canopy" / "skills" / "share-to-slack" / "SKILL.md"


def _frontmatter_and_body() -> tuple[str, str]:
    text = SKILL.read_text()
    assert text.startswith("---\n")
    front, _, body = text[4:].partition("\n---\n")
    return front, body


def test_names_both_canopy_web_tools() -> None:
    _front, body = _frontmatter_and_body()
    assert "`share_session_to_slack`" in body
    assert "`detach_session_from_slack`" in body


def test_description_triggers_on_detach() -> None:
    front, _body = _frontmatter_and_body()
    for word in ("unbind", "detach", "disconnect"):
        assert word in front, f"description should mention {word!r}"


def test_unbind_section_reports_both_outcomes() -> None:
    _front, body = _frontmatter_and_body()
    assert "## Unbind" in body
    assert "status: detached" in body and "status: not_bound" in body


def test_description_fits_the_skill_limit() -> None:
    front, _body = _frontmatter_and_body()
    assert len(front) <= 1024
