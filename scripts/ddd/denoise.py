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


def plan_confirmations(
    caps: list[dict],
    *,
    cell_floor: dict | None,
    rejudged: dict[str, list[int] | None] | None = None,
    threshold: float = CAP_THRESHOLD,
    judge: str = "concept",
    targeted: set[str] | None = None,
) -> dict:
    """Which first-draw capping cells are worth the k=3 re-judge THIS pass.

    A confirmation is worth its ``CONFIRM_K - 1`` extra dispatches only when its
    result could change what the loop does — the gate/convergence reading or a
    stop decision. On noora-health/20261015-1129 ~32 of 96 judge agents were
    confirmations and none changed a verdict: the score was held at 2 by the user
    judge and by cells the loop may not edit, so whatever the median, the loop's
    next move was the same. Skipped:

    * a cap on a PINNED cell (``cell_floor``, :func:`scripts.ddd.floor.classify_cells`)
      — every finding on it is out of scope; it is reported, never fixed, and it
      is excluded from the workable score and the caps signal either way;
    * every cap, when another judge's WORKABLE cell at or below ``threshold``
      holds the workable floor and is NOT re-judged this pass (``rejudged``: judge
      -> scenes re-judged, ``None`` = all, a missing judge = carried) — that cell
      keeps its value, so no concept median can move the workable score, the gate,
      or the stall/no-gain decisions that read them.

    A cap on a cell the last batch PREDICTED to move (``targeted`` cell keys,
    :mod:`scripts.ddd.predict`) is always confirmed: ``stop_prediction_failed``
    reads it, so a single noisy draw must not decide that stop.

    Otherwise confirm (the default, and always when there is no ``cell_floor``).
    ``{confirm: [cap], skip: [{**cap, why}], reason}``. A judge that skips all of
    them writes ``distribution.confirmation: skipped`` so the caps count is read
    as not comparable (:func:`scripts.ddd.progress._confirmed_caps`).
    """
    caps = [c for c in caps or [] if isinstance(c, dict)]
    if not cell_floor or not caps:
        return {"confirm": caps, "skip": [], "reason": "no cell floor recorded — confirm every cap"}
    cells = [c for c in cell_floor.get("cells") or [] if isinstance(c, dict)]
    pinned = {c.get("key") for c in cells if c.get("status") == "pinned"}

    def key(c: dict) -> str:
        scene = str(c.get("scene")).strip()
        return f"{judge}:{scene}:{c.get('dimension')}"

    def residual(c: dict) -> str:
        return f"{judge}:*:{c.get('dimension')}"

    rejudged = rejudged if rejudged is not None else {}

    def carried(cell: dict) -> bool:
        j = cell.get("judge")
        if j not in rejudged:
            return True
        scenes = rejudged[j]
        if scenes is None:
            return False
        return cell.get("scene") is not None and int(cell["scene"]) not in {int(s) for s in scenes}

    holders = [
        c for c in cells
        if c.get("judge") != judge
        and c.get("status") != "pinned"
        and float(c.get("score") or 0) <= threshold
        and carried(c)
    ]
    confirm: list[dict] = []
    skip: list[dict] = []
    targeted = set(targeted or ())
    for c in caps:
        if key(c) in targeted or residual(c) in targeted:
            confirm.append(c)
        elif key(c) in pinned or (key(c) not in {x.get("key") for x in cells} and residual(c) in pinned):
            skip.append({**c, "why": "pinned: every finding on this cell is out of the loop's edit scope"})
        elif holders:
            h = holders[0]
            skip.append({
                **c,
                "why": (
                    f"the workable floor is held at {h.get('score')} by {h.get('key')}, which is "
                    "carried this pass — no median here can move the score or a stop decision"
                ),
            })
        else:
            confirm.append(c)
    reason = (
        f"confirm {len(confirm)} of {len(caps)} cap(s)"
        + (f"; skip {len(skip)} whose result cannot change the loop's decision" if skip else "")
    )
    return {"confirm": confirm, "skip": skip, "reason": reason}


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
    "plan_confirmations",
    "summarize",
    "to_cells",
    "trend",
    "unconfirmed_caps",
]


def _main(argv: list[str] | None = None) -> int:
    """``python -m scripts.ddd.denoise plan-confirm <run_id> --caps '<json list>'``.

    Prints :func:`plan_confirmations` for this pass's first-draw capping cells,
    reading the last assemble's ``cell_floor`` and this pass's ``judge-scope.json``.
    """
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(prog="python -m scripts.ddd.denoise")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan-confirm", help="which capping cells deserve the k=3 re-judge")
    p.add_argument("run", help="run_id, or the run dir (holding run_state.yaml)")
    p.add_argument("--caps", required=True, help="JSON list of {scene, dimension, score}, or @file")
    args = ap.parse_args(argv)
    from pathlib import Path

    import yaml

    from scripts.ddd.runstate import _resolve_ddd_dir, _run_dir_for, load
    from scripts.ddd.schemas.models import RunState

    raw = args.caps
    if raw.startswith("@"):
        raw = Path(raw[1:]).read_text()
    try:
        caps = json.loads(raw)
        if (Path(args.run) / "run_state.yaml").exists():
            run_dir = Path(args.run)
            state = RunState.model_validate(yaml.safe_load((run_dir / "run_state.yaml").read_text()) or {})
        else:
            state = load(args.run)
            run_dir = _run_dir_for(_resolve_ddd_dir(), args.run)
    except (ValueError, OSError) as exc:
        print(f"denoise plan-confirm: {exc}", file=sys.stderr)
        return 2
    scope_path = run_dir / "judge-scope.json"
    rejudged: dict[str, list[int] | None] = {}
    try:
        scope = json.loads(scope_path.read_text()) if scope_path.exists() else {}
    except (OSError, ValueError):
        scope = {}
    full = bool(scope.get("full", True))
    for j in scope.get("judges") or ["concept", "user", "arc"]:
        if full:
            rejudged[j] = None
        elif j == "user":
            rejudged[j] = list(scope.get("user_scenes") or scope.get("rejudge") or [])
        elif j == "arc":
            rejudged[j] = None if scope.get("arc", True) else []
        else:
            rejudged[j] = list(scope.get("rejudge") or [])
    from scripts.ddd import predict

    targeted = predict.targeted_cells(state)
    print(json.dumps(
        plan_confirmations(caps, cell_floor=state.cell_floor, rejudged=rejudged, targeted=targeted),
        indent=1,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
