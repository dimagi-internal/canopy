"""A ``scroll_to`` that cannot move the page must clamp AND say so.

Centring a target wants ``y + scrollY - innerHeight/2``. For anything in the top
half of the page that is NEGATIVE, the browser clamps it to 0, the page does not
move, and the scene captures whatever screen the camera was last left on. It
fails in the worst possible way: the scroll "succeeds", the cursor glide
re-measures and lands correctly, and the frame looks like a perfectly legitimate
screenshot of the wrong thing. Only an LLM arc judge ever caught it, twice, on
two different runs (canopy#587).

Two things are pinned here. The clamp — the evaluated JS must never ask the
browser for an out-of-range scrollTop, so the arithmetic can report that the
request was impossible instead of having the fact swallowed. And the report —
a motionless scroll produces a ``warning`` that reaches the run report's action
trace, WITHOUT flipping ``ok``, because a defensive ``scroll_to`` onto an
already-framed element is a supported idiom and failing those runs would make
the arc judge skip them entirely.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.walkthrough._lib.config import RecorderConfig  # noqa: E402
from scripts.walkthrough._lib.recorder import (  # noqa: E402
    ScrollResult,
    _classify_scroll,
    scroll_to,
    scroll_to_reporting,
)
from scripts.walkthrough._lib.results import ActionResult, action_trace_by_scene  # noqa: E402

BOX = {"x": 100.0, "y": 40.0, "width": 200.0, "height": 50.0}


class FakeMouse:
    def move(self, x, y, *, steps=1):
        pass


class FakeLocator:
    def bounding_box(self):
        return BOX

    def scroll_into_view_if_needed(self, *, timeout=None):
        pass


class FakePage:
    """Page stub whose ``evaluate`` answers the recorder's two real queries:
    the scroll JS (returns the before/requested/applied/max record) and the
    settled ``window.scrollY`` read."""

    def __init__(self, *, before, scroll_height, inner_height, after=None, box_y=None):
        self.mouse = FakeMouse()
        self.before = float(before)
        # Viewport-relative y of the target, as the recorder re-measures it.
        self.box_y = float(BOX["y"] if box_y is None else box_y)
        self.max = max(0.0, float(scroll_height) - float(inner_height))
        self.inner_height = float(inner_height)
        self.after = float(before if after is None else after)
        self.scripts: list[str] = []

    def evaluate(self, expr, arg=None):
        self.scripts.append(expr)
        if "window.scrollY" in expr and "scrollTo" not in expr:
            return self.after
        requested = self.box_y + self.before - self.inner_height / 2
        applied = min(max(0.0, requested), self.max)
        return {
            "before": self.before,
            "requested": requested,
            "applied": applied,
            "max": self.max,
        }

    def wait_for_timeout(self, ms):
        pass


def _patch_resolve(monkeypatch):
    from scripts.walkthrough._lib import recorder as recorder_mod
    from scripts.walkthrough._lib.targets import ResolvedTarget

    monkeypatch.setattr(
        recorder_mod,
        "resolve_target",
        lambda *a, **k: ResolvedTarget(locator=FakeLocator(), box={"x": 200.0, "y": 65.0}, kind="text"),
    )


# --------------------------------------------------------------------------- #
# the clamp
# --------------------------------------------------------------------------- #


def test_the_scroll_js_never_asks_for_a_negative_scrolltop(monkeypatch):
    """The bug, at the source. The old JS handed the browser a raw
    ``y + scrollY - innerHeight/2``; for a near-top target that is negative."""
    _patch_resolve(monkeypatch)
    page = FakePage(before=0, scroll_height=4000, inner_height=800)
    scroll_to_reporting(page, "Near the top", config=RecorderConfig())

    scroll_js = next(s for s in page.scripts if "scrollTo" in s)
    assert "Math.max" in scroll_js, "the top of the range must be clamped"
    assert "Math.min" in scroll_js, "the far end of the range must be clamped too"
    # And the value the JS would actually apply is inside the range.
    record = page.evaluate(scroll_js)
    assert record["requested"] < 0, "fixture must reproduce the negative request"
    assert record["applied"] == 0.0


def test_the_clamp_holds_at_the_bottom_of_the_range(monkeypatch):
    _patch_resolve(monkeypatch)
    # Target sitting low in the viewport of an already-fully-scrolled page.
    page = FakePage(before=3200, scroll_height=4000, inner_height=800, box_y=760)
    scroll_to_reporting(page, "Near the bottom", config=RecorderConfig())
    record = page.evaluate(next(s for s in page.scripts if "scrollTo" in s))
    assert record["requested"] > record["max"]
    assert record["applied"] == record["max"]


# --------------------------------------------------------------------------- #
# the report
# --------------------------------------------------------------------------- #


def test_a_scroll_that_could_not_move_the_page_warns(monkeypatch):
    _patch_resolve(monkeypatch)
    page = FakePage(before=0, scroll_height=4000, inner_height=800)  # after == before
    result = scroll_to_reporting(page, "Near the top", config=RecorderConfig())

    assert result.ok is True, "a motionless scroll is not a failed action"
    assert result.warning is not None
    assert "did not move the page" in result.warning
    assert "Near the top" in result.warning


def test_a_scroll_that_moved_says_nothing(monkeypatch):
    _patch_resolve(monkeypatch)
    page = FakePage(before=1000, scroll_height=4000, inner_height=800, after=1600)
    result = scroll_to_reporting(page, "Mid page", config=RecorderConfig())
    assert result == ScrollResult(ok=True, warning=None)


def test_the_warning_names_which_of_the_three_causes_it_was():
    """'already at top', 'already at bottom', 'page shorter than the viewport'
    are different spec problems and need different fixes."""
    top = _classify_scroll("T", {"before": 0, "requested": -360, "applied": 0, "max": 3200}, 0)
    assert "above the top of the page" in top.warning

    bottom = _classify_scroll("T", {"before": 3200, "requested": 3900, "applied": 3200, "max": 3200}, 3200)
    assert "past the bottom" in bottom.warning

    short = _classify_scroll("T", {"before": 0, "requested": -360, "applied": 0, "max": 0}, 0)
    assert "no taller than the viewport" in short.warning

    centred = _classify_scroll("T", {"before": 900, "requested": 900, "applied": 900, "max": 3200}, 900)
    assert "already framed" in centred.warning


def test_an_unobservable_page_makes_no_claim():
    """A stubbed page (or one that navigated away mid-settle) returns nothing
    measurable. Silence beats a warning invented from missing numbers."""
    assert _classify_scroll("T", None, None).warning is None
    assert _classify_scroll("T", {"before": 0}, 0).warning is None
    assert _classify_scroll("T", {"before": 0, "requested": -1, "applied": 0, "max": 10}, None).warning is None


def test_bool_scroll_to_is_unchanged(monkeypatch):
    """The old name keeps the old contract — a bool — so every existing caller
    and the pinned back-compat tests are untouched."""
    _patch_resolve(monkeypatch)
    page = FakePage(before=0, scroll_height=4000, inner_height=800)
    assert scroll_to(page, "Near the top", config=RecorderConfig()) is True


def test_the_warning_reaches_the_action_trace():
    """The whole point: the author and the judges have to be able to SEE it.
    `ok` stays true, so without this field the trace shows a healthy scroll."""
    report = {
        "actions": [
            {
                "kind": "scroll_to",
                "target": "Mean PPI score",
                "ok": True,
                "warning": "scroll_to('Mean PPI score') did not move the page: ...",
                "scene_index": 4,
            }
        ]
    }
    trace = action_trace_by_scene(report)
    assert trace[4][0]["warning"].startswith("scroll_to('Mean PPI score') did not move")


def test_action_result_defaults_to_no_warning():
    assert ActionResult(kind="click", ok=True).warning is None
