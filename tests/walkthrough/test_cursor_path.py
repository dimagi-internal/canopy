"""Natural cursor paths for `style: recorded` walkthroughs.

A recorded walkthrough has to read as a person operating the page. Playwright's
``mouse.move(steps=N)`` is a straight constant-speed slide; ``cursor_path:
natural`` replaces it with an arc + minimum-jerk timing. These tests pin the
path geometry (pure), that ``slow_move`` uses it only when asked, and that
``record_video`` turns it on for recorded specs and nothing else.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "walkthrough"))

from scripts.walkthrough._lib import recorder  # noqa: E402
from scripts.walkthrough._lib.config import RecorderConfig  # noqa: E402
from scripts.walkthrough._lib.cursor_path import min_jerk, natural_path  # noqa: E402


class FakeMouse:
    def __init__(self):
        self.moves: list[tuple[float, float, int]] = []

    def move(self, x, y, *, steps=1):
        self.moves.append((float(x), float(y), int(steps)))


class FakePage:
    def __init__(self):
        self.mouse = FakeMouse()


def _dist_to_line(p, a, b):
    (x, y), (x0, y0), (x1, y1) = p, a, b
    return abs((x1 - x0) * (y0 - y) - (x0 - x) * (y1 - y0)) / math.hypot(x1 - x0, y1 - y0)


def test_min_jerk_profile():
    assert min_jerk(0) == 0 and min_jerk(1) == 1
    assert abs(min_jerk(0.5) - 0.5) < 1e-9
    # slow start and slow finish: the first and last tenth cover little ground
    assert min_jerk(0.1) < 0.02 and 1 - min_jerk(0.9) < 0.02


def test_natural_path_ends_exactly_on_target_with_requested_steps():
    pts = natural_path(100, 100, 700, 400, steps=30)
    assert len(pts) == 30
    assert pts[-1] == (700.0, 400.0)


def test_natural_path_arcs_off_the_straight_line_and_eases():
    a, b = (100.0, 100.0), (700.0, 400.0)
    pts = natural_path(*a, *b, steps=40)
    bow = max(_dist_to_line(p, a, b) for p in pts)
    assert bow > 10, "a natural path is not ruler-straight"
    assert bow <= 60 + 1e-6, "the arc is gentle (capped)"
    # minimum-jerk: steps are short at both ends and longest in the middle
    seg = [math.dist(p, q) for p, q in zip([a] + pts[:-1], pts)]
    assert seg[0] < seg[len(seg) // 2] and seg[-1] < seg[len(seg) // 2]


def test_natural_path_is_deterministic_and_nudges_go_straight():
    assert natural_path(0, 0, 300, 50, steps=20) == natural_path(0, 0, 300, 50, steps=20)
    assert natural_path(10, 10, 12, 13, steps=20) == [(12.0, 13.0)]


def test_slow_move_linear_is_unchanged():
    page = FakePage()
    recorder.slow_move(page, 300, 200, steps=36)
    recorder.slow_move(page, 500, 260, steps=36)
    assert page.mouse.moves == [(300.0, 200.0, 36), (500.0, 260.0, 36)]


def test_slow_move_natural_walks_the_path_from_the_last_position():
    page = FakePage()
    # _LAST_POS is keyed by id(page); a collected FakePage from an earlier test can
    # share this id and leave a stale "last position" behind.
    recorder._LAST_POS.pop(id(page), None)
    recorder.slow_move(page, 100, 100, steps=36, path="natural")  # no known start → linear
    assert page.mouse.moves == [(100.0, 100.0, 36)]
    recorder.slow_move(page, 700, 400, steps=24, path="natural")
    after = page.mouse.moves[1:]
    assert len(after) == 24 and all(s == 1 for *_, s in after)
    assert after[-1][:2] == (700.0, 400.0)
    assert [m[:2] for m in after] == natural_path(100, 100, 700, 400, steps=24)


def test_cursor_path_defaults_linear():
    assert RecorderConfig().cursor_path == "linear"
    assert RecorderConfig.for_pace("fast").cursor_path == "linear"


def test_record_video_turns_natural_on_for_recorded_specs_only():
    from record_video import recorder_config_for_spec

    assert recorder_config_for_spec({}, "fast").cursor_path == "linear"
    assert recorder_config_for_spec({"style": "explainer"}, "fast").cursor_path == "linear"
    assert recorder_config_for_spec({"style": "recorded"}, "fast").cursor_path == "natural"
    # an explicit override still wins
    spec = {"style": "recorded", "video_recorder_config": {"cursor_path": "linear"}}
    assert recorder_config_for_spec(spec, "fast").cursor_path == "linear"
    # and the rest of the pace preset is untouched
    assert recorder_config_for_spec({"style": "recorded"}, "fast").typing_delay_ms == 20
