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
  Between checkpoints only the concept judge runs, on changed scenes; the
  user-artifact and arc verdicts are carried from the last full pass. Every
  checkpoint and every decision runs all three.

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


def choose(state: Any, cfg: DDDConfig) -> dict[str, Any]:
    """The next pass's target + judges. Pure: reads state, never writes it."""
    checkpoint = bool(getattr(state, "next_judge_full", True)) or (
        getattr(state, "auto_iterate_next_action", None) in ("checkpoint", "confirm_full")
    )
    tiered = cfg.loop.tiered(getattr(state, "loop_mode", None))
    judges = list(ALL_JUDGES) if (checkpoint or not tiered) else list(CONCEPT_ONLY)
    if checkpoint:
        reason = "checkpoint — full render + every judge against the real deploy target"
        target = DEPLOY
    elif cfg.inner_loop.enabled:
        target = INNER
        reason = f"between checkpoints — render against the local build at {cfg.inner_loop.base_url}"
    else:
        target = DEPLOY
        reason = "between checkpoints — no inner_loop configured, so the deploy target"
    if not checkpoint and tiered:
        reason += "; judge tiering: concept judge only, on changed scenes"
    return {
        "target": target,
        "base_url": cfg.inner_loop.base_url if target == INNER else None,
        "setup": cfg.inner_loop.setup if target == INNER else None,
        "judges": judges,
        "checkpoint": checkpoint,
        "reason": reason,
    }


def decision_needs_checkpoint(target: str | None, judges: list[str] | None) -> bool:
    """True when this pass may not DECIDE anything (inner target or partial judges)."""
    if (target or DEPLOY) != DEPLOY:
        return True
    return judges is not None and not set(ALL_JUDGES) <= set(judges)


def current(state: Any) -> dict[str, Any]:
    """This iteration's stamped target, or ``{}`` (a stale stamp is ignored)."""
    tgt = getattr(state, "current_target", None) or {}
    return tgt if tgt.get("iteration") == getattr(state, "iteration", None) else {}


def plan_flags(state: Any, cfg: DDDConfig) -> str:
    """``judge_scope plan`` flags for this pass (from the stamp, else a fresh choice)."""
    tgt = current(state) or choose(state, cfg)
    flags = []
    if tgt.get("checkpoint") or getattr(state, "next_judge_full", True):
        flags.append("--full")
    if "user" not in (tgt.get("judges") or ALL_JUDGES):
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
    out = choose(state, cfg)
    state.current_target = {**out, "iteration": state.iteration}
    save(state)
    if args.json:
        print(json.dumps(out, indent=1))
    else:
        print(f"target: {out['target']}  judges: {','.join(out['judges'])}  — {out['reason']}")
        if out["base_url"]:
            print(f"render with: --base-url {out['base_url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
