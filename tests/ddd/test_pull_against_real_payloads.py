"""The pull half, end to end, against REAL canopy-web review payloads.

`write_lock` and `web_narrative_to_spec_parts` are unit-tested against a
hand-written five-key dict. That proves the code does what I wrote it to do; it
does not prove it survives what canopy-web actually sends. These fixtures are
verbatim `request_json` bodies captured 2026-07-26 from the current version of
the three RF Surveys narratives — the ones a domain expert actually reviewed.

Captured (not fetched live) so the suite stays offline and deterministic, and so
it keeps testing the payload shape as it was when this code was written.

The chain under test is the whole cloud→disk path:

    request_json → web_narrative_to_spec_parts → write_lock
                 → compose(recipe, lock) → UnifiedSpec
"""
from __future__ import annotations

import json
import pathlib

import pytest

from scripts.ddd.narrative import web_narrative_to_spec_parts, write_lock
from scripts.ddd.schemas.models import UnifiedSpec
from scripts.ddd.spec_io import compose

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "web_payloads"
PAYLOADS = sorted(FIXTURES.glob("*.json"))


def _payload(path):
    return json.loads(path.read_text())


def _recipe_for(parts: dict) -> dict:
    """A minimal but complete recipe for whatever scenes the story declares."""
    return {
        "base_url": "http://localhost:8000",
        "scenes": [
            {
                "id": s["id"],
                "show": f"film {s['id']}",
                "concept_claim": "A local falsifiable claim of at least five words.",
                "role": "overview",
            }
            for s in parts["scenes"]
        ],
    }


def test_fixtures_are_present():
    assert [p.stem for p in PAYLOADS] == [
        "create-survey-solicitation",
        "microplans-study-groups",
        "verified-monitoring",
    ]


@pytest.mark.parametrize("payload_file", PAYLOADS, ids=lambda p: p.stem)
def test_real_payload_composes_into_a_valid_spec(payload_file, tmp_path):
    rj = _payload(payload_file)
    parts = web_narrative_to_spec_parts(rj)

    write_lock(tmp_path, payload_file.stem, 1, parts)
    lock = json.loads((tmp_path / f"{payload_file.stem}.narrative.lock.json").read_text())

    spec = UnifiedSpec.model_validate(compose(_recipe_for(parts), lock))
    assert len(spec.scenes) == len(rj["narration"])


@pytest.mark.parametrize("payload_file", PAYLOADS, ids=lambda p: p.stem)
def test_scene_ids_match_the_narration_ids_canopy_web_stored(payload_file):
    rj = _payload(payload_file)
    parts = web_narrative_to_spec_parts(rj)
    assert [s["id"] for s in parts["scenes"]] == [n["id"] for n in rj["narration"]]


@pytest.mark.parametrize("payload_file", PAYLOADS, ids=lambda p: p.stem)
def test_the_reviewed_line_becomes_the_voiceover_not_the_concept_claim(payload_file, tmp_path):
    """The D2 guard, against real data.

    canopy-web sends the approved line as `narration[].text`. It must land in
    `scene.narrative` (what the push reads back and what the video speaks), and
    must NOT overwrite `concept_claim`, which is local-owned and never
    transmitted.
    """
    rj = _payload(payload_file)
    parts = web_narrative_to_spec_parts(rj)
    recipe = _recipe_for(parts)

    write_lock(tmp_path, payload_file.stem, 1, parts)
    lock = json.loads((tmp_path / f"{payload_file.stem}.narrative.lock.json").read_text())
    composed = compose(recipe, lock)

    for scene, item in zip(composed["scenes"], rj["narration"]):
        assert scene["narrative"] == (item.get("text") or "").strip()
        assert scene["concept_claim"] == "A local falsifiable claim of at least five words."


@pytest.mark.parametrize("payload_file", PAYLOADS, ids=lambda p: p.stem)
def test_no_real_payload_smuggles_a_recipe_field_into_the_lock(payload_file, tmp_path):
    rj = _payload(payload_file)
    parts = web_narrative_to_spec_parts(rj)
    write_lock(tmp_path, payload_file.stem, 1, parts)
    lock = json.loads((tmp_path / f"{payload_file.stem}.narrative.lock.json").read_text())

    for scene in lock["scenes"]:
        for code_field in ("show", "url", "viewport", "actions", "concept_claim", "pace"):
            assert code_field not in scene
