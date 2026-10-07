"""Refuse to upload a deck or package from a failed or stale render (M16).

Why this module exists
----------------------
On the first live v1 run the render wrapper returned 0 on a FAILED recorder, so
``render.sh && post.sh`` uploaded the PREVIOUS iteration's deck as iteration 3
before the real render ran (``loop-metrics.md`` M16). ddd-run's Step 2b had the
same shape: generate + upload straight after the render, with no exit-code or
freshness check in the snippet. A stale deck then carries the wrong frames into
every surfaced finding, gate and digest that links it.

Two checks, both required before any upload:

1. **exit code** — the recorder / render wrapper's own exit status (pass it with
   ``--exit-code``; a non-zero code fails the check whatever the files say).
2. **freshness** — every artifact the upload reads was written by THIS render:
   ``walkthrough-run-data.json``, ``run-report.json``, and ``scene_<N>.png`` for
   every scene in the manifest's ``scenes_run`` (plus the clip, if named), all
   with mtime >= ``--since`` (the epoch second the render started).

    RENDER_START=$(date +%s)
    <render> ; RENDER_RC=$?
    python -m scripts.ddd.render_check <run_dir> --since "$RENDER_START" \\
        --exit-code "$RENDER_RC" [--clip <run_dir>/iter<N>_clip.mp4] \\
      || { echo "render failed or stale — NOT uploading"; exit 1; }

Exit 0 = fresh and successful; 1 = do not upload (the JSON says why).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Filesystem mtime granularity + clock skew between the recorder process and
# the shell that took RENDER_START.
_SLACK_S = 2.0


def _find(run_dir: Path, name: str) -> Path | None:
    for base in (run_dir / "snapshots", run_dir):
        p = base / name
        if p.exists():
            return p
    return None


def check(
    run_dir: str | Path,
    *,
    since: float,
    exit_code: int | None = None,
    clip: str | Path | None = None,
) -> dict[str, Any]:
    run = Path(run_dir)
    problems: list[str] = []
    if exit_code is not None and int(exit_code) != 0:
        problems.append(f"render exited {exit_code}")

    def fresh(path: Path | None, label: str) -> None:
        if path is None or not path.exists():
            problems.append(f"{label} missing")
            return
        if path.stat().st_mtime + _SLACK_S < since:
            problems.append(f"{label} is stale (written before this render started)")

    manifest_path = run / "walkthrough-run-data.json"
    fresh(manifest_path if manifest_path.exists() else None, "walkthrough-run-data.json")
    report = run / "run-report.json"
    fresh(report if report.exists() else None, "run-report.json")

    scenes: list[int] = []
    carried: list[int] = []
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
            # A scene-scoped capture (canopy#785) re-films some scenes and keeps
            # the rest: only the re-filmed ones must be fresh.
            carried = [int(s) for s in manifest.get("carried_scenes") or []]
            scenes = [int(s) for s in (manifest.get("scenes_run") or []) if int(s) not in carried]
        except (ValueError, TypeError, OSError):
            problems.append("walkthrough-run-data.json unreadable")
    for n in scenes:
        fresh(_find(run, f"scene_{n}.png"), f"scene_{n}.png")
    if clip is not None:
        fresh(Path(clip), Path(clip).name)

    return {
        "ok": not problems,
        "upload": not problems,
        "problems": problems,
        "scenes_checked": scenes,
        "scenes_carried": carried,
        "reason": "render fresh and successful" if not problems else "; ".join(problems),
    }


def stamp(run_id: str, result: dict[str, Any]) -> None:
    """Record the verdict for the run's CURRENT iteration (read by ddd-upload)."""
    from datetime import datetime, timezone

    from scripts.ddd.runstate import load, save

    state = load(run_id)
    state.steps = {
        **(state.steps or {}),
        "render_check": {
            "iteration": state.iteration,
            "ok": bool(result.get("ok")),
            "reason": result.get("reason"),
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    }
    save(state)


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.ddd.render_check")
    ap.add_argument("run_dir")
    ap.add_argument("--since", type=float, required=True, help="epoch seconds the render started")
    ap.add_argument("--exit-code", type=int, default=None)
    ap.add_argument("--clip", default=None)
    ap.add_argument(
        "--run-id",
        default=None,
        help="stamp the verdict on RunState.steps['render_check'] (ddd-upload refuses a failed one)",
    )
    args = ap.parse_args(argv)
    out = check(args.run_dir, since=args.since, exit_code=args.exit_code, clip=args.clip)
    if args.run_id:
        stamp(args.run_id, out)
    print(json.dumps(out, indent=1))
    if not out["ok"]:
        print(f"render_check: NOT uploading — {out['reason']}", file=sys.stderr)
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(_main())
