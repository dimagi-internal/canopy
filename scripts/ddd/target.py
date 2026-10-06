"""Where the next pass renders and which judges it runs — inner loop vs checkpoint.

Why this module exists
----------------------
On the first live v1 run every fix batch paid the full outer loop before a
single frame could be judged: build 15–22 min, CI 7–9, deploy 8–11, render
2.5–5.8, judge 4–7 min and ~450k tokens (concept ~170k, user ~150k, arc
~130k). Most batches only needed the concept judge to say whether a changed
scene got better, and none needed production to answer that.

Two cheaper tiers, both FIDELITY-SAFE — neither can decide anything:

* **inner-loop target** (``inner_loop:`` in ``.canopy/ddd/config.yaml``, default
  OFF). Between checkpoints, batch fixes are rendered against a locally served
  build of the fix branch (``inner_loop.base_url``) — no merge, no CI, no
  deploy. Every CHECKPOINT (every ``loop.full_rejudge_every``-th batch — the
  same passes that are judged in full) and every decision renders against the
  real deploy target, through the deploy gate.
* **judge tiering** (``loop.judge_tiering``, ``auto`` = on in backlog mode).
  Between checkpoints the concept judge runs on changed scenes, and —
  FLOOR-FIRST (``loop.floor_first``, default on; canopy#780) — so does the judge
  holding the gating floor, on the floor's scenes when they changed
  (``judge_scope plan`` decides from ``state.gating_floor``). Every other
  verdict is carried from the last full pass. Before floor-first, a floor held
  by the user-artifact judge could only move at a checkpoint: two of every three
  passes on ACE Spark run ``-002`` could not change the gating score. Every
  checkpoint and every decision runs all three.
* **recipe-only batch** (always on). A batch whose every fix is recorder
  framing, narration or why-brief (``fix_scope.batch_plan``) changes no product
  code, so between checkpoints its pass skips the deploy gate
  (``deploy_gate: skip`` — nothing to merge, CI or deploy) and re-judges only
  the scenes it edited, holding the rest to their ledger cells. A pass that held
  scenes cannot decide either.

``expected_scope`` is what ``judge_scope plan`` derives from run_state itself,
so the orchestrator never passes (or forgets to drop) ``--full``.

The guard lives in ``run_pipeline.compute_auto_iterate``: a pass that was
``inner`` or concept-only can never return ``stop_done`` or any stop — it
returns ``checkpoint`` (land the batches, then a full pass on the deploy target)
and the decision is made there. The convergence bar is unchanged.

    python -m scripts.ddd.target plan <run_id> [--json]   # stamps state.current_target
    python -m scripts.ddd.target ready <run_id>           # probe the inner health_url

A repo opts in with::

    inner_loop:
      base_url: http://localhost:8000
      setup: make serve-demo
      health_url: http://localhost:8000/health/

and since 0.2.554 a repo with a ``deploy_gate`` MUST: :func:`inner_loop_policy`
makes a backlog loop there stop (``stop_inner_loop_required``) unless it is
configured or declared ``inner_loop: off`` with ``inner_loop_off_reason:``.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from typing import Any, Callable

from scripts.ddd.loop_config import DDDConfig

DEPLOY = "deploy"
INNER = "inner"
ALL_JUDGES = ("concept", "user", "arc")
CONCEPT_ONLY = ("concept",)


def recipe_batch(state: Any) -> dict | None:
    """The recipe-only batch this pass renders, or ``None``.

    ``assemble`` stamps ``state.batch_plan`` on a ``continue`` for the NEXT
    iteration; a stale plan (another iteration) is ignored.
    """
    bp = getattr(state, "batch_plan", None) or {}
    if bp.get("scope") != "recipe":
        return None
    if bp.get("for_iteration") != getattr(state, "iteration", None):
        return None
    return bp


def choose(state: Any, cfg: DDDConfig) -> dict[str, Any]:
    """The next pass's target + judges. Pure: reads state, never writes it."""
    checkpoint = bool(getattr(state, "next_judge_full", True)) or (
        getattr(state, "auto_iterate_next_action", None) in ("checkpoint", "confirm_full")
    )
    tiered = cfg.loop.tiered(getattr(state, "loop_mode", None))
    judges = list(ALL_JUDGES) if (checkpoint or not tiered) else list(CONCEPT_ONLY)
    recipe = None if checkpoint else recipe_batch(state)
    deploy_gate = "check"
    judge_scenes = None
    if checkpoint:
        reason = "checkpoint — full render + every judge against the real deploy target"
        target = DEPLOY
    elif recipe:
        # No product code changed, so there is nothing to merge, wait on in CI, or
        # deploy: the render reads the recipe from the local checkout and the
        # deploy target already serves state.last_fix_sha.
        target = DEPLOY
        deploy_gate = "skip"
        judge_scenes = recipe.get("judge_scenes")
        reason = (
            "recipe-only batch — no product change: no PR/CI/deploy wait, deploy gate "
            "skipped, full render"
            + (f", re-judge only scene(s) {judge_scenes}" if judge_scenes else "")
        )
    elif cfg.inner_loop.enabled:
        target = INNER
        reason = f"between checkpoints — render against the local build at {cfg.inner_loop.base_url}"
    else:
        target = DEPLOY
        reason = "between checkpoints — no inner_loop configured, so the deploy target"
    if not checkpoint and tiered:
        reason += (
            "; judge tiering: concept judge on changed scenes"
            + (", plus the floor's judge on the floor's scenes (floor-first)" if cfg.loop.floor_first else "")
        )
    return {
        "target": target,
        "base_url": cfg.inner_loop.base_url if target == INNER else None,
        "setup": cfg.inner_loop.setup if target == INNER else None,
        "judges": judges,
        "checkpoint": checkpoint,
        "deploy_gate": deploy_gate,
        "judge_scenes": judge_scenes,
        "reason": reason,
    }


