"""The L1 acceptance test: every LIVE spec survives split → compose intact.

The unit tests for compose/split use synthetic three-field fixtures. This one
runs the real migration over byte-for-byte copies of all twelve walkthrough
specs from connect-labs (`fixtures/live_specs/`), because the failure mode that
matters is a field nobody thought to put in a fixture — a per-scene `viewport`,
an `actions` list with a `capture`, a `setup:` block, a `full_page: false`.

If this passes, `python -m scripts.ddd.split_spec docs/walkthroughs/*.yaml` is
safe to run on the real repo. If it fails, the migration would have silently
dropped whatever it names.

Fixtures are copies, not symlinks, so this keeps testing the shapes that
existed at migration time even after connect-labs moves on.
"""
from __future__ import annotations

import pathlib
import shutil

import pytest
import yaml

from scripts.ddd.spec_io import load_spec_raw
from scripts.ddd.split_spec import split

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "live_specs"
LIVE_SPECS = sorted(FIXTURES.glob("*.yaml"))

# Everything the recorder, the deck builder and the judges read off a scene.
# A field added to Scene later should be added here too — that is the point.
SCENE_FIELDS = (
    "id", "title", "persona", "provenance", "narrative", "concept_claim",
    "show", "url", "viewport", "full_page", "pace", "actions", "features",
    "design_intent", "impressive_because", "role",
)
TOP_FIELDS = (
    "name", "narrative", "base_url", "personas", "build_order", "setup",
    "video_viewport_width", "video_viewport_height", "auth", "prewarm",
    "why_brief", "narrative_locked",
)


# Verified against the models, not assumed:
#   Scene.features          default=[]  required=False
#   UnifiedSpec.build_order default=[]  required=False
_EMPTY_LIST_DEFAULTED = frozenset({"features", "build_order"})


def _norm(key: str, value):
    """Normalise ONLY where absent and empty are provably the same thing.

    ``Scene.features`` defaults to ``[]``, so an absent key and an empty list are
    indistinguishable through the model and ``write_lock`` may write either.
    Nothing else is relaxed on purpose — ``full_page: false`` vs absent is a real
    difference (viewport-only vs full-page capture), and blanket-normalising
    falsy values would hide it.
    """
    if key in _EMPTY_LIST_DEFAULTED and value is None:
        return []
    return value


def test_fixtures_are_present():
    assert len(LIVE_SPECS) == 12, [p.name for p in LIVE_SPECS]


@pytest.mark.parametrize("spec_file", LIVE_SPECS, ids=lambda p: p.stem)
def test_split_then_compose_is_lossless(spec_file, tmp_path):
    work = tmp_path / spec_file.name
    shutil.copy(spec_file, work)

    before = load_spec_raw(work)
    split(work)
    after = load_spec_raw(work.with_name(f"{spec_file.stem}.recipe.yaml"))

    for key in TOP_FIELDS:
        assert _norm(key, before.get(key)) == _norm(key, after.get(key)), (
            f"top-level {key!r} changed"
        )

    b_scenes, a_scenes = before.get("scenes") or [], after.get("scenes") or []
    assert len(b_scenes) == len(a_scenes), "scene count changed"

    for i, (b, a) in enumerate(zip(b_scenes, a_scenes), start=1):
        for key in SCENE_FIELDS:
            if key == "id":
                continue  # minted by the split for legacy specs; checked below
            assert _norm(key, b.get(key)) == _norm(key, a.get(key)), (
                f"scene {i} field {key!r} changed"
            )


@pytest.mark.parametrize("spec_file", LIVE_SPECS, ids=lambda p: p.stem)
def test_split_gives_every_scene_a_stable_id(spec_file, tmp_path):
    work = tmp_path / spec_file.name
    shutil.copy(spec_file, work)
    split(work)

    recipe = yaml.safe_load(work.with_name(f"{spec_file.stem}.recipe.yaml").read_text())
    ids = [(s.get("id") or "").strip() for s in recipe["scenes"]]
    assert all(ids), f"scene(s) without an id: {ids}"
    assert len(ids) == len(set(ids)), f"duplicate ids: {ids}"


@pytest.mark.parametrize("spec_file", LIVE_SPECS, ids=lambda p: p.stem)
def test_the_lock_holds_story_only_and_the_recipe_holds_code_only(spec_file, tmp_path):
    import json

    work = tmp_path / spec_file.name
    shutil.copy(spec_file, work)
    out = split(work)

    lock = json.loads(out["lock"].read_text())
    recipe = yaml.safe_load(out["recipe"].read_text())

    for scene in lock["scenes"]:
        for code_field in ("show", "url", "viewport", "actions", "concept_claim",
                           "pace", "full_page", "design_intent"):
            assert code_field not in scene, f"recipe field {code_field!r} leaked into the lock"

    for scene in recipe["scenes"]:
        for story_field in ("title", "persona", "provenance", "narrative", "features"):
            assert story_field not in scene, f"story field {story_field!r} left in the recipe"

    # The sync stamps the merge algorithm fed are gone for good.
    for dead in ("narrative_synced_version", "narrative_synced_hash", "narrative_synced_at"):
        assert dead not in recipe
