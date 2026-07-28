"""Two scenes that captured the same picture.

A ``scroll_to`` whose target is already in the viewport scrolls zero pixels. It
does not fail, it does not warn, and the scene captures the frame the previous
one already captured. The narration keeps going over a picture that has stopped.

This happened twice in two iterations of one narrative, and neither time did
anything mechanical notice. Both were found by an LLM arc judge pixel-diffing
screenshots by hand — a ten-minute, non-deterministic pass spent on a comparison
that is four lines of arithmetic. The second time, the "fix" for the first was
itself the no-op.

The rubric already prices this: *"two adjacent scenes make the same point with
different pixels: max 2"*, and its harsher sibling *"more than half the scenes
are the same surface at different scroll offsets: max 2"*. A run that trips them
is capped at 2 out of 5 however good everything else is, so finding out before a
judge does is worth the twenty milliseconds this takes.

    python -m scripts.ddd.duplicate_frames <run_dir> [--json] [--threshold 0.02]

Exit codes: 0 clean, 1 duplicates found, 2 usage/setup error.

Compares only CONSECUTIVE scenes. Two similar frames a long way apart are a
legitimate callback; two in a row are a stall.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Fraction of differing pixels below which two frames are "the same picture".
# A moved mouse cursor is ~0.2-0.4% of a 1280x720 frame, so the floor has to sit
# above that or every scene pair looks identical. A real scroll, even a short
# one, moves double-digit percentages.
DEFAULT_THRESHOLD = 0.02


def _scene_pngs(snapshots_dir: Path) -> list[tuple[int, Path]]:
    out = []
    for path in snapshots_dir.glob("scene_*.png"):
        stem = path.stem
        if stem.endswith("_before"):
            continue
        try:
            out.append((int(stem.split("_")[1]), path))
        except (IndexError, ValueError):
            continue
    return sorted(out)


def _difference(a: Path, b: Path) -> float | None:
    """Fraction of pixels that differ. None when it cannot be computed."""
    try:
        import numpy as np
        from PIL import Image
    except ImportError:  # pragma: no cover - optional dependency
        return None

    try:
        with Image.open(a) as ia, Image.open(b) as ib:
            first = ia.convert("RGB")
            second = ib.convert("RGB")
            if first.size != second.size:
                # A different viewport is by definition a different picture.
                return 1.0
            left = np.asarray(first, dtype=np.int16)
            right = np.asarray(second, dtype=np.int16)
    except OSError:
        return None

    # Any channel differing by more than a hair counts. Exact equality would be
    # defeated by JPEG-ish noise and antialiasing on a re-render.
    differing = (np.abs(left - right).max(axis=2) > 8).sum()
    return float(differing) / float(left.shape[0] * left.shape[1])


def check(run_dir: str | Path, threshold: float = DEFAULT_THRESHOLD) -> dict[str, Any]:
    run = Path(run_dir)
    snapshots = run / "snapshots"
    if not snapshots.is_dir():
        return {
            "run_dir": str(run),
            "verdict": "fail",
            "reason": f"no snapshots directory at {snapshots}",
            "pairs": [],
        }

    scenes = _scene_pngs(snapshots)
    if len(scenes) < 2:
        return {
            "run_dir": str(run),
            "verdict": "pass",
            "reason": "fewer than two scenes — nothing to compare",
            "pairs": [],
        }

    pairs: list[dict[str, Any]] = []
    unavailable = False
    for (index_a, path_a), (index_b, path_b) in zip(scenes, scenes[1:]):
        diff = _difference(path_a, path_b)
        if diff is None:
            unavailable = True
            continue
        pairs.append(
            {
                "scenes": [index_a, index_b],
                "difference": round(diff, 5),
                "duplicate": diff < threshold,
            }
        )

    if unavailable and not pairs:
        return {
            "run_dir": str(run),
            "verdict": "pass",
            "reason": "pillow/numpy unavailable — frames could not be compared",
            "pairs": [],
        }

    duplicates = [p for p in pairs if p["duplicate"]]
    if not duplicates:
        return {
            "run_dir": str(run),
            "verdict": "pass",
            "reason": f"every consecutive pair differs by more than {threshold:.0%}",
            "pairs": pairs,
        }

    worst = min(duplicates, key=lambda p: p["difference"])
    return {
        "run_dir": str(run),
        "verdict": "fail",
        "reason": (
            f"scenes {worst['scenes'][0]} and {worst['scenes'][1]} captured the same picture "
            f"({worst['difference']:.2%} of pixels differ). The usual cause is a scroll_to "
            f"whose target was already in the viewport, which scrolls zero pixels and does "
            f"not fail."
        ),
        "pairs": pairs,
    }


def _cli(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    if not args:
        print("usage: python -m scripts.ddd.duplicate_frames <run_dir> [--json] [--threshold N]", file=sys.stderr)
        return 2

    threshold = DEFAULT_THRESHOLD
    for i, token in enumerate(argv):
        if token == "--threshold" and i + 1 < len(argv):
            try:
                threshold = float(argv[i + 1])
            except ValueError:
                print("--threshold takes a fraction, e.g. 0.02", file=sys.stderr)
                return 2

    result = check(args[0], threshold=threshold)
    if "--json" in argv:
        print(json.dumps(result, indent=2))
    else:
        print(f"duplicate-frames: {result['verdict']}  ({len(result['pairs'])} pairs compared)")
        print(f"  {result['reason']}")
        for pair in result["pairs"]:
            if pair["duplicate"]:
                a, b = pair["scenes"]
                print(f"  scenes {a} → {b}: {pair['difference']:.2%} of pixels differ")
    return 0 if result["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv))
