"""The local/cloud merge algorithm is gone — one writer per field (L1).

These functions existed only because two writers shared one YAML file, with a
content hash over five of its fields standing between the user and a merge.
With the story on canopy-web and the recipe in git there is nothing to
reconcile: you cannot diverge from something you do not duplicate.

The concrete cost of the old model, measured 2026-07-26: 68 local copies of
three narratives across two user accounts, in 10 distinct states.
"""
from __future__ import annotations

import pathlib

import scripts.ddd.narrative as narrative

ROOT = pathlib.Path(__file__).resolve().parents[2]

GONE = [
    "narrative_content_hash",
    "decide_narrative_sync",
    "merge_narrative_into_spec",
    "_NARRATIVE_SCENE_FIELDS",
]


def test_merge_functions_no_longer_exist():
    present = [n for n in GONE if hasattr(narrative, n)]
    assert present == [], f"still present: {present}"


# split_spec names the dead stamps in order to STRIP them: legacy specs on disk
# still carry them, and they must not survive into a recipe. That is the one
# legitimate reason to mention them, and it disappears once the migration has
# run everywhere.
_MAY_NAME_DEAD_STAMPS = {"scripts/ddd/split_spec.py"}


def test_no_module_references_narrative_synced_fields():
    offenders = []
    for path in (ROOT / "scripts").rglob("*.py"):
        rel = str(path.relative_to(ROOT))
        if rel in _MAY_NAME_DEAD_STAMPS:
            continue
        if "narrative_synced" in path.read_text():
            offenders.append(rel)
    assert offenders == [], f"sync stamps still referenced in: {offenders}"


def test_the_carve_out_only_strips_and_never_writes():
    """split_spec may NAME the dead stamps, but only to drop them."""
    text = (ROOT / "scripts/ddd/split_spec.py").read_text()
    for line in text.splitlines():
        if "narrative_synced" not in line:
            continue
        stripped = line.strip()
        assert stripped.startswith(('"', "#")) or stripped.startswith("narrative_synced"), (
            f"split_spec does something other than list-to-strip: {stripped!r}"
        )


def test_unified_spec_no_longer_carries_sync_stamps():
    from scripts.ddd.schemas.models import UnifiedSpec

    fields = set(UnifiedSpec.model_fields)
    assert not (fields & {
        "narrative_synced_version", "narrative_synced_hash", "narrative_synced_at",
    })
