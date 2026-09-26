"""Video only after convergence.

The narrated video is the most expensive artifact the loop makes (record a
master clip, TTS, Remotion render, a multimodal judge) and it films whatever
the product is at that moment. Rendered before convergence it films a product
about to change: the chlorine narrative's verdict-video was rendered at
iteration 4 with a gating score of 2, and every finding it produced described
defects the concept loop was still fixing.

    python -m scripts.ddd.video_gate (--run-id <id> | --slug <narrative-slug>) [--allow-unconverged]

Exit 0 = the video may be rendered, 1 = refused (reason printed), 2 = usage.
``--allow-unconverged`` is the explicit, logged override for a deliberate
one-off (e.g. a stakeholder asked for a preview) — never a loop default.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from scripts.ddd.schemas.models import RunState

CONVERGED_STATUSES = frozenset({"converged_clean", "converged_with_open_questions"})
CONVERGED_PHASES = frozenset({"converged", "uploaded", "promoted"})


def is_converged(state: RunState) -> tuple[bool, str]:
    """Converged on a FULL spec and a FULL judge pass."""
    if state.scene_filter:
        return False, f"run {state.run_id} judged a filtered scope ({state.scene_filter})"
    if not state.last_judge_full:
        return False, f"run {state.run_id}'s last judge pass reused cells (incremental)"
    if state.auto_iterate_next_action == "stop_done":
        return True, f"run {state.run_id} converged (stop_done)"
    if state.terminal_status in CONVERGED_STATUSES:
        return True, f"run {state.run_id} converged ({state.terminal_status})"
    if state.phase in CONVERGED_PHASES:
        return True, f"run {state.run_id} is {state.phase}"
    return False, (
        f"run {state.run_id} has not converged (next action "
        f"{state.auto_iterate_next_action or 'none'}, status {state.terminal_status or 'none'}, "
        f"score history {state.score_history})"
    )


def check(state: RunState | None, *, allow_unconverged: bool = False) -> dict[str, Any]:
    if state is None:
        ok, reason = False, "no DDD run found for this narrative"
    else:
        ok, reason = is_converged(state)
    if ok:
        return {"allowed": True, "override": False, "reason": reason}
    if allow_unconverged:
        return {
            "allowed": True,
            "override": True,
            "reason": f"OVERRIDE (--allow-unconverged): {reason}",
        }
    return {
        "allowed": False,
        "override": False,
        "reason": (
            f"{reason}. The narrated video renders only after convergence — it would film "
            "a product the loop is still changing. Finish the loop, or pass "
            "--allow-unconverged for a deliberate preview."
        ),
    }


def newest_run_for_slug(slug: str, ddd_dir: Path | None = None) -> RunState | None:
    """The most recently modified run whose narrative_slug is ``slug``."""
    from scripts.ddd.runstate import legacy_runs_dir, runs_dir

    candidates: list[tuple[float, RunState]] = []
    for root in (runs_dir(ddd_dir), legacy_runs_dir(ddd_dir)):
        if not root.is_dir():
            continue
        for sf in root.glob("*/run_state.yaml"):
            try:
                raw = yaml.safe_load(sf.read_text()) or {}
                if raw.get("narrative_slug") != slug:
                    continue
                candidates.append((sf.stat().st_mtime, RunState.model_validate(raw)))
            except Exception:
                continue
    if not candidates:
        return None
    return max(candidates, key=lambda c: c[0])[1]


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.ddd.video_gate")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--run-id")
    g.add_argument("--slug")
    ap.add_argument("--allow-unconverged", action="store_true")
    args = ap.parse_args(argv)
    try:
        if args.run_id:
            from scripts.ddd.runstate import load

            state: RunState | None = load(args.run_id)
        else:
            state = newest_run_for_slug(args.slug)
    except (OSError, ValueError) as exc:
        print(f"video_gate: {exc}", file=sys.stderr)
        return 2
    result = check(state, allow_unconverged=args.allow_unconverged)
    print(json.dumps(result, indent=1))
    return 0 if result["allowed"] else 1


if __name__ == "__main__":
    raise SystemExit(_main())
