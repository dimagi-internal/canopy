"""The skills agents read must state the L1 ownership contract.

Skills are the operating instructions — if they still describe a single unified
spec that both sides edit, agents will keep hand-editing the story locally no
matter what the code enforces. This is the doc-rot guard for the one invariant
the whole design rests on.
"""
from __future__ import annotations

import pathlib

import pytest

SKILLS = pathlib.Path(__file__).resolve().parents[2] / "plugins/canopy/skills"


def _text(name: str) -> str:
    return (SKILLS / name / "SKILL.md").read_text()


@pytest.mark.parametrize("skill", ["ddd-spec", "ddd-narrative-review", "ddd-run"])
def test_skill_names_the_generated_lock(skill):
    assert "narrative.lock.json" in _text(skill), (
        f"{skill} does not mention the narrative lock — an agent reading it "
        f"would not know the story is fetched, not authored locally"
    )


@pytest.mark.parametrize("skill", ["ddd-spec", "ddd-narrative-review"])
def test_skill_forbids_hand_editing_the_lock(skill):
    text = _text(skill).lower()
    assert "hand-edit" in text or "hand-write" in text, (
        f"{skill} does not warn against editing the generated lock"
    )


def test_narrative_review_documents_pull_as_one_way():
    text = _text("ddd-narrative-review").lower()
    assert "one-way" in text
    assert "no merge" in text or "nothing to reconcile" in text


def test_ddd_spec_requires_a_stable_scene_id():
    text = _text("ddd-spec")
    assert "`id:`" in text
    assert "never derived" in text.lower() or "not derived" in text.lower()


def test_no_skill_still_advertises_the_deleted_sync_command():
    """`narrative sync` and `pull --force` no longer exist."""
    offenders = []
    for path in SKILLS.rglob("SKILL.md"):
        text = path.read_text()
        if "narrative sync" in text or "pull --force" in text:
            offenders.append(path.parent.name)
    assert offenders == [], f"skills reference deleted commands: {offenders}"
