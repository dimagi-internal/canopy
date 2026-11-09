"""Unit tests for ``_render_stars`` float tolerance.

The judge schema (``ai_evaluation.score``) is a float — 4.0, 4.5, 5.0.
A plain ``"★" * score`` throws ``TypeError`` on
float multiplication, so the rounding (``int(round(score))``) lives in the
renderer rather than at each call site, and every caller benefits.

These tests pin:
  - ``_render_stars(4.5)`` doesn't raise, renders 4 or 5 stars (rounded)
  - integer scores keep rendering identically (no regression)
  - ``max_score=5.0`` (defensive) is also tolerated
  - the visible numeric tail still shows the underlying float so callers
    can read "4.5/5" in the deck without precision loss
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.walkthrough.generate_presentation import _render_stars  # noqa: E402


# Unicode constants — using the same escape sequence the source file does so
# the test doesn't lie about which glyphs the renderer emits.
STAR_FILLED = "★"  # ★
STAR_EMPTY = "☆"  # ☆


def test_render_stars_accepts_float_score():
    """4.5 must not raise; rounds to 5 visible stars."""
    out = _render_stars(4.5)
    # 4.5 rounds to 4 in Python's banker's rounding — half-to-even — so we
    # accept either 4 or 5 filled. The point is that it doesn't raise.
    n_filled = out.count(STAR_FILLED)
    n_empty = out.count(STAR_EMPTY)
    assert n_filled in (4, 5)
    assert n_filled + n_empty == 5
    # Numeric tail preserves the float so half-star precision isn't lost
    # downstream — a viewer reading the deck sees "4.5/5", not "5/5".
    assert "4.5/5" in out


def test_render_stars_accepts_integer_score():
    """No regression: ``_render_stars(4)`` still renders 4 stars."""
    out = _render_stars(4)
    assert out.count(STAR_FILLED) == 4
    assert out.count(STAR_EMPTY) == 1
    assert "4/5" in out


def test_render_stars_perfect_score():
    """``_render_stars(5)`` renders all-filled and no empty stars."""
    out = _render_stars(5)
    assert out.count(STAR_FILLED) == 5
    assert out.count(STAR_EMPTY) == 0
    assert "5/5" in out


def test_render_stars_perfect_float_score():
    """``_render_stars(5.0)`` renders all-filled, no TypeError."""
    out = _render_stars(5.0)
    assert out.count(STAR_FILLED) == 5
    assert out.count(STAR_EMPTY) == 0


def test_render_stars_zero():
    """``_render_stars(0)`` renders zero filled, all empty."""
    out = _render_stars(0)
    assert out.count(STAR_FILLED) == 0
    assert out.count(STAR_EMPTY) == 5


def test_render_stars_float_max_score():
    """A float ``max_score`` (defensive — schema says int) is also tolerated."""
    out = _render_stars(3.0, max_score=5.0)
    assert out.count(STAR_FILLED) == 3
    assert out.count(STAR_EMPTY) == 2


def test_render_stars_below_three_floor_rounds_down():
    """``_render_stars(2.4)`` rounds to 2 visible stars."""
    out = _render_stars(2.4)
    assert out.count(STAR_FILLED) == 2
    assert out.count(STAR_EMPTY) == 3
    # Numeric tail preserves the float.
    assert "2.4/5" in out


def test_render_stars_empty_never_negative():
    """An out-of-range score doesn't render a negative number of empty stars.

    Guards against a future contributor mistake (``max_score=3, score=4`` →
    ``-1`` empty stars, where ``"" * -1 = ""`` would be fine but
    confusing). ``max(0, …)`` makes it explicit.
    """
    out = _render_stars(7, max_score=5)
    assert out.count(STAR_FILLED) == 7
    assert out.count(STAR_EMPTY) == 0
