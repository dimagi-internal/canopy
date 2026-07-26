"""One-shot backfill: give every scene in a pre-L0 spec a stable explicit id.

The id written is the scene's CURRENT title slug — which is exactly what
canopy-web already stored as ``NarrationItem.id`` for every existing narrative
version. Running this before anyone rewords a title makes local specs and cloud
history line up for free; running it after simply costs the before/after view
of an old version, which is an acceptable loss (history is disposable).

Also re-stamps ``narrative_synced_hash`` when the spec has one, because
``_NARRATIVE_SCENE_FIELDS`` changed shape in L0 (gained ``id`` + ``narrative``,
lost ``concept_claim``) and every stored hash is otherwise stale — which a later
``pull`` would misread as an unpushed local edit.

    python -m scripts.ddd.backfill_scene_ids docs/walkthroughs/*.yaml
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

from scripts.ddd.identity import slugify
from scripts.ddd.narrative import narrative_content_hash


def backfill(spec_path) -> dict:
    """Add ``id:`` to every id-less scene. Idempotent.

    Returns ``{"added": int, "rehashed": bool, "skipped": bool}``. ``skipped``
    marks a file that is not a scene-bearing spec (e.g. a ``.why_brief.yaml``),
    which is left completely untouched.
    """
    p = Path(spec_path)
    raw = yaml.safe_load(p.read_text()) or {}
    scenes = raw.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        return {"added": 0, "rehashed": False, "skipped": True}

    added = 0
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        if (scene.get("id") or "").strip():
            continue
        scene["id"] = slugify(scene.get("title", "") or "")
        added += 1

    rehashed = False
    if raw.get("narrative_synced_version") is not None:
        raw["narrative_synced_hash"] = narrative_content_hash(raw)
        rehashed = True

    if added or rehashed:
        p.write_text(
            yaml.dump(raw, default_flow_style=False, allow_unicode=True, sort_keys=False)
        )

    return {"added": added, "rehashed": rehashed, "skipped": False}


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: python -m scripts.ddd.backfill_scene_ids <spec.yaml> [...]")
        return 2
    for arg in argv:
        result = backfill(arg)
        if result["skipped"]:
            print(f"{arg}: skipped (no scenes)")
        else:
            print(f"{arg}: +{result['added']} ids, rehashed={result['rehashed']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
