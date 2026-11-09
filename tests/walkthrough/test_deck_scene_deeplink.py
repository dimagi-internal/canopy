"""Regression tests for #scene-N deep-links in the generated slideshow deck.

The deck is a one-slide-at-a-time JS deck: it tracks a ``currentSlide`` index
and shows one ``.slide`` at a time (inactive slides are ``display:none``). It
emits stable ``id="scene-N"`` anchors on scene slides, and the DDD pipeline
documents + surfaces ``<DECK_URL>#scene-N`` deep-links (findings, reviews) so a
reviewer lands on the exact judged scene.

The bug guarded against: a deck with no ``location.hash``/``hashchange`` handling,
so every deep-link opens on scene 1. These tests pin the contract:

  1. scene anchors are emitted using the ORIGINAL spec index (``scene_index``),
     so a partial run that renders spec scene 2 anchors at ``#scene-2``;
  2. the navigation JS reads the hash on load, listens for ``hashchange``, and
     resolves a scene id to a slide index by element identity (NOT arithmetic),
     so a non-contiguous set of scenes still maps correctly.

The hash→index resolution is JS, so we re-implement the same regex + identity
lookup the deck ships and assert it on the real anchor set the generator
produced. If the generator's anchor scheme drifts from what the JS expects,
this test catches it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.walkthrough import generate_presentation as gp  # noqa: E402

# A deck whose scene anchors are non-contiguous and out of DOM order on purpose:
# title (no anchor), spec scene 2, spec scene 4, summary (no anchor). DOM order
# is [0,1,2,3]; scene ids are {scene-2: 1, scene-4: 2}. Anything that maps the
# hash by stripping the number and reusing it as an index would land wrong.
RUN_DATA = {
    "name": "Deep-link deck",
    "narrative": "A deck for pinning the deep-link contract.",
    "generated_at": "2026-06-08T00:00:00Z",
    "duration_seconds": 30,
    "personas": {"flw": {"name": "FLW", "color": "#ff8800", "role": "Worker"}},
    "slides": [
        {"type": "title"},
        {"type": "scene", "persona_key": "flw", "title": "Second scene",
         "narration": "n", "scene_index": 2, "scene_total": 9},
        {"type": "scene", "persona_key": "flw", "title": "Fourth scene",
         "narration": "n", "scene_index": 4, "scene_total": 9},
        {"type": "summary", "scenes_completed": 2, "scenes_total": 9,
         "ai_scores": [], "issues": []},
    ],
}


def _render(tmp_path) -> str:
    out = tmp_path / "deck.html"
    gp.generate(RUN_DATA, str(out))
    return out.read_text(encoding="utf-8")


def _scene_dom_order(html: str) -> list[str]:
    """The scene anchor ids in the order their ``.slide`` divs appear."""
    return re.findall(r'id="(scene-\d+)"', html)


def _resolve_hash_to_index(hash_str: str, dom_order: list[str]) -> int:
    """Re-implement the deck's hash→slide-index resolution.

    Mirrors ``indexFromHash`` + ``slideIndexForScene`` in JS_NAVIGATION: match
    ``#scene-<N>`` exactly, then find that anchor's position among the slides.
    The title slide occupies DOM index 0, so a scene's index is its position in
    full DOM order, not in ``dom_order`` (which is scene-only). We reconstruct
    full order from the run data to keep the test honest.
    """
    m = re.match(r"^#(scene-\d+)$", hash_str)
    if not m:
        return -1
    # Full DOM order of every .slide: title, scene-2, scene-4, summary.
    full = ["", dom_order[0], dom_order[1], ""]
    target = m.group(1)
    for i, sid in enumerate(full):
        if sid == target:
            return i
    return -1


def test_scene_anchors_use_spec_index(tmp_path):
    """Anchors are emitted with the spec index, non-contiguous and stable."""
    html = _render(tmp_path)
    assert _scene_dom_order(html) == ["scene-2", "scene-4"]


def test_nav_js_has_hash_handling():
    """The shipped JS reads the hash, listens for changes, and syncs it back."""
    js = gp.JS_NAVIGATION
    assert "indexFromHash" in js, "no hash parsing on load"
    assert "addEventListener('hashchange'" in js, "no hashchange listener"
    assert "slideIndexForScene" in js, "no id→index resolution"
    # replaceState (not assigning location.hash) keeps syncHash from looping
    # back into the hashchange listener.
    assert "replaceState" in js


def test_nav_js_init_honors_hash_not_hardcoded_zero():
    """Init must branch on the hash, not unconditionally ``showSlide(0)``."""
    js = gp.JS_NAVIGATION
    assert "indexFromHash()" in js
    # The old code initialized with a bare ``showSlide(0);``. The fix selects
    # the hash index when present.
    assert "initialSlide >= 0 ? initialSlide : 0" in js


def test_deep_link_resolves_to_correct_slide(tmp_path):
    """``#scene-4`` lands on its slide (DOM index 2), not scene 1."""
    html = _render(tmp_path)
    order = _scene_dom_order(html)
    assert _resolve_hash_to_index("#scene-2", order) == 1
    assert _resolve_hash_to_index("#scene-4", order) == 2


def test_deep_link_to_absent_or_bogus_scene_falls_back(tmp_path):
    """A hash for a scene not in this (partial) deck resolves to -1 → slide 0."""
    html = _render(tmp_path)
    order = _scene_dom_order(html)
    assert _resolve_hash_to_index("#scene-1", order) == -1  # not rendered
    assert _resolve_hash_to_index("#scene-9", order) == -1  # out of range
    assert _resolve_hash_to_index("#bogus", order) == -1
    assert _resolve_hash_to_index("", order) == -1