#: ``inner_loop_policy`` statuses.
POLICY_CONFIGURED = "configured"
POLICY_OFF = "off"
POLICY_MISSING = "missing"
POLICY_NOT_REQUIRED = "not_required"


def inner_loop_policy(cfg: DDDConfig) -> dict[str, Any]:
    """May this repo run a backlog (v1-product) loop? ``{status, reason}``.

    A repo with a configured ``deploy_gate`` ships every fix batch through PR, CI
    and deploy before a frame can be judged. On connect-labs that was ~35 of every
    50 minutes of an iteration — paid on batches that only needed the concept
    judge to see a changed scene. So such a repo must say how it renders between
    checkpoints: an ``inner_loop:`` (``configured``), or ``inner_loop: off`` WITH a
    reason (``off`` — recorded in run_state, printed by every assemble). Anything
    else is ``missing``, and ``compute_auto_iterate`` refuses to continue a
    backlog loop (``stop_inner_loop_required``). A repo with no deploy gate has
    nothing to wait on: ``not_required``.
    """
    inner = cfg.inner_loop
    if inner.enabled:
        return {"status": POLICY_CONFIGURED, "reason": f"inner loop at {inner.base_url}"}
    if not cfg.deploy_gate.enabled:
        return {"status": POLICY_NOT_REQUIRED, "reason": "no deploy_gate configured"}
    if inner.off and inner.off_reason:
        return {"status": POLICY_OFF, "reason": inner.off_reason}
    if inner.off:
        return {
            "status": POLICY_MISSING,
            "reason": "`inner_loop: off` needs a reason — add `inner_loop_off_reason: <why>` "
            "to .canopy/ddd/config.yaml",
        }
    return {
        "status": POLICY_MISSING,
        "reason": "deploy_gate is configured but inner_loop is not — add `inner_loop: {base_url, "
        "setup, health_url}` to .canopy/ddd/config.yaml, or `inner_loop: off` with "
        "`inner_loop_off_reason: <why>`",
    }


def expected_scope(state: Any, cfg: DDDConfig) -> dict[str, Any]:
    """What ``judge_scope plan`` must do for this pass, derived from state alone.

    ``judge_scope plan`` reads this itself, so the orchestrator never has to
    pass (or remember NOT to pass) ``--full``: a literal ``--full`` that
    contradicts it is refused unless ``--reason`` is given and recorded.
    """
    rr = getattr(state, "recipe_rejudge", None) or {}
    if rr.get("status") == "pending" and rr.get("iteration") == getattr(state, "iteration", None):
        # M17: the SAME iteration, re-judged on the recipe-capped scenes only.
        return {
            "full": False,
            "tiered": False,
            "judge_scenes": list(rr.get("scenes") or []) or None,
            "why": f"incremental — recipe re-judge of scene(s) {rr.get('scenes')} (M17)",
        }
    tgt = current(state) or choose(state, cfg)
    full = bool(tgt.get("checkpoint")) or bool(getattr(state, "next_judge_full", True))
    action = getattr(state, "auto_iterate_next_action", None)
    if not full:
        why = "incremental — between checkpoints (state.next_judge_full is false)"
    elif action in ("checkpoint", "confirm_full"):
        why = f"full — the last decision was {action!r}"
    elif getattr(state, "loop_mode", None) != "backlog":
        why = "full — polish mode (or no mode yet) judges every pass in full"
    else:
        why = (
            f"full — checkpoint: every {cfg.loop.full_rejudge_every}th batch is judged in full"
        )
    return {
        "full": full,
        "tiered": "user" not in (tgt.get("judges") or ALL_JUDGES),
        "judge_scenes": None if full else tgt.get("judge_scenes"),
        "why": why,
    }


