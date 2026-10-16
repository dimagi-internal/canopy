"""What a finished DDD run found — a part of its RUN PACKAGE, never a notes file.

Why this replaced ``.canopy/ddd/learnings.md`` (Jonathan, 2026-10-16)
---------------------------------------------------------------------
canopy#788 made every run-ending ``assemble`` append a block to
``<repo>/.canopy/ddd/learnings.md`` so the next run's bootstrap could read it.
In practice that file never reached the next run:

* the operator works across four machines, and ACE runs resolve their DDD dir to
  per-run home directories (``~/.canopy/ddd/ace-<opp>-<run>/learnings.md``,
  ``~/.canopy/ddd/runtime/learnings.md``) that no later run reads;
* the repo copies diverged (nine different versions on one laptop) and were
  rarely committed;
* product context written into a DDD notes file goes stale on a fast-moving
  product — a lesson about the product belongs in the product's docs or as a
  product fix, not in a side file only the loop reads.

So the summary now travels WITH THE RUN:

* ``state.run_summary`` — pushed to canopy-web on every ``runstate.save`` as part
  of the run document, and a compact :func:`actionable` digest rides in the run
  record's ``summary`` (``run_store.summary``), which the run package page shows
  and the run list returns — that is what the next run's bootstrap reads
  (:mod:`scripts.ddd.prior_runs`);
* ``<run_dir>/run-summary.json`` + ``run-summary.md`` — the same content on the
  runner's disk, kept even when canopy-web is unreachable (and saying so);
* findings with an owner go TO that owner as GitHub issues
  (:mod:`scripts.ddd.owner_routing`) — what was filed is recorded here.

:func:`finalize` runs inside ``assemble`` on every pass that ENDS the run (a
``stop_*`` decision). It never raises: a summary that cannot be routed or
uploaded is still written locally, and the run's ending is never changed by it.

    python -m scripts.ddd.run_summary <run_id>     # (re)write it by hand
    python -m scripts.ddd.run_learnings <run_id>   # the old name; same thing
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SUMMARY_JSON = "run-summary.json"
SUMMARY_MD = "run-summary.md"
#: How many entries of each list the compact digest (run record) carries.
DIGEST_CAP = 8


def ends_run(action: str | None) -> bool:
    return bool(action) and str(action).startswith("stop_")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _short(text: Any, n: int = 300) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Sections — each read from run state alone, no judgment
# ---------------------------------------------------------------------------


def never_cleared(state: Any) -> list[dict]:
    """Cells with a finding open for >= 2 passes: the fixes tried did not land."""
    from scripts.ddd import fix_scope

    cells: dict[str, dict] = {}
    for f in getattr(state, "findings", None) or []:
        if not isinstance(f, dict) or int(f.get("recurring") or 0) < 2:
            continue
        key = f"{fix_scope.scene_key(f.get('scene')) or '?'}:{f.get('dimension')}"
        c = cells.setdefault(key, {"cell": key, "scene": f.get("scene"), "dimension": f.get("dimension"),
                                   "open_passes": 0, "finding_ids": []})
        c["open_passes"] = max(c["open_passes"], int(f.get("recurring") or 0))
        if f.get("finding_id") and f["finding_id"] not in c["finding_ids"]:
            c["finding_ids"].append(f["finding_id"])
    return [cells[k] for k in sorted(cells)]


def out_of_scope(state: Any) -> list[dict]:
    """Floor findings the loop may not edit, with who owns the fix.

    The union of the gating floor's ``edit_scope: out`` findings and every
    PINNED cell's ``blocked_by`` (DDD-1), keyed by stable ``finding_id`` and
    enriched from ``state.findings`` (scene, surface, owner, issue).
    """
    index = {str(f.get("finding_id")): f for f in getattr(state, "findings", None) or []
             if isinstance(f, dict) and f.get("finding_id")}
    rows: dict[str, dict] = {}

    def add(raw: dict, cell: str | None = None) -> None:
        fid = str(raw.get("finding_id") or "")
        src = {**index.get(fid, {}), **{k: v for k, v in raw.items() if v not in (None, "")}}
        key = fid or f"{src.get('scene')}:{src.get('dimension')}:{_short(src.get('fix_recommendation') or src.get('text'), 60)}"
        row = rows.get(key)
        if row is None:
            row = rows[key] = {
                "finding_id": fid or None,
                "scene": src.get("scene"),
                "dimension": src.get("dimension"),
                "fix": _short(src.get("fix_recommendation") or src.get("text") or src.get("detail"), 300),
                "reason": _short(src.get("edit_scope_reason") or src.get("reason"), 240),
                "surface": src.get("surface"),
                "owner": src.get("owner"),
                "issue": src.get("issue"),
                "cells": [],
            }
        for k in ("surface", "owner", "issue", "scene", "dimension"):
            if not row.get(k) and src.get(k):
                row[k] = src[k]
        if cell and cell not in row["cells"]:
            row["cells"].append(cell)

    floor = getattr(state, "gating_floor", None) or {}
    for r in floor.get("findings") or []:
        if isinstance(r, dict) and r.get("edit_scope") == "out":
            add(r)
    for c in (getattr(state, "cell_floor", None) or {}).get("cells") or []:
        if c.get("status") != "pinned":
            continue
        for b in c.get("blocked_by") or []:
            if isinstance(b, dict):
                add(b, c.get("key"))
    return list(rows.values())


def product_clarity(state: Any) -> list[dict]:
    """Findings a judge tagged ``product_clarity`` — the judge (or the narrative)
    could not tell what the product does from the product itself.

    The tag is set by the judges (ddd-concept-eval, ddd-product-review, the user
    judge): ``product_clarity: "<what was ambiguous>"`` (or ``true``). It is the
    one signal routed to the PRODUCT repo as a documentation / UI-copy issue —
    never inferred from prose here, because a heuristic we cannot test would
    file noise against a product team.
    """
    out = []
    for f in getattr(state, "findings", None) or []:
        if not isinstance(f, dict):
            continue
        tag = f.get("product_clarity")
        if not tag:
            continue
        out.append({
            "finding_id": f.get("finding_id"),
            "scene": f.get("scene"),
            "dimension": f.get("dimension"),
            "ambiguity": _short(tag if isinstance(tag, str) else (f.get("detail") or f.get("fix_recommendation")), 300),
            "detail": _short(f.get("detail"), 300),
            "fix": _short(f.get("fix_recommendation"), 300),
            "source": f.get("source"),
        })
    return out


def canopy_tooling(state: Any) -> list[dict]:
    """Problems with the DDD tooling itself this run hit — listed, never filed
    (an auto-issue per run against canopy would be noise; a human or
    ``canopy agent-review`` reads these across runs)."""
    out: list[dict] = []
    policy = getattr(state, "inner_loop_policy", None) or {}
    if policy.get("status") in ("dropped", "off", "missing"):
        out.append({"kind": f"inner_loop_{policy.get('status')}", "detail": _short(policy.get("reason"), 240)})
    for o in getattr(state, "decision_overrides", None) or []:
        if isinstance(o, dict):
            out.append({"kind": "decision_override", "iteration": o.get("iteration"),
                        "action": o.get("action"), "detail": _short(o.get("reason"), 240)})
    for t in getattr(state, "judge_timeouts", None) or []:
        if isinstance(t, dict):
            out.append({"kind": "judge_timeout", "iteration": t.get("iteration"), "judge": t.get("judge"),
                        "minutes": t.get("minutes"), "detail": _short(t.get("reason"), 200)})
    for w in getattr(state, "version_warnings", None) or []:
        out.append({"kind": "version_skew", "detail": _short(w, 240)})
    return out


def timings(state: Any) -> dict | None:
    rows = [t for t in getattr(state, "pass_timings", None) or [] if isinstance(t, dict)]
    if not rows:
        return None
    captures: dict[str, int] = {}
    for t in rows:
        if t.get("capture"):
            captures[str(t["capture"])] = captures.get(str(t["capture"]), 0) + 1
    return {
        "passes": len(rows),
        "wall_minutes": round(sum(float(t.get("wall_minutes") or 0) for t in rows), 1),
        "rejudged": sum(int(t.get("rejudged") or 0) for t in rows),
        "reused": sum(int(t.get("reused") or 0) for t in rows),
        "captures": captures,
    }


def calibration(state: Any) -> dict:
    """DDD-1's predicted-vs-actual rows and their hit rate."""
    from scripts.ddd import predict

    rows = [
        {k: r.get(k) for k in ("planned_iteration", "iteration", "cell", "from", "expected_to", "actual", "hit", "met")}
        for r in getattr(state, "prediction_calibration", None) or [] if isinstance(r, dict) and r.get("judged")
    ]
    return {**predict.calibration_summary(state), "rows": rows[-20:]}


