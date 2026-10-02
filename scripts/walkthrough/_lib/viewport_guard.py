"""Refuse a render whose frame size silently differs from the previous one (#625).

The render viewport is a spec default (1280x720). For a one-shot render that is
fine; for the DDD iterate loop it is not, because the loop's whole premise is
that iteration N is comparable to N-1 — ``compute_auto_iterate`` stops on a
score STALL measured across those iterations — and frame size is the one
parameter that breaks comparability while everything still succeeds. A spec
rebuilt mid-loop that drops ``video_viewport_width``/``_height`` re-renders a
1440x900 run at 1280x720, every pixel scroll offset goes wrong, and the score
movement it reports is a frame-size change.

So every render that writes ``--snapshots`` also records the default viewport
it used, in ``render-viewport.json`` beside the frames. The next render into the
same directory compares against that record:

* **recorded, and different** → ``refuse``. The record is a fact, not a guess.
  ``--allow-viewport-change`` is the deliberate override.
* **no record, but earlier ``scene_<N>.png`` frames** (a run that predates this
  guard) → infer the old width from those PNGs and ``warn`` only. Inference is
  weaker than a record: a scene with its own ``viewport`` override, or a
  hand-copied frame, can mislead it, so it never blocks.
* **nothing on disk** → ``ok``; a first render is free to pick any size.

Only the spec-level DEFAULT viewport is compared. Per-scene ``viewport``
overrides are part of the spec and travel with it; the failure here is the
default changing underneath an unchanged scene list.
"""

from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass
from pathlib import Path

RECORD_NAME = "render-viewport.json"
_SCENE_PNG = re.compile(r"^scene_(\d+)\.png$")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True)
class ViewportCheck:
    verdict: str  # "ok" | "warn" | "refuse"
    message: str = ""


def png_size(path: Path) -> tuple[int, int] | None:
    """(width, height) from a PNG's IHDR chunk, or None if it isn't a PNG."""
    try:
        with path.open("rb") as f:
            head = f.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != _PNG_SIGNATURE or head[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", head[16:24])


def _read_record(snap_dir: Path) -> tuple[int, int] | None:
    try:
        data = json.loads((snap_dir / RECORD_NAME).read_text())
        return int(data["width"]), int(data["height"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _inferred_widths(snap_dir: Path, overridden_scenes: set[int]) -> set[int]:
    widths: set[int] = set()
    for p in snap_dir.glob("scene_*.png"):
        m = _SCENE_PNG.match(p.name)
        if not m or int(m.group(1)) in overridden_scenes:
            continue
        size = png_size(p)
        if size:
            widths.add(size[0])
    return widths


def _fix_hint(width: int, height: int | None) -> str:
    keys = f"video_viewport_width: {width}"
    if height is not None:
        keys += f" / video_viewport_height: {height}"
    return (
        f"Restore the spec's {keys} so this iteration matches, or pass "
        "--allow-viewport-change if the change is deliberate (scores across it are "
        "then NOT like-for-like)."
    )


def check_viewport(
    snap_dir: Path,
    width: int,
    height: int,
    *,
    overridden_scenes: set[int] = frozenset(),
    allow_change: bool = False,
) -> ViewportCheck:
    """Compare this render's default viewport with what ``snap_dir`` last used."""
    snap_dir = Path(snap_dir)
    if not snap_dir.is_dir():
        return ViewportCheck("ok")

    recorded = _read_record(snap_dir)
    if recorded is not None:
        if recorded == (width, height):
            return ViewportCheck("ok")
        msg = (
            f"Viewport changed: {snap_dir} was last rendered at "
            f"{recorded[0]}x{recorded[1]} ({RECORD_NAME}); this render resolves "
            f"{width}x{height}. Pixel scroll offsets and cross-iteration scores "
            "assume one frame size."
        )
        if allow_change:
            return ViewportCheck("warn", msg + " Proceeding: --allow-viewport-change.")
        return ViewportCheck("refuse", msg + " " + _fix_hint(*recorded))

    widths = _inferred_widths(snap_dir, set(overridden_scenes))
    if len(widths) == 1 and width not in widths:
        (old,) = widths
        return ViewportCheck(
            "warn",
            f"Viewport may have changed: earlier scene_<N>.png frames in {snap_dir} "
            f"are {old}px wide; this render resolves {width}x{height}. (Inferred from "
            f"the PNGs — no {RECORD_NAME} yet, so not refusing.) " + _fix_hint(old, None),
        )
    return ViewportCheck("ok")


def record_viewport(snap_dir: Path, width: int, height: int) -> None:
    snap_dir = Path(snap_dir)
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / RECORD_NAME).write_text(
        json.dumps({"width": width, "height": height}, indent=2) + "\n"
    )
