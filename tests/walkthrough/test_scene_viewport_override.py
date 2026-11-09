"""Tests for the per-scene viewport override (Scene.viewport).

Background: ``video_viewport_width`` / ``video_viewport_height`` work at the
spec top level, but bumping one dense scene would bump the whole recording. The
per-scene override lets scene 4 run at 1440×900 to fit a Mapbox + inspector
panel without inflating the other 5 scenes.

The mechanism: ``Scene.viewport: {"width": int, "height": int}`` (optional). When set,
``Recorder.run_scene`` calls ``page.set_viewport_size`` BEFORE the goto so the
freshly-loaded page lays out at the requested size, then restores the spec-
level default after ``final_hold_ms``.

These tests pin:
  - Pydantic ``Scene`` validates the new field.
  - run_scene applies the override only when it differs from the current size.
  - run_scene restores the default after the scene.
  - back-compat: scenes without ``viewport`` don't trigger any resize.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.narrative.models import Scene  # noqa: E402
from scripts.walkthrough._lib.config import RecorderConfig  # noqa: E402
from scripts.walkthrough._lib.orchestrator import Recorder  # noqa: E402


SENTINEL_CONFIG = RecorderConfig(
    initial_hold_ms=1,
    final_hold_ms=1,
    min_hold_ms=0,
    goto_settle_ms=1,
)


class FakePage:
    """Page-shaped stub that records ``set_viewport_size`` calls.

    Tracks the sequence of viewport changes so we can assert exact resize
    behaviour: apply on scene start, restore on scene end, no gratuitous
    resize when the requested viewport matches the current.
    """

    def __init__(self, *, url: str = "https://example.com/"):
        self.url = url
        self.viewport_calls: list[dict] = []
        self.timeouts: list[int] = []
        self.gotos: list[dict] = []

    def set_viewport_size(self, size):
        # Record a copy — set_viewport_size receives a dict; tracking the
        # object would let later mutations alter past entries.
        self.viewport_calls.append(dict(size))

    def wait_for_timeout(self, ms):
        self.timeouts.append(int(ms))

    def wait_for_load_state(self, *args, **kwargs):
        pass

    def goto(self, url, *, wait_until=None, timeout=None):
        self.gotos.append({"url": url, "wait_until": wait_until})
        self.url = url

    def wait_for_selector(self, selector, *, timeout=None, state=None):
        class _Handle:
            def click(self_inner, *a, **k):
                pass

            def scroll_into_view_if_needed(self_inner, *a, **k):
                pass

        return _Handle()

    def wait_for_function(self, expr, *, timeout=None):
        pass

    def evaluate(self, *a, **k):
        return None

    @property
    def keyboard(self):
        class _K:
            def press(self, *a, **k):
                pass

            def type(self, *a, **k):
                pass

        return _K()


# ---- Pydantic Scene field validation ---------------------------------------


def _required_scene_fields(**overrides):
    base = {
        "persona": "dana",
        "title": "test",
        "show": "x",
        "concept_claim": "y is shown clearly to the reviewer with concrete data",
        "provenance": "spine-1",
    }
    base.update(overrides)
    return base


def test_scene_viewport_field_validates():
    """``Scene.viewport`` accepts ``{"width": int, "height": int}``."""
    scene = Scene.model_validate(
        _required_scene_fields(viewport={"width": 1440, "height": 900})
    )
    assert scene.viewport == {"width": 1440, "height": 900}


def test_scene_viewport_defaults_to_none():
    """``Scene.viewport`` is optional — default ``None`` so existing specs
    validate unchanged."""
    scene = Scene.model_validate(_required_scene_fields())
    assert scene.viewport is None


# ---- Recorder applies + restores viewport ----------------------------------


def test_run_scene_applies_viewport_override_before_goto():
    """Scene with ``viewport`` triggers ``set_viewport_size`` BEFORE the
    goto so the freshly-loaded page lays out at the requested size from
    the first paint."""
    page = FakePage()
    rec = Recorder(
        config=SENTINEL_CONFIG,
        base_url="https://example.com",
        default_viewport={"width": 1280, "height": 720},
    )
    scene = {
        "title": "wide scene",
        "url": "https://example.com/wide",
        "viewport": {"width": 1440, "height": 900},
        "actions": [{"kind": "press"}],
        "scene_index": 1,
    }
    rec.run_scene(page, scene)

    # First viewport call is the override (1440x900), BEFORE the goto
    assert page.viewport_calls, "expected set_viewport_size to fire for scene with viewport"
    assert page.viewport_calls[0] == {"width": 1440, "height": 900}
    # The override fired before the goto
    assert page.gotos, "scene with url should have called goto"


def test_run_scene_restores_default_viewport_after_scene():
    """After the overriding scene finishes, the recorder restores the
    spec-level default so the next scene starts at the original size."""
    page = FakePage()
    rec = Recorder(
        config=SENTINEL_CONFIG,
        base_url="https://example.com",
        default_viewport={"width": 1280, "height": 720},
    )
    scene = {
        "title": "wide scene",
        "url": "https://example.com/wide",
        "viewport": {"width": 1440, "height": 900},
        "actions": [{"kind": "press"}],
        "scene_index": 1,
    }
    rec.run_scene(page, scene)

    # Two viewport calls: apply override, then restore default
    assert len(page.viewport_calls) == 2, (
        f"expected 2 viewport calls (apply + restore); got {page.viewport_calls}"
    )
    assert page.viewport_calls[0] == {"width": 1440, "height": 900}
    assert page.viewport_calls[1] == {"width": 1280, "height": 720}


def test_run_scene_skips_viewport_resize_when_matches_current():
    """Override matches the current viewport → no gratuitous resize. Avoids
    firing a resize event for a scene that explicitly declared the same
    size the spec-level default already provides."""
    page = FakePage()
    rec = Recorder(
        config=SENTINEL_CONFIG,
        base_url="https://example.com",
        default_viewport={"width": 1280, "height": 720},
    )
    scene = {
        "title": "matching viewport",
        "url": "https://example.com/x",
        "viewport": {"width": 1280, "height": 720},  # same as default
        "actions": [{"kind": "press"}],
        "scene_index": 1,
    }
    rec.run_scene(page, scene)

    # No resize: target equals current viewport
    assert page.viewport_calls == [], (
        f"matching viewport should produce no resize; got {page.viewport_calls}"
    )


def test_run_scene_without_viewport_override_does_not_resize():
    """Back-compat: a scene with no ``viewport`` field doesn't resize at
    all. Existing specs are unaffected."""
    page = FakePage()
    rec = Recorder(
        config=SENTINEL_CONFIG,
        base_url="https://example.com",
        default_viewport={"width": 1280, "height": 720},
    )
    scene = {
        "title": "no override",
        "url": "https://example.com/x",
        "actions": [{"kind": "press"}],
        "scene_index": 1,
        # no viewport
    }
    rec.run_scene(page, scene)

    assert page.viewport_calls == [], (
        f"no viewport override should produce no resize; got {page.viewport_calls}"
    )


def test_run_scene_no_default_viewport_skips_restore():
    """If the Recorder was constructed without ``default_viewport`` (older
    callers / tests that don't track viewport), applying a per-scene
    override still works but the restore is a no-op — we don't know what
    to restore to."""
    page = FakePage()
    rec = Recorder(
        config=SENTINEL_CONFIG,
        base_url="https://example.com",
        # default_viewport=None
    )
    scene = {
        "title": "override no default",
        "url": "https://example.com/x",
        "viewport": {"width": 1440, "height": 900},
        "actions": [{"kind": "press"}],
        "scene_index": 1,
    }
    rec.run_scene(page, scene)

    # Apply fired, but restore is a no-op (no default to restore to)
    assert page.viewport_calls == [{"width": 1440, "height": 900}], (
        f"expected only the override apply; got {page.viewport_calls}"
    )


def test_run_scene_two_scenes_override_then_default():
    """Two-scene sequence: first scene overrides, second has no override.
    Expected: apply override, restore default, then NO further resize
    (the restored size already matches what scene 2 wants)."""
    page = FakePage()
    rec = Recorder(
        config=SENTINEL_CONFIG,
        base_url="https://example.com",
        default_viewport={"width": 1280, "height": 720},
    )
    scenes = [
        {
            "title": "wide",
            "url": "https://example.com/wide",
            "viewport": {"width": 1440, "height": 900},
            "actions": [{"kind": "press"}],
            "scene_index": 1,
        },
        {
            "title": "default",
            "url": "https://example.com/default",
            "actions": [{"kind": "press"}],
            "scene_index": 2,
        },
    ]
    rec.run(page, scenes)

    # Exactly two viewport calls: apply override (scene 1), restore (after scene 1).
    # Scene 2 has no override AND the restored default already matches → no extra call.
    assert page.viewport_calls == [
        {"width": 1440, "height": 900},
        {"width": 1280, "height": 720},
    ], f"unexpected viewport sequence: {page.viewport_calls}"
