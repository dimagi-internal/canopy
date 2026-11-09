"""Unit tests for per-action ``scene_index`` stamping.

The DDD orchestrator reported every action in ``run-report.json`` came back
with ``scene_index: None``. Looking at the code: the orchestrator's
``run_scene`` knew which scene each action belonged to, but ``execute_action``
was called scene-agnostically and the result was never tagged.

The fix is in ``Recorder.run_scene``: stamp the scene index onto each
``ActionResult`` via ``dataclasses.replace`` (the dataclass is frozen, so we
can't mutate in place). This keeps ``execute_action`` scene-agnostic — the
dispatcher doesn't need to know which scene it's serving — and lets downstream
graders group results by scene without re-parsing the spec.

Tests here pin:
  - every result in ``RunReport`` has ``scene_index`` matching its source scene
  - the index is the 1-based ORIGINAL spec index (not the loop's position
    after ``--scene`` filtering)
  - an explicit ``scene_index`` kwarg on ``run_scene`` overrides the dict field
  - the field is preserved through ``RunReport.to_json``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.walkthrough._lib.orchestrator import Recorder  # noqa: E402


class FakePage:
    """Just enough Page surface to drive the dispatcher's no-target verbs.

    The dispatcher routes ``press`` / ``type`` / ``hold`` / ``goto`` through
    ``page.keyboard`` / ``page.wait_for_timeout`` / ``page.goto`` — none of
    them need a real DOM or Playwright. ``run_scene`` also calls
    ``page.url`` (read) and ``page.wait_for_load_state`` (no-op).
    """

    def __init__(self):
        self.url = "https://example.com/"
        self.keyboard = _FakeKeyboard()
        self.timeouts: list[int] = []
        self.gotos: list[str] = []

    def wait_for_timeout(self, ms):
        self.timeouts.append(int(ms))

    def wait_for_load_state(self, *args, **kwargs):
        pass

    def goto(self, url, *, wait_until=None, timeout=None):
        self.gotos.append(url)
        self.url = url

    def screenshot(self, **kwargs):
        # Snapshots aren't relevant to scene_index tests; no-op.
        pass

    def evaluate(self, script, *args):
        return ""


class _FakeKeyboard:
    def __init__(self):
        self.pressed: list[str] = []
        self.typed: list[tuple[str, int]] = []

    def press(self, key):
        self.pressed.append(key)

    def type(self, text, *, delay=0):
        self.typed.append((text, delay))


def _scene(*, idx, n_actions=1, title=None):
    """Build a scene with N ``press`` actions and an explicit ``scene_index``."""
    return {
        "title": title or f"Scene {idx}",
        "actions": [{"kind": "press", "value": "Enter"}] * n_actions,
        "scene_index": idx,
    }


def test_single_scene_stamps_index_on_every_action():
    page = FakePage()
    rec = Recorder()
    rec.run_scene(page, _scene(idx=3, n_actions=4))

    indices = [r.scene_index for r in rec.report.results]
    assert indices == [3, 3, 3, 3]
    # And ok=True is preserved (the stamp doesn't clobber other fields).
    assert all(r.ok for r in rec.report.results)
    assert all(r.kind == "press" for r in rec.report.results)


def test_multi_scene_run_tags_actions_per_scene():
    page = FakePage()
    rec = Recorder()
    scenes = [_scene(idx=1, n_actions=2), _scene(idx=2, n_actions=3), _scene(idx=3, n_actions=1)]
    rec.run(page, scenes)

    grouped: dict[int, int] = {}
    for r in rec.report.results:
        grouped[r.scene_index] = grouped.get(r.scene_index, 0) + 1
    assert grouped == {1: 2, 2: 3, 3: 1}


def test_scene_index_uses_original_spec_position_not_loop_position():
    """``--scene 5,7`` keeps the original spec indices on the results.

    If the orchestrator stamped the loop's ``i`` (1, 2) on the filtered list,
    we'd lose the link back to the source spec — and the snapshot filenames
    (scene_5.png) wouldn't match the report's scene_index field (1).
    """
    page = FakePage()
    rec = Recorder()
    # Two scenes that survived a ``--scene 5,7`` filter — list positions
    # 1 and 2, original spec indices 5 and 7.
    scenes = [_scene(idx=5, n_actions=2), _scene(idx=7, n_actions=2)]
    rec.run(page, scenes)

    indices = sorted({r.scene_index for r in rec.report.results})
    assert indices == [5, 7]


def test_explicit_kwarg_overrides_scene_dict_field():
    """``run_scene(scene_index=...)`` wins over ``scene["scene_index"]``.

    Lets ad-hoc test callers pass an index without mutating the scene dict.
    """
    page = FakePage()
    rec = Recorder()
    scene = _scene(idx=99, n_actions=1)  # dict says 99
    rec.run_scene(page, scene, scene_index=42)  # kwarg says 42

    assert rec.report.results[0].scene_index == 42


def test_run_falls_back_to_loop_position_when_scene_index_missing():
    """Scenes without ``scene_index`` fall back to the 1-based loop position.

    This is the ``run`` (not ``run_scene``) path — the orchestrator passes
    ``scene.get("scene_index", i)``, so a raw scene dict from a test still
    gets a sensible index.
    """
    page = FakePage()
    rec = Recorder()
    rec.run(
        page,
        [
            {"title": "a", "actions": [{"kind": "press", "value": "Enter"}]},  # no scene_index
            {"title": "b", "actions": [{"kind": "press", "value": "Enter"}]},
        ],
    )
    indices = [r.scene_index for r in rec.report.results]
    assert indices == [1, 2]


def test_scene_index_survives_to_json():
    """``RunReport.to_json`` serialises ``scene_index`` so external tools see it."""
    page = FakePage()
    rec = Recorder()
    rec.run(page, [_scene(idx=4, n_actions=2)])

    parsed = json.loads(rec.report.to_json())
    assert parsed["total"] == 2
    for action in parsed["actions"]:
        assert action["scene_index"] == 4


def test_scene_index_defaults_to_none_when_no_orchestrator():
    """Direct ``execute_action`` calls (no orchestrator) leave the field None.

    The dispatcher is scene-agnostic on purpose; only the orchestrator
    stamps. A unit test that calls ``execute_action`` directly should
    still see a coherent default.
    """
    from scripts.walkthrough._lib.recorder import execute_action

    page = FakePage()
    r = execute_action(page, {"kind": "press", "value": "Enter"})
    assert r.scene_index is None