# ---------------------------------------------------------------------------
# The summary
# ---------------------------------------------------------------------------


def build(state: Any) -> dict:
    """The full run summary (JSON-able), without routing results."""
    hist = [s for s in (_num(x) for x in getattr(state, "score_history", None) or []) if s is not None]
    policy = getattr(state, "inner_loop_policy", None) or {}
    return {
        "schema": 1,
        "run_id": getattr(state, "run_id", None),
        "narrative_slug": getattr(state, "narrative_slug", None),
        "written_at": _now(),
        "ending": {
            "action": getattr(state, "auto_iterate_next_action", None),
            "terminal_status": getattr(state, "terminal_status", None),
            "reason": _short(getattr(state, "auto_iterate_reason", None), 600),
            "judged_passes": len(hist),
            "objective": getattr(state, "objective", None),
        },
        "score_history": hist,
        "never_cleared": never_cleared(state),
        "out_of_scope": out_of_scope(state),
        "product_clarity": product_clarity(state),
        "timings": timings(state),
        "inner_loop": (
            {"status": policy.get("status"), "reason": _short(policy.get("reason"), 240)}
            if policy.get("status") in ("dropped", "off", "missing") else None
        ),
        "calibration": calibration(state),
        "canopy_tooling": canopy_tooling(state),
        "routing": None,
    }


