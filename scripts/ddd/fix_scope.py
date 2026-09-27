"""Where a finding's fix LANDS — the recipe, or the product.

Why this module exists
----------------------
The first live v1-mode run (connect-labs ``supply-sophie-rutf-2026-09-26-001``,
canopy 0.2.528) stopped at iteration 4 as ``stop_concept_change`` /
``diverging`` because of ONE confirmed cap: scene 4 ``motion_friction`` 2/2/2.
The cause was the orchestrator's own recipe edit (``scroll: top`` before the
snapshot cut the narrated card off). The fix was a one-line recipe change — no
PR, no CI, no deploy — yet the loop read the capped floor as the run going
backwards and opened a human gate over it, while the mean cell and open
findings were flat within noise (M17).

A recipe (SCRIPTING) fix is the cheapest fix the loop can make: edit the
recipe, re-render, re-judge the scene. It must be made — and its scene
re-judged — before any cap it causes is allowed to open a gate or name the run
``diverging``. This module answers the one question that needs:
*does this finding's fix live in the recipe?*

Classification (``classify``)
-----------------------------
* explicit ``fix_scope: recipe | product | narrative`` on the finding wins;
* a ``[SCRIPTING`` / ``SCRIPTING`` tag in the recommendation (the concept
  rubric's own word for narrated-subject framing fixes) -> ``recipe``;
* ``motion_friction`` whose recommendation names recorder actions (scroll_to,
  snapshot, hover target, hold, framing) and names no product change ->
  ``recipe``;
* otherwise ``unknown`` — never assumed to be a recipe fix. Like
  :mod:`scripts.ddd.finding_class`, this only GRANTS the cheap path on
  evidence.
"""
from __future__ import annotations

import re

RECIPE = "recipe"
PRODUCT = "product"
NARRATIVE = "narrative"
UNKNOWN = "unknown"

EXPLICIT = (RECIPE, PRODUCT, NARRATIVE)

_SCRIPTING_TAG = re.compile(r"\bSCRIPTING\b")

# Recorder vocabulary: a fix phrased in these terms edits the recipe.
_RECIPE_SIGNALS = tuple(
    re.compile(p, re.I)
    for p in (
        r"\bscroll_to\b",
        r"\bscroll(?:\s*:\s*|\s+to\s+the\s+)(?:top|bottom|\d)",
        r"\bbefore\s+the\s+snapshot\b",
        r"\bsnapshot\b",
        r"\bhover\s+target\b",
        r"\bpark(?:-hover|\s+the\s+(?:cursor|pointer|hover))\b",
        r"\bmove\s+the\s+(?:pointer|cursor)\b",
        r"\bhold\b.*\b(?:seconds?|voice\s*over|narration)\b",
        r"\b(?:fully\s+)?in\s+frame\b",
        r"\brecipe\b",
        r"\bspec\s+edit\b",
        r"\btrim\s+the\s+clip\b",
    )
)

# Product vocabulary: any of these means the fix changes the product, even if
# recorder words also appear.
_PRODUCT_SIGNALS = tuple(
    re.compile(p, re.I)
    for p in (
        r"\bcss\b",
        r"\btemplate\b",
        r"\bendpoint\b",
        r"\bmigration\b",
        r"\bcomponent\b",
        r"\b(?:the\s+)?(?:product|page|view|app)\s+should\b",
        r"\badd\s+(?:a|an)\s+(?:button|column|field|panel|label|link|badge)\b",
        r"\brename\s+the\s+(?:button|column|field|label|heading)\b",
    )
)


def classify(finding: dict) -> str:
    """``recipe`` | ``product`` | ``narrative`` | ``unknown`` for one finding."""
    explicit = str(finding.get("fix_scope") or "").strip().lower()
    if explicit in EXPLICIT:
        return explicit
    rec = str(finding.get("fix_recommendation") or "")
    text = f"{finding.get('detail') or ''} {rec}"
    if _SCRIPTING_TAG.search(rec) or _SCRIPTING_TAG.search(text):
        return RECIPE
    if str(finding.get("dimension") or "") != "motion_friction":
        return UNKNOWN
    if any(p.search(rec) for p in _PRODUCT_SIGNALS):
        return PRODUCT
    if any(p.search(rec) for p in _RECIPE_SIGNALS):
        return RECIPE
    return UNKNOWN


def scene_key(raw: object) -> str | None:
    """``3``, ``"3"`` and ``"3: title"`` -> ``"3"``; unreadable -> ``None``."""
    m = re.match(r"\s*(\d+)", str(raw if raw is not None else ""))
    return m.group(1) if m else None


def recipe_caps(distribution: dict | None, findings: list[dict] | None) -> list[dict]:
    """Confirmed capping cells whose every open finding is a recipe fix.

    A cell qualifies when its CONFIRMED score is <= 2 and at least one non-DEFER
    finding names that (scene, dimension), every such finding is ``mechanical``
    and classifies as ``recipe``. A cell that also carries a product finding is
    excluded — re-rendering the recipe alone would not lift it.
    """
    if not distribution:
        return []
    cells = distribution.get("capping_cells")
    if not isinstance(cells, list):
        return []
    by_cell: dict[tuple[str, str], list[dict]] = {}
    for f in findings or []:
        if not isinstance(f, dict) or f.get("route") == "DEFER":
            continue
        key = (scene_key(f.get("scene")), str(f.get("dimension") or ""))
        if key[0] is None:
            continue
        by_cell.setdefault(key, []).append(f)
    out: list[dict] = []
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        try:
            confirmed = float(cell.get("confirmed", cell.get("score")))
        except (TypeError, ValueError):
            continue
        if confirmed > 2.0:
            continue
        key = (scene_key(cell.get("scene")), str(cell.get("dimension") or ""))
        attached = by_cell.get(key) or []
        if not attached:
            continue
        if all(
            f.get("fix_kind") == "mechanical" and classify(f) == RECIPE for f in attached
        ):
            out.append(
                {
                    "scene": key[0],
                    "dimension": key[1],
                    "confirmed": confirmed,
                    "fix_recommendation": attached[0].get("fix_recommendation") or "",
                }
            )
    return out


__all__ = [
    "NARRATIVE",
    "PRODUCT",
    "RECIPE",
    "UNKNOWN",
    "classify",
    "recipe_caps",
    "scene_key",
]
