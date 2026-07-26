"""Scene identity — the one place a DDD scene's name is derived.

Deliberately dependency-free: the validator, the narrative gate, the spec
composer and the renderer all need to agree on what a scene is called, and
none of them should have to import the network layer to find out.

Before this module the same slug expression was written out three times
(``narrative.py`` twice, ``validate.py`` once), which is how ``build_order``
came to be VALIDATED against title-derived slugs while it was GENERATED from
scene ids — two spellings of "identity" that agreed only for as long as no
scene carried an explicit one.
"""
from __future__ import annotations

import re


def slugify(text: str) -> str:
    """Lowercase, hyphen-separated, alphanumeric-only.

    Examples:
        "Area Selection"   -> "area-selection"
        "Sample Gen (v2)"  -> "sample-gen-v2"
    """
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def scene_id(scene) -> str:
    """The stable identity of a scene — explicit ``id``, else the title slug.

    Accepts either a ``Scene`` model or the raw dict form used by the
    apply/merge paths, because both halves of the roundtrip need the SAME
    identity function. The title-slug fallback is a migration path for pre-L0
    specs, not a supported authoring mode: it is exactly what canopy-web
    already stored as ``NarrationItem.id`` for every existing narrative, so a
    spec that has not been backfilled still matches its own history.
    """
    if isinstance(scene, dict):
        explicit = (scene.get("id") or "").strip()
        title = scene.get("title") or ""
    else:
        explicit = (getattr(scene, "id", "") or "").strip()
        title = getattr(scene, "title", "") or ""
    return explicit or slugify(title)
