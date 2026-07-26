"""narrative pull writes a lockfile and nothing else (L1)."""
from __future__ import annotations

import json

from scripts.ddd.narrative import write_lock


PARTS = {
    "name": "demo",
    "narrative": "The goal. The proof.",
    "personas": {"maya": {"name": "Maya", "role": "PM"}},
    "build_order": ["the-goal"],
    "scenes": [{
        "id": "the-goal", "title": "The goal", "persona": "maya",
        "provenance": "S1", "narrative": "The goal.", "features": [],
    }],
}


def test_write_lock_records_slug_and_version(tmp_path):
    p = write_lock(tmp_path, "demo", 4, PARTS)
    lock = json.loads(p.read_text())
    assert p.name == "demo.narrative.lock.json"
    assert lock["slug"] == "demo"
    assert lock["version"] == 4
    assert lock["scenes"][0]["id"] == "the-goal"
    assert "fetched_at" in lock


def test_write_lock_is_deterministic_apart_from_fetched_at(tmp_path):
    a = json.loads(write_lock(tmp_path, "demo", 4, PARTS).read_text())
    b = json.loads(write_lock(tmp_path, "demo", 4, PARTS).read_text())
    a.pop("fetched_at")
    b.pop("fetched_at")
    assert a == b


def test_write_lock_carries_no_recipe_fields(tmp_path):
    """The lock is the STORY. A selector in here means someone hand-edited it."""
    parts = {**PARTS, "scenes": [{
        **PARTS["scenes"][0],
        "show": "css:text=/^Hyperzoomed$/",
        "url": "/plans/3536/review/",
        "concept_claim": "a local claim",
        "viewport": {"width": 1440, "height": 900},
    }]}
    lock = json.loads(write_lock(tmp_path, "demo", 4, parts).read_text())
    scene = lock["scenes"][0]
    for recipe_field in ("show", "url", "viewport", "actions", "concept_claim", "pace"):
        assert recipe_field not in scene


def test_write_lock_ends_with_a_newline_and_sorted_keys(tmp_path):
    """A re-pull of an unchanged version should be a no-op in git."""
    text = write_lock(tmp_path, "demo", 4, PARTS).read_text()
    assert text.endswith("\n")
    lock = json.loads(text)
    assert list(lock.keys()) == sorted(lock.keys())
