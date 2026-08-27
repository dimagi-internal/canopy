"""Unit tests for per-scene snapshots (PR #105 gap 1).

``record_video.py``'s ``--snapshots <dir>`` flag — and the underlying
``Recorder.take_snapshot`` hook + ``Recorder.run_scene`` wiring — is exercised
here against a fake Page that records ``screenshot`` and ``evaluate`` calls.
The real Playwright path is exercised manually via the walkthrough loop; these
tests just pin the contract:

  - one PNG + one page-text JSON per scene-with-actions
  - filenames use the 1-based ORIGINAL spec index (so ``--scene 3`` still
    produces ``scene_3.png``, not ``scene_1.png``)
  - empty-action scenes are skipped unless ``snapshot_empty_scenes=True``
  - if ``snapshot_dir`` is None, no captures happen
  - the JSON includes scene_index, url, title, and page_text
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.walkthrough._lib.orchestrator import Recorder  # noqa: E402


class FakePage:
    """Page-shaped object that records screenshot + evaluate calls.

    The dispatcher's verb routing isn't under test here — only the
    snapshot capture path. Actions are stubbed by giving the page no
    matching elements; the orchestrator's ``run_scene`` still calls
    ``wait_for_timeout`` and the snapshot hook in the right order, which
    is what we care about.
    """

    def __init__(self, *, url: str = "https://example.com/"):
        self.url = url
        self.screenshots: list[dict] = []
        self.eval_calls: list[str] = []
        self.body_text = "Sample page text — settled steady-state."
        self.timeouts: list[int] = []
        self.gotos: list[str] = []

    # surface methods the orchestrator / dispatcher poke at
    def wait_for_timeout(self, ms):
        self.timeouts.append(int(ms))

    def wait_for_load_state(self, *args, **kwargs):
        pass

    def goto(self, url, *, wait_until=None, timeout=None):
        self.gotos.append(url)
        self.url = url

    def screenshot(self, *, path: str, full_page: bool = False, timeout: int | None = None):
        # Create an empty file so the assert-on-existence path in callers works
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")  # 8-byte PNG magic
        self.screenshots.append({"path": path, "full_page": full_page})

    def evaluate(self, script, *args):
        self.eval_calls.append(script)
        # Mimic ``document.body.innerText``
        if "innerText" in script:
            return self.body_text
        return None


def test_snapshot_emits_png_and_json_for_action_bearing_scene(tmp_path):
    page = FakePage(url="https://example.com/plan")
    rec = Recorder(snapshot_dir=tmp_path)
    scene = {
        "title": "Resolve buildings",
        "actions": [{"kind": "press", "value": "Enter"}],
        "scene_index": 3,
    }
    rec.run_scene(page, scene)

    png = tmp_path / "scene_3.png"
    txt = tmp_path / "scene_3_page_text.json"
    assert png.exists(), f"expected {png}"
    assert txt.exists(), f"expected {txt}"
    payload = json.loads(txt.read_text())
    assert payload["scene_index"] == 3
    assert payload["url"] == "https://example.com/plan"
    assert payload["title"] == "Resolve buildings"
    assert payload["page_text"] == "Sample page text — settled steady-state."
    # Screenshot was the full-page variety
    assert page.screenshots == [{"path": str(png), "full_page": True}]
    assert rec.snapshots_taken == [3]


def test_snapshot_full_page_false_captures_viewport(tmp_path):
    """A scene with ``full_page: false`` is captured at the viewport, not full-page.

    Used for map+table pages (e.g. plan-review) so the map is the hero instead of
    a sliver atop a tall strip. Regression guard: the default stays full-page.
    """
    page = FakePage(url="https://example.com/group/1/map")
    rec = Recorder(snapshot_dir=tmp_path)
    scene = {
        "title": "Both arms on one map",
        "actions": [{"kind": "press", "value": "Enter"}],
        "scene_index": 2,
        "full_page": False,
    }
    rec.run_scene(page, scene)
    assert page.screenshots == [{"path": str(tmp_path / "scene_2.png"), "full_page": False}]


def test_build_scenes_from_spec_preserves_full_page(tmp_path):
    """``build_scenes_from_spec`` must carry ``full_page`` through to the recorder.

    Dropping it (the original bug) made map+table scenes capture as unreadable
    full-page strips even when the author set ``full_page: false``.
    """
    from scripts.walkthrough.record_video import build_scenes_from_spec

    spec = {
        "scenes": [
            {"title": "normal", "url": "/a", "actions": []},
            {"title": "map", "url": "/b", "full_page": False, "actions": []},
        ]
    }
    scenes = build_scenes_from_spec(spec, base_url="https://example.com", run_data=None)
    assert scenes[0]["full_page"] is None  # default → recorder treats as full-page
    assert scenes[1]["full_page"] is False  # explicit override survives


def test_snapshot_uses_original_spec_index_not_loop_position(tmp_path):
    """A ``--scene 3`` partial run still produces ``scene_3.*``, not ``scene_1.*``.

    The orchestrator's ``run`` threads ``scene["scene_index"]`` into each
    ``run_scene`` call. That index is the 1-based spec-source index set by
    ``build_scenes_from_spec``, NOT the position in the (filtered) list.
    """
    page = FakePage()
    rec = Recorder(snapshot_dir=tmp_path)
    # Imagine ``--scene 5,7`` — two scenes survive the filter but with
    # their original spec indices intact.
    scenes = [
        {"title": "five", "actions": [{"kind": "press", "value": "Enter"}], "scene_index": 5},
        {"title": "seven", "actions": [{"kind": "press", "value": "Enter"}], "scene_index": 7},
    ]
    rec.run(page, scenes)

    assert (tmp_path / "scene_5.png").exists()
    assert (tmp_path / "scene_5_page_text.json").exists()
    assert (tmp_path / "scene_7.png").exists()
    assert (tmp_path / "scene_7_page_text.json").exists()
    # No leakage to the loop-position filenames
    assert not (tmp_path / "scene_1.png").exists()
    assert not (tmp_path / "scene_2.png").exists()
    assert rec.snapshots_taken == [5, 7]


def test_snapshot_skips_empty_action_scenes_by_default(tmp_path):
    """Narrative-only scenes (the back half of microplans-10-wards) skip capture.

    Default behaviour: an empty ``actions`` list means nothing the cursor
    could change between init-hold and final-hold, so the snapshot would
    duplicate the previous scene's. Skip.
    """
    page = FakePage()
    rec = Recorder(snapshot_dir=tmp_path)
    scenes = [
        {"title": "narrative", "actions": [], "scene_index": 6},
        {"title": "narrative2", "actions": None, "scene_index": 7},
        {"title": "active", "actions": [{"kind": "press", "value": "Enter"}], "scene_index": 8},
    ]
    rec.run(page, scenes)

    assert not (tmp_path / "scene_6.png").exists()
    assert not (tmp_path / "scene_7.png").exists()
    assert (tmp_path / "scene_8.png").exists()
    assert rec.snapshots_taken == [8]


def test_snapshot_empty_scenes_toggle_captures_action_free_scenes(tmp_path):
    """``snapshot_empty_scenes=True`` overrides the action-gate."""
    page = FakePage()
    rec = Recorder(snapshot_dir=tmp_path, snapshot_empty_scenes=True)
    scenes = [
        {"title": "narrative", "actions": [], "scene_index": 6},
        {"title": "active", "actions": [{"kind": "press", "value": "Enter"}], "scene_index": 8},
    ]
    rec.run(page, scenes)

    assert (tmp_path / "scene_6.png").exists()
    assert (tmp_path / "scene_8.png").exists()
    assert rec.snapshots_taken == [6, 8]


def test_no_snapshots_when_snapshot_dir_unset(tmp_path):
    """Default Recorder produces no snapshot files."""
    page = FakePage()
    rec = Recorder()  # no snapshot_dir
    scene = {"title": "x", "actions": [{"kind": "press", "value": "Enter"}], "scene_index": 1}
    rec.run_scene(page, scene)

    assert list(tmp_path.iterdir()) == []
    assert rec.snapshots_taken == []
    assert page.screenshots == []


def test_snapshot_dir_created_lazily(tmp_path):
    """Recorder doesn't pre-create the dir; first scene to snapshot creates it.

    Lets the caller pass a path inside a tmp tree without worrying about
    whether the parent exists.
    """
    target = tmp_path / "deeply" / "nested" / "snaps"
    assert not target.exists()
    page = FakePage()
    rec = Recorder(snapshot_dir=target)
    rec.run_scene(
        page,
        {"title": "x", "actions": [{"kind": "press", "value": "Enter"}], "scene_index": 1},
    )
    assert target.is_dir()
    assert (target / "scene_1.png").exists()


def test_snapshot_falls_back_to_scene_index_attr_when_kwarg_missing(tmp_path):
    """``run_scene`` reads ``scene["scene_index"]`` when no kwarg passed."""
    page = FakePage()
    rec = Recorder(snapshot_dir=tmp_path)
    scene = {"title": "x", "actions": [{"kind": "press", "value": "Enter"}], "scene_index": 42}
    rec.run_scene(page, scene)  # no scene_index kwarg
    assert (tmp_path / "scene_42.png").exists()


def test_snapshot_skipped_when_no_scene_index_available(tmp_path):
    """If the scene has no ``scene_index`` and no kwarg, no snapshot fires.

    The filename would be ambiguous; refuse rather than guess.
    """
    page = FakePage()
    rec = Recorder(snapshot_dir=tmp_path)
    rec.run_scene(page, {"title": "x", "actions": [{"kind": "press", "value": "Enter"}]})
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# Visual capture (canopy#525) — the recording is never at risk over a lens input
# ---------------------------------------------------------------------------


def _recorder_with(tmp_path, evaluate):
    """A Recorder pointed at tmp_path whose page.evaluate does `evaluate`."""

    class _Page:
        url = "https://example.com/"

        def evaluate(self, script, *args):
            return evaluate(script)

    recorder = Recorder.__new__(Recorder)
    recorder.snapshot_dir = tmp_path
    recorder.render_id = "render-1"
    return recorder, _Page()


def test_visual_capture_writes_the_lens_input(tmp_path):
    payload = {"elements": [], "defined_class_selectors": ["h-28"], "stylesheets_readable": True}
    recorder, page = _recorder_with(tmp_path, lambda script: dict(payload))

    assert recorder.take_visual_capture(page, 3) is True
    written = json.loads((tmp_path / "scene_3_visual.json").read_text())
    assert written["scene_index"] == 3
    assert written["url"] == "https://example.com/"
    assert written["render_id"] == "render-1"
    assert written["defined_class_selectors"] == ["h-28"]


def test_visual_capture_of_a_page_that_returns_nothing_is_not_a_recording_failure(tmp_path):
    """A page (or a test double) whose evaluate returns None must not take the
    recording down with it — the mp4 is the expensive artifact and the lens
    degrades to `skip`. This is a regression test: adding the capture broke
    eleven existing snapshot tests exactly this way before the guard existed."""
    recorder, page = _recorder_with(tmp_path, lambda script: None)

    assert recorder.take_visual_capture(page, 3) is False
    assert not (tmp_path / "scene_3_visual.json").exists()


def test_visual_capture_swallows_an_evaluate_that_raises(tmp_path):
    def _boom(script):
        raise RuntimeError("execution context destroyed")

    recorder, page = _recorder_with(tmp_path, _boom)
    assert recorder.take_visual_capture(page, 3) is False
    assert not (tmp_path / "scene_3_visual.json").exists()
