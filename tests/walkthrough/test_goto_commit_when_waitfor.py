"""Tests for ``goto_and_settle`` using ``wait_until="commit"`` when the next
action is ``wait_for``.

Background: when leaving a WebGL / Mapbox-heavy scene to a new page, the torn-
down GL context's residual telemetry and tile-fetch network activity can stall
Playwright's lifecycle tracking — the ``load`` event signal can hang for the
full ``load_settle_timeout_ms`` while Chromium hasn't painted the new page's
first frame yet. The symptom is a multi-second gray
viewport window between scenes (about 7s when measured by frame-sampling a clip).

The fix: when ``skip_settle=True`` (the caller already knows the next action
is ``wait_for``), use ``wait_until="commit"`` — return as soon as the
navigation request is committed — and skip the ``load`` event wait AND the
``goto_settle_ms`` blind hold. The wait_for action that's about to fire will
do its own polling — much more accurate than guessing at ``load`` timing.

These tests pin the new fast-path while preserving the back-compat slow-path
for every non-wait_for first action.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.walkthrough._lib.config import RecorderConfig  # noqa: E402
from scripts.walkthrough._lib.orchestrator import Recorder  # noqa: E402


SENTINEL_CONFIG = RecorderConfig(
    initial_hold_ms=111,
    final_hold_ms=333,
    min_hold_ms=0,
    goto_settle_ms=222,
    load_settle_timeout_ms=8000,
    goto_timeout_ms=60000,
)


class FakePage:
    """Page-shaped stub that records what ``goto_and_settle`` calls.

    Tracks the ``wait_until`` kwarg passed to ``goto`` so we can pin the new
    commit-mode behavior. Also tracks whether ``wait_for_load_state`` and
    ``wait_for_timeout`` fired — both must NOT be called on the skip_settle
    path.
    """

    def __init__(self, *, url: str = "https://example.com/"):
        self.url = url
        self.timeouts: list[int] = []
        self.gotos: list[dict] = []
        self.load_state_calls: list[tuple] = []
        self.wait_for_selector_calls: list[str] = []
        self.wait_for_function_calls: list[str] = []

    def wait_for_timeout(self, ms):
        self.timeouts.append(int(ms))

    def wait_for_load_state(self, *args, **kwargs):
        self.load_state_calls.append((args, kwargs))

    def goto(self, url, *, wait_until=None, timeout=None):
        self.gotos.append({"url": url, "wait_until": wait_until, "timeout": timeout})
        self.url = url

    def wait_for_selector(self, selector, *, timeout=None, state=None):
        self.wait_for_selector_calls.append(selector)

        class _Handle:
            def click(self_inner, *a, **k):
                pass

            def scroll_into_view_if_needed(self_inner, *a, **k):
                pass

        return _Handle()

    def wait_for_function(self, expr, *, timeout=None):
        self.wait_for_function_calls.append(expr)

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


def test_goto_uses_commit_when_skip_settle_true():
    """The fast-path: ``goto_and_settle(skip_settle=True)`` calls ``goto``
    with ``wait_until="commit"`` and returns immediately — no
    ``wait_for_load_state``, no ``wait_for_timeout(goto_settle_ms)``."""
    page = FakePage()
    rec = Recorder(config=SENTINEL_CONFIG)
    rec.goto_and_settle(page, "https://example.com/x", skip_settle=True)

    assert len(page.gotos) == 1
    assert page.gotos[0]["wait_until"] == "commit", (
        f"expected wait_until=commit; got {page.gotos[0]['wait_until']}"
    )
    assert page.load_state_calls == [], (
        f"wait_for_load_state must NOT be called on commit-mode path; "
        f"got {page.load_state_calls}"
    )
    assert 222 not in page.timeouts, (
        f"goto_settle_ms (222) must NOT fire on commit-mode path; got {page.timeouts}"
    )


def test_goto_uses_domcontentloaded_when_skip_settle_false():
    """Back-compat: ``skip_settle=False`` keeps the original
    ``domcontentloaded`` + ``load`` + ``goto_settle_ms`` flow. Existing specs
    that don't open with ``wait_for`` record identically."""
    page = FakePage()
    rec = Recorder(config=SENTINEL_CONFIG)
    rec.goto_and_settle(page, "https://example.com/x", skip_settle=False)

    assert len(page.gotos) == 1
    assert page.gotos[0]["wait_until"] == "domcontentloaded", (
        f"expected wait_until=domcontentloaded; got {page.gotos[0]['wait_until']}"
    )
    assert page.load_state_calls != [], (
        "wait_for_load_state must fire on the back-compat path"
    )
    assert 222 in page.timeouts, (
        f"goto_settle_ms (222) must fire on back-compat path; got {page.timeouts}"
    )


def test_run_scene_with_waitfor_first_uses_commit():
    """End-to-end: when the spec's first action is ``wait_for``,
    ``run_scene`` propagates ``skip_settle=True`` and the goto uses
    ``wait_until="commit"`` — covering the gray-viewport case that motivated
    the fix."""
    page = FakePage()
    rec = Recorder(config=SENTINEL_CONFIG, base_url="https://example.com")
    scene = {
        "title": "leave mapbox, land on glossary",
        "url": "https://example.com/glossary",
        "actions": [{"kind": "wait_for", "target": "Glossary"}],
        "scene_index": 5,
    }
    rec.run_scene(page, scene)

    assert len(page.gotos) == 1
    assert page.gotos[0]["wait_until"] == "commit"
    assert page.load_state_calls == []
    # Neither goto_settle_ms (222) nor initial_hold_ms (111) fired
    assert 222 not in page.timeouts
    assert 111 not in page.timeouts


def test_run_scene_with_click_first_uses_domcontentloaded():
    """Back-compat: a scene whose first action is ``click`` (or anything
    non-wait_for) keeps ``domcontentloaded`` so the page has a chance to
    paint before the cursor lands. The slow-path stays slow."""
    page = FakePage()
    rec = Recorder(config=SENTINEL_CONFIG, base_url="https://example.com")
    scene = {
        "title": "click first",
        "url": "https://example.com/x",
        "actions": [{"kind": "press"}],
        "scene_index": 1,
    }
    rec.run_scene(page, scene)

    assert page.gotos[0]["wait_until"] == "domcontentloaded"
    # Back-compat timeouts fired
    assert 222 in page.timeouts
    assert 111 in page.timeouts
