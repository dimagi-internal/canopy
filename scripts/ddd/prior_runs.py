"""What earlier runs of this narrative found — read from canopy-web, not a notes file.

Replaces the bootstrap's read of ``.canopy/ddd/learnings.md`` (see
:mod:`scripts.ddd.run_summary` for why that file is gone). Every finished run's
compact summary (``run_summary.actionable``) rides in its run record's
``summary`` on canopy-web, and ``GET /api/agent-runs/?kind=ddd&subject=<narrative>``
already returns it per run — the same list ``run_store.resolve`` reads. So this
is one existing-API read, from any machine, of the last few FINISHED runs of the
narrative (falling back to finished runs of the same repo when the narrative has
none), reduced to what the loop can act on:

* cells that never cleared — brief a different approach, not the same fix again;
* out-of-scope blockers and their owner/issue — park them up front, don't rediscover;
* prediction calibration — how far the loop over-predicted its own fixes;
* what was already filed — don't re-raise it.

Best-effort and read-only: no canopy-web (local store, no token, unreachable)
prints why and exits 0 — a run is self-contained without it; the cross-run
signal also lives in the owners' issues.

    python -m scripts.ddd.prior_runs <narrative> [--limit 3] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

DEFAULT_LIMIT = 3


def fetch(
    narrative: str,
    ddd_dir: Path,
    *,
    limit: int = DEFAULT_LIMIT,
    exclude: str | None = None,
    _call: Callable[..., tuple[int, Any]] | None = None,
    _mode: Callable[[Path], tuple[str, str]] | None = None,
) -> dict:
    """``{"source": "canopy-web"|"none", "why", "runs": [{run_id, status, learned}]}``."""
    from scripts.ddd import run_store

    mode, val = (_mode or run_store.mode)(ddd_dir)
    if mode != "web" and _call is None:
        return {"source": "none", "why": f"no canopy-web store ({val})", "runs": []}
    call = _call or run_store._call
    scopes = [("narrative", {"subject": narrative})]
    repo = val if mode == "web" else None
    if repo:
        scopes.append(("repo", {"repo_slug": repo}))
    for scope, q in scopes:
        try:
            status, body = call("GET", run_store.BASE, query={"kind": run_store.KIND, "active": "false",
                                                               "limit": max(limit * 4, 10), **q})
        except Exception as exc:  # noqa: BLE001 — best-effort read
            return {"source": "none", "why": f"canopy-web unreachable ({str(exc)[:200]})", "runs": []}
        if status != 200 or not isinstance(body, list):
            return {"source": "none", "why": f"canopy-web list runs -> HTTP {status}", "runs": []}
        runs = [
            {"run_id": r.get("ext_id"), "subject": r.get("subject"), "status": r.get("status"),
             "learned": (r.get("summary") or {}).get("learned")}
            for r in body
            if isinstance(r, dict) and r.get("ext_id") != exclude and (r.get("summary") or {}).get("learned")
        ][:limit]
        if runs:
            return {"source": "canopy-web", "scope": scope, "why": "", "runs": runs}
    return {"source": "canopy-web", "scope": None, "why": "no finished run with a run summary yet", "runs": []}


def brief(found: dict) -> str:
    """The lines the orchestrator hands the loop (and puts in the fixer briefs)."""
    runs = found.get("runs") or []
    if not runs:
        return f"Prior runs: none to learn from — {found.get('why') or 'no summaries'}.\n"
    scope = " (same repo — this narrative has none)" if found.get("scope") == "repo" else ""
    lines = [f"Prior runs{scope}, newest first:"]
    for r in runs:
        ln = r.get("learned") or {}
        hist = ", ".join(f"{s:g}" for s in ln.get("score_history") or [])
        lines.append(f"- {r['run_id']}: ended `{ln.get('ending')}` ({ln.get('terminal_status')})"
                     + (f", scores {hist}" if hist else ""))
        nc = ln.get("never_cleared") or []
        if nc:
            lines.append("  - never cleared (brief a DIFFERENT approach): "
                         + ", ".join(f"{c['cell']} ({c.get('open_passes')} passes)" for c in nc))
        for o in ln.get("out_of_scope") or []:
            who = o.get("owner") or "no owner"
            lines.append(f"  - out of scope, park up front: scene {o.get('scene')} {o.get('dimension')} — "
                         f"{who}{' ' + o['issue'] if o.get('issue') else ''}: {o.get('fix') or ''}".rstrip(": "))
        cal = ln.get("calibration") or {}
        if cal.get("judged"):
            lines.append(f"  - predictions: {cal.get('hits')}/{cal.get('judged')} hit, mean overshoot "
                         f"{cal.get('mean_overshoot')} — predict conservatively")
        for f in ln.get("filed") or []:
            lines.append(f"  - already filed ({f.get('kind')}, {f.get('status')}): "
                         f"{f.get('url') or f.get('issue') or f.get('repo')} — don't re-raise")
    return "\n".join(lines) + "\n"


def _main(argv: list[str] | None = None) -> int:
    from scripts.ddd.runstate import _resolve_ddd_dir

    ap = argparse.ArgumentParser(prog="python -m scripts.ddd.prior_runs")
    ap.add_argument("narrative")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ap.add_argument("--exclude", default=None, help="a run id to leave out (the run being resumed)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    found = fetch(args.narrative, _resolve_ddd_dir(), limit=args.limit, exclude=args.exclude)
    print(json.dumps(found, indent=1) if args.json else brief(found), end="" if not args.json else "\n")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
