"""Tests for the redundant-first-goto strip in ``build_scenes_from_spec``.

Background: ``Scene.url`` is the declarative entry point — the
orchestrator's ``run_scene`` already does ``page.goto(scene.url)`` before
running the action list. But many existing specs *also* lead with
``{kind: goto, target: <same-url>}`` (the legacy entry pattern), so the
recorder ends up navigating twice — a ~2.5s reload that the viewer sees as
dead-air right after the scene title.

This test pins the strip rule:

  - leading ``goto`` whose absolutized target equals ``scene.url`` → drop it
  - leading ``goto`` whose target is different from ``scene.url`` → keep it
    (intentional reload-then-elsewhere pattern is preserved)
  - leading goto when no explicit ``scene.url`` is set, but the goto's own
    target defines the URL the orchestrator will navigate to → drop it (it
    would otherwise fire twice — the orchestrator's nav + the action list)
  - non-leading goto → never dropped (could be a mid-scene route change)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.walkthrough.record_video import build_scenes_from_spec  # noqa: E402


BASE = "https://labs.connect.dimagi.com"


def _spec(scenes):
    return {"scenes": scenes}


def test_leading_goto_matching_scene_url_is_dropped():
    """Scene with both ``url:`` and a leading ``goto`` to the same path —
    the goto is dropped so the orchestrator's auto-nav doesn't double-fire."""
    spec = _spec([
        {
            "title": "Maya opens",
            "url": "/microplans/program/133/",
            "actions": [
                {"kind": "goto", "target": "/microplans/program/133/"},
                {"kind": "wait_for", "target": "Microplan portfolio"},
            ],
        }
    ])
    scenes = build_scenes_from_spec(spec, BASE, run_data=None)
    assert len(scenes) == 1
    assert scenes[0]["url"] == BASE + "/microplans/program/133/"
    kinds = [a["kind"] for a in scenes[0]["actions"]]
    assert kinds == ["wait_for"], f"expected goto to be dropped; got {kinds}"


def test_leading_goto_to_different_url_is_preserved():
    """Intentional reload-then-elsewhere: scene starts at /a then explicitly
    routes to /b. The goto to /b must NOT be dropped."""
    spec = _spec([
        {
            "title": "Cross-page beat",
            "url": "/microplans/program/133/",
            "actions": [
                {"kind": "goto", "target": "/microplans/program/133/compare/"},
                {"kind": "wait_for", "target": "Pick plans"},
            ],
        }
    ])
    scenes = build_scenes_from_spec(spec, BASE, run_data=None)
    assert len(scenes) == 1
    kinds = [a["kind"] for a in scenes[0]["actions"]]
    assert kinds == ["goto", "wait_for"], f"expected goto to be preserved; got {kinds}"


def test_leading_goto_no_explicit_url_is_dropped():
    """No ``url:`` field — the orchestrator inferred URL from the leading
    goto's target (legacy behavior). The goto still must be dropped from
    the action list, otherwise it dispatches twice (orchestrator's nav +
    action loop). This is the live bug for the legacy spec dialect."""
    spec = _spec([
        {
            "title": "Old-style scene",
            "actions": [
                {"kind": "goto", "target": "/microplans/program/133/"},
                {"kind": "wait_for", "target": "Microplan portfolio"},
            ],
        }
    ])
    scenes = build_scenes_from_spec(spec, BASE, run_data=None)
    assert len(scenes) == 1
    # URL was inferred from the goto's target
    assert scenes[0]["url"] == BASE + "/microplans/program/133/"
    # And the goto is gone from actions so it doesn't double-fire
    kinds = [a["kind"] for a in scenes[0]["actions"]]
    assert kinds == ["wait_for"], f"expected inferred-url goto to be dropped; got {kinds}"


def test_non_leading_goto_is_never_dropped():
    """A ``goto`` later in the action list is an intentional mid-scene
    navigation (e.g. clicking through to a page that has no labelled
    trigger). Strip only inspects the FIRST action."""
    spec = _spec([
        {
            "title": "Mid-scene route change",
            "url": "/microplans/program/133/",
            "actions": [
                {"kind": "wait_for", "target": "Microplan portfolio"},
                {"kind": "goto", "target": "/microplans/program/133/"},  # would match url
            ],
        }
    ])
    scenes = build_scenes_from_spec(spec, BASE, run_data=None)
    kinds = [a["kind"] for a in scenes[0]["actions"]]
    assert kinds == ["wait_for", "goto"], f"non-leading goto should survive; got {kinds}"


def test_leading_goto_absolute_matches_relative_scene_url():
    """The strip rule absolutizes both targets before comparing — so an
    absolute-URL leading goto whose target matches the relative scene.url
    (after _absolutize) is still recognized as redundant."""
    spec = _spec([
        {
            "title": "Mixed absolute/relative",
            "url": "/microplans/program/133/",
            "actions": [
                {"kind": "goto", "target": BASE + "/microplans/program/133/"},
                {"kind": "wait_for", "target": "Microplan portfolio"},
            ],
        }
    ])
    scenes = build_scenes_from_spec(spec, BASE, run_data=None)
    kinds = [a["kind"] for a in scenes[0]["actions"]]
    assert kinds == ["wait_for"], f"absolute leading goto should be dropped; got {kinds}"


def test_no_actions_no_crash():
    """A scene with ``url:`` and no actions list — strip rule must be a
    no-op, not raise IndexError on actions[0]."""
    spec = _spec([
        {
            "title": "Action-empty scene",
            "url": "/microplans/program/133/",
        }
    ])
    scenes = build_scenes_from_spec(spec, BASE, run_data=None)
    assert scenes[0]["actions"] == []
