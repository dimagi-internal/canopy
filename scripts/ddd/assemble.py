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
* the gating FLOOR (:mod:`scripts.ddd.floor`) and what the loop may edit —
  ``loop.fixed_surfaces``, the spec's narrative lock, ``out_of_scope.json``

stamps this pass's timing on ``state.pass_timings`` (:mod:`scripts.ddd.pass_timing`),
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


#: Findings files written by the product objective's two lenses: the LLM product
#: review (skills/ddd-product-review) and the deterministic product lint
#: (scripts.ddd.product_lint). Each finding is stamped with its ``source``.
PRODUCT_FINDING_FILES = {
    "product_findings.json": "product_lens",
    "lint_findings.json": "product_lint",
}


def _load_findings(run_dir: Path, *, demo: bool = False) -> list[dict]:
    """Every judge's and lens's findings, in the one findings contract.

    ``demo``: the run's objective is ``demo`` — the product lens/lint findings are
    still loaded (and reported) but as ``route: DEFER``, so they never drive the
    demo loop. The exception is prose density
    (:data:`scripts.ddd.objective.ALWAYS_BLOCKING_LINT`, canopy#786): explanatory
    copy accreting on a screen blocks in every objective.
    """
    from scripts.ddd.objective import blocks_every_objective, feeds_every_objective

    out: list[dict] = []
    for name, source in PRODUCT_FINDING_FILES.items():
        p = run_dir / name
        if not p.exists():
            continue
        data = json.loads(p.read_text())
        if isinstance(data, dict):
            data = data.get("findings") or []
        for f in data:
            if not isinstance(f, dict):
                continue
            g = dict(f)
            g.setdefault("source", source)
            g.setdefault("route", "PRODUCT")
            if demo and not (blocks_every_objective(g) or feeds_every_objective(g)):
                g["route"] = "DEFER"
            out.append(g)
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


def _narrative_guard(state: Any, spec: str | None, run_dir: Path, cfg: Any) -> dict | None:
    """The safety net under canopy#789: a narrative edit nobody ran through the guard.

    Every narrative revision is meant to go through ``narrative_guard check``
    when it is made. If the spec's story changed since the run's last recorded
    version anyway, review it here so it cannot ride into a render unreviewed.
    The first assemble of a run records the starting story as ``v0``. Never
    fails the assemble: a guard error is reported and skipped.
    """
    if not spec or not Path(spec).exists():
        return None
    try:
        from scripts.ddd import narrative_guard as ng

        mode = ng.resolve_mode(cfg.loop.narrative_mode, state.objective or cfg.loop.objective)
        if ng.latest_version(run_dir) is not None and not ng.unchecked_change(spec, run_dir):
            return state.narrative_guard
        current = ng.view_hash(ng.view(ng._read_raw(spec)))
        if (state.narrative_guard or {}).get("hash") == current:
            return state.narrative_guard  # this exact revision was already reviewed
        rec = ng.check(spec, run_dir, reason="narrative changed without a guard check (caught at assemble)", mode=mode)
    except Exception as exc:  # pragma: no cover - defensive
        return {"decision": "error", "detail": str(exc)}
    out = {
        "iteration": state.iteration,
        "mode": mode,
        **{k: rec.get(k) for k in ("hash", "decision", "version", "material", "material_why", "violations", "standing")},
    }
    state.narrative_guard = out
    return out


