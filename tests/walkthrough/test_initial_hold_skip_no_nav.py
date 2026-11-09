"""Tests for skipping ``initial_hold_ms`` when no navigation happens.

Background: ``initial_hold_ms`` is a blind post-nav settle — it gives the
freshly-loaded page a moment to paint before the cursor starts moving. It is
skipped when the first action is ``wait_for`` (the wait_for IS
the settle), and also when ``goto_for_scene`` returns
``None`` (stay-on-page scene — typical of the continuation pattern in
``SkipSameUrlRecorder`` and any scene authored without ``url:``): there's
no page-load transition to settle for. The previous scene's
``final_hold_ms`` already provided any transition pause.

These tests pin the no-navigation skip path alongside the existing cases
(url-having scenes keep the wait_for-only skip).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.walkthrough._lib.config import RecorderConfig  # noqa: E402
from scripts.walkthrough._lib.orchestrator import Recorder  # noqa: E402


# Sentinel pacing so each timeout is identifiable.
SENTINEL_CONFIG = RecorderConfig(
    initial_hold_ms=111,
    final_hold_ms=333,
    min_hold_ms=0,
    goto_settle_ms=222,
)


class FakePage:
    """Page-shaped stub that records ``wait_for_timeout`` calls.

    Just enough surface for the orchestrator's per-scene loop with target-
    less first actions (``press``, ``hold``, ``wait_for``). The dispatcher's
    target-requiring verbs need real DOM and aren't exercised here.
    """

    def __init__(self, *, url: str = "https://example.com/already-here"):
        self.url = url
        self.timeouts: list[int] = []
        self.gotos: list[str] = []

    def wait_for_timeout(self, ms):
        self.timeouts.append(int(ms))

    def wait_for_load_state(self, *args, **kwargs):
        pass

    def goto(self, url, *, wait_until=None, timeout=None):
        self.gotos.append(url)
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


def test_no_nav_skips_initial_hold_for_non_waitfor_first_action():
    """The no-navigation skip path: scene has no ``url`` AND first action isn't
    wait_for. The wait_for skip alone does not cover this case (e.g. a stay-on-page
    scene whose first action is ``scroll_to`` or ``click``), which would eat
    ``initial_hold_ms`` of dead air."""
    page = FakePage()
    rec = Recorder(config=SENTINEL_CONFIG, base_url="https://example.com")
    # No ``url`` → stay-on-page. First action is a target-less ``press``
    # so we don't need to mock locator resolution.
    scene = {"title": "continue", "actions": [{"kind": "press"}], "scene_index": 1}
    rec.run_scene(page, scene)
    assert 111 not in page.timeouts, (
        f"no-nav scene should skip initial_hold_ms (111); got {page.timeouts}"
    )
    # final_hold_ms still fires — it's unrelated to nav.
    assert 333 in page.timeouts, (
        f"final_hold_ms (333) must always fire; got {page.timeouts}"
    )


def test_no_nav_also_skips_initial_hold_for_waitfor_first_action():
    """Stacked condition: no nav AND wait_for first. Either condition
    alone is enough to skip initial_hold_ms; the combined case still
    skips (idempotent — no double-print, but that's a log concern, not
    a behavior concern)."""
    page = FakePage()
    rec = Recorder(config=SENTINEL_CONFIG, base_url="https://example.com")
    scene = {"title": "continue+wait", "actions": [{"kind": "wait_for", "target": "Loaded"}], "scene_index": 1}
    rec.run_scene(page, scene)
    assert 111 not in page.timeouts, (
        f"no-nav + wait_for should skip initial_hold_ms (111); got {page.timeouts}"
    )


def test_nav_with_non_waitfor_first_action_still_holds():
    """Back-compat: a scene with ``url`` AND non-wait_for first action
    keeps the wait_for-only skip behavior — initial_hold_ms fires so the
    freshly-loaded page has a moment to paint before the cursor moves."""
    page = FakePage()
    rec = Recorder(config=SENTINEL_CONFIG, base_url="https://example.com")
    scene = {
        "title": "navigate",
        "url": "https://example.com/somewhere",
        "actions": [{"kind": "press"}],
        "scene_index": 1,
    }
    rec.run_scene(page, scene)
    assert 111 in page.timeouts, (
        f"nav + non-wait_for first action should fire initial_hold_ms (111); got {page.timeouts}"
    )


def test_nav_with_waitfor_first_action_skips_initial_hold():
    """Back-compat: the wait_for-skip path is preserved — nav happens,
    but wait_for is the settle, so initial_hold_ms is skipped."""
    page = FakePage()
    rec = Recorder(config=SENTINEL_CONFIG, base_url="https://example.com")
    scene = {
        "title": "navigate+wait",
        "url": "https://example.com/somewhere",
        "actions": [{"kind": "wait_for", "target": "Loaded"}],
        "scene_index": 1,
    }
    rec.run_scene(page, scene)
    assert 111 not in page.timeouts, (
        f"nav + wait_for first should skip initial_hold_ms (111); got {page.timeouts}"
    )


def test_no_nav_means_no_goto_at_all():
    """Sanity: when there's no url, ``page.goto`` is never called. The
    cheaper path doesn't accidentally re-navigate to the existing URL."""
    page = FakePage(url="https://example.com/somewhere")
    rec = Recorder(config=SENTINEL_CONFIG, base_url="https://example.com")
    scene = {"title": "stay", "actions": [{"kind": "press"}], "scene_index": 1}
    rec.run_scene(page, scene)
    assert page.gotos == [], f"no-nav scene should never call goto; got {page.gotos}"
    # Therefore no goto_settle_ms either.
    assert 222 not in page.timeouts, (
        f"no nav → no goto_settle_ms; got {page.timeouts}"
    )


def test_no_nav_empty_actions_still_skips_initial_hold():
    """Edge: a stay-on-page scene with no actions. The
    ``first_action_kind`` is empty (not wait_for), but ``url is None``
    triggers the skip on its own — so initial_hold_ms doesn't fire."""
    page = FakePage()
    rec = Recorder(config=SENTINEL_CONFIG, base_url="https://example.com")
    scene = {"title": "empty-stay", "actions": [], "scene_index": 1}
    rec.run_scene(page, scene)
    assert 111 not in page.timeouts, (
        f"no-nav empty-actions scene should skip initial_hold_ms; got {page.timeouts}"
    )
    # final_hold_ms unaffected.
    assert 333 in page.timeouts
