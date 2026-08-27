"""Composition of a recipe + narrative lock into a UnifiedSpec (L1)."""
from __future__ import annotations

import json

import pytest
import yaml

from scripts.ddd.spec_io import (
    SpecCompositionError,
    compose,
    discarded_recipe_fields,
    load_spec,
    load_spec_raw,
)


RECIPE = {
    "base_url": "http://localhost:8000",
    "scenes": [
        {
            "id": "the-goal",
            "show": "css:text=/^Hyperzoomed$/",
            "url": "/plans/3536/review/",
            "viewport": {"width": 1440, "height": 900},
            "concept_claim": "The plan renders in under two seconds.",
            "role": "overview",
        },
        {
            "id": "the-proof",
            "show": "css:text=/^Timeline$/",
            "concept_claim": "Each round shows a confidence interval.",
            "role": "overview",
        },
    ],
}

LOCK = {
    "slug": "demo",
    "version": 4,
    "fetched_at": "2026-07-26T10:00:00Z",
    "name": "demo",
    "narrative": "The goal. The proof.",
    "personas": {"maya": {
        "name": "Maya", "role": "PM", "color": "#4F46E5",
        "intro": "Maya runs a vitamin-A program and wants independent proof.",
    }},
    "build_order": ["the-goal", "the-proof"],
    "scenes": [
        {"id": "the-goal", "title": "The goal", "persona": "maya",
         "provenance": "S1", "narrative": "The goal.", "features": []},
        {"id": "the-proof", "title": "The proof", "persona": "maya",
         "provenance": "S2", "narrative": "The proof.", "features": []},
    ],
}


def test_compose_merges_recipe_and_lock_by_scene_id():
    raw = compose(RECIPE, LOCK)
    assert raw["name"] == "demo"
    assert raw["narrative"] == "The goal. The proof."
    assert raw["base_url"] == "http://localhost:8000"
    scene = raw["scenes"][0]
    assert scene["id"] == "the-goal"
    assert scene["show"] == "css:text=/^Hyperzoomed$/"          # recipe
    assert scene["viewport"] == {"width": 1440, "height": 900}  # recipe
    assert scene["title"] == "The goal"                         # lock
    assert scene["narrative"] == "The goal."                    # lock
    assert scene["concept_claim"] == "The plan renders in under two seconds."  # recipe


def test_compose_orders_scenes_by_the_lock_not_the_recipe():
    recipe = {**RECIPE, "scenes": list(reversed(RECIPE["scenes"]))}
    raw = compose(recipe, LOCK)
    assert [s["id"] for s in raw["scenes"]] == ["the-goal", "the-proof"]


def test_compose_raises_when_the_lock_has_a_scene_the_recipe_lacks():
    recipe = {**RECIPE, "scenes": [RECIPE["scenes"][0]]}
    with pytest.raises(SpecCompositionError) as exc:
        compose(recipe, LOCK)
    assert "the-proof" in str(exc.value)


def test_compose_raises_when_the_recipe_has_an_orphan_scene():
    recipe = {**RECIPE, "scenes": RECIPE["scenes"] + [{"id": "ghost", "show": "x"}]}
    with pytest.raises(SpecCompositionError) as exc:
        compose(recipe, LOCK)
    assert "ghost" in str(exc.value)


def test_load_spec_composes_from_disk(tmp_path):
    (tmp_path / "demo.recipe.yaml").write_text(yaml.dump(RECIPE))
    (tmp_path / "demo.narrative.lock.json").write_text(json.dumps(LOCK))
    spec = load_spec("demo", base_dir=tmp_path)
    assert spec.name == "demo"
    assert spec.scenes[0].show == "css:text=/^Hyperzoomed$/"
    assert spec.scenes[0].title == "The goal"


def test_load_spec_from_a_recipe_path(tmp_path):
    (tmp_path / "demo.recipe.yaml").write_text(yaml.dump(RECIPE))
    (tmp_path / "demo.narrative.lock.json").write_text(json.dumps(LOCK))
    spec = load_spec(tmp_path / "demo.recipe.yaml")
    assert spec.scenes[1].title == "The proof"


def test_load_spec_without_a_lock_says_how_to_get_one(tmp_path):
    (tmp_path / "demo.recipe.yaml").write_text(yaml.dump(RECIPE))
    with pytest.raises(SpecCompositionError) as exc:
        load_spec("demo", base_dir=tmp_path)
    assert "pull" in str(exc.value)


def test_load_spec_still_reads_a_legacy_unified_yaml(tmp_path):
    legacy = {
        "name": "legacy", "narrative": "One line.", "base_url": "http://x",
        "personas": {"maya": {
            "name": "Maya", "role": "PM", "color": "#4F46E5",
            "intro": "Maya runs a vitamin-A program and wants independent proof.",
        }},
        "scenes": [{"id": "only", "persona": "maya", "title": "Only",
                    "show": "x", "concept_claim": "A claim with five words here.",
                    "provenance": "S1", "narrative": "One line.", "role": "overview"}],
    }
    p = tmp_path / "legacy.yaml"
    p.write_text(yaml.dump(legacy))
    spec = load_spec(p)
    assert spec.name == "legacy"
    assert spec.scenes[0].show == "x"


