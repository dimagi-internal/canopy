"""Per-pass timing — where each DDD pass spent its wall clock (canopy#780).

"Why is this slow" was answered on 2026-10-06 by reconstructing four runs from
step timestamps by hand: about 13 minutes per partial pass (fixers ~5, render
2–4, concept judge ~6), plus the user-artifact and arc judges on full passes.
``RunState.steps`` holds only the LAST entry per step name, so the render of
iteration 3 overwrote iteration 2's, and judges were never timed at all.

:func:`record` (called by every ``assemble``) appends one row per pass to
``state.pass_timings``:

    {iteration, assembled_at, judge_full, judges, wall_minutes, fix_minutes,
     render_minutes, judge_minutes: {concept, user, arc}, other_minutes,
     judge_timing_source, steps: {name: minutes},
     rejudged, reused, held, capture, captured, carried}

``rejudged`` / ``reused`` / ``held`` count the scenes the judge scope re-judged
and carried; ``capture`` is the pass's capture mode (``full`` | ``scenes`` |
``none``, :mod:`scripts.ddd.capture_scope`) with ``captured`` / ``carried``
scene counts — so what scoping saved is read off the table, not reconstructed
(canopy#785).

Steps come from the watchdog (``fixer:<batch>``, ``render``, and
``judge:<name>`` when the orchestrator wraps each judge dispatch). A judge with
no ``judge:<name>`` step is timed from the files it writes instead: from the
judge scope's ``planned_at`` (judging starts after the plan) to its verdict
file's mtime — marked ``judge_timing_source: files``.

    python -m scripts.ddd.pass_timing <run_dir>    # the table, one row per pass
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

JUDGE_FILES = {"concept": "verdict-concept.yaml", "user": "verdict-user.yaml", "arc": "verdict-arc.yaml"}


def _ts(raw: Any) -> float | None:
    if raw in (None, ""):
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _minutes(seconds: float | None) -> float | None:
    return None if seconds is None else round(max(seconds, 0.0) / 60.0, 2)


def _span(entries: list[tuple[float, float]]) -> float | None:
    """Wall-clock span of possibly-parallel steps (first start -> last end)."""
    if not entries:
        return None
    return max(e for _, e in entries) - min(s for s, _ in entries)


def summarize(
    steps: dict[str, dict],
    *,
    since: float | None,
    now: float,
    judges: list[str] | None = None,
    run_dir: str | Path | None = None,
    planned_at: float | None = None,
) -> dict[str, Any]:
    """Time this pass's steps (those started at or after ``since``)."""
    window: dict[str, tuple[float, float]] = {}
    for name, entry in (steps or {}).items():
        if not isinstance(entry, dict):
            continue
        start = _ts(entry.get("started_at"))
        if start is None or (since is not None and start < since):
            continue
        end = _ts(entry.get("finished_at")) or now
        window[name] = (start, end)

    fixers = [v for k, v in window.items() if k.split(":", 1)[0] == "fixer"]
    render = window.get("render")
    judge_min: dict[str, float | None] = {}
    source = "watchdog"
    for k, (s, e) in window.items():
        if k.startswith("judge:"):
            judge_min[k.split(":", 1)[1]] = _minutes(e - s)
    if run_dir is not None and planned_at is not None:
        for judge in judges or []:
            if judge in judge_min:
                continue
            p = Path(run_dir) / JUDGE_FILES.get(judge, "")
            if p.is_file() and p.stat().st_mtime >= planned_at:
                judge_min[judge] = _minutes(p.stat().st_mtime - planned_at)
                source = "files" if not any(k.startswith("judge:") for k in window) else "mixed"
    return {
        "fix_minutes": _minutes(_span(fixers)),
        "render_minutes": _minutes(render[1] - render[0]) if render else None,
        "judge_minutes": judge_min,
        "judge_timing_source": source if judge_min else None,
        "steps": {k: _minutes(e - s) for k, (s, e) in sorted(window.items())},
    }


def record(
    state: Any,
    *,
    scope: dict | None = None,
    run_dir: str | Path | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Append this pass's timing row to ``state.pass_timings`` and return it."""
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    history = [r for r in (getattr(state, "pass_timings", None) or []) if isinstance(r, dict)]
    since = _ts(history[-1].get("assembled_at")) if history else None
    scope = scope or {}
    judges = list(scope.get("judges") or ["concept", "user", "arc"])
    row = summarize(
        getattr(state, "steps", None) or {},
        since=since,
        now=now,
        judges=judges,
        run_dir=run_dir,
        planned_at=_ts(scope.get("planned_at")),
    )
    wall = _minutes(now - since) if since is not None else None
    accounted = sum(
        v for v in [row["fix_minutes"], row["render_minutes"], *(row["judge_minutes"] or {}).values()]
        if isinstance(v, (int, float))
    )
    row = {
        "iteration": getattr(state, "iteration", None),
        "assembled_at": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
        "judge_full": bool(scope.get("full", True)),
        "judges": judges,
        "wall_minutes": wall,
        **row,
        "other_minutes": round(max(wall - accounted, 0.0), 2) if wall is not None else None,
        "rejudged": len(scope.get("rejudge") or []) if scope else None,
        "reused": len(scope.get("reuse") or []) if scope else None,
        "held": len(scope.get("held") or []) if scope else None,
    }
    cap = scope.get("capture") or {}
    if cap:
        row["capture"] = cap.get("mode")
        row["captured"] = len(cap.get("scenes") or []) if cap.get("mode") != "none" else 0
        row["carried"] = len(cap.get("carried") or [])
    state.pass_timings = history + [row]
    return row


def _n(v: Any) -> str:
    return str(v) if isinstance(v, int) else "-"


def format_table(rows: list[dict]) -> str:
    head = (
        f"{'iter':>4} {'judge':<8} {'wall':>6} {'fix':>6} {'render':>6} {'concept':>7} {'user':>6} "
        f"{'arc':>6} {'other':>6} {'rejudge':>7} {'reuse':>5} {'capture':<14}"
    )
    lines = [head]

    def f(v: Any) -> str:
        return f"{v:6.1f}" if isinstance(v, (int, float)) else f"{'-':>6}"

    for r in rows:
        j = r.get("judge_minutes") or {}
        lines.append(
            f"{str(r.get('iteration')):>4} {('full' if r.get('judge_full') else 'scoped'):<8} "
            f"{f(r.get('wall_minutes'))} {f(r.get('fix_minutes'))} {f(r.get('render_minutes'))} "
            f"{f(j.get('concept')):>7} {f(j.get('user'))} {f(j.get('arc'))} {f(r.get('other_minutes'))} "
            f"{_n(r.get('rejudged')):>7} {_n(r.get('reused')):>5} "
            + (f"{r.get('capture')} {r.get('captured')}/{(r.get('captured') or 0) + (r.get('carried') or 0)}"
               if r.get("capture") else "-")
        )
    return "\n".join(lines)


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.ddd.pass_timing")
    ap.add_argument("run_dir")
    args = ap.parse_args(argv)
    import yaml

    p = Path(args.run_dir) / "run_state.yaml"
    if not p.exists():
        print(f"pass_timing: no run_state.yaml in {args.run_dir}", file=sys.stderr)
        return 2
    state = yaml.safe_load(p.read_text()) or {}
    print(format_table(state.get("pass_timings") or []))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