def actionable(summary: dict | None) -> dict | None:
    """The compact digest a LATER run acts on — what rides in the run record's
    ``summary`` (canopy-web lists it per run; ``prior_runs`` reads it back)."""
    if not summary:
        return None
    ending = summary.get("ending") or {}
    cal = summary.get("calibration") or {}
    routing = summary.get("routing") or {}
    filed = [
        {k: r.get(k) for k in ("kind", "finding_id", "status", "url", "repo", "issue")}
        for r in routing.get("results") or [] if r.get("status") in ("commented", "opened", "intended")
    ]
    return {
        "ending": ending.get("action"),
        "terminal_status": ending.get("terminal_status"),
        "score_history": (summary.get("score_history") or [])[-10:],
        "never_cleared": [
            {"cell": c["cell"], "open_passes": c.get("open_passes")} for c in summary.get("never_cleared") or []
        ][:DIGEST_CAP],
        "out_of_scope": [
            {k: o.get(k) for k in ("finding_id", "scene", "dimension", "owner", "issue", "fix")}
            for o in summary.get("out_of_scope") or []
        ][:DIGEST_CAP],
        "calibration": {k: cal.get(k) for k in ("judged", "hits", "hit_rate", "mean_overshoot")},
        "product_clarity": len(summary.get("product_clarity") or []),
        "canopy_tooling": sorted({t.get("kind") for t in summary.get("canopy_tooling") or [] if t.get("kind")}),
        "filed": filed[:DIGEST_CAP],
    }


def markdown(summary: dict) -> str:
    """The short human-readable version (the old learnings block, plus routing)."""
    ending = summary.get("ending") or {}
    lines = [f"### Run {summary.get('run_id')} — {summary.get('narrative_slug') or ''}".rstrip(" —")]
    lines.append(
        f"- Ended `{ending.get('action')}` ({ending.get('terminal_status')}) after "
        f"{ending.get('judged_passes')} judged pass(es); objective {ending.get('objective')}."
    )
    hist = summary.get("score_history") or []
    if hist:
        lines.append(f"- Score trajectory: {', '.join(f'{s:g}' for s in hist)}.")
    if ending.get("reason"):
        lines.append(f"- Why it stopped: {ending['reason']}")
    nc = summary.get("never_cleared") or []
    if nc:
        lines.append(
            "- Never cleared — the fixes tried did not land; brief a different approach next run: "
            + ", ".join(f"{c['cell']} (open {c['open_passes']} passes)" for c in nc) + "."
        )
    for o in summary.get("out_of_scope") or []:
        who = o.get("owner") or "no owner recorded"
        issue = f" {o['issue']}" if o.get("issue") else ""
        lines.append(
            f"- Floor outside the loop's edit scope — scene {o.get('scene') or '?'} {o.get('dimension')}: "
            f"{_short(o.get('fix'), 200)} [{o.get('reason')}] — owner {who}{issue}."
        )
    for p in summary.get("product_clarity") or []:
        lines.append(f"- Product clarity (scene {p.get('scene') or '?'} {p.get('dimension')}): {p.get('ambiguity')}")
    t = summary.get("timings")
    if t:
        caps = ", ".join(f"{k}×{v}" for k, v in sorted((t.get("captures") or {}).items()))
        lines.append(
            f"- Time: {t['wall_minutes']:.0f} min over {t['passes']} pass(es); scenes re-judged "
            f"{t['rejudged']}, reused {t['reused']}" + (f"; captures {caps}" if caps else "") + "."
        )
    il = summary.get("inner_loop")
    if il:
        lines.append(f"- Inner loop {il['status']}: {il.get('reason')}")
    cal = summary.get("calibration") or {}
    if cal.get("judged"):
        lines.append(
            f"- Predicted vs actual: {cal['hits']}/{cal['judged']} predicted cell(s) improved "
            f"(mean overshoot {cal.get('mean_overshoot')})."
        )
    tooling = summary.get("canopy_tooling") or []
    if tooling:
        lines.append("- DDD tooling: " + "; ".join(f"{x['kind']}: {x.get('detail') or ''}".rstrip(": ") for x in tooling))
    routing = summary.get("routing") or {}
    for r in routing.get("results") or []:
        where = r.get("url") or r.get("issue") or r.get("repo") or "?"
        lines.append(f"- Routed `{r.get('finding_id')}` ({r.get('kind')}): {r.get('status')} → {where}"
                     + (f" — {r['why']}" if r.get("why") else ""))
    for u in routing.get("unrouted") or []:
        lines.append(f"- Not routed `{u.get('finding_id')}` ({u.get('kind')}): {u.get('why')}")
    delivery = summary.get("delivery") or {}
    if delivery.get("canopy_web"):
        lines.append(f"- canopy-web: {delivery['canopy_web']}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def finalize(
    state: Any,
    run_dir: str | Path,
    *,
    cfg: Any = None,
    client: Any = None,
    repo_root: str | Path | None = None,
) -> dict:
    """Build the summary, route owned findings (best-effort), store it on the state.

    Call BEFORE the run-ending ``runstate.save`` so the summary is pushed with the
    run document. Never raises; a routing failure is recorded in the summary.
    """
    summary = build(state)
    try:
        from scripts.ddd import owner_routing

        prior = (getattr(state, "run_summary", None) or {}).get("routing")
        summary["routing"] = owner_routing.route(
            summary, cfg=cfg, client=client, repo_root=repo_root, prior=prior,
        )
    except Exception as exc:  # noqa: BLE001 — routing is best-effort, never the run's ending
        summary["routing"] = {"mode": "error", "why": _short(exc, 300), "results": [], "unrouted": []}
    state.run_summary = summary
    return summary


