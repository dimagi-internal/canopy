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


def test_no_module_references_narrative_synced_fields():
    offenders = []
    for path in (ROOT / "scripts").rglob("*.py"):
        if "narrative_synced" in path.read_text():
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], f"sync stamps still referenced in: {offenders}"


def test_unified_spec_no_longer_carries_sync_stamps():
    from scripts.ddd.schemas.models import UnifiedSpec

    fields = set(UnifiedSpec.model_fields)
    assert not (fields & {
        "narrative_synced_version", "narrative_synced_hash", "narrative_synced_at",
    })
