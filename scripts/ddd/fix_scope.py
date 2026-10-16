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
    """``3``, ``"3"``, ``"3: title"`` and ``"scene 3"`` -> ``"3"``; unreadable -> ``None``."""
    m = re.match(r"\s*(?:scene[_ ]?)?(\d+)", str(raw if raw is not None else ""), re.I)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Scene attribution (DDD-2)
# ---------------------------------------------------------------------------
#
# A finding whose ``scene`` the loop cannot read used to make the whole next
# batch "unscoped", and an unscoped batch re-filmed EVERY scene WITH VIDEO
# (capture_scope). Measured on ACE smallholder-seasonal-programme-cascade-
# 2026-10-16-001: iterations 1 and 2 were full video re-films of all four scenes
# because the arc judge named its two findings ``scene: partner-benchmarks`` —
# the scene's spec ``id`` — which the integer-only parser could not read. Both
# were arc findings (arc_shape, visual_variety): neither could have changed a
# pixel. So: resolve ids / titles / slugs to the 1-based index the rest of the
# loop speaks, and put what is still unreadable in a GLOBAL bucket that decides
# its capture by what the fix TOUCHES, not by the missing scene.


def scene_index_map(scenes: list | dict | None) -> dict[str, int]:
    """``{lowercased id / title / title-slug: 1-based index}`` for a spec's scenes.

    Accepts the spec's ``scenes`` list or the ``{index: scene}`` mapping
    :func:`scripts.ddd.judge_scope.spec_scenes` returns. A name two scenes share
    maps to neither (an ambiguous name is not an attribution).
    """
    from scripts.ddd.identity import scene_id, slugify

    if isinstance(scenes, dict):
        items = sorted((int(k), v) for k, v in scenes.items())
    else:
        items = list(enumerate(scenes or [], start=1))
    seen: dict[str, set[int]] = {}
    for idx, sc in items:
        if not isinstance(sc, dict):
            continue
        names = {scene_id(sc), str(sc.get("id") or ""), str(sc.get("title") or ""), slugify(str(sc.get("title") or ""))}
        for n in names:
            n = n.strip().lower()
            if n:
                seen.setdefault(n, set()).add(idx)
    return {n: next(iter(ix)) for n, ix in seen.items() if len(ix) == 1}


def resolve_scene(raw: object, index: dict[str, int] | None) -> int | None:
    """The 1-based scene a finding's ``scene`` names: a number, a spec id, a title
    or its slug (``"partner-benchmarks"``, ``"Partner benchmarks"``)."""
    k = scene_key(raw)
    if k is not None:
        return int(k)
    if not index or raw is None:
        return None
    from scripts.ddd.identity import slugify

    text = str(raw).strip()
    for cand in (text.lower(), slugify(text)):
        if cand in index:
            return index[cand]
    return None


def attribute_scenes(findings: list[dict] | None, scenes: list | dict | None) -> list[dict]:
    """Findings with ``scene`` rewritten to its 1-based index where it resolves.

    The original value is kept as ``scene_ref`` so a reader can see what the
    judge wrote. Unresolvable scenes are left as they were (they go to the
    global bucket of :func:`batch_plan`). Returns new dicts.
    """
    index = scene_index_map(scenes)
    out: list[dict] = []
    for f in findings or []:
        if not isinstance(f, dict):
            out.append(f)
            continue
        raw = f.get("scene")
        if raw in (None, "") or scene_key(raw) is not None:
            out.append(f)
            continue
        idx = resolve_scene(raw, index)
        if idx is None:
            out.append(f)
            continue
        g = dict(f)
        g["scene_ref"] = raw
        g["scene"] = idx
        out.append(g)
    return out


#: Judges / sources whose scene-less findings are about the SEQUENCE or the
#: words — the arc judge, the narrative guard, the why-brief. Their fix edits the
#: spec or the why-brief, never a frame.
_GLOBAL_WORD_SOURCES = frozenset({"arc", "arc_eval", "ddd-arc-eval", "narrative", "narrative_guard", "why"})
_ARC_DIMENSIONS = frozenset({"arc_shape", "escalation", "visual_variety", "persona_coherence", "opening_and_close"})


