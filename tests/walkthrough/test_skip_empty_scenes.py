"""Unit tests for ``record_video.py --skip-empty-scenes``.

The filter is a tiny pure function (``filter_empty_scenes``) so the
recording-loop-level effect can be pinned without spinning a Playwright
browser:

  - Scenes with empty / missing ``actions`` are dropped.
  - Scenes that survive keep their 1-based ORIGINAL spec ``scene_index``
    intact — matches snapshot filenames + ActionResult tagging.
  - The default (no flag) is back-compat: every scene records identically.
  - The flag composes cleanly with the per-scene snapshot gate:
    ``--skip-empty-scenes`` removes the scene from the recording loop AND
    ``Recorder.take_snapshot``'s own action-gate skips it — no double-handling.

The motivation lives in microplans-10-wards: scenes 6-11 are narrative-only
(no actions). With the flag, the mp4 ends after the action-bearing scenes
instead of holding ~30s on a static URL the previous scene happened to
leave the page on.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# record_video.py lives as a script in scripts/walkthrough/. Importing it as a
# module is fine — Python finds it via the sys.path entry above.
from scripts.walkthrough.record_video import (  # noqa: E402
    _is_empty_scene,
    filter_empty_scenes,
)
from scripts.walkthrough._lib.orchestrator import Recorder  # noqa: E402


# ---- the pure helper ------------------------------------------------------


def test_is_empty_scene_for_empty_actions_list():
    assert _is_empty_scene({"title": "x", "actions": []}) is True


def test_is_empty_scene_for_missing_actions_key():
    assert _is_empty_scene({"title": "x"}) is True


def test_is_empty_scene_for_none_actions():
    assert _is_empty_scene({"title": "x", "actions": None}) is True


def test_is_empty_scene_false_for_non_empty():
    assert _is_empty_scene({"title": "x", "actions": [{"kind": "press"}]}) is False


# ---- the list filter preserves scene_index --------------------------------


def test_filter_drops_empty_scenes_preserving_order():
    scenes = [
        {"title": "a", "actions": [{"kind": "press"}], "scene_index": 1},
        {"title": "b", "actions": [], "scene_index": 2},
        {"title": "c", "actions": [{"kind": "press"}], "scene_index": 3},
        {"title": "d", "actions": None, "scene_index": 4},
        {"title": "e", "actions": [{"kind": "press"}], "scene_index": 5},
    ]
    out = filter_empty_scenes(scenes)
    assert [s["title"] for s in out] == ["a", "c", "e"]
    # CRITICAL: 1-based ORIGINAL spec indices survive — the snapshot
    # filename for "c" is still scene_3.png, not scene_2.png.
    assert [s["scene_index"] for s in out] == [1, 3, 5]


def test_filter_empty_input_returns_empty():
    assert filter_empty_scenes([]) == []


def test_filter_all_empty_returns_empty():
    scenes = [
        {"title": "a", "actions": [], "scene_index": 1},
        {"title": "b", "actions": None, "scene_index": 2},
        {"title": "c", "scene_index": 3},  # no actions key at all
    ]
    assert filter_empty_scenes(scenes) == []


def test_filter_all_non_empty_returns_input():
    scenes = [
        {"title": "a", "actions": [{"kind": "press"}], "scene_index": 1},
        {"title": "b", "actions": [{"kind": "press"}], "scene_index": 2},
    ]
    out = filter_empty_scenes(scenes)
    assert out == scenes


# ---- microplans-10-wards shape (back half is narrative-only) --------------


def test_filter_microplans_back_half_pattern():
    """Scenes 1-5 do work, 6-11 are narrative-only — the exact shape that
    motivated the flag. The filter should leave scenes 1-5 intact."""
    scenes = []
    for i in range(1, 6):
        scenes.append({
            "title": f"action scene {i}",
            "actions": [{"kind": "press", "value": "Enter"}],
            "scene_index": i,
        })
    for i in range(6, 12):
        scenes.append({
            "title": f"narrative scene {i}",
            "actions": [],
            "scene_index": i,
        })

    out = filter_empty_scenes(scenes)
    assert len(out) == 5
    assert [s["scene_index"] for s in out] == [1, 2, 3, 4, 5]


# ---- composition with Recorder snapshot gate --------------------


class _FakePage:
    """Same shape as test_record_video_snapshots.FakePage but only the
    methods the orchestrator + snapshot path actually call."""

    def __init__(self):
        self.url = "https://example.com/"
        self.screenshots: list[str] = []
        self.body_text = "page text"
        self.timeouts: list[int] = []
        self.gotos: list[str] = []
        # The dispatcher's ``press`` action calls page.keyboard.press —
        # stub it so the test scenes can execute without a real browser.

        class _K:
            def press(self_inner, key):
                pass

            def type(self_inner, text, *, delay=0):
                pass

        self.keyboard = _K()

    def wait_for_timeout(self, ms):
        self.timeouts.append(int(ms))

    def wait_for_load_state(self, *a, **k):
        pass

    def goto(self, url, *, wait_until=None, timeout=None):
        self.gotos.append(url)
        self.url = url

    def screenshot(self, *, path, full_page=False, timeout=None):
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")
        self.screenshots.append(path)

    def evaluate(self, script, *args):
        if "innerText" in script:
            return self.body_text
        return None


def test_filter_composes_with_snapshot_gate(tmp_path):
    """``--skip-empty-scenes`` removes the scene from the recording loop;
    ``Recorder.take_snapshot``'s own action-gate would have skipped it too.
    Both behaviours together = the empty scene leaves NO trace (no clip
    frame, no snapshot file)."""
    page = _FakePage()
    rec = Recorder(snapshot_dir=tmp_path)
    pre_filter = [
        {"title": "a", "actions": [{"kind": "press", "value": "Enter"}], "scene_index": 1},
        {"title": "narrative", "actions": [], "scene_index": 2},
        {"title": "c", "actions": [{"kind": "press", "value": "Enter"}], "scene_index": 3},
    ]
    filtered = filter_empty_scenes(pre_filter)
    rec.run(page, filtered)

    # Snapshot files only for the action-bearing scenes
    assert (tmp_path / "scene_1.png").exists()
    assert not (tmp_path / "scene_2.png").exists()
    assert (tmp_path / "scene_3.png").exists()
    assert rec.snapshots_taken == [1, 3]


def test_recorder_records_all_scenes_when_filter_not_applied():
    """Default (no --skip-empty-scenes) preserves the full
    iteration contract: the orchestrator visits every scene, including
    the action-empty ones. The no-nav initial_hold_ms is skipped
    (a stay-on-page scene has no page-load transition to settle for) so
    a same-URL scene contributes ``final_hold_ms`` only — but it
    still runs through the loop, the snapshot gate still
    fires (skipped for empty scenes), and ``run`` returns elapsed time
    for both."""
    page = _FakePage()
    rec = Recorder()  # no snapshot_dir → no snapshot files; clip behaviour unchanged
    scenes = [
        # Both scenes omit ``url`` so they're no-nav stay-on-page scenes —
        # initial_hold_ms is correctly skipped for a no-nav scene.
        {"title": "a", "actions": [{"kind": "press", "value": "Enter"}], "scene_index": 1},
        {"title": "narrative", "actions": [], "scene_index": 2},
    ]
    elapsed = rec.run(page, scenes)

    # Both scenes' final_hold_ms (1000ms each) fires — proves the loop visits
    # every scene, even when initial_hold_ms is correctly deferred.
    assert page.timeouts.count(rec.config.final_hold_ms) == 2, (
        f"final_hold_ms should fire once per scene; got timeouts={page.timeouts}"
    )
    # And run() returns total elapsed time covering both scenes.
    assert elapsed > 0
