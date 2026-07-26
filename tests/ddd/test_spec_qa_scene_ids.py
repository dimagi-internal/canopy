"""spec_qa enforces stable scene ids (L0).

The title-slug fallback in scripts.ddd.identity is a MIGRATION path, not a
second supported authoring mode — this is the gate that keeps it that way.
"""
from __future__ import annotations

from scripts.ddd.spec_qa import spec_qa


def _spec(scenes: list[dict]) -> dict:
    return {
        "name": "demo",
        "narrative": "A story about a dashboard that loads quickly.",
        "base_url": "http://localhost:8000",
        "personas": {"maya": {
            "name": "Maya", "role": "PM", "color": "#4F46E5",
            "intro": "Maya runs a vitamin-A program and wants independent proof.",
        }},
        "scenes": scenes,
    }


def _scene(**over) -> dict:
    base = {
        "id": "the-goal",
        "persona": "maya",
        "title": "The goal",
        "show": "Open the dashboard.",
        "concept_claim": "The dashboard loads in under two seconds.",
        "provenance": "S1",
        "role": "overview",
        "narrative": "Maya opens the dashboard and the whole picture is there.",
    }
    base.update(over)
    return base


def _reason(spec: dict) -> str:
    return (spec_qa(spec).blocking_reason or "").lower()


def test_missing_scene_id_is_a_violation():
    assert "scene id" in _reason(_spec([_scene(id="")]))


def test_duplicate_scene_ids_are_a_violation():
    spec = _spec([
        _scene(id="dup"),
        _scene(id="dup", title="Second", provenance="S2"),
    ])
    assert "duplicate" in _reason(spec)


def test_malformed_scene_id_is_a_violation():
    assert "scene id" in _reason(_spec([_scene(id="The Goal!")]))


def test_underscored_scene_id_is_a_violation():
    assert "scene id" in _reason(_spec([_scene(id="the_goal")]))


def test_well_formed_ids_produce_no_id_violation():
    assert "scene id" not in _reason(_spec([_scene(id="the-goal")]))
