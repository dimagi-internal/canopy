"""One-shot: split a legacy unified spec into a recipe + a narrative lock.

Splits from the CURRENT on-disk spec, which is by definition the latest local
version. Deliberately does NOT consult canopy-web and does NOT reconcile with
any prior version — per the design's standing constraint, history is disposable
and canopy-web owns the story from here (the first ``pull`` replaces the lock).

Writes ``id:`` onto any scene lacking one as it goes, so a legacy spec needs no
separate backfill pass before being split.

    python -m scripts.ddd.split_spec docs/walkthroughs/verified-monitoring.yaml
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

from scripts.ddd.identity import scene_id
from scripts.ddd.narrative import write_lock
from scripts.ddd.spec_io import _LOCK_SCENE_FIELDS, _LOCK_TOP_FIELDS, recipe_path

# The split and the compose have to agree on the seam or a round-trip loses
# fields, so they read the SAME tuples rather than mirroring them. They were
# mirrored by hand until the copies had no test holding them together — the
# kind of drift that only shows up as a field quietly vanishing.

# Dead as of L1 — the merge algorithm these fed no longer exists.
_DEAD_TOP_FIELDS = (
    "narrative_synced_version",
    "narrative_synced_hash",
    "narrative_synced_at",
)


def split(spec_path) -> dict:
    """Write ``<slug>.recipe.yaml`` + ``<slug>.narrative.lock.json`` beside the spec.

    Returns ``{"recipe": Path, "lock": Path, "scenes": int}``.
    """
    p = Path(spec_path)
    slug = p.name[: -len(".yaml")] if p.name.endswith(".yaml") else p.stem
    raw = yaml.safe_load(p.read_text()) or {}
    scenes = [s for s in (raw.get("scenes") or []) if isinstance(s, dict)]

    lock_parts = {k: raw.get(k) for k in _LOCK_TOP_FIELDS}
    # Only carry keys the scene actually HAS — writing an explicit None for an
    # omitted key turns "absent" into "null", which fails validation on compose.
    lock_parts["scenes"] = [
        {"id": scene_id(s), **{k: s[k] for k in _LOCK_SCENE_FIELDS if k in s}}
        for s in scenes
    ]
    # Version 0 means "this story came off a local file, not from canopy-web".
    # Deliberately NOT seeded from the old narrative_synced_version stamp: that
    # stamp is dead, and the first `pull` replaces this lock wholesale anyway.
    # Pretending a local split produced v3 would be a lie with no reader.
    lock = write_lock(p.parent, slug, 0, lock_parts)

    recipe = {
        k: v
        for k, v in raw.items()
        if k not in _LOCK_TOP_FIELDS and k not in _DEAD_TOP_FIELDS and k != "scenes"
    }
    recipe["scenes"] = [
        {
            "id": scene_id(s),
            **{k: v for k, v in s.items() if k not in _LOCK_SCENE_FIELDS and k != "id"},
        }
        for s in scenes
    ]
    rpath = recipe_path(p.parent, slug)
    rpath.write_text(
        yaml.dump(recipe, default_flow_style=False, allow_unicode=True, sort_keys=False)
    )
    return {"recipe": rpath, "lock": lock, "scenes": len(scenes)}


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: python -m scripts.ddd.split_spec <spec.yaml> [...]")
        return 2
    for arg in argv:
        raw = yaml.safe_load(Path(arg).read_text()) or {}
        if not (raw.get("scenes") or []):
            print(f"{arg}: skipped (no scenes)")
            continue
        out = split(arg)
        print(f"{arg} -> {out['recipe'].name} + {out['lock'].name} ({out['scenes']} scenes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
