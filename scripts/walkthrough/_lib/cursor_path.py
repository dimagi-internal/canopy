"""Natural cursor paths for the walkthrough recorder (pure, no Playwright).

Playwright's ``page.mouse.move(x, y, steps=N)`` interpolates in a straight line
at constant speed: the synthetic cursor slides across the page like a plotter
pen. That is fine for the explainer cut, where the footage is time-warped and
narrated over anyway, but a ``style: recorded`` walkthrough has to read as a
person operating the page — and nobody moves a mouse in a ruler-straight line at
constant speed.

:func:`natural_path` gives the path a person's hand takes instead:

* a gentle arc (a quadratic Bézier bowed off the straight line — the wrist
  pivots, so the path curves; the bow is ~10% of the distance, capped), and
* minimum-jerk timing (``10t³ − 15t⁴ + 6t⁵``) — the hand accelerates out of
  rest and decelerates into the target, the classic profile of human reaching.

Deterministic on purpose (no randomness): the same spec records the same path,
so a re-record is comparable to the last one.
"""
from __future__ import annotations

import math

#: Bow of the arc as a fraction of the straight-line distance, and its clamp.
ARC_FRACTION = 0.10
ARC_MIN_PX = 4.0
ARC_MAX_PX = 60.0

#: Below this distance the move is a nudge — go straight there.
NUDGE_PX = 6.0

CURSOR_PATHS = ("linear", "natural")


def min_jerk(t: float) -> float:
    """Minimum-jerk position profile on [0, 1] (0 → 0, 1 → 1, zero end velocity)."""
    t = min(1.0, max(0.0, t))
    return t * t * t * (10 - 15 * t + 6 * t * t)


def natural_path(
    x0: float, y0: float, x1: float, y1: float, *, steps: int
) -> list[tuple[float, float]]:
    """Points from (x0, y0) to (x1, y1) along a hand-like path.

    Returns ``max(2, steps)`` points (just the target for a nudge under
    :data:`NUDGE_PX`), the last one exactly ``(x1, y1)``. The start point is not
    included — the cursor is already there.
    """
    dx, dy = x1 - x0, y1 - y0
    dist = math.hypot(dx, dy)
    if dist < NUDGE_PX:
        return [(float(x1), float(y1))]
    n = max(2, int(steps))
    bow = min(ARC_MAX_PX, max(ARC_MIN_PX, ARC_FRACTION * dist))
    # Bow consistently to one side relative to travel direction (a right-handed
    # wrist arcs the same way each time), so paths look like one person's.
    px, py = -dy / dist, dx / dist
    if dx < 0:
        px, py = -px, -py
    cx = (x0 + x1) / 2 + px * bow
    cy = (y0 + y1) / 2 + py * bow
    pts: list[tuple[float, float]] = []
    for i in range(1, n + 1):
        t = min_jerk(i / n)
        u = 1 - t
        bx = u * u * x0 + 2 * u * t * cx + t * t * x1
        by = u * u * y0 + 2 * u * t * cy + t * t * y1
        pts.append((round(bx, 2), round(by, 2)))
    pts[-1] = (float(x1), float(y1))
    return pts