def _judge_timeouts(state: Any, run_dir: Path) -> list[dict]:
    """Judges whose watchdog step for THIS pass timed out (or is past its budget).

    A judge that hung (one user judge ran 98 min on noora iteration 2) must not
    hold the pass: the orchestrator stops waiting at its budget and keeps the
    previous verdict file, and this pass treats that judge's cells as unchanged
    from the last pass — it did not run, so the pass cannot decide (any stop or
    convergence becomes a checkpoint) and no prediction is scored on its cells.
    """
    from scripts.ddd import watchdog

    since = None
    if state.pass_timings:
        since = watchdog._parse_iso((state.pass_timings[-1] or {}).get("assembled_at"))
    out: list[dict] = []
    for step, entry in (state.steps or {}).items():
        if not str(step).startswith("judge:") or not isinstance(entry, dict):
            continue
        started = watchdog._parse_iso(entry.get("started_at"))
        if since is not None and (started is None or started < since):
            continue
        res = watchdog.check(state, step, run_dir=run_dir)
        if res.get("status") == "timed_out":
            out.append({
                "iteration": state.iteration,
                "judge": str(step).split(":", 1)[1],
                "minutes": res.get("elapsed_minutes") or entry.get("timeout_minutes"),
                "reason": res.get("reason") or entry.get("reason") or "timed out",
            })
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
    scope = judge_scope.load_scope(run_dir) or {}
    judge_full = bool(scope.get("full", True))
    cfg = loop_config.load(ddd_dir)

    state = load(run_id, ddd_dir=ddd_dir)
    demo = cfg.loop.objective == "demo" or getattr(state, "objective", None) == "demo"
    findings = _load_findings(run_dir, demo=demo)
    if spec and Path(spec).exists():
        # A judge that names a scene by its spec id / title ("partner-benchmarks")
        # gets the index the loop speaks, so the next batch is not "unscoped"
        # and re-filmed in full (fix_scope.attribute_scenes, DDD-2).
        from scripts.ddd import fix_scope

        try:
            findings = fix_scope.attribute_scenes(findings, judge_scope.spec_scenes(spec))
        except Exception as exc:  # noqa: BLE001 — attribution is an optimisation, never a failure
            print(f"assemble: scene attribution skipped ({exc})", file=sys.stderr)
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
    from scripts.ddd import floor as floor_mod
    from scripts.ddd import target as target_mod

    state.inner_loop_policy = target_mod.inner_loop_policy(cfg, prior=state.inner_loop_policy)
    objective_guess = state.objective or (
        cfg.loop.objective if cfg.loop.objective in ("product", "demo") else "demo"
    )
    floor = floor_mod.locate(
        {**extra, "concept": concept, "user_artifact": user},
        run_dir=run_dir,
        objective=objective_guess,
    )
    narrative_locked = False
    if spec:
        from scripts.ddd.narrative import is_narrative_locked

        narrative_locked = is_narrative_locked(spec)
    edit_scope = {
        "narrative_locked": narrative_locked,
        "fixed_surfaces": cfg.loop.fixed_surfaces,
        "surfaces": cfg.loop.surfaces,
        "declined": floor_mod.load_declined(run_dir),
    }
    from scripts.ddd import target_rubric as target_rubric_mod

    timeouts = _judge_timeouts(state, run_dir)
    pass_judges = scope.get("judges")
    if timeouts:
        gone = {t["judge"] for t in timeouts}
        pass_judges = [j for j in (pass_judges or ["concept", "user", "arc"]) if j not in gone]
        state.judge_timeouts = [
            t for t in state.judge_timeouts or [] if t.get("iteration") != state.iteration
        ] + timeouts
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
        judges=pass_judges,
        held=[*(scope.get("held") or []), *(scope.get("carried_unverified") or [])] or None,
        inner_loop_policy=state.inner_loop_policy,
        product_config=cfg.product,
        extra_verdicts=extra,
        floor=floor,
        edit_scope=edit_scope,
        target_rubric={
            "run": state.target_rubric,
            "spec": target_rubric_mod.spec_rubric(spec),
            "config": target_rubric_mod.config_rubric(ddd_dir),
        },
        run_dir=run_dir,
        pass_scope=scope,
    )
    if timeouts:
        reason = (
            "JUDGE TIMEOUT: "
            + "; ".join(f"{t['judge']} ({t['reason']})" for t in timeouts)
            + " — its cells were carried unchanged from the last pass, so this pass could not "
            "decide. "
            + reason
        )
    from scripts.ddd import pass_timing

    timing = pass_timing.record(state, scope=scope, run_dir=run_dir)
    from scripts.ddd import objective as objective_mod

    if state.target and state.target.get("iteration") == state.iteration:
        converged = bool(state.target.get("converged"))
    elif state.objective == objective_mod.PRODUCT:
        converged = objective_mod.converged(
            {**extra, "concept": concept, "user_artifact": user}, state.findings, cfg.product
        )[0]
    guard = _narrative_guard(state, spec, run_dir, cfg)
    if guard and guard.get("decision") == "reject":
        reason = (
            "NARRATIVE EDIT REJECTED (scripts.ddd.narrative_guard, canopy#789) — revert it "
            "before the next batch: "
            + "; ".join(f"[{v.get('rule')}] {v.get('detail')}" for v in guard.get("violations") or [])
            + ". "
            + reason
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
    from scripts.ddd import run_summary

    ends = run_summary.ends_run(action)
    if ends:
        # The run's summary is part of the RUN (pushed with this save to its
        # canopy-web run document) and its owned findings go to their owners —
        # not to a learnings file nobody else reads. Best-effort: never raises.
        from scripts.ddd.runstate import _enclosing_repo

        run_summary.finalize(state, run_dir, cfg=cfg, repo_root=_enclosing_repo(Path(ddd_dir).resolve()))
    save(state, ddd_dir=ddd_dir)
    # What is blocked, by whom, what improved, predicted vs actual — written on
    # every stop and whenever a cell is pinned, so a run never ends (or keeps
    # going around a blocked cell) without saying who has to act.
    blocked = None
    from scripts.ddd import predict as predict_mod

    pinned_now = bool((state.cell_floor or {}).get("pinned"))
    if pinned_now or str(action).startswith("stop_"):
        try:
            path, lines = predict_mod.write_report(state, run_dir)
            blocked = {"path": str(path), "summary": lines}
        except OSError as exc:  # pragma: no cover - defensive
            print(f"assemble: could not write the blocked report ({exc})", file=sys.stderr)
    summary_out = None
    if ends:
        # After the save, so the file can say whether canopy-web has it. A run
        # whose summary could not be uploaded keeps it here and says so.
        try:
            jp, mp = run_summary.write(state, run_dir)
            delivered = run_summary.delivery(state)
            summary_out = {"path": str(jp), "markdown": str(mp), **delivered,
                           "routing": (state.run_summary or {}).get("routing")}
            if not delivered.get("uploaded"):
                print(f"assemble: run summary {delivered['canopy_web']} ({jp})", file=sys.stderr)
        except OSError as exc:
            print(f"assemble: could not write the run summary ({exc})", file=sys.stderr)

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
        "judges": pass_judges or ["concept", "user", "arc"],
        "loop_mode": state.loop_mode,
        "objective": state.objective,
        "objective_roles": objective_mod.summary(state.findings),
        "polish_pass": state.polish_pass,
        "inner_loop_policy": state.inner_loop_policy,
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
        "carried_unverified": scope.get("carried_unverified") or [],
        "capture": scope.get("capture"),
        "decision_overrides": list(state.decision_overrides or []),
        "version_warnings": list(state.version_warnings or []),
        "gating_floor": state.gating_floor,
        "cell_floor": {
            k: (state.cell_floor or {}).get(k) for k in ("true_score", "workable_score", "pinned", "workable")
        } if state.cell_floor else None,
        "predictions_required": state.predictions_required,
        "judge_timeouts": [t for t in state.judge_timeouts or [] if t.get("iteration") == state.iteration],
        "blocked_report": blocked,
        "narrative_guard": state.narrative_guard,
        "target_rubric": state.target,
        "pass_timing": timing,
        "changed_components": scope.get("changed_components"),
        "ledger": ledger,
        "run_summary": summary_out,
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
    cap = out.get("capture") or {}
    if cap:
        print(
            f"  Capture:      {cap.get('mode')}"
            + (f" — re-filmed {cap.get('scenes')}" if cap.get("mode") == "scenes" else "")
            + (f", carried {cap.get('carried')}" if cap.get("carried") else "")
            + (" (unverified after a product batch: next checkpoint re-films)" if out.get("carried_unverified") else "")
        )
    for o in out["decision_overrides"]:
        print(f"  Decision OVERRIDE (iteration {o.get('iteration')}, overruled {o.get('action')!r}): {o.get('reason')}")
    print(f"  Target:       {out['target']}  (judges: {', '.join(out['judges'])})")
    roles = out.get("objective_roles") or {}
    print(
        f"  Objective:    {out.get('objective')}  (blocking {roles.get('blocking', 0)}, "
        f"ride-along {roles.get('ride_along', 0)}, deferred {roles.get('deferred', 0)}"
        + (f", polish {roles['polish']}" if roles.get("polish") else "")
        + ")"
    )
    print(f"  Loop mode:    {out['loop_mode']}  (next judge: {'full' if out['next_judge_full'] else 'incremental'})")
    policy = out.get("inner_loop_policy") or {}
    if policy.get("status") == "off":
        print(f"  Inner loop:   OFF by config — {policy.get('reason')}")
    elif policy.get("status") == "missing":
        print(f"  Inner loop:   MISSING — {policy.get('reason')}")
    elif policy.get("status") == "dropped":
        print(f"  Inner loop:   DROPPED MID-RUN — {policy.get('reason')}")
    elif policy.get("status") == "configured":
        print(f"  Inner loop:   {policy.get('reason')}")
    print(
        f"  Progress:     score {p.get('score')}  open findings {p.get('open_findings')}  "
        f"mean cell {p.get('mean_cell')}  confirmed caps {p.get('confirmed_caps')}"
    )
    gf = out.get("gating_floor") or {}
    if gf:
        n_out = sum(1 for r in gf.get("findings") or [] if r.get("edit_scope") == "out")
        print(
            f"  Floor:        {'/'.join(gf.get('judges') or [])} "
            f"{', '.join(gf.get('dimensions') or []) or 'overall'} = {gf.get('score')}"
            + (f" on scene(s) {gf.get('scenes')}" if gf.get("scenes") else "")
            + f"  ({len(gf.get('findings') or [])} finding(s), {n_out} outside the edit scope)"
        )
    cf = out.get("cell_floor") or {}
    if cf:
        print(
            f"  Cells:        true score {cf.get('true_score')}, workable score {cf.get('workable_score')} "
            f"({len(cf.get('pinned') or [])} pinned, {len(cf.get('workable') or [])} workable below the bar)"
        )
    for t in out.get("judge_timeouts") or []:
        print(f"  JUDGE TIMEOUT: {t['judge']} — {t['reason']} (cells carried from the last pass)")
    if out.get("predictions_required"):
        print("  Next batch:   declare predictions first — python -m scripts.ddd.predict declare "
              f"{out['run_id']} --batch <items.json>")
    t = out.get("pass_timing") or {}
    if t:
        jm = t.get("judge_minutes") or {}
        print(
            "  Pass timing:  "
            + ", ".join(
                f"{k} {v:.1f}m"
                for k, v in [("wall", t.get("wall_minutes")), ("fix", t.get("fix_minutes")),
                             ("render", t.get("render_minutes")), *sorted(jm.items())]
                if isinstance(v, (int, float))
            )
        )
    ng = out.get("narrative_guard") or {}
    if ng.get("decision") in ("reject", "accept"):
        print(
            f"  Narrative:    {ng['decision'].upper()} ({ng.get('mode')})"
            + (f" -> v{ng['version']}" if ng.get("version") is not None else "")
            + (f"  MATERIAL: {'; '.join(ng.get('material_why') or [])}" if ng.get("material") else "")
        )
    tgt = out.get("target_rubric") or {}
    if tgt:
        crit = tgt.get("criteria") or []
        print(
            f"  Rubric:       {tgt.get('source')} — "
            f"{sum(1 for c in crit if c.get('passing'))}/{len(crit)} criteria passing"
        )
        for c in crit:
            if not c.get("passing"):
                print(f"                  {c.get('id')}: {c.get('why')} (last passes {c.get('window')})")
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
    br = out.get("blocked_report") or {}
    if br:
        print("")
        for line in br.get("summary") or []:
            print(line)
        print(f"  ({br.get('path')})")
    rs = out.get("run_summary") or {}
    if rs:
        print("")
        print(f"  Run summary:  {rs.get('path')} — {rs.get('canopy_web')}")
        routing = rs.get("routing") or {}
        for r in routing.get("results") or []:
            print(f"    routed {r.get('finding_id')} ({r.get('kind')}): {r.get('status')} -> "
                  f"{r.get('url') or r.get('issue') or r.get('repo')}" + (f"  [{r['why']}]" if r.get("why") else ""))
        for u in routing.get("unrouted") or []:
            print(f"    not routed {u.get('finding_id')} ({u.get('kind')}): {u.get('why')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
