"""De-noising the weakest-link verdict.

The problem, measured
---------------------
``ddd-concept-eval``'s ``overall_rule: lowest`` makes the run's headline score the
**minimum over every (scene x gating dimension) cell** — for a 12-scene spec with
5 gating dimensions that is 60 independently-drawn cells.  Each cell is an LLM
draw, and the observed per-cell variance is +/-1: in ACE run
``spark-facilitator/20260813-2126`` scene 12 scored 3 / 2 / 3 and scene 10 scored
4 / 3 **on byte-identical frames**.

The minimum of 60 noisy draws is a biased-low statistic that barely responds to
real improvement.  Iteration 4 of that run fixed its entire target scene
(2/3/2/3/3 -> 4/3/3/4/4) and the headline stayed 2, because the minimum simply
migrated to untouched scenes that had drawn 3 the round before.  Four iterations
of real fixes, identical headline; the loop was chasing a moving target.

The protocol implemented here
-----------------------------
Three pieces, cheapest first.

1. **Report the distribution, not just the floor** (:func:`summarize`).  The
   signal was always there — per-dimension means moved exactly as the fixes
   predicted while the min did not — but nothing read it.  A run at 8/12 scenes
   >= 3 with rising means is a different object from one at 3/12 and falling, and
   today they print the same ``overall_score: 2``.

2. **Confirm a cap before it blocks** (:func:`confirm`, :func:`confirmed_floor`).
   Only a cell at or below :data:`CAP_THRESHOLD` can set the floor.  Re-judge
   *those cells only*, k=3, and take the median.  Cost is 2 x (number of capping
   cells) extra dispatches — 8 in the measured run — not the 120 a full k=3 over
   all 60 cells would cost.  Median-of-3 turns the probability that a single low
   draw sets the floor from ``p`` into ``p^3 + 3p^2(1-p)``: at the observed
   p ~ 0.3 that is 0.216 vs 0.300, at p = 0.2 it is 0.104 vs 0.200.

   Why median-of-3 on capping cells rather than judging only what changed: the
   measured failure was caps appearing on scenes that did **not** change, so
   "judge only the diff" would have missed every one of them.  And why not simply
   average all 60 into the headline: the weakest-link rule is deliberate — one
   genuinely broken scene should fail a demo.  Confirming the cap keeps that
   property while removing the noise that manufactured most of the caps.

3. **A noise band on the loop's progress reading** (:data:`NOISE_BAND`,
   :func:`trend`).  With per-cell sigma ~ 1, a half-point move in either
   direction is not evidence.  The loop must not call a run "improving" or
   "regressing" inside that band — reading noise as regression is exactly what
   let a run be declared stalled after a round of real fixes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

# A cell at or below this score is a "capping cell" — one that can set the
# weakest-link floor. Matches ddd-concept-eval's verdict table (<= 2 == fail).
CAP_THRESHOLD: float = 2.0

# Measured per-cell judge variance is +/-1 on byte-identical frames, so a move
# smaller than this is not evidence of anything in either direction.
NOISE_BAND: float = 0.5

# Draws per capping cell when confirming a cap. Odd so the median is a real draw.
CONFIRM_K: int = 3


@dataclass(frozen=True)
class Cell:
    """One (scene, dimension) score — the atom the weakest-link rule runs over."""

    scene: str
    dimension: str
    score: float

    @property
    def key(self) -> str:
        return f"{self.scene}::{self.dimension}"


@dataclass
class DistributionSummary:
    """What the loop should read instead of a single biased-low number."""

    n_cells: int = 0
    floor: float | None = None
    mean: float | None = None
    per_dimension_mean: dict[str, float] = field(default_factory=dict)
    cells_at_or_above_3: int = 0
    capping_cells: list[Cell] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "n_cells": self.n_cells,
            "floor": self.floor,
            "mean": self.mean,
            "per_dimension_mean": dict(self.per_dimension_mean),
            "cells_at_or_above_3": self.cells_at_or_above_3,
            "capping_cells": [
                {"scene": c.scene, "dimension": c.dimension, "score": c.score}
                for c in self.capping_cells
            ],
        }


def to_cells(scene_scores: dict[str, dict[str, float]]) -> list[Cell]:
    """Flatten ``{scene: {dimension: score}}`` into a cell list."""
    out: list[Cell] = []
    for scene, dims in (scene_scores or {}).items():
        for dim, score in (dims or {}).items():
            if score is None:
                continue
            out.append(Cell(scene=str(scene), dimension=str(dim), score=float(score)))
    return out


def capping_cells(cells: list[Cell], *, threshold: float = CAP_THRESHOLD) -> list[Cell]:
    """The cells that can set the floor — the only ones worth re-judging."""
    return [c for c in cells if c.score <= threshold]


def summarize(cells: list[Cell], *, threshold: float = CAP_THRESHOLD) -> DistributionSummary:
    """Distribution over all cells: floor, mean, per-dimension means, cap list."""
    if not cells:
        return DistributionSummary()
    scores = [c.score for c in cells]
    per_dim: dict[str, list[float]] = {}
    for c in cells:
        per_dim.setdefault(c.dimension, []).append(c.score)
    return DistributionSummary(
        n_cells=len(cells),
        floor=min(scores),
        mean=sum(scores) / len(scores),
        per_dimension_mean={d: sum(v) / len(v) for d, v in sorted(per_dim.items())},
        cells_at_or_above_3=sum(1 for s in scores if s >= 3),
        capping_cells=capping_cells(cells, threshold=threshold),
    )


def confirm(draws: list[float]) -> float:
    """The confirmed score for one cell: the median of its draws.

    One draw is the score itself (back-compat — no confirmation was run).
    """
    if not draws:
        raise ValueError("confirm() needs at least one draw")
    return float(median(sorted(draws)))


def confirmed_floor(
    cells: list[Cell],
    confirmations: dict[str, list[float]] | None = None,
    *,
    threshold: float = CAP_THRESHOLD,
) -> float | None:
    """The weakest-link floor computed over CONFIRMED capping cells.

    ``confirmations`` maps a cell key (``"<scene>::<dimension>"``) to every draw
    taken for it, INCLUDING the original.  A capping cell with confirmations uses
    ``median(draws)``; one without keeps its single draw (so behaviour is
    unchanged when nothing was confirmed).  Non-capping cells are never re-judged
    and are used as-is.

    Returns ``None`` for an empty cell list — a floor must be demonstrated, not
    defaulted.
    """
    if not cells:
        return None
    confirmations = confirmations or {}
    resolved: list[float] = []
    for c in cells:
        draws = confirmations.get(c.key)
        if draws and c.score <= threshold:
            resolved.append(confirm(list(draws)))
        else:
            resolved.append(c.score)
    return min(resolved)


def unconfirmed_caps(
    cells: list[Cell],
    confirmations: dict[str, list[float]] | None = None,
    *,
    threshold: float = CAP_THRESHOLD,
) -> list[Cell]:
    """Capping cells whose low score did NOT reproduce under re-judge.

    These are the noise the protocol removes; report them so the count of caps
    that dissolved on re-judge is visible rather than silently swallowed.
    """
    confirmations = confirmations or {}
    out: list[Cell] = []
    for c in capping_cells(cells, threshold=threshold):
        draws = confirmations.get(c.key)
        if draws and confirm(list(draws)) > threshold:
            out.append(c)
    return out


def improved(previous: float | None, current: float | None, *, band: float = NOISE_BAND) -> bool | None:
    """``True`` improved, ``False`` regressed, ``None`` inside the noise band.

    ``None`` is the important return: with +/-1 per-cell variance, a move smaller
    than ``band`` is not evidence, and treating it as either is how a round of
    real fixes got read as a regression.
    """
    if previous is None or current is None:
        return None
    delta = current - previous
    if abs(delta) < band:
        return None
    return delta > 0


def trend(history: list[float] | None, *, band: float = NOISE_BAND, window: int = 3) -> str:
    """``"improving" | "flat" | "regressing" | "unknown"`` over the last ``window`` points.

    ``flat`` means every step inside the last window sat within the noise band —
    the run is measurably not moving, which is the honest reading of "we applied
    fixes and the number did not respond".  ``unknown`` means too few points.
    """
    hist = list(history or [])
    if len(hist) < 2:
        return "unknown"
    tail = hist[-window:]
    steps = [improved(a, b, band=band) for a, b in zip(tail, tail[1:])]
    if any(s is True for s in steps):
        return "improving"
    if any(s is False for s in steps):
        return "regressing"
    return "flat"


def fingerprint_findings(findings: list[dict] | None) -> list[str]:
    """A stable, order-independent fingerprint set for one iteration's findings.

    Two iterations that produce the same fingerprint set found the same problems
    — the loop is re-deriving, not progressing.  Unlike the score this signal is
    NOT noisy, which is why it is the better plateau detector: the judge's score
    for a cell wobbles, but the defect it names does not.
    """
    out = set()
    for f in findings or []:
        scene = str(f.get("scene") or "")
        dim = str(f.get("dimension") or "")
        detail = " ".join(str(f.get("detail") or "").lower().split())
        out.add(f"{scene}::{dim}::{detail[:160]}")
    return sorted(out)


__all__ = [
    "CAP_THRESHOLD",
    "CONFIRM_K",
    "Cell",
    "DistributionSummary",
    "NOISE_BAND",
    "capping_cells",
    "confirm",
    "confirmed_floor",
    "fingerprint_findings",
    "improved",
    "summarize",
    "to_cells",
    "trend",
    "unconfirmed_caps",
]