def decision_needs_checkpoint(
    target: str | None, judges: list[str] | None, held: list | None = None
) -> bool:
    """True when this pass may not DECIDE anything (inner target, partial judges,
    or scenes whose inputs moved but were held to their ledger cells)."""
    if (target or DEPLOY) != DEPLOY:
        return True
    if held:
        return True
    return judges is not None and not set(ALL_JUDGES) <= set(judges)


def current(state: Any) -> dict[str, Any]:
    """This iteration's stamped target, or ``{}`` (a stale stamp is ignored)."""
    tgt = getattr(state, "current_target", None) or {}
    return tgt if tgt.get("iteration") == getattr(state, "iteration", None) else {}


def plan_flags(state: Any, cfg: DDDConfig) -> str:
    """``judge_scope plan`` flags for this pass (from the stamp, else a fresh choice).

    Kept for older skill text: ``judge_scope plan`` now derives the same scope
    from run_state itself, so passing nothing is equivalent."""
    exp = expected_scope(state, cfg)
    flags = []
    if exp["full"]:
        flags.append("--full")
    if exp["tiered"]:
        flags.append("--tiered")
    return " ".join(flags)


def _fetch_status(url: str, timeout: float = 5.0) -> int:
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — configured URL
        return int(resp.status)


def wait_ready(
    health_url: str | None,
    *,
    timeout_s: float,
    fetch: Callable[[str], int] = _fetch_status,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Poll the inner build's health URL until it answers 2xx (or give up)."""
    if not health_url:
        return {"status": "skipped", "reason": "no inner_loop.health_url configured"}
    start = clock()
    last = None
    while True:
        try:
            code = fetch(health_url)
            if 200 <= code < 300:
                return {"status": "ready", "reason": f"{health_url} answered {code}"}
            last = f"HTTP {code}"
        except Exception as exc:  # not up yet
            last = type(exc).__name__
        if clock() - start >= timeout_s:
            return {
                "status": "not_ready",
                "reason": f"{health_url} not ready after {timeout_s:.0f}s (last: {last}) — "
                "run inner_loop.setup, or fall back to the deploy target",
            }
        sleep(2.0)


def _main(argv: list[str] | None = None) -> int:
    from scripts.ddd import loop_config
    from scripts.ddd.runstate import load, save

    ap = argparse.ArgumentParser(prog="python -m scripts.ddd.target")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("run_id")
    p.add_argument("--json", action="store_true")
    r = sub.add_parser("ready")
    r.add_argument("run_id")
    f = sub.add_parser("flags", help="judge_scope plan flags for this pass: --full / --tiered")
    f.add_argument("run_id")
    args = ap.parse_args(argv)
    cfg = loop_config.load()
    state = load(args.run_id)
    if args.cmd == "flags":
        print(plan_flags(state, cfg))
        return 0
    if args.cmd == "ready":
        out = wait_ready(cfg.inner_loop.health_url, timeout_s=cfg.inner_loop.ready_timeout_seconds)
        print(json.dumps(out))
        return 1 if out["status"] == "not_ready" else 0
    from scripts.ddd import decision

    try:
        decision.require(state, where="target plan")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    out = choose(state, cfg)
    state.current_target = {**out, "iteration": state.iteration}
    save(state)
    if args.json:
        print(json.dumps(out, indent=1))
    else:
        print(f"target: {out['target']}  judges: {','.join(out['judges'])}  — {out['reason']}")
        if out["base_url"]:
            print(f"render with: --base-url {out['base_url']}")
        if out["deploy_gate"] == "skip":
            print("deploy gate: SKIPPED — recipe-only batch; do not open a product PR or wait on CI/deploy")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