def delivery(state: Any) -> dict:
    """Where the summary lives besides this disk, in words."""
    store = getattr(state, "store", None) or {}
    if store.get("version") and not store.get("pending"):
        return {"canopy_web": f"in the run record (state_version {store.get('version')})", "uploaded": True}
    if store.get("pending"):
        return {"canopy_web": f"NOT uploaded — canopy-web write pending ({store.get('error')}); "
                              "kept locally, retried on the next save", "uploaded": False}
    return {"canopy_web": "NOT uploaded — this run is local-only (no canopy-web run record); "
                          "the summary is only in this run dir", "uploaded": False}


def write(state: Any, run_dir: str | Path) -> tuple[Path, Path]:
    """Write ``run-summary.json`` + ``run-summary.md`` from ``state.run_summary``."""
    summary = dict(getattr(state, "run_summary", None) or build(state))
    summary["delivery"] = delivery(state)
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    jp = run_dir / SUMMARY_JSON
    mp = run_dir / SUMMARY_MD
    jp.write_text(json.dumps(summary, indent=1, default=str) + "\n")
    mp.write_text(markdown(summary))
    return jp, mp


def ensure(state: Any, run_dir: str | Path) -> dict:
    """A summary for a run that has none yet (ended before this module, or
    uploaded without a stop): build it WITHOUT routing — routing belongs to the
    run's end, and re-filing from an upload would double-comment."""
    if not getattr(state, "run_summary", None):
        summary = build(state)
        summary["routing"] = {"mode": "not_run", "why": "summary built at upload; findings route at the run's end",
                              "results": [], "unrouted": []}
        state.run_summary = summary
    return state.run_summary


def _main(argv: list[str] | None = None) -> int:
    from scripts.ddd import loop_config
    from scripts.ddd.runstate import _enclosing_repo, _resolve_ddd_dir, _run_dir_for, load, save

    ap = argparse.ArgumentParser(prog="python -m scripts.ddd.run_summary")
    ap.add_argument("run_id")
    ap.add_argument("--no-route", action="store_true", help="rebuild the summary without filing issues")
    args = ap.parse_args(argv)
    ddd_dir = _resolve_ddd_dir()
    state = load(args.run_id, ddd_dir=ddd_dir)
    run_dir = _run_dir_for(ddd_dir, args.run_id)
    if args.no_route:
        prior = (getattr(state, "run_summary", None) or {}).get("routing")
        summary = build(state)
        summary["routing"] = prior or {"mode": "not_run", "why": "--no-route", "results": [], "unrouted": []}
        state.run_summary = summary
    else:
        finalize(state, run_dir, cfg=loop_config.load(ddd_dir), repo_root=_enclosing_repo(Path(ddd_dir).resolve()))
    save(state, ddd_dir=ddd_dir)
    jp, mp = write(state, run_dir)
    print(jp)
    print(mp)
    print(f"run summary: {delivery(state)['canopy_web']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
