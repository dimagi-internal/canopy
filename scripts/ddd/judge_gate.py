"""Pre-judge gate — never pay for a judge round the loop already knows is wasted.

Two measured ways a judge round was burned for nothing:

1. **Judging before the fix was deployed.** A batch of fixes merged, the render
   ran against the still-old deployment, and the judges re-found the same caps
   ("Iteration 2 must render AFTER #1989 is deployed or the same caps recur" —
   connect-labs learnings). Labs deploys are rolling; a single healthy sample
   can come from an old task mid-rollout. So the gate samples the target's
   health endpoint N times and requires EVERY sample to report the merge SHA of
   the last fix batch (``state.last_fix_sha``).
2. **Judging a take a deterministic check already failed.** A recording bug, a
   regression the guard caught, or a narrated element rendered at zero height
   is a mechanical defect that costs milliseconds to find; judging the take
   anyway spends ~600k tokens re-discovering it.

Configured per target repo in ``.canopy/ddd/config.yaml`` (``deploy_gate:`` —
see :mod:`scripts.ddd.loop_config`). No config -> the deploy half reports
``skipped`` and only the lens half gates.

    python -m scripts.ddd.judge_gate set-fix-sha <run_id> <sha>
    python -m scripts.ddd.judge_gate check <run_id> [--expect SHA] \\
        [--lens regression_guard=pass --lens visual_geometry=fail ...] [--no-wait]

``check`` exits 0 when the judges may run, 1 when they must not (the JSON says
``wait_deploy`` or ``fix_render``), 2 on a usage error.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from typing import Any, Callable

from scripts.ddd.loop_config import DeployGateConfig

# Lens verdicts that mean "do not judge this take". Everything else (warn, skip,
# pass) lets the judges run and folds the lens findings in with theirs.
HARD_FAIL: dict[str, frozenset[str]] = {
    "regression_guard": frozenset({"fail"}),
    "visual_geometry": frozenset({"fail"}),
    "render_pacing_audit": frozenset({"fail", "recording_bug"}),
    "snapshot_consistency": frozenset({"fail"}),
    "recipe_preflight": frozenset({"fail"}),
}

_MIN_SHA = 7


def _dig(obj: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


def _http_fetch(url: str, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — configured URL
        return json.loads(resp.read().decode("utf-8", "replace"))


def sha_matches(reported: Any, expected: str) -> bool:
    """Prefix match either way (health endpoints often report a short SHA)."""
    if not isinstance(reported, str) or not expected:
        return False
    a, b = reported.strip().lower(), expected.strip().lower()
    if min(len(a), len(b)) < _MIN_SHA:
        return a == b
    return a.startswith(b) or b.startswith(a)


def check_deploy(
    cfg: DeployGateConfig,
    expected_sha: str | None,
    *,
    fetch: Callable[[str], dict] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    wait: bool = True,
) -> dict[str, Any]:
    """Sample the health URL until every sample reports ``expected_sha``."""
    if not cfg.enabled:
        return {"status": "skipped", "reason": "no deploy_gate.health_url configured"}
    if not expected_sha:
        return {
            "status": "skipped",
            "reason": "no fix SHA recorded (state.last_fix_sha) — nothing to wait for",
        }
    fetch = fetch or _http_fetch
    attempts = cfg.attempts if wait else 1
    rounds: list[list[Any]] = []
    for attempt in range(attempts):
        seen: list[Any] = []
        for i in range(cfg.samples):
            try:
                seen.append(_dig(fetch(cfg.health_url), cfg.sha_field))
            except Exception as exc:  # network error = not ready, never a crash
                seen.append(f"<error: {type(exc).__name__}>")
            if i < cfg.samples - 1:
                sleep(cfg.interval_seconds)
        rounds.append(seen)
        if all(sha_matches(s, expected_sha) for s in seen):
            return {
                "status": "ready",
                "reason": f"all {cfg.samples} samples report {expected_sha[:12]}",
                "rounds": rounds,
            }
        if attempt < attempts - 1:
            sleep(cfg.retry_seconds)
    return {
        "status": "not_ready",
        "reason": (
            f"after {attempts} round(s), not every sample of {cfg.health_url} reports "
            f"{expected_sha[:12]} (last round: {rounds[-1]}) — the fix is not fully "
            "deployed; judging now would re-find the defects it fixes"
        ),
        "rounds": rounds,
    }


def decide(deploy: dict | None, lenses: dict[str, str] | None) -> dict[str, Any]:
    """Judge, or say exactly why not."""
    hard = sorted(
        f"{name}={verdict}"
        for name, verdict in (lenses or {}).items()
        if verdict in HARD_FAIL.get(name, frozenset())
    )
    if deploy and deploy.get("status") == "not_ready":
        return {
            "judge": False,
            "action": "wait_deploy",
            "reason": deploy.get("reason"),
            "hard_fails": hard,
        }
    if hard:
        return {
            "judge": False,
            "action": "fix_render",
            "reason": (
                "deterministic check(s) hard-failed: "
                + ", ".join(hard)
                + " — fix the recording/product defect they name and re-render; do not "
                "spend a judge round re-discovering it"
            ),
            "hard_fails": hard,
        }
    return {
        "judge": True,
        "action": "judge",
        "reason": "deploy "
        + ((deploy or {}).get("status") or "skipped")
        + "; no deterministic hard-fail",
        "hard_fails": [],
    }


def _parse_lenses(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"--lens expects name=verdict, got {item!r}")
        name, verdict = item.split("=", 1)
        out[name.strip()] = verdict.strip().lower()
    return out


def _main(argv: list[str] | None = None) -> int:
    from scripts.ddd import loop_config
    from scripts.ddd.runstate import load, save

    ap = argparse.ArgumentParser(prog="python -m scripts.ddd.judge_gate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set-fix-sha", help="record the merge SHA of the last fix batch")
    s.add_argument("run_id")
    s.add_argument("sha")
    c = sub.add_parser("check")
    c.add_argument("run_id")
    c.add_argument("--expect", default=None, help="override state.last_fix_sha")
    c.add_argument("--lens", action="append", default=[])
    c.add_argument("--no-wait", action="store_true", help="one sampling round only")
    args = ap.parse_args(argv)
    try:
        state = load(args.run_id)
        if args.cmd == "set-fix-sha":
            state.last_fix_sha = args.sha.strip()
            save(state)
            print(json.dumps({"run_id": state.run_id, "last_fix_sha": state.last_fix_sha}))
            return 0
        cfg = loop_config.load().deploy_gate
        deploy = check_deploy(cfg, args.expect or state.last_fix_sha, wait=not args.no_wait)
        result = decide(deploy, _parse_lenses(args.lens))
        result["deploy"] = deploy
    except (ValueError, OSError) as exc:
        print(f"judge_gate {args.cmd}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=1))
    return 0 if result["judge"] else 1


if __name__ == "__main__":
    raise SystemExit(_main())
