"""Progress signal for the DDD loop — what "getting better" means on a v1 product.

The problem, measured
---------------------
The loop's gating score is ``min(concept, user)``, and the concept score is
itself a minimum over every (scene x gating dimension) cell. One bad cell pins
it. Across five freshly-built supply narratives (connect-labs,
``supply-*-2026-09-23-001``) 19 of 23 judged iterations scored 2 while the mean
cell rose 3.14 -> 3.68, confirmed caps fell 13 -> 1 and open findings fell
61 -> 21. The loop read that as "flat". Worse, the ``mechanical -> continue``
branch sat ahead of the stall check, so stall detection could never fire while
any mechanical finding existed — which on a v1 product is always.

The fix
-------
Record four signals per judged iteration and call it a stall only when NONE of
them improved across the last two iterations:

* ``score``          — the gating floor (noise-banded, :data:`denoise.NOISE_BAND`)
* ``open_findings``  — non-DEFER findings after normalization (lower is better)
* ``mean_cell``      — mean over every concept cell (higher is better; banded
                        by :data:`MEAN_BAND` — the mean of ~70 cells with a
                        per-cell sigma of ~1 moves ~0.12 on noise alone)
* ``confirmed_caps`` — concept cells whose CONFIRMED score is <= 2 (lower is
                        better)

The convergence bar is untouched (every gating judge >= 4). This only changes
how the loop decides it has stopped making progress.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# A mean over ~70 cells with per-cell sigma ~1 has a standard error ~0.12, so a
# smaller move is not evidence either way.
MEAN_BAND: float = 0.15

SIGNALS = ("score", "open_findings", "mean_cell", "confirmed_caps")
_LOWER_IS_BETTER = {"open_findings", "confirmed_caps"}


def load_distribution(verdict_path: str | Path) -> dict | None:
    """The ``distribution:`` block of a concept verdict (dropped by the Verdict model)."""
    try:
        raw = yaml.safe_load(Path(verdict_path).read_text()) or {}
    except Exception:
        return None
    dist = raw.get("distribution")
    return dist if isinstance(dist, dict) else None


def _confirmed_caps(distribution: dict | None) -> int | None:
    if not distribution:
        return None
    cells = distribution.get("capping_cells")
    if not isinstance(cells, list):
        return None
    n = 0
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        score = cell.get("confirmed", cell.get("score"))
        try:
            if float(score) <= 2.0:
                n += 1
        except (TypeError, ValueError):
            continue
    return n


def measure(
    findings: list[dict],
    distribution: dict | None,
    score: float,
    *,
    full: bool = True,
) -> dict[str, Any]:
    """One iteration's progress point."""
    open_findings = sum(1 for f in findings or [] if f.get("route", "PRODUCT") != "DEFER")
    mean = None
    if distribution and distribution.get("mean") is not None:
        try:
            mean = round(float(distribution["mean"]), 4)
        except (TypeError, ValueError):
            mean = None
    return {
        "score": float(score),
        "open_findings": open_findings,
        "mean_cell": mean,
        "confirmed_caps": _confirmed_caps(distribution),
        "full": bool(full),
    }


def _better(signal: str, best: float, value: float) -> bool:
    from scripts.ddd import denoise

    if signal == "score":
        return denoise.improved(best, value) is True
    if signal == "mean_cell":
        return value > best + MEAN_BAND
    return value < best  # lower-is-better counts


def improved_signals(before: list[dict], point: dict) -> list[str]:
    """Signals on which ``point`` beats the best of ``before``."""
    out: list[str] = []
    for signal in SIGNALS:
        value = point.get(signal)
        if value is None:
            continue
        prior = [p.get(signal) for p in before if p.get(signal) is not None]
        if not prior:
            continue
        best = min(prior) if signal in _LOWER_IS_BETTER else max(prior)
        if _better(signal, float(best), float(value)):
            out.append(signal)
    return out


def stalled(history: list[dict]) -> bool:
    """True when neither of the last two points improved ANY signal.

    Needs three points (a baseline plus two to judge). Each of the last two is
    compared against the best of everything before the pair — the same window
    the score-only stall used, so a single noisy bounce cannot mask a flat run.
    """
    if len(history) < 3:
        return False
    before = history[:-2]
    return all(not improved_signals(before, p) for p in history[-2:])


def last_step_progressed(history: list[dict]) -> bool:
    """Did the most recent point improve any signal over the one before it?"""
    if len(history) < 2:
        return False
    return bool(improved_signals([history[-2]], history[-1]))


def select_mode(configured: str, open_findings: int, *, backlog_min_findings: int) -> str:
    """Backlog vs polish. Explicit config wins; ``auto`` reads the backlog size."""
    if configured in ("backlog", "polish"):
        return configured
    return "backlog" if open_findings >= backlog_min_findings else "polish"
