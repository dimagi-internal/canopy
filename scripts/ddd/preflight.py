"""Pre-iteration auth preflight — fail fast, naming the credential that is gone.

Why this module exists
----------------------
On the first live v1 run the local AWS SSO session expired between iterations:
iteration 3's seed failed one second in with "Token has expired and refresh
failed", and renewing it needed a person (``loop-metrics.md`` M15). The loop had
no notion that the credential its seeding depended on was about to lapse, so it
found out mid-iteration — after the fix batch had merged and deployed.

A repo lists the credentials its loop depends on; every iteration checks them
BEFORE any render, seed or fix batch starts, and the first failure stops the
iteration with the credential's name and the command's own error::

    # .canopy/ddd/config.yaml
    auth_preflight:
      timeout_seconds: 20
      commands:
        - name: aws-labs
          run: aws sts get-caller-identity --profile labs
        - gh auth status

    python -m scripts.ddd.preflight [--json]

Exit 0 = every credential live (or none configured -> ``skipped``), 1 = a
credential failed (the JSON names it), 2 = config/usage error.
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from typing import Any, Callable

from scripts.ddd.loop_config import AuthPreflightConfig


def _run(cmd: str, timeout: float) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd if any(c in cmd for c in "|&;$><") else shlex.split(cmd),
            shell=any(c in cmd for c in "|&;$><"),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f}s"
    except (FileNotFoundError, PermissionError) as exc:
        return 127, f"{type(exc).__name__}: {exc}"
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
    return proc.returncode, " | ".join(tail)


def check(
    cfg: AuthPreflightConfig,
    *,
    runner: Callable[[str, float], tuple[int, str]] = _run,
) -> dict[str, Any]:
    """Run every configured command in order; stop at the first failure."""
    if not cfg.enabled:
        return {"status": "skipped", "reason": "no auth_preflight.commands configured", "checked": []}
    checked: list[dict[str, Any]] = []
    for cmd in cfg.commands:
        code, out = runner(cmd.run, cfg.timeout_seconds)
        row = {"name": cmd.name, "run": cmd.run, "ok": code == 0, "exit_code": code}
        checked.append(row)
        if code != 0:
            return {
                "status": "failed",
                "credential": cmd.name,
                "reason": (
                    f"credential {cmd.name!r} is not live (`{cmd.run}` exited {code}: {out}) — "
                    "renew it before this iteration; nothing has been rendered, seeded or merged"
                ),
                "checked": checked,
            }
    return {
        "status": "ok",
        "reason": f"{len(checked)} credential(s) live",
        "checked": checked,
    }


def _main(argv: list[str] | None = None) -> int:
    from scripts.ddd import loop_config

    ap = argparse.ArgumentParser(prog="python -m scripts.ddd.preflight")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        cfg = loop_config.load().auth_preflight
    except Exception as exc:  # pragma: no cover - load() already swallows
        print(f"preflight: {exc}", file=sys.stderr)
        return 2
    out = check(cfg)
    if args.json:
        print(json.dumps(out, indent=1))
    else:
        print(f"auth preflight: {out['status']} — {out['reason']}")
    return 1 if out["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(_main())
