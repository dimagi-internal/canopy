"""The narrated title card invariant in scripts.ddd.snippets.build_explainer_spec.

A DDD narrative's opening slide is a `role: overview` scene: the goal-setting
voiceover that sets up the whole demo. The explainer builder must make that VO
the intro TITLE CARD's narration — spoken over the held card — NOT replay it as
a separate body scene while the card narrates the raw tagline. That split was
the "opening-slide mismatch" bug: the video opened on a stale headline voiceover
instead of the narrated overview.

These tests lock the wiring:
  * an overview scene → its narrative becomes the intro-card VO, and the scene
    is dropped from the body walkthrough (not narrated twice);
  * the title CARD text stays the tagline (a tight headline);
  * no overview scene → the tagline is spoken (unchanged legacy behavior);
  * an overview-ONLY narrative is NOT absorbed (never a body-less title card).
"""
from __future__ import annotations

from scripts.ddd import snippets


def _snippet(idx: int, *, role: str | None, narration: str) -> dict:
    """A minimal snippet manifest entry with the fields the builder reads."""
    return {
        "id": f"demo-scene-{idx}",
        "scene_index": idx,
        "title": f"Scene {idx}",
        "segments": [{"start_seconds": 0.0, "duration_seconds": 5.0}],
        "action_marks": [],
        "in_seconds": 0.0,
        "out_seconds": 5.0,
        "duration_seconds": 5.0,
        "narration": narration,
        "sentence": "",
        "role": role,
    }


def _build(snips: list[dict], *, tagline: str) -> dict:
    manifest = {"narrative_slug": "demo", "name": "Demo", "snippets": snips}
    return snippets.build_explainer_spec(
        manifest,
        workspace="dimagi-team",
        master_ref="library:video/ddd/demo.mp4",
        base_url="https://example/",
        tagline=tagline,
        country_focus="",
    )


def _body_beat_ids(spec: dict) -> list[str]:
    return [b["id"] for b in spec["beats"] if b["kind"] == "body_walkthrough"]


def test_overview_scene_narrates_the_title_card():
    """The overview VO is the intro-card narration; the card text stays the tagline."""
    spec = _build(
        [
            _snippet(1, role="overview", narration="The value-prop overview, spoken."),
            _snippet(2, role="demo", narration="Priya opens the grid."),
        ],
        tagline="One manager, three programs, every paid visit verified.",
    )
    # The card is NARRATED with the overview VO, not the tagline.
    assert spec["narration"]["by_beat"]["title"] == "The value-prop overview, spoken."
    # The card TEXT (displayed headline) is still the tagline.
    assert spec["tagline"] == "One manager, three programs, every paid visit verified."
    # The overview scene is absorbed — NOT replayed as a body beat.
    assert _body_beat_ids(spec) == ["s2"]
    assert "s1" not in spec["walkthrough"]
    # The full VO script leads with the overview, then the demo scene.
    assert spec["narration"]["script"].splitlines()[0] == "The value-prop overview, spoken."


def test_no_overview_scene_speaks_the_tagline():
    """Legacy behavior preserved: with no overview scene the tagline is spoken."""
    spec = _build(
        [
            _snippet(1, role="demo", narration="Priya opens the grid."),
            _snippet(2, role="demo", narration="She drills into a cell."),
        ],
        tagline="A real headline.",
    )
    assert spec["narration"]["by_beat"]["title"] == "A real headline."
    assert _body_beat_ids(spec) == ["s1", "s2"]


def test_overview_only_narrative_is_not_absorbed():
    """An overview-only spec keeps the scene as a body beat — never a bodyless card."""
    spec = _build(
        [_snippet(1, role="overview", narration="Only scene.")],
        tagline="Fallback headline.",
    )
    # Not absorbed → the tagline is spoken and the scene stays a body beat.
    assert spec["narration"]["by_beat"]["title"] == "Fallback headline."
    assert _body_beat_ids(spec) == ["s1"]