def global_kind(finding: dict) -> str:
    """For a finding with no readable scene: ``words`` (arc / narrative / why-brief
    — re-judge the arc and the scenes whose spec moved, film nothing) or
    ``product`` (a shared-template change — stills of every scene, then the
    impact fingerprint decides which actually changed)."""
    dim = str(finding.get("dimension") or "").strip().lower()
    src = str(finding.get("source") or finding.get("judge") or "").strip().lower()
    if dim in _ARC_DIMENSIONS or src in _GLOBAL_WORD_SOURCES:
        # An arc finding can still ask for a product change ("add a panel");
        # only the words-side ones are free.
        return "product" if classify(finding) == PRODUCT else "words"
    return "product" if lands_in_product(finding) else "words"


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


# Routes whose mechanical fix edits the spec or the why-brief, never product code.
_SPEC_ROUTES = frozenset({"CONCEPT", "RESEARCH"})


def lands_in_product(finding: dict) -> bool:
    """True when applying this finding changes product code (so it needs a deploy)."""
    if str(finding.get("route") or "PRODUCT").upper() in _SPEC_ROUTES:
        return False
    return classify(finding) not in (RECIPE, NARRATIVE)


def batch_plan(findings: list[dict] | None, *, for_iteration: int | None = None) -> dict | None:
    """What the next ``continue`` batch touches, and what the next pass may skip.

    ``scope`` is ``recipe`` when no actionable mechanical finding lands in product
    code (recipe framing, narration, why-brief) — nothing to merge into the
    product, so nothing to wait on in CI or deploy, and only the scenes it edits
    need a fresh judge (``judge_scenes``). ``None`` when there is no actionable
    mechanical finding at all.

    ``scenes`` is every scene the batch names; ``product_scenes`` only those a
    PRODUCT fix names (what :mod:`scripts.ddd.capture_scope` reports as edited
    on screen).

    Findings with no readable scene go to ``global``
    (``{findings, words, product, judges}``) instead of poisoning the batch:

    * ``words`` (arc / narrative / why-brief): film nothing for them; the arc
      judge re-runs (``global.judges``) and the fingerprint re-judges whichever
      scenes' spec moved.
    * ``product`` (a shared template, a global stylesheet): ``unscoped`` is set —
      any scene may have changed on screen — and the capture takes STILLS of
      every scene so the impact fingerprint can tell which did. Never a video
      re-film: a scene-less finding is not a reason to re-record the demo.
    """
    actionable = [
        f
        for f in findings or []
        if isinstance(f, dict)
        and f.get("route") != "DEFER"
        and f.get("fix_kind") == "mechanical"
        and not f.get("parked")
    ]
    if not actionable:
        return None
    product = [f for f in actionable if lands_in_product(f)]
    research = [f for f in actionable if str(f.get("route") or "").upper() == "RESEARCH"]
    keys = [scene_key(f.get("scene")) for f in actionable]
    scenes = sorted({int(k) for k in keys if k is not None})
    product_scenes = sorted(
        {int(k) for f in product if (k := scene_key(f.get("scene"))) is not None}
    )
    scene_less = [f for f, k in zip(actionable, keys) if k is None]
    kinds = [global_kind(f) for f in scene_less]
    global_words = kinds.count("words")
    global_product = kinds.count("product")
    plan: dict = {
        "for_iteration": for_iteration,
        "scope": PRODUCT if product else RECIPE,
        "findings": len(actionable),
        "product_findings": len(product),
        "scenes": scenes,
        "product_scenes": product_scenes,
        # A PRODUCT finding with no readable scene may have changed any scene on
        # screen. A scene-less arc / narrative finding cannot — it is in ``global``.
        "unscoped": bool(global_product),
        "global": {
            "findings": len(scene_less),
            "words": global_words,
            "product": global_product,
            # The arc judge reads the whole sequence: a scene-less finding about
            # it (or about the words) is answered by re-running it.
            "judges": ["arc"] if scene_less else [],
        },
        "deploy": bool(product),
        # A recipe-only batch re-judges only the scenes it edits; every other
        # scene keeps its ledger cells even when a reseed moved its frame bytes.
        # None = scope by fingerprint as usual (a why-brief edit is run-wide, and
        # a scene-less words fix may have edited any scene's spec — the
        # fingerprint sees exactly which).
        "judge_scenes": (
            scenes
            if not product and not scene_less and scenes and not research
            else None
        ),
    }
    return plan


__all__ = [
    "NARRATIVE",
    "PRODUCT",
    "RECIPE",
    "UNKNOWN",
    "attribute_scenes",
    "batch_plan",
    "classify",
    "global_kind",
    "lands_in_product",
    "recipe_caps",
    "resolve_scene",
    "scene_index_map",
    "scene_key",
]
