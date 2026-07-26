"""One-shot backfill: give every scene in a pre-L0 spec a stable explicit id.

The id written is the scene's CURRENT title slug — which is exactly what
canopy-web already stored as ``NarrationItem.id`` for every existing narrative
version. Running this before anyone rewords a title makes local specs and cloud
history line up for free; running it after simply costs the before/after view
of an old version, which is an acceptable loss (history is disposable).

    python -m scripts.ddd.backfill_scene_ids docs/walkthroughs/*.yaml
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

from scripts.ddd.identity import slugify


def backfill(spec_path) -> dict:
    """Add ``id:`` to every id-less scene. Idempotent.

    Returns ``{"added": int, "skipped": bool}``. ``skipped``
    marks a file that is not a scene-bearing spec (e.g. a ``.why_brief.yaml``),
    which is left completely untouched.
    """
    p = Path(spec_path)
    raw = yaml.safe_load(p.read_text()) or {}
    scenes = raw.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        return {"added": 0, "skipped": True}

    added = 0
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        if (scene.get("id") or "").strip():
            continue
        scene["id"] = slugify(scene.get("title", "") or "")
        added += 1

    if added:
        p.write_text(
            yaml.dump(raw, default_flow_style=False, allow_unicode=True, sort_keys=False)
        )

    return {"added": added, "skipped": False}


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: python -m scripts.ddd.backfill_scene_ids <spec.yaml> [...]")
        return 2
    for arg in argv:
        result = backfill(arg)
        if result["skipped"]:
            print(f"{arg}: skipped (no scenes)")
        else:
            print(f"{arg}: +{result['added']} ids")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
