"""One command for ddd-run Steps 4 + 5: assemble, decide, persist, report.

Agents kept hand-writing an ``assemble.py`` per run (load the verdicts, merge
the findings, call ``assemble_run_state`` / ``compute_convergence`` /
``compute_auto_iterate``, save) — each copy slightly different, each a place for
the loop's contract to drift. This is that script, once:

    python -m scripts.ddd.assemble <run_id> [--json]

It reads everything from the run dir the judges already wrote:

* ``verdict-concept.yaml`` / ``verdict-user.yaml`` (gating pair) and any extra
  verdict (arc, timing, video, why, actionability) via ``discover_extra_verdicts``
* findings = ``design_findings.json`` + ``arc_findings.json`` (both use the one
  findings contract ``compute_auto_iterate`` dispatches on)
* the concept verdict's ``distribution:`` block (the progress signal)
* ``judge-scope.json`` — whether this pass was FULL or incremental
* ``.canopy/ddd/config.yaml`` ``loop:`` block (backlog vs polish)

then records the judged iteration in the judge ledger (``judge-cache/``) so the
next pass can reuse unchanged scenes, and prints the report lines.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _load_findings(run_dir: Path) -> list[dict]:
    out: list[dict] = []
    for name in ("design_findings.json", "arc_findings.json"):
        p = run_dir / name
        if not p.exists():
            continue
        data = json.loads(p.read_text())
        if isinstance(data, dict):
            data = data.get("findings") or []
        out.extend(f for f in data if isinstance(f, dict))
    return out


def assemble(run_id: str, *, spec: str | None = None, ddd_dir: Path | None = None) -> dict[str, Any]:
    from scripts.ddd import judge_scope, loop_config, progress
    from scripts.ddd.run_pipeline import (
        assemble_run_state,
        compute_auto_iterate,
        compute_convergence,
        format_verdict_line,
    )
    from scripts.ddd.runstate import _resolve_ddd_dir, _run_dir_for, load, save
    from scripts.ddd.verdicts import discover_extra_verdicts, load_verdict

    ddd_dir = ddd_dir or _resolve_ddd_dir()
    run_dir = _run_dir_for(ddd_dir, run_id)
    concept_path = run_dir / "verdict-concept.yaml"
    user_path = run_dir / "verdict-user.yaml"
    for p in (concept_path, user_path):
        if not p.exists():
            raise FileNotFoundError(f"{p} missing — the judges have not written it")

    concept = load_verdict(concept_path)
    user = load_verdict(user_path)
    extra, extra_paths = discover_extra_verdicts(run_dir)
    manifest_path = run_dir / "walkthrough-run-data.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    findings = _load_findings(run_dir)
    scope = judge_scope.load_scope(run_dir) or {}
    judge_full = bool(scope.get("full", True))
    cfg = loop_config.load(ddd_dir)

    state = load(run_id, ddd_dir=ddd_dir)
    assemble_run_state(
        state,
        concept,
        user,
        findings,
        concept_path=str(concept_path),
        user_path=str(user_path),
        manifest=manifest,
        extra_verdict_paths=extra_paths,
    )
    converged = compute_convergence(concept, user, extra=extra)
    action, reason = compute_auto_iterate(
        state,
        concept,
        user,
        findings,
        converged=converged,
        distribution=progress.load_distribution(concept_path),
        judge_full=judge_full,
        loop_config=cfg.loop,
    )
    state.auto_iterate_next_action = action
    state.auto_iterate_reason = reason
    save(state, ddd_dir=ddd_dir)

    ledger = None
    if spec:
        ledger = judge_scope.record(run_dir, spec, iteration=state.iteration)

    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "concept": format_verdict_line(concept),
        "user_artifact": format_verdict_line(user),
        "advisory": {k: format_verdict_line(v) for k, v in extra.items()},
        "converged": converged,
        "judge_full": judge_full,
        "loop_mode": state.loop_mode,
        "next_judge_full": state.next_judge_full,
        "progress": state.progress_history[-1] if state.progress_history else None,
        "auto_iterate_next_action": action,
        "auto_iterate_reason": reason,
        "terminal_status": state.terminal_status,
        "open_findings": len([f for f in state.findings if f.get("route") != "DEFER"]),
        "ledger": ledger,
    }


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.ddd.assemble")
    ap.add_argument("run_id")
    ap.add_argument(
        "--spec",
        default=None,
        help="spec path — records the judge ledger so the next pass can reuse unchanged scenes",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        out = assemble(args.run_id, spec=args.spec)
    except (FileNotFoundError, ValueError) as exc:
        print(f"assemble: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(out, indent=1, default=str))
        return 0
    p = out["progress"] or {}
    print(f"DDD assemble — {out['run_id']}")
    print(f"  Concept judge:       {out['concept']}")
    print(f"  User-artifact judge: {out['user_artifact']}")
    for kind, line in out["advisory"].items():
        print(f"  {kind:<20} {line}")
    print(f"  Judge pass:   {'FULL' if out['judge_full'] else 'incremental (reused unchanged scenes)'}")
    print(f"  Loop mode:    {out['loop_mode']}  (next judge: {'full' if out['next_judge_full'] else 'incremental'})")
    print(
        f"  Progress:     score {p.get('score')}  open findings {p.get('open_findings')}  "
        f"mean cell {p.get('mean_cell')}  confirmed caps {p.get('confirmed_caps')}"
    )
    print(f"  Convergence:  {'YES' if out['converged'] else 'NO'}")
    print(f"  Auto-iterate: {out['auto_iterate_next_action']}  ({out['auto_iterate_reason']})")
    print(f"  Termination:  {out['terminal_status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
