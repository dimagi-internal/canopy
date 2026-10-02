"""One command for ddd-run Steps 4 + 5: assemble, decide, persist, report.

Agents kept hand-writing an ``assemble.py`` per run (load the verdicts, merge
the findings, call ``assemble_run_state`` / ``compute_convergence`` /
``compute_auto_iterate``, save) — each copy slightly different, each a place for
the loop's contract to drift. This is that script, once:

    python -m scripts.ddd.assemble <run_id> [--json]

It reads everything from the run dir the judges already wrote:

* ``verdict-concept.yaml`` / ``verdict-user.yaml`` (gating pair) and any extra
  verdict (arc, timing, video, why, actionability) via ``discover_extra_verdicts``
* findings = ``design_findings.json`` + ``arc_findings.json`` + the user-artifact
  verdict's ``findings:`` (all normalised to the one findings contract
  ``compute_auto_iterate`` dispatches on)
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


def _user_findings(run_dir: Path) -> list[dict]:
    """The user-artifact judge's ``findings:`` in the one findings contract.

    ``verdict-user.yaml`` carries its own findings list (scene, dimension, score,
    fix_kind, fix_recommendation) — the same defects a person using the feature
    would hit. Before 0.2.531 assemble never read it, so they were invisible to
    routing, ``fix_kind`` dispatch and the open-findings stall signal, and the
    orchestrator folded them in by hand. Each is stamped ``source:
    user_artifact`` and given the contract's ``route`` (PRODUCT unless the judge
    set one) and ``detail`` (its recommendation when it wrote no detail), so
    ``finding_class`` and the fixers read it like any other finding.
    """
    p = run_dir / "verdict-user.yaml"
    if not p.exists():
        return []
    try:
        import yaml

        data = yaml.safe_load(p.read_text())
    except Exception:
        return []
    raw = data.get("findings") if isinstance(data, dict) else None
    out: list[dict] = []
    for f in raw or []:
        if not isinstance(f, dict):
            continue
        g = dict(f)
        g.setdefault("source", "user_artifact")
        g.setdefault("route", "PRODUCT")
        if not g.get("detail"):
            g["detail"] = g.get("fix_recommendation") or ""
        out.append(g)
    return out


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
    out.extend(_user_findings(run_dir))
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
    # The pass's target, if `target plan` stamped it for THIS iteration; a stale
    # or missing stamp means the render used the spec's own base_url (deploy).
    tgt = state.current_target or {}
    if tgt.get("iteration") != state.iteration:
        tgt = {}
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
        target=tgt.get("target"),
        judges=scope.get("judges"),
        held=scope.get("held"),
    )
    state.auto_iterate_next_action = action
    state.auto_iterate_reason = reason
    # Seal the decision: a later rewrite of the fields it rests on, or a new pass
    # past a stop, is refused until `decision override --reason` logs why.
    from scripts.ddd import decision

    decision.seal(state)
    # M18: the run is pinned to one canopy version; say so loudly (and keep it
    # for the digest) when this assemble ran from another runtime.
    from scripts.ddd import pin

    pin.ensure(state)  # backfills runs started before pinning existed
    for msg in pin.skew(state):
        pin.warn(state, msg)
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
        "target": tgt.get("target") or "deploy",
        "judges": scope.get("judges") or ["concept", "user", "arc"],
        "loop_mode": state.loop_mode,
        "next_judge_full": state.next_judge_full,
        "progress": state.progress_history[-1] if state.progress_history else None,
        "auto_iterate_next_action": action,
        "auto_iterate_reason": reason,
        "terminal_status": state.terminal_status,
        "open_findings": len([f for f in state.findings if f.get("route") != "DEFER"]),
        "parked_scenes": sorted({int(x) for p in state.parked if p.get("status") == "pending" for x in p.get("scenes") or []}),
        "recipe_rejudge": state.recipe_rejudge,
        "batch_plan": state.batch_plan,
        "scope_override": scope.get("override"),
        "held_scenes": scope.get("held") or [],
        "decision_overrides": list(state.decision_overrides or []),
        "version_warnings": list(state.version_warnings or []),
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
    if out["scope_override"]:
        print(f"  Scope OVERRIDE: forced full against the loop's incremental call — {out['scope_override'].get('reason')}")
    if out["held_scenes"]:
        print(f"  Held scenes:  {out['held_scenes']} (recipe-only batch; judged fresh at the next checkpoint)")
    for o in out["decision_overrides"]:
        print(f"  Decision OVERRIDE (iteration {o.get('iteration')}, overruled {o.get('action')!r}): {o.get('reason')}")
    print(f"  Target:       {out['target']}  (judges: {', '.join(out['judges'])})")
    print(f"  Loop mode:    {out['loop_mode']}  (next judge: {'full' if out['next_judge_full'] else 'incremental'})")
    print(
        f"  Progress:     score {p.get('score')}  open findings {p.get('open_findings')}  "
        f"mean cell {p.get('mean_cell')}  confirmed caps {p.get('confirmed_caps')}"
    )
    print(f"  Convergence:  {'YES' if out['converged'] else 'NO'}")
    print(f"  Auto-iterate: {out['auto_iterate_next_action']}  ({out['auto_iterate_reason']})")
    print(f"  Termination:  {out['terminal_status']}")
    bp = out.get("batch_plan") or {}
    if bp.get("scope") == "recipe":
        print("  Next batch:   RECIPE-ONLY — no product PR, no CI/deploy wait, no set-fix-sha")
    if out["parked_scenes"]:
        print(f"  Parked:       scene(s) {out['parked_scenes']} wait on a pending decision")
    for msg in out["version_warnings"]:
        print(f"  WARNING:      {msg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
