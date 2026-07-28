"""Did every snapshot in this run dir come from the SAME render?

Iterations of a narrative reuse one run dir. A re-render therefore OVERWRITES
the snapshots a judge may still be reading, one scene at a time, while it reads
them — and nothing anywhere says so. The judge returns a verdict scored against
a mixture of two iterations: some scenes as they are now, some as they were
before the fix, and findings that describe frames which no longer exist.

That happened. A fourteen-minute concept-eval came back describing a review
queue that had been fixed two renders earlier, and the only reason it was caught
is that a human noticed the prose did not match the current frames. Acting on
those findings would have meant "fixing" things that were already right and
re-breaking things that were not.

The render now stamps every ``scene_<N>_page_text.json`` with the id of the
render that wrote it (``Recorder.render_id``). This checks they all agree.

    python -m scripts.ddd.snapshot_consistency <run_dir> [--json]

Exit codes: 0 consistent, 1 mixed (do not judge this run dir), 2 usage error.

Unstamped snapshots are tolerated and reported as ``unknown`` — runs captured
before the stamp existed are not retroactively broken, and a directory in which
NOTHING is stamped is reported as a pass with a note rather than a failure.
Mixing stamped and unstamped IS a failure: it means a fresh render landed on top
of an older one.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_UNKNOWN = "unknown"


def _page_text_files(snapshots_dir: Path) -> list[Path]:
    return sorted(
        snapshots_dir.glob("scene_*_page_text.json"),
        key=lambda p: int(p.name.split("_")[1]),
    )


def check(run_dir: str | Path) -> dict[str, Any]:
    """Group the run's snapshots by the render that wrote them."""
    run = Path(run_dir)
    snapshots_dir = run / "snapshots"
    if not snapshots_dir.is_dir():
        return {
            "run_dir": str(run),
            "verdict": "fail",
            "reason": f"no snapshots directory at {snapshots_dir}",
            "renders": {},
            "scenes": 0,
        }

    files = _page_text_files(snapshots_dir)
    if not files:
        return {
            "run_dir": str(run),
            "verdict": "fail",
            "reason": "no scene_<N>_page_text.json files — nothing to judge",
            "renders": {},
            "scenes": 0,
        }

    renders: dict[str, list[int]] = {}
    for path in files:
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            return {
                "run_dir": str(run),
                "verdict": "fail",
                "reason": f"{path.name} is unreadable ({type(exc).__name__}) — the render did not finish",
                "renders": {},
                "scenes": len(files),
            }
        render_id = payload.get("render_id") or _UNKNOWN
        renders.setdefault(render_id, []).append(payload.get("scene_index"))

    distinct = set(renders)
    if distinct == {_UNKNOWN}:
        return {
            "run_dir": str(run),
            "verdict": "pass",
            "reason": "captured before render stamping existed — consistency cannot be verified, but nothing indicates a mix",
            "renders": renders,
            "scenes": len(files),
        }
    if len(distinct) == 1:
        return {
            "run_dir": str(run),
            "verdict": "pass",
            "reason": "every snapshot came from one render",
            "renders": renders,
            "scenes": len(files),
        }

    return {
        "run_dir": str(run),
        "verdict": "fail",
        "reason": (
            f"{len(distinct)} different renders wrote these snapshots — a re-render "
            "landed on top of a judge's inputs. Re-render the whole run before judging it."
        ),
        "renders": renders,
        "scenes": len(files),
    }


def _cli(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        print("usage: python -m scripts.ddd.snapshot_consistency <run_dir> [--json]", file=sys.stderr)
        return 2

    result = check(args[0])
    if "--json" in argv:
        print(json.dumps(result, indent=2))
    else:
        print(f"snapshot-consistency: {result['verdict']}  ({result['scenes']} scenes)")
        print(f"  {result['reason']}")
        if result["verdict"] == "fail" and result["renders"]:
            for render_id, scenes in sorted(result["renders"].items()):
                shown = ", ".join(str(s) for s in scenes)
                print(f"  render {render_id}: scenes {shown}")
    return 0 if result["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv))
