"""Predicted gain — does the next fix batch expect to move the score, and did it?

Why this module exists
----------------------
On the 2026-10-15 ACE runs the loop kept fixing without ever asking whether the
fixes could move the number it was judged on. ``noora-health/20261015-1129``
ran six passes at a flat 2.0: the floor was a labs-template defect the loop may
not edit (ace#2337) and the in-scope fixes it did make were on cells that were
not holding anything down. ``smallholder-seasonal/20261015-1402`` ran four
passes at 2.0, every one re-filming every scene. Nobody had written down, before
a batch, what it was expected to change — so nothing could notice that the
answer was "nothing", and nothing could check the prediction afterwards.

The contract
------------
Before the fixers run, every batch item declares what it expects::

    {finding_id, scene, dimension, in_scope, from, expected_to}

``python -m scripts.ddd.predict declare <run_id> --batch <file>`` records it in
``state.predictions`` (keyed by the iteration whose assemble planned the batch)
and checks it against the last assemble's per-cell floor
(``state.cell_floor``, :func:`scripts.ddd.floor.classify_cells`):

* the code, not the item, decides ``in_scope`` (an item claiming a finding the
  loop may not edit is recorded and ignored);
* ``from`` is the cell's recorded value;
* ``expected_to`` is clamped to the cell's CEILING — the score the judge gave an
  out-of-scope defect still on that cell. A recipe fix cannot lift a cell above
  a labs-template defect that stays;
* if NO workable cell's predicted best exceeds its current value the batch is
  not worth a pass: the decision becomes ``stop_no_predicted_gain``, sealed, and
  ``blocked_report.json`` says why.

After the pass, :func:`evaluate` (called by ``compute_auto_iterate``) compares
each predicted cell with what the judges now say, via
:func:`scripts.ddd.denoise.improved`. Only cells actually re-judged this pass
count. When every targeted, re-judged cell missed, the loop stops with
``stop_prediction_failed``: the loop's model of its own fixes is wrong, and
another pass on the same model is waste. Every comparison is kept in
``state.prediction_calibration`` so the predictions can be tuned.

    python -m scripts.ddd.predict declare <run_id> --batch batch.json
    python -m scripts.ddd.predict show    <run_id>
    python -m scripts.ddd.predict report  <run_id>      # blocked report + summary
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

STOP_NO_GAIN = "stop_no_predicted_gain"
STOP_FAILED = "stop_prediction_failed"
REPORT_FILE = "blocked_report.json"
REQUIRED_KEYS = ("finding_id", "expected_to")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _cells(state: Any) -> dict[str, dict]:
    return {c["key"]: c for c in ((getattr(state, "cell_floor", None) or {}).get("cells") or []) if c.get("key")}


def _finding_index(state: Any) -> dict[str, dict]:
    return {str(f.get("finding_id")): f for f in getattr(state, "findings", None) or [] if f.get("finding_id")}


def _cell_of_finding(fid: str, cells: dict[str, dict]) -> dict | None:
    for c in cells.values():
        if fid in (c.get("findings") or []):
            return c
    return None


# ---------------------------------------------------------------------------
# Declare (before the fixers run)
# ---------------------------------------------------------------------------


def declare(state: Any, items: list[dict], *, iteration: int | None = None) -> dict:
    """Record the batch's predictions and decide whether the batch is worth a pass.

    Returns ``{iteration, items, gain_cells, decision, reason}``; ``decision`` is
    ``proceed`` or :data:`STOP_NO_GAIN`. On a stop the caller must seal and save
    (the CLI does), and the blocked report is due.
    """
    iteration = state.iteration if iteration is None else iteration
    cells = _cells(state)
    index = _finding_index(state)
    rows: list[dict] = []
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        missing = [k for k in REQUIRED_KEYS if raw.get(k) in (None, "")]
        fid = str(raw.get("finding_id") or "")
        f = index.get(fid)
        cell = _cell_of_finding(fid, cells) if fid else None
        row: dict[str, Any] = {
            "finding_id": fid,
            "scene": raw.get("scene", (f or {}).get("scene")),
            "dimension": raw.get("dimension", (f or {}).get("dimension")),
            "declared_in_scope": raw.get("in_scope"),
            "declared_from": raw.get("from"),
            "expected_to": _num(raw.get("expected_to")),
            "cell": (cell or {}).get("key") or raw.get("cell"),
            "notes": [],
        }
        if missing:
            row["notes"].append(f"missing {', '.join(missing)}")
        if f is None:
            row["in_scope"] = False
            row["notes"].append("unknown finding_id (not in this pass's findings)")
        else:
            row["in_scope"] = f.get("edit_scope") != "out"
            if raw.get("in_scope") is True and not row["in_scope"]:
                row["notes"].append(f"declared in scope, but it is not: {f.get('edit_scope_reason')}")
        if cell is not None:
            row["from"] = float(cell["score"])
            if _num(raw.get("from")) is not None and abs(_num(raw.get("from")) - row["from"]) > 1e-9:
                row["notes"].append(f"declared from {raw.get('from')}, recorded {row['from']}")
            ceiling = cell.get("ceiling")
            if row["expected_to"] is not None and ceiling is not None and row["expected_to"] > ceiling:
                row["notes"].append(
                    f"expected_to {row['expected_to']} clamped to {ceiling}: an out-of-scope defect "
                    "the judge scored that low stays on this cell"
                )
                row["expected_to"] = float(ceiling)
            if cell.get("status") == "pinned":
                row["notes"].append("cell is pinned (every finding out of scope)")
        else:
            row["from"] = _num(raw.get("from"))
            row["notes"].append("no scored cell for this finding — cannot count toward a gain")
        rows.append(row)
    gain: dict[str, dict] = {}
    for r in rows:
        cell = cells.get(r.get("cell") or "")
        if not r["in_scope"] or cell is None or cell.get("status") != "workable" or r["expected_to"] is None:
            continue
        best = gain.get(cell["key"], {}).get("predicted_best", float(cell["score"]))
        if r["expected_to"] > best:
            gain[cell["key"]] = {"from": float(cell["score"]), "predicted_best": r["expected_to"]}
    gain_cells = {k: v for k, v in gain.items() if v["predicted_best"] > v["from"]}
    if gain_cells:
        decision, reason = "proceed", (
            f"{len(gain_cells)} workable cell(s) predicted to improve: "
            + "; ".join(f"{k} {v['from']} -> {v['predicted_best']}" for k, v in sorted(gain_cells.items()))
        )
    else:
        decision = STOP_NO_GAIN
        workable = [c for c in cells.values() if c.get("status") == "workable"]
        reason = (
            f"No workable cell is predicted to improve: {len(rows)} batch item(s), "
            f"{sum(1 for r in rows if r['in_scope'])} in scope, none predicting a workable cell above "
            f"its current value ({len(workable)} workable cell(s) below the bar). The remaining "
            "work cannot move the score; see blocked_report.json for what is blocked and by whom."
        )
    entry = {
        "iteration": iteration,
        "declared_at": _now(),
        "items": rows,
        "gain_cells": gain_cells,
        "decision": decision,
        "reason": reason,
        "outcome": None,
        "evaluated_at_iteration": None,
    }
    state.predictions = {**(getattr(state, "predictions", None) or {}), str(iteration): entry}
    state.predictions_required = False
    if decision == STOP_NO_GAIN:
        state.auto_iterate_next_action = STOP_NO_GAIN
        state.auto_iterate_reason = reason
        state.terminal_status = "stopped_no_predicted_gain"
    return entry


def has_predictions(state: Any, iteration: int | None) -> bool:
    return str(iteration) in (getattr(state, "predictions", None) or {})


def targeted_cells(state: Any) -> set[str]:
    """Cell keys the latest undecided batch predicted to move."""
    preds = getattr(state, "predictions", None) or {}
    pending = [p for p in preds.values() if isinstance(p, dict) and p.get("outcome") is None]
    if not pending:
        return set()
    latest = max(pending, key=lambda p: int(p.get("iteration") or 0))
    return set((latest.get("gain_cells") or {}).keys())


# ---------------------------------------------------------------------------
# Evaluate (after the pass)
# ---------------------------------------------------------------------------


def judged_predicate(scope: dict | None, judges: list[str] | None) -> Callable[[dict], bool]:
    """``cell -> was it re-judged this pass`` from ``judge-scope.json``.

    Full pass: every cell of every judge that ran. Incremental: concept cells on
    the ``rejudge`` scenes, user cells on ``user_scenes`` (else ``rejudge``), arc
    cells when ``arc`` ran. A judge that did not run (tiering, or a timeout) is
    carried — its cells say nothing about the batch.
    """
    scope = scope or {}
    ran = set(judges or scope.get("judges") or ["concept", "user", "arc"])
    full = bool(scope.get("full", True))
    rejudge = {int(s) for s in scope.get("rejudge") or [] if str(s).isdigit() or isinstance(s, int)}
    user_scenes = scope.get("user_scenes")
    user_set = {int(s) for s in user_scenes} if isinstance(user_scenes, list) else rejudge

    def judged(cell: dict) -> bool:
        j = cell.get("judge")
        if j not in ran:
            return False
        if full:
            return True
        if j == "arc":
            return bool(scope.get("arc", False))
        scenes = user_set if j == "user" else rejudge
        if cell.get("scene") is None:
            return bool(scenes)
        return int(cell["scene"]) in scenes

    return judged


def evaluate(state: Any, cell_floor: dict, *, judged: Callable[[dict], bool]) -> dict | None:
    """Compare the last declared batch with this pass's cells; record calibration.

    Returns ``{planned_iteration, outcome: hit|miss|unjudged, rows}`` or ``None``
    when no batch is waiting. ``miss`` = at least one predicted cell was re-judged
    and NONE of the re-judged ones improved beyond the noise band
    (:func:`scripts.ddd.denoise.improved`). Idempotent per iteration: a recipe
    re-judge re-assessing the same iteration replaces its rows.
    """
    from scripts.ddd import denoise

    preds = getattr(state, "predictions", None) or {}
    pending = [
        p for p in preds.values()
        if isinstance(p, dict)
        and p.get("decision") == "proceed"
        and int(p.get("iteration") or 0) < int(state.iteration)
        and (p.get("outcome") is None or p.get("evaluated_at_iteration") == state.iteration)
    ]
    if not pending:
        return None
    entry = max(pending, key=lambda p: int(p.get("iteration") or 0))
    planned = int(entry.get("iteration") or 0)
    now = {c["key"]: c for c in (cell_floor or {}).get("cells") or [] if c.get("key")}
    rows: list[dict] = []
    for key, g in sorted((entry.get("gain_cells") or {}).items()):
        cell = now.get(key)
        was = float(g["from"])
        actual = float(cell["score"]) if cell else None
        is_judged = bool(cell) and judged(cell)
        moved = denoise.improved(was, actual) if is_judged else None
        rows.append({
            "planned_iteration": planned,
            "iteration": state.iteration,
            "cell": key,
            "finding_ids": [i["finding_id"] for i in entry.get("items") or [] if i.get("cell") == key],
            "from": was,
            "expected_to": float(g["predicted_best"]),
            "actual": actual,
            "judged": is_judged,
            "hit": moved is True if is_judged else None,
            "met": (actual is not None and actual >= float(g["predicted_best"])) if is_judged else None,
        })
    judged_rows = [r for r in rows if r["judged"]]
    if not judged_rows:
        outcome = "unjudged"
    elif any(r["hit"] for r in judged_rows):
        outcome = "hit"
    else:
        outcome = "miss"
    calib = [
        r for r in getattr(state, "prediction_calibration", None) or []
        if not (r.get("planned_iteration") == planned and r.get("iteration") == state.iteration)
    ]
    state.prediction_calibration = calib + rows
    if outcome != "unjudged":
        entry["outcome"] = outcome
        entry["evaluated_at_iteration"] = state.iteration
    else:
        entry["outcome"] = None
    state.predictions = {**preds, str(planned): entry}
    return {"planned_iteration": planned, "outcome": outcome, "rows": rows}


def calibration_summary(state: Any) -> dict:
    """Hit rate and mean over-prediction over every judged comparison so far."""
    rows = [r for r in getattr(state, "prediction_calibration", None) or [] if r.get("judged")]
    if not rows:
        return {"judged": 0, "hits": 0, "hit_rate": None, "mean_overshoot": None}
    hits = sum(1 for r in rows if r.get("hit"))
    over = [float(r["expected_to"]) - float(r["actual"]) for r in rows if r.get("actual") is not None]
    return {
        "judged": len(rows),
        "hits": hits,
        "hit_rate": round(hits / len(rows), 3),
        "mean_overshoot": round(sum(over) / len(over), 3) if over else None,
    }


# ---------------------------------------------------------------------------
# Blocked report
# ---------------------------------------------------------------------------


def blocked_report(state: Any) -> dict:
    """What is blocked, by whom, what the run improved, predicted vs actual."""
    cf = getattr(state, "cell_floor", None) or {}
    cells = cf.get("cells") or []
    since = getattr(state, "pinned_since", None) or {}
    pinned = []
    by_owner: dict[str, dict] = {}
    for c in cells:
        if c.get("status") != "pinned":
            continue
        owners = c.get("owners") or [{}]
        pinned.append({
            "cell": c["key"],
            "judge": c.get("judge"),
            "scene": c.get("scene"),
            "dimension": c.get("dimension"),
            "score": c.get("score"),
            "pinned_since": since.get(c["key"]),
            "owners": owners,
            "blocked_by": c.get("blocked_by") or [],
        })
        for o in owners:
            who = (
                o.get("owner")
                or o.get("surface")
                or (f"owner of {o['issue']}" if o.get("issue") else None)
                or "unassigned — out of scope, no owner recorded (configure loop.fixed_surfaces)"
            )
            slot = by_owner.setdefault(who, {"owner": who, "issues": [], "cells": []})
            if o.get("issue") and o["issue"] not in slot["issues"]:
                slot["issues"].append(o["issue"])
            if c["key"] not in slot["cells"]:
                slot["cells"].append(c["key"])
    base = getattr(state, "cell_baseline", None) or {}
    improved = []
    for c in cells:
        was = base.get(c["key"])
        if was is not None and float(c["score"]) > float(was):
            improved.append({"cell": c["key"], "from": float(was), "to": float(c["score"])})
    preds = getattr(state, "predictions", None) or {}
    last = max(preds.values(), key=lambda p: int(p.get("iteration") or 0)) if preds else None
    workable = [
        {"cell": c["key"], "score": c.get("score"), "in_scope": c.get("in_scope"), "ceiling": c.get("ceiling")}
        for c in cells if c.get("status") == "workable"
    ]
    return {
        "run_id": getattr(state, "run_id", None),
        "iteration": getattr(state, "iteration", None),
        "action": getattr(state, "auto_iterate_next_action", None),
        "terminal_status": getattr(state, "terminal_status", None),
        "reason": getattr(state, "auto_iterate_reason", None),
        "true_score": cf.get("true_score"),
        "workable_score": cf.get("workable_score"),
        "pinned": pinned,
        "by_owner": sorted(by_owner.values(), key=lambda o: o["owner"]),
        "workable": workable,
        "improved": improved,
        "last_prediction": (
            {k: last.get(k) for k in ("iteration", "decision", "reason", "gain_cells", "outcome")} if last else None
        ),
        "calibration": calibration_summary(state),
        "calibration_rows": list(getattr(state, "prediction_calibration", None) or [])[-20:],
    }


def summary_lines(report: dict) -> list[str]:
    """The short human-readable version the loop prints and returns."""
    lines = [
        f"DDD blocked report — {report.get('run_id')} (iteration {report.get('iteration')}, "
        f"{report.get('action')})",
        f"  Score: true {report.get('true_score')}  workable {report.get('workable_score')}",
    ]
    owners = report.get("by_owner") or []
    if owners:
        lines.append(f"  Blocked ({len(report.get('pinned') or [])} pinned cell(s)):")
        for o in owners:
            issues = f" [{', '.join(o['issues'])}]" if o.get("issues") else ""
            lines.append(f"    - {o['owner']}{issues}: {', '.join(o['cells'][:8])}"
                         + (f" (+{len(o['cells']) - 8} more)" if len(o["cells"]) > 8 else ""))
        for p in (report.get("pinned") or [])[:5]:
            first = (p.get("blocked_by") or [{}])[0]
            if first.get("text"):
                lines.append(f"      {p['cell']} = {p.get('score')}: {first['text'][:140]}")
    else:
        lines.append("  Blocked: nothing pinned")
    imp = report.get("improved") or []
    lines.append(
        "  Improved: " + (", ".join(f"{i['cell']} {i['from']}->{i['to']}" for i in imp[:8]) if imp else "no cell rose")
    )
    cal = report.get("calibration") or {}
    if cal.get("judged"):
        lines.append(
            f"  Predicted vs actual: {cal['hits']}/{cal['judged']} predicted cell(s) improved "
            f"(mean overshoot {cal.get('mean_overshoot')})"
        )
    lp = report.get("last_prediction") or {}
    if lp:
        lines.append(f"  Last batch prediction: {lp.get('decision')} — {str(lp.get('reason') or '')[:200]}")
    return lines


def write_report(state: Any, run_dir: str | Path) -> tuple[Path, list[str]]:
    rep = blocked_report(state)
    lines = summary_lines(rep)
    rep["summary"] = lines
    path = Path(run_dir) / REPORT_FILE
    path.write_text(json.dumps(rep, indent=1, default=str) + "\n")
    return path, lines


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    from scripts.ddd import decision
    from scripts.ddd.runstate import _resolve_ddd_dir, _run_dir_for, load, save

    ap = argparse.ArgumentParser(prog="python -m scripts.ddd.predict")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("declare", help="record the next batch's predictions (before the fixers run)")
    d.add_argument("run_id")
    d.add_argument("--batch", required=True, help="JSON list of batch items (a file path, or - for stdin)")
    s = sub.add_parser("show", help="print predictions + calibration")
    s.add_argument("run_id")
    r = sub.add_parser("report", help="write blocked_report.json and print its summary")
    r.add_argument("run_id")
    args = ap.parse_args(argv)
    try:
        state = load(args.run_id)
        run_dir = _run_dir_for(_resolve_ddd_dir(), args.run_id)
        if args.cmd == "declare":
            raw = sys.stdin.read() if args.batch == "-" else Path(args.batch).read_text()
            items = json.loads(raw)
            if isinstance(items, dict):
                items = items.get("items") or []
            seal = getattr(state, "decision_seal", None) or {}
            entry = declare(state, items, iteration=seal.get("iteration", state.iteration))
            if entry["decision"] == STOP_NO_GAIN:
                # The loop's own decision, not an override: seal it so the next
                # pass is refused like any other stop.
                decision.seal(state)
                save(state)
                path, lines = write_report(state, run_dir)
                print(json.dumps({**entry, "report": str(path)}, indent=1, default=str))
                print("\n".join(lines), file=sys.stderr)
                return 3
            save(state)
            print(json.dumps(entry, indent=1, default=str))
            return 0
        if args.cmd == "show":
            print(json.dumps(
                {
                    "predictions": state.predictions,
                    "calibration": calibration_summary(state),
                    "rows": state.prediction_calibration,
                },
                indent=1,
                default=str,
            ))
            return 0
        path, lines = write_report(state, run_dir)
        print("\n".join(lines))
        print(f"  ({path})")
        return 0
    except (ValueError, OSError) as exc:
        print(f"predict {args.cmd}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
