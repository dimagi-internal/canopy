"""Tests for ``Recorder.take_snapshot`` retrying once on WebGL screenshot
timeout.

Background: WebGL / Mapbox / Canvas-heavy pages hang
``Page.captureScreenshot`` in headless Chromium — the recurring SwiftShader-
headless bug
(``reference_browse_webgl_swiftshader``). The manual workaround is to re-capture
the failing scene in a separate ``playwright.sync_api`` session with an explicit
8-10s sleep before retrying ``page.screenshot()``. That is a built-in retry
path.

Contract:
  - First screenshot timeout → settle 8s → retry once.
  - Retry succeeds → PNG written, ``snapshots_taken`` updated.
  - Retry also fails → ``take_snapshot`` doesn't raise; text dump still
    written (so visual-judge has at least one input per scene); the failed
    scene is NOT added to ``snapshots_taken``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.walkthrough._lib.orchestrator import Recorder  # noqa: E402


class FlakyScreenshotPage:
    """Page-shaped stub whose ``screenshot`` raises a configurable number of
    times before succeeding (or never succeeds).

    Tracks ``wait_for_timeout`` calls so we can pin the 8s settle between
    attempts — the agent's documented workaround.
    """

    def __init__(self, *, url: str = "https://example.com/", fail_times: int = 1):
        self.url = url
        self.fail_times = fail_times
        self.screenshot_attempts: list[dict] = []
        self.timeouts: list[int] = []
        self.eval_calls: list[str] = []
        self.body_text = "Captured page text — even with PNG failure."
        self.gotos: list[str] = []

    def wait_for_timeout(self, ms):
        self.timeouts.append(int(ms))

    def wait_for_load_state(self, *args, **kwargs):
        pass

    def goto(self, url, *, wait_until=None, timeout=None):
        self.gotos.append(url)
        self.url = url

    def screenshot(self, *, path: str, full_page: bool = False, timeout: int | None = None):
        self.screenshot_attempts.append({"path": path, "full_page": full_page, "timeout": timeout})
        if len(self.screenshot_attempts) <= self.fail_times:
            # Mimic Playwright's timeout exception — orchestrator catches Exception broadly.
            raise TimeoutError(
                f"page.screenshot timed out (mock; attempt {len(self.screenshot_attempts)})"
            )
        # Success: write a tiny PNG so existence checks pass.
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")

    def evaluate(self, script, *args):
        self.eval_calls.append(script)
        if "innerText" in script:
            return self.body_text
        return None


def _scene_with_action(index: int = 3) -> dict:
    return {
        "title": "WebGL-heavy plan map",
        "actions": [{"kind": "press", "value": "Enter"}],
        "scene_index": index,
    }


def test_snapshot_retries_after_one_screenshot_timeout(tmp_path):
    """First screenshot raises → recorder settles 8000ms → second screenshot
    succeeds → PNG written, scene tracked as captured."""
    page = FlakyScreenshotPage(fail_times=1)
    rec = Recorder(snapshot_dir=tmp_path)
    rec.run_scene(page, _scene_with_action(index=4))

    # Two screenshot attempts (one fail + one success)
    assert len(page.screenshot_attempts) == 2, (
        f"expected 2 screenshot attempts; got {len(page.screenshot_attempts)}: "
        f"{page.screenshot_attempts}"
    )
    # The retry path settled for 8000ms between the two attempts
    assert 8000 in page.timeouts, (
        f"expected 8000ms settle between screenshot attempts; got timeouts {page.timeouts}"
    )
    # PNG exists; scene is in snapshots_taken
    png = tmp_path / "scene_4.png"
    assert png.exists(), "expected the retry to produce the PNG"
    assert rec.snapshots_taken == [4]
    # Text dump produced regardless
    txt = tmp_path / "scene_4_page_text.json"
    assert txt.exists()
    assert json.loads(txt.read_text())["page_text"] == page.body_text


def test_snapshot_succeeds_first_try_no_settle(tmp_path):
    """No timeout → no retry → no 8000ms settle, single screenshot attempt."""
    page = FlakyScreenshotPage(fail_times=0)
    rec = Recorder(snapshot_dir=tmp_path)
    rec.run_scene(page, _scene_with_action(index=2))

    assert len(page.screenshot_attempts) == 1
    assert 8000 not in page.timeouts, (
        f"no retry → no 8000ms settle; got timeouts {page.timeouts}"
    )
    assert (tmp_path / "scene_2.png").exists()
    assert rec.snapshots_taken == [2]


def test_snapshot_does_not_raise_when_both_attempts_fail(tmp_path):
    """Pathological case: both attempts time out. Recorder must NOT raise —
    one bad snapshot can't kill a multi-scene run. The text dump is still
    written so visual-judge has at least one input per scene; the PNG is
    just missing."""
    page = FlakyScreenshotPage(fail_times=2)
    rec = Recorder(snapshot_dir=tmp_path)

    # Should not raise
    rec.run_scene(page, _scene_with_action(index=5))

    # Both attempts fired (one initial + one retry)
    assert len(page.screenshot_attempts) == 2
    # Settle still happened between them
    assert 8000 in page.timeouts
    # PNG missing
    assert not (tmp_path / "scene_5.png").exists()
    # Text dump written (the load-bearing reason for the fall-through)
    txt = tmp_path / "scene_5_page_text.json"
    assert txt.exists()
    payload = json.loads(txt.read_text())
    assert payload["scene_index"] == 5
    assert payload["page_text"] == page.body_text
    # snapshots_taken does NOT include the failed scene
    assert rec.snapshots_taken == []


def test_snapshot_retry_uses_longer_timeout_on_second_attempt(tmp_path):
    """Second screenshot attempt uses a longer timeout (20s) than the first
    (10s) so we don't burn the retry on the same race that killed attempt
    one."""
    page = FlakyScreenshotPage(fail_times=1)
    rec = Recorder(snapshot_dir=tmp_path)
    rec.run_scene(page, _scene_with_action(index=6))

    assert len(page.screenshot_attempts) == 2
    first_timeout = page.screenshot_attempts[0]["timeout"]
    second_timeout = page.screenshot_attempts[1]["timeout"]
    assert first_timeout == 10000, (
        f"first attempt should use 10000ms timeout; got {first_timeout}"
    )
    assert second_timeout == 20000, (
        f"retry should use 20000ms timeout; got {second_timeout}"
    )
