"""Lockfiles must stay generated and correspond to their recipe (L1)."""
from __future__ import annotations

import json

import yaml

from scripts.ddd.check_locks import check
from scripts.ddd.split_spec import split


def _pair(tmp_path, recipe_scenes, lock_scenes, version=1, slug="demo"):
    (tmp_path / f"{slug}.recipe.yaml").write_text(yaml.dump({
        "base_url": "http://x", "scenes": recipe_scenes,
    }))
    (tmp_path / f"{slug}.narrative.lock.json").write_text(json.dumps({
        "slug": slug, "version": version, "fetched_at": "2026-07-26T10:00:00Z",
        "name": slug, "narrative": "A story.", "personas": {},
        "build_order": [s["id"] for s in lock_scenes], "scenes": lock_scenes,
    }, indent=2, sort_keys=True) + "\n")


def _lock_scene(sid, title="T"):
    return {"id": sid, "title": title, "persona": "maya",
            "provenance": "S1", "narrative": "A line.", "features": []}


def test_matching_recipe_and_lock_is_clean(tmp_path):
    _pair(tmp_path, [{"id": "a", "show": "x"}], [_lock_scene("a")])
    assert check(tmp_path) == []


def test_lock_scene_with_no_recipe_is_reported(tmp_path):
    _pair(tmp_path, [{"id": "a", "show": "x"}], [_lock_scene("a"), _lock_scene("b")])
    assert any("'b'" in p for p in check(tmp_path))


def test_recipe_scene_with_no_lock_is_reported(tmp_path):
    _pair(tmp_path, [{"id": "a", "show": "x"}, {"id": "ghost", "show": "y"}],
          [_lock_scene("a")])
    assert any("ghost" in p for p in check(tmp_path))


def test_a_lock_carrying_recipe_fields_is_reported_as_hand_edited(tmp_path):
    scene = _lock_scene("a")
    scene["show"] = "someone pasted a selector in here"
    _pair(tmp_path, [{"id": "a", "show": "x"}], [scene])
    assert any("hand-edited" in p for p in check(tmp_path))


def test_a_recipe_with_no_lock_is_reported(tmp_path):
    (tmp_path / "orphan.recipe.yaml").write_text(yaml.dump({"scenes": []}))
    assert any("orphan" in p for p in check(tmp_path))


def test_malformed_lock_json_is_reported_as_hand_edited(tmp_path):
    (tmp_path / "demo.recipe.yaml").write_text(yaml.dump({"scenes": [{"id": "a", "show": "x"}]}))
    (tmp_path / "demo.narrative.lock.json").write_text("{ not json")
    assert any("hand-edited" in p for p in check(tmp_path))


def test_a_lock_missing_generated_keys_is_reported(tmp_path):
    (tmp_path / "demo.recipe.yaml").write_text(yaml.dump({"scenes": [{"id": "a", "show": "x"}]}))
    (tmp_path / "demo.narrative.lock.json").write_text(json.dumps(
        {"scenes": [_lock_scene("a")]}) + "\n")
    assert any("missing generated key" in p for p in check(tmp_path))


def test_an_empty_directory_is_clean(tmp_path):
    assert check(tmp_path) == []


def test_what_split_spec_produces_passes_the_check(tmp_path):
    """The two halves of the migration must agree with each other.

    split_spec writes the pair; check_locks gates it. If these ever disagree
    the migration produces something CI immediately rejects.
    """
    spec = tmp_path / "demo.yaml"
    spec.write_text(yaml.dump({
        "name": "demo", "narrative": "The goal.", "base_url": "http://x",
        "personas": {"maya": {"name": "Maya", "role": "PM",
                              "color": "#4F46E5", "intro": "Maya runs a program."}},
        "scenes": [{
            "persona": "maya", "title": "The goal", "show": "css:text=/^Go$/",
            "concept_claim": "A claim with at least five words.",
            "provenance": "S1", "narrative": "The goal.",
            "viewport": {"width": 1440, "height": 900},
        }],
    }))
    split(spec)
    assert check(tmp_path) == []