def test_load_spec_raw_reads_a_spec_that_fails_schema_validation(tmp_path):
    """Composition and validation are separate concerns.

    Six of the twelve live walkthrough specs predate `concept_claim` being
    required. The recorder has always filmed them as plain dicts; gaining the
    two-file layout must not newly impose a schema on them.
    """
    from scripts.ddd.spec_io import load_spec_raw

    legacy = {
        "name": "old", "narrative": "One line.", "base_url": "http://x",
        "personas": {},
        "scenes": [{"title": "Only", "show": "x"}],  # no concept_claim/persona/provenance
    }
    p = tmp_path / "old.yaml"
    p.write_text(yaml.dump(legacy))

    with pytest.raises(Exception):
        load_spec(p)                       # strict: rejects it

    raw = load_spec_raw(p)                 # lenient: reads it
    assert raw["scenes"][0]["show"] == "x"


def test_compose_preserves_the_recipe_when_the_story_rewords_a_title():
    """The D1 regression, carried forward to where the behaviour now lives.

    Pre-L1 this was merge_narrative_into_spec matching scenes by title-slug, so
    a reviewer rewording a title on canopy-web silently replaced that scene's
    render recipe with an empty `show`. compose() joins on the stable id, and
    the recipe is not in the lock at all, so there is nothing to lose.
    """
    lock = {**LOCK, "scenes": [
        {**LOCK["scenes"][0], "title": "Completely reworded title",
         "narrative": "A reworded line."},
        LOCK["scenes"][1],
    ]}
    scene = compose(RECIPE, lock)["scenes"][0]

    assert scene["show"] == "css:text=/^Hyperzoomed$/"
    assert scene["url"] == "/plans/3536/review/"
    assert scene["viewport"] == {"width": 1440, "height": 900}
    assert scene["title"] == "Completely reworded title"
    assert scene["narrative"] == "A reworded line."
    # concept_claim is recipe-side and a pull can never touch it
    assert scene["concept_claim"] == "The plan renders in under two seconds."


# --- story written into a recipe is discarded, and must not be silent --------
# 20 `narrative:` blocks shipped into connect-labs recipes and every one was
# thrown away here. The render that was meant to prove them measured no change
# and had nothing to say why. check_locks is the gate, but no CI runs it — the
# loader is the one place every render path goes through.


def test_a_clean_recipe_discards_nothing():
    assert discarded_recipe_fields(RECIPE, LOCK) == []


def test_a_recipe_scene_carrying_narrative_is_named_as_discarded():
    recipe = {**RECIPE, "scenes": [
        {**RECIPE["scenes"][0], "narrative": "written here, never rendered"},
        RECIPE["scenes"][1],
    ]}
    assert discarded_recipe_fields(recipe, LOCK) == ["scene 'the-goal' 'narrative'"]


def test_top_level_story_in_a_recipe_is_named_as_discarded():
    assert discarded_recipe_fields({**RECIPE, "narrative": "x"}, LOCK) == ["top-level 'narrative'"]


def test_a_recipe_field_the_lock_does_not_have_is_not_discarded():
    """compose only overwrites what the lock actually carries."""
    lean = {**LOCK, "scenes": [{"id": "the-goal"}, {"id": "the-proof"}]}
    recipe = {**RECIPE, "scenes": [
        {**RECIPE["scenes"][0], "narrative": "this one really does survive"},
        RECIPE["scenes"][1],
    ]}
    assert discarded_recipe_fields(recipe, lean) == []
    assert compose(recipe, lean)["scenes"][0]["narrative"] == "this one really does survive"


def test_the_loader_warns_on_stderr_when_it_discards_story(tmp_path, capsys):
    recipe = {**RECIPE, "scenes": [
        {**RECIPE["scenes"][0], "narrative": "written here, never rendered"},
        RECIPE["scenes"][1],
    ]}
    (tmp_path / "demo.recipe.yaml").write_text(yaml.dump(recipe))
    (tmp_path / "demo.narrative.lock.json").write_text(json.dumps(LOCK))

    raw = load_spec_raw("demo", base_dir=tmp_path)

    err = capsys.readouterr().err
    assert "WARNING" in err and "the-goal" in err and "narrative pull demo" in err
    # and the composed result is still the lock's story, unchanged
    assert raw["scenes"][0]["narrative"] == "The goal."


def test_the_loader_is_quiet_when_the_recipe_carries_no_story(tmp_path, capsys):
    (tmp_path / "demo.recipe.yaml").write_text(yaml.dump(RECIPE))
    (tmp_path / "demo.narrative.lock.json").write_text(json.dumps(LOCK))
    load_spec("demo", base_dir=tmp_path)
    assert capsys.readouterr().err == ""
