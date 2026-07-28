"""Did this iteration break something the last one had working?

An improvement loop that only measures the thing it is trying to fix will
happily trade one defect for another. It happened on the first narrative through
the loop: a fix that made a row reflect an already-open shortfall — correct, and
what a judge asked for — removed the button the next scene clicked, and the
render went from 29/29 to 26/29 with the regression invisible until the run
report was read by hand.

Two comparisons, both cheap, both from artifacts the loop already writes:

  * **Actions** — an action that was ``ok`` last iteration and is not now.
    Unambiguous; a render is not a judgment call.
  * **Scores** — a per-dimension score that fell. A drop is not automatically
    wrong (a judge is not deterministic, and a harder look at a fixed scene can
    legitimately score lower) so this reports rather than fails.

Only the action regression sets a failing verdict. The point is to be loud about
the thing that is certainly a regression and merely informative about the thing
that might be.

    python -m scripts.ddd.regression_guard <run_dir> [--json]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_HISTORY = "iteration-history.json"


def _action_key(action: dict) -> str:
    return f"{action.get('scene_index')}:{action.get('kind')}:{action.get('target')}"


def _snapshot_actions(report: dict) -> dict[str, bool]:
    return {_action_key(a): bool(a.get("ok")) for a in (report.get("actions") or [])}


def _snapshot_scores(verdict: dict | None) -> dict[str, float]:
    if not verdict:
        return {}
    dims = verdict.get("dimensions") or {}
    out = {}
    for name, entry in dims.items():
        score = entry.get("score") if isinstance(entry, dict) else entry
        if isinstance(score, (int, float)):
            out[name] = float(score)
    return out


def record(run_dir: str | Path, *, iteration: int | None = None) -> dict:
    """Append this iteration's snapshot and diff it against the previous one."""
    run = Path(run_dir)
    report_path = run / "run-report.json"
    if not report_path.exists():
        raise SystemExit(f"regression_guard: no run-report.json in {run}")

    report = json.loads(report_path.read_text())
    verdict_path = run / "verdict-concept.yaml"
    verdict = None
    if verdict_path.exists():
        text = verdict_path.read_text()
        try:
            verdict = json.loads(text)
        except json.JSONDecodeError:
            try:
                import yaml

                verdict = yaml.safe_load(text)
            except Exception:
                verdict = None

    snapshot = {
        "iteration": iteration,
        "actions": _snapshot_actions(report),
        "scores": _snapshot_scores(verdict),
        "ok": sum(1 for a in (report.get("actions") or []) if a.get("ok")),
        "total": len(report.get("actions") or []),
    }

    history_path = run / _HISTORY
    history = json.loads(history_path.read_text()) if history_path.exists() else []
    previous = history[-1] if history else None

    findings: list[dict[str, Any]] = []
    score_moves: list[dict[str, Any]] = []

    if previous:
        for key, was_ok in (previous.get("actions") or {}).items():
            now_ok = snapshot["actions"].get(key)
            if was_ok and now_ok is False:
                scene, kind, target = key.split(":", 2)
                findings.append(
                    {
                        "kind": "action_regression",
                        "scene": scene,
                        "action": kind,
                        "target": target,
                        "detail": f"{kind} on {target} succeeded last iteration and fails now",
                    }
                )
            elif was_ok and key not in snapshot["actions"]:
                scene, kind, target = key.split(":", 2)
                findings.append(
                    {
                        "kind": "action_disappeared",
                        "scene": scene,
                        "action": kind,
                        "target": target,
                        "detail": (
                            f"{kind} on {target} ran last iteration and is absent now — "
                            "either the recipe dropped it or the scene no longer reaches it"
                        ),
                    }
                )

        for name, was in (previous.get("scores") or {}).items():
            now = snapshot["scores"].get(name)
            if now is not None and now < was:
                score_moves.append({"dimension": name, "from": was, "to": now, "direction": "down"})
            elif now is not None and now > was:
                score_moves.append({"dimension": name, "from": was, "to": now, "direction": "up"})

    history.append(snapshot)
    history_path.write_text(json.dumps(history, indent=1))

    return {
        "iteration": len(history),
        "actions_ok": f"{snapshot['ok']}/{snapshot['total']}",
        "previous_actions_ok": f"{previous['ok']}/{previous['total']}" if previous else None,
        "regressions": findings,
        "score_moves": score_moves,
        "verdict": "pass" if not findings else "fail",
    }


def _cli() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__, file=sys.stderr)
        return 2
    result = record(args[0])
    if "--json" in sys.argv:
        print(json.dumps(result, indent=1))
        return 0 if result["verdict"] == "pass" else 1

    print(f"regression-guard: {result['verdict']}  (iteration {result['iteration']})")
    print(f"  actions ok: {result['actions_ok']}", end="")
    if result["previous_actions_ok"]:
        print(f"  (was {result['previous_actions_ok']})")
    else:
        print("  (first iteration — nothing to compare)")
    for finding in result["regressions"]:
        print(f"  ! {finding['detail']}")
    for move in result["score_moves"]:
        arrow = "↑" if move["direction"] == "up" else "↓"
        print(f"  {arrow} {move['dimension']}: {move['from']:g} -> {move['to']:g}")
    return 0 if result["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
