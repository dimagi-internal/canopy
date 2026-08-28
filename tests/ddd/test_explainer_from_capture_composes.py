"""The explainer emitter must compose, like every other spec consumer.

It raw-loaded the spec path. Handed a two-file `<slug>.recipe.yaml`, that meant
the RECORDER filmed the lock's story (record_video goes through spec_io) while
this emitter put the recipe's compose-discarded copy into the voiceover — the
two halves of one render disagreeing about what the demo says. The slug came
from the filename too, yielding "<slug>.recipe", which matches no narrative.
"""
from __future__ import annotations

import json

import yaml

from scripts.ddd.snippets import _slug_from_spec_path, emit_explainer_from_capture


RECIPE = {
    "base_url": "http://localhost:8000",
    "scenes": [{
        "id": "the-goal",
        "show": "css:text=/^Goal$/",
        "concept_claim": "It renders.",
        "role": "overview",
        "pace": "teach",
        "narrative": "RECIPE COPY — compose throws this away.",
        "actions": [{"kind": "hold", "seconds": 2}],
    }],
}

LOCK = {
    "slug": "demo", "version": 4, "fetched_at": "2026-07-26T10:00:00Z",
    "name": "demo", "narrative": "The story.", "personas": {},
    "build_order": ["the-goal"],
    "scenes": [{"id": "the-goal", "title": "The goal", "persona": "maya",
                "provenance": "S1", "narrative": "PUBLISHED LINE — this is what is spoken.",
                "features": []}],
}

REPORT = {"scenes": [{"scene_index": 1, "start_seconds": 0.0, "duration_seconds": 6.0}]}


def _pair(tmp_path):
    (tmp_path / "demo.recipe.yaml").write_text(yaml.dump(RECIPE))
    (tmp_path / "demo.narrative.lock.json").write_text(json.dumps(LOCK))
    (tmp_path / "report.json").write_text(json.dumps(REPORT))


def test_the_voiceover_is_the_published_story_not_the_recipes_discarded_copy(tmp_path):
    _pair(tmp_path)
    out = tmp_path / "explainer.yaml"
    emit_explainer_from_capture(
        str(tmp_path / "demo.recipe.yaml"), str(tmp_path / "report.json"),
        clip_path=str(tmp_path / "master.mp4"), out_path=str(out),
    )
    text = out.read_text()
    assert "PUBLISHED LINE" in text
    assert "RECIPE COPY" not in text


def test_the_slug_drops_the_recipe_suffix(tmp_path):
    _pair(tmp_path)
    out = tmp_path / "explainer.yaml"
    spec = emit_explainer_from_capture(
        str(tmp_path / "demo.recipe.yaml"), str(tmp_path / "report.json"),
        clip_path=str(tmp_path / "master.mp4"), out_path=str(out),
    )
    assert "recipe" not in json.dumps(spec.get("slug") or spec.get("name") or "")


def test_slug_from_spec_path_strips_both_layouts():
    assert _slug_from_spec_path("docs/walkthroughs/a-b.recipe.yaml") == "a-b"
    assert _slug_from_spec_path("docs/walkthroughs/a-b.yaml") == "a-b"
    assert _slug_from_spec_path("/tmp/a-b.yml") == "a-b"


def test_a_legacy_single_file_spec_still_loads(tmp_path):
    """load_spec_raw passes a unified <slug>.yaml straight through, so the
    specs that predate the two-file layout keep rendering unchanged."""
    legacy = {**RECIPE, "name": "legacy"}
    legacy["scenes"][0] = {**RECIPE["scenes"][0], "narrative": "LEGACY LINE"}
    (tmp_path / "legacy.yaml").write_text(yaml.dump(legacy))
    (tmp_path / "report.json").write_text(json.dumps(REPORT))
    out = tmp_path / "explainer.yaml"
    emit_explainer_from_capture(
        str(tmp_path / "legacy.yaml"), str(tmp_path / "report.json"),
        clip_path=str(tmp_path / "master.mp4"), out_path=str(out),
    )
    assert "LEGACY LINE" in out.read_text()
