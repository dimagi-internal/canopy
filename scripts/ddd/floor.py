"""The gating FLOOR — which judge, dimension(s) and scene(s) hold the score down.

Why this module exists (canopy#780)
-----------------------------------
The loop's gating score is a minimum, so at any moment one cell holds it: a
judge, a dimension, one or a few scenes. Three measured failures on four ACE
Spark runs (2026-10-06, about 40 judged passes, none converged) all came from
the loop not knowing which cell that was:

* **Partial passes could not move the gate.** Judge tiering ran the
  user-artifact and arc judges only at checkpoints (every third batch). When the
  floor was the user-artifact judge, two of every three passes could not change
  the gating score; run ``-002``'s score moved only at full passes.
* **Fixes did not aim at the floor.** Each batch applied every mechanical
  finding, in no order, so the cell capping the score competed with dozens of polish
  nits for the fixers' attention.
* **An unfixable floor did not stop the loop.** In run ``-004`` the floor was a
  registry text string the loop may not edit (user-artifact clarity 2); the
  loop iterated on to its stall rule anyway.

This module answers "where is the floor?" (:func:`locate`), "which findings are
about it?" (:func:`floor_findings`), and "can this loop fix them at all?"
(:func:`edit_scope`). :mod:`scripts.ddd.judge_scope` uses the first to re-judge
the floor every pass; :func:`scripts.ddd.run_pipeline.compute_auto_iterate` uses
the other two to order the next batch floor-first and to stop with
``stop_out_of_scope`` when every floor finding is outside the loop's reach.

Judge semantics and convergence thresholds are untouched: this decides what to
re-judge and what to fix first, never what a score is.

It also holds the per-cell view (DDD-1): every finding gets a stable id
(:func:`assign_ids`) and an edit scope with its provenance (:func:`scope_detail`
— a configured ``loop.fixed_surfaces`` entry names an owner and issue), and
:func:`classify_cells` splits the gating cells into PINNED (below the bar, every
finding out of scope — reported and parked), WORKABLE and PASSING, so the loop
optimizes what it can move and reports the rest instead of stopping on it.

    python -m scripts.ddd.floor show <run_dir>
    python -m scripts.ddd.floor decline <run_dir> --finding-id <id> --reason "<why>" \
        [--owner WHO] [--issue owner/repo#N]
    python -m scripts.ddd.floor decline <run_dir> --match "<text>" --reason "<why>" \
        [--scene N] [--dimension D]                   # text fallback
        # a fixer found a finding's fix outside the loop's edit scope
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

#: Verdict key (``discover_extra_verdicts`` / assemble) -> the judge name the
#: judge scope dispatches.
JUDGE_OF = {"concept": "concept", "user_artifact": "user", "arc": "arc"}
VERDICT_FILE = {"concept": "verdict-concept.yaml", "user": "verdict-user.yaml", "arc": "verdict-arc.yaml"}
#: Dimensions recorded but excluded from a judge's overall score.
ADVISORY_DIMENSIONS = frozenset({"claim_reality_coherence"})
DECLINED_FILE = "out_of_scope.json"

IN = "in"
OUT = "out"

_TAG_OUT = re.compile(r"\[\s*(DATA|REGISTRY|EXTERNAL|UPSTREAM|SEED|THIRD[- ]PARTY)\b", re.I)
_TAG_NARRATION = re.compile(r"\[\s*NARRATION\b", re.I)
_OUT_SCOPES = frozenset({"data", "registry", "external", "upstream", "seed"})


# ---------------------------------------------------------------------------
# Per-scene scores, from whatever shape the judge wrote
# ---------------------------------------------------------------------------


def _num(v: Any) -> float | None:
    if isinstance(v, dict):
        v = v.get("score")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _scene_key(raw: Any) -> int | None:
    m = re.match(r"\s*(?:scene[_ ]?)?(\d+)", str(raw if raw is not None else ""), re.I)
    return int(m.group(1)) if m else None


_MIN_OVER = re.compile(r"scores by scene:\s*([^)]*)\)", re.I)
_SCENE_SCORE = re.compile(r"s(\d+)\s*=\s*(\d+(?:\.\d+)?)")
_MIN_SCENES = re.compile(r"min(?:imum)?\s+(?:over\s+scenes\s*\[([\d,\s]+)\]|is\s+scenes?\s+([\d,\sand]+))", re.I)


def scene_scores(raw: dict | None) -> dict[int, dict[str, float]]:
    """``{scene: {dimension: score}}`` from a verdict file's per-scene rows.

    The user-artifact judge's output shape varies by emitter — ``per_scene``
    mapping, a ``scenes`` list with a ``dimensions`` mapping or flat dimension
    keys, or ``scores``; the concept verdict states it in each dimension's
    justification (``min over scenes [5] (scores by scene: s1=4, ...)``). All
    are read; unknown shapes give ``{}`` (the floor's scenes are then unknown,
    which every caller treats as "any scene").
    """
    out: dict[int, dict[str, float]] = {}
    if not isinstance(raw, dict):
        return out
    dims = set((raw.get("dimensions") or {}).keys())

    def put(scene: Any, dim: str, val: Any) -> None:
        k, n = _scene_key(scene), _num(val)
        if k is not None and n is not None:
            out.setdefault(k, {})[str(dim)] = n

    per = raw.get("per_scene")
    if isinstance(per, dict):
        for scene, row in per.items():
            if isinstance(row, dict):
                for d, v in row.items():
                    if d in dims or not dims:
                        put(scene, d, v)
    scenes = raw.get("scenes")
    if isinstance(scenes, dict):
        # ``scenes: {'1': {scene, dimensions: {dim: {score}}}}`` — the user-artifact
        # judge's shape on the 2026-10-15 ACE runs. Unread, the floor's scenes were
        # unknown (``scenes: []``) on every pass of noora-health/20261015-1129.
        scenes = [
            {**row, "scene": row.get("scene", key)} if isinstance(row, dict) else row
            for key, row in scenes.items()
        ]
    if isinstance(scenes, list):
        for row in scenes:
            if not isinstance(row, dict):
                continue
            scene = row.get("scene", row.get("scene_index"))
            for source in (row.get("dimensions"), row.get("scores"), row):
                if not isinstance(source, dict):
                    continue
                for d, v in source.items():
                    if d in dims:
                        put(scene, d, v)
    if not out:
        for d, entry in (raw.get("dimensions") or {}).items():
            just = str((entry or {}).get("justification") or "") if isinstance(entry, dict) else ""
            m = _MIN_OVER.search(just)
            if m:
                for s, v in _SCENE_SCORE.findall(m.group(1)):
                    put(s, d, v)
    return out


def _floor_scenes_from_text(entry: dict) -> list[int]:
    just = str(entry.get("justification") or "")
    m = _MIN_SCENES.search(just)
    if not m:
        m2 = re.search(r"minimum is scene\s+(\d+)", just, re.I)
        return [int(m2.group(1))] if m2 else []
    return sorted({int(x) for x in re.findall(r"\d+", m.group(1) or m.group(2) or "")})


# ---------------------------------------------------------------------------
# Locate
# ---------------------------------------------------------------------------


def _dim_scores(verdict: Any) -> dict[str, float]:
    out: dict[str, float] = {}
    for d, entry in (getattr(verdict, "dimensions", None) or {}).items():
        if d in ADVISORY_DIMENSIONS:
            continue
        score = getattr(entry, "score", None)
        if score is None and isinstance(entry, dict):
            score = entry.get("score")
        n = _num(score)
        if n is not None:
            out[d] = n
    return out


def _dim_entry(verdict: Any, dim: str) -> dict:
    entry = (getattr(verdict, "dimensions", None) or {}).get(dim)
    if entry is None:
        return {}
    if isinstance(entry, dict):
        return entry
    try:
        return entry.model_dump()
    except AttributeError:
        return {}


def locate(
    verdicts: dict[str, Any],
    *,
    run_dir: str | Path | None = None,
    objective: str | None = None,
) -> dict | None:
    """The cell(s) holding the gating score down, or ``None``.

    ``verdicts`` maps verdict keys (``concept``, ``user_artifact``, ``arc`` …) to
    loaded Verdicts; only gating ones count. ``demo`` objective: the floor is
    the lowest ``overall_score`` and, within that judge, its lowest dimension(s).
    ``product`` objective: the weakest PRODUCT dimension across the gating
    judges (:func:`scripts.ddd.objective.product_score`). Scenes come from the
    judge's per-scene rows in ``run_dir`` (empty when unknown).
    """
    gating = {
        k: v for k, v in verdicts.items()
        if k in JUDGE_OF and getattr(v, "gate", None) == "gating" and v is not None
    }
    if not gating:
        return None
    cells: list[dict] = []
    if objective == "product":
        from scripts.ddd.objective import PRODUCT_DIMENSIONS

        scored = [
            (s, k, d)
            for k, v in gating.items()
            for d, s in _dim_scores(v).items()
            if d in PRODUCT_DIMENSIONS
        ]
        if not scored:
            return None
        score = min(s for s, _, _ in scored)
        for s, k, d in scored:
            if s == score:
                cells.append({"verdict": k, "judge": JUDGE_OF[k], "dimension": d})
    else:
        score = min(float(v.overall_score) for v in gating.values())
        for k, v in gating.items():
            if float(v.overall_score) != score:
                continue
            dims = _dim_scores(v)
            if not dims:
                cells.append({"verdict": k, "judge": JUDGE_OF[k], "dimension": None})
                continue
            low = min(dims.values())
            for d, s in dims.items():
                if s == low:
                    cells.append({"verdict": k, "judge": JUDGE_OF[k], "dimension": d})
    run = Path(run_dir) if run_dir else None
    raw_cache: dict[str, dict | None] = {}
    for cell in cells:
        judge = cell["judge"]
        if judge == "arc":
            cell["scenes"] = []  # the arc judges the whole sequence
            continue
        if run is not None and judge not in raw_cache:
            p = run / VERDICT_FILE[judge]
            try:
                raw_cache[judge] = yaml.safe_load(p.read_text()) if p.exists() else None
            except yaml.YAMLError:
                raw_cache[judge] = None
        per = scene_scores(raw_cache.get(judge))
        dim = cell["dimension"]
        rows = {s: r[dim] for s, r in per.items() if dim in r}
        if rows:
            low = min(rows.values())
            cell["scenes"] = sorted(s for s, v in rows.items() if v == low)
        else:
            cell["scenes"] = _floor_scenes_from_text(_dim_entry(gating[cell["verdict"]], dim)) if dim else []
    return {
        "score": float(score),
        "objective": objective or "demo",
        "cells": cells,
        "judges": sorted({c["judge"] for c in cells}),
        "dimensions": sorted({c["dimension"] for c in cells if c["dimension"]}),
        "scenes": sorted({s for c in cells for s in c.get("scenes") or []}),
    }


# ---------------------------------------------------------------------------
# Floor findings + edit scope
# ---------------------------------------------------------------------------


def _finding_scene(f: dict) -> int | None:
    return _scene_key(f.get("scene")) if f.get("scene") not in (None, "") else None


def floor_findings(floor: dict | None, findings: list[dict], verdicts: dict[str, Any] | None = None) -> list[dict]:
    """The findings that name the floor cell(s): same dimension, same scene.

    A finding with no scene, or a floor cell whose scenes are unknown, matches on
    dimension alone. When a floor cell has no finding at all but its verdict
    dimension carries a ``fix_recommendation`` (the user-artifact judge's
    per-dimension fix), that recommendation stands in as the cell's finding.
    """
    if not floor:
        return []
    out: list[dict] = []
    for cell in floor.get("cells") or []:
        dim = cell.get("dimension")
        scenes = set(cell.get("scenes") or [])
        hits = [
            f for f in findings
            if isinstance(f, dict)
            and dim is not None
            and f.get("dimension") == dim
            and (not scenes or _finding_scene(f) is None or _finding_scene(f) in scenes)
        ]
        if not hits and dim and verdicts and cell.get("verdict") in verdicts:
            entry = _dim_entry(verdicts[cell["verdict"]], dim)
            if entry.get("fix_recommendation"):
                hits = [
                    {
                        "scene": ",".join(str(s) for s in sorted(scenes)) or None,
                        "dimension": dim,
                        "route": "PRODUCT",
                        "fix_kind": entry.get("fix_kind") or "mechanical",
                        "detail": entry.get("justification") or "",
                        "fix_recommendation": entry.get("fix_recommendation"),
                        "source": f"{cell['judge']}_dimension",
                    }
                ]
        for f in hits:
            if f not in out:
                out.append(f)
    return out


def load_declined(run_dir: str | Path | None) -> list[dict]:
    if run_dir is None:
        return []
    p = Path(run_dir) / DECLINED_FILE
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return [
        d for d in (data if isinstance(data, list) else [])
        if isinstance(d, dict) and (d.get("match") or d.get("finding_id"))
    ]


def _declined_by(f: dict, declined: list[dict]) -> dict | None:
    """The decline that covers ``f``: by stable ``finding_id`` first, then by text.

    The id match is what keeps a decline closed when the judge re-words the same
    defect next pass (text matching alone needed 145 entries on noora).
    """
    fid = f.get("finding_id")
    if fid:
        for d in declined:
            if d.get("finding_id") and d["finding_id"] == fid:
                return d
    text = f"{f.get('detail') or ''} {f.get('fix_recommendation') or ''}".lower()
    for d in declined:
        if not d.get("match"):
            continue
        if d.get("dimension") and d["dimension"] != f.get("dimension"):
            continue
        if d.get("scene") not in (None, "") and _finding_scene(f) not in (None, _scene_key(d["scene"])):
            continue
        if str(d["match"]).lower() in text:
            return d
    return None


_ISSUE_REF = re.compile(r"\b((?:[\w.-]+/)?[\w.-]+#\d+)\b")


def _issue_in(text: str | None) -> str | None:
    m = _ISSUE_REF.search(str(text or ""))
    return m.group(1) if m else None


def _surface_fields(s: Any) -> tuple[str, str, str | None, str | None]:
    """``(match, label, owner, issue)`` from a FixedSurface, a dict, or a bare regex."""
    if isinstance(s, str):
        return s, s, None, None
    if isinstance(s, dict):
        pat = str(s.get("match") or "")
        return pat, str(s.get("name") or pat), s.get("owner"), s.get("issue")
    pat = str(getattr(s, "match", "") or "")
    return pat, str(getattr(s, "name", "") or pat), getattr(s, "owner", None), getattr(s, "issue", None)


def scope_detail(
    f: dict,
    *,
    narrative_locked: bool = False,
    fixed_surfaces: tuple[str, ...] | list[str] = (),
    surfaces: tuple | list = (),
    declined: list[dict] | None = None,
    sticky: dict[str, dict] | None = None,
) -> dict:
    """``{edit_scope, reason, via, surface, owner, issue}`` — can THIS loop make the fix?

    OUT when the finding is ``route: DEFER``; carries ``edit_scope: out`` or a
    ``fix_scope`` of data / registry / external / upstream / seed; tags its fix
    ``[DATA]``, ``[REGISTRY]``, ``[EXTERNAL]``, ``[UPSTREAM]``, ``[SEED]``; is a
    NARRATION fix (``[NARRATION]``, ``route: NARRATION``, ``fix_scope:
    narrative``) under a locked narrative; names a configured fixed surface
    (``surfaces`` — with owner/issue — or a bare ``fixed_surfaces`` regex); a
    fixer declined it (``out_of_scope.json``, by finding id or text); or its
    stable ``finding_id`` was out of scope on an earlier pass (``sticky``: a
    re-worded finding does not re-open). Everything else is IN — the loop only
    stops on evidence that it cannot act.

    ``via`` names the mechanism (``route``, ``marked``, ``fix_scope``, ``tag``,
    ``narration``, ``surface``, ``decline``, ``sticky``) so the blocked report can
    tell configuration from after-the-fact text matching.
    """

    def out(reason: str, via: str, surface: str | None = None, owner: Any = None, issue: Any = None) -> dict:
        return {
            "edit_scope": OUT,
            "reason": reason,
            "via": via,
            "surface": surface,
            "owner": owner,
            "issue": issue or _issue_in(reason),
        }

    rec = f"{f.get('fix_recommendation') or ''}"
    text = f"{f.get('detail') or ''} {rec}"
    route = str(f.get("route") or "PRODUCT").upper()
    if route == "DEFER":
        return out("route DEFER (advisory; the loop never acts on it)", "route")
    if str(f.get("edit_scope") or "").lower() == OUT:
        # Already classified (this pass) or marked by its emitter: keep the provenance.
        return out(
            str(f.get("edit_scope_reason") or "marked edit_scope: out"),
            str(f.get("edit_scope_via") or "marked"),
            f.get("surface"),
            f.get("owner"),
            f.get("issue"),
        )
    # A configured surface first: it is the one mechanism that also says WHO
    # owns the fix, so it should name the reason even when a tag would also match.
    named = {_surface_fields(x)[0] for x in surfaces or ()}
    for s in [*(surfaces or ()), *(p for p in fixed_surfaces or () if p not in named)]:
        pat, label, owner, issue = _surface_fields(s)
        if not pat:
            continue
        try:
            if re.search(pat, text, re.I):
                who = f"; owner {owner}" if owner else ""
                return out(f"names a fixed surface ({label}{who})", "surface", label, owner, issue)
        except re.error:
            continue
    scope = str(f.get("fix_scope") or "").lower()
    if scope in _OUT_SCOPES:
        return out(f"fix_scope {scope}", "fix_scope")
    m = _TAG_OUT.search(rec)
    if m:
        return out(f"fix tagged [{m.group(1).upper()}]", "tag")
    narration = route == "NARRATION" or scope == "narrative" or bool(_TAG_NARRATION.search(rec))
    if narration and narrative_locked:
        return out("a narration edit, and the narrative is locked", "narration")
    hit = _declined_by(f, declined or [])
    if hit:
        return out(
            f"fixer declined: {hit.get('reason') or 'outside the edit scope'}",
            "decline",
            hit.get("surface"),
            hit.get("owner"),
            hit.get("issue"),
        )
    prior = (sticky or {}).get(str(f.get("finding_id") or ""))
    if prior:
        return out(
            f"out of scope since iteration {prior.get('since')} (same finding, re-worded): {prior.get('reason')}",
            "sticky",
            prior.get("surface"),
            prior.get("owner"),
            prior.get("issue"),
        )
    return {"edit_scope": IN, "reason": "in the loop's edit scope", "via": None, "surface": None, "owner": None, "issue": None}


def edit_scope(
    f: dict,
    *,
    narrative_locked: bool = False,
    fixed_surfaces: tuple[str, ...] | list[str] = (),
    declined: list[dict] | None = None,
    surfaces: tuple | list = (),
    sticky: dict[str, dict] | None = None,
) -> tuple[str, str]:
    """``(IN | OUT, reason)`` — :func:`scope_detail` without the provenance."""
    d = scope_detail(
        f,
        narrative_locked=narrative_locked,
        fixed_surfaces=fixed_surfaces,
        surfaces=surfaces,
        declined=declined,
        sticky=sticky,
    )
    return d["edit_scope"], d["reason"]


def classify_floor(
    floor: dict | None,
    findings: list[dict],
    *,
    verdicts: dict[str, Any] | None = None,
    narrative_locked: bool = False,
    fixed_surfaces: tuple[str, ...] | list[str] = (),
    declined: list[dict] | None = None,
    surfaces: tuple | list = (),
    sticky: dict[str, dict] | None = None,
) -> dict:
    """``{findings: [...], in_scope, out_of_scope, all_out}`` for the floor cell(s).

    ``all_out`` is True only when the floor HAS findings and every one is out of
    scope: a floor with no finding at all is not evidence the loop cannot act.
    """
    rows = []
    for f in floor_findings(floor, findings, verdicts):
        d = scope_detail(
            f,
            narrative_locked=narrative_locked,
            fixed_surfaces=fixed_surfaces,
            surfaces=surfaces,
            declined=declined,
            sticky=sticky,
        )
        rows.append({**f, "edit_scope": d["edit_scope"], "edit_scope_reason": d["reason"]})
    out_rows = [r for r in rows if r["edit_scope"] == OUT]
    return {
        "findings": rows,
        "in_scope": len(rows) - len(out_rows),
        "out_of_scope": len(out_rows),
        "all_out": bool(rows) and len(out_rows) == len(rows),
    }


# ---------------------------------------------------------------------------
# Stable finding ids
# ---------------------------------------------------------------------------
#
# A judge re-words the same defect every pass ("The floating 'Ask an agent'
# button occludes footer text" -> "'Ask an agent' floating button overlaps the
# Doll demonstration sparkline"). Keyed on text, a decline re-opens on every
# re-wording; on noora-health/20261015-1129 ``out_of_scope.json`` grew to 145
# text matches for a handful of defects. An id is assigned per (scene,
# dimension) cell by content overlap with the findings the ledger already
# knows, so a re-worded finding keeps its id — and with it its decline.

_STOP = frozenset(
    "this that with from into onto than then they them their there these those "
    "when what which while where your have will would should could also only "
    "each every more most less very same such over under scene frame page".split()
)
_WORD = re.compile(r"[a-z0-9][a-z0-9'’-]*")
#: Share of the SMALLER token set two findings on one cell must have in common
#: (overlap coefficient) to be the same finding, with at least ID_MIN_SHARED words.
#: Calibrated on noora's re-wordings of one defect ("floating 'Ask an agent'
#: button occludes footer text" / "... overlaps the sparkline"): ~0.45 overlap,
#: 6 shared words; unrelated findings on the same cell share 0-2.
ID_OVERLAP = 0.4
ID_MIN_SHARED = 4


def finding_tokens(f: dict) -> set[str]:
    text = f"{f.get('detail') or ''} {f.get('fix_recommendation') or ''}".lower()
    return {w.strip("'’-") for w in _WORD.findall(text) if len(w) >= 4 and w not in _STOP}


def _cell_slug(f: dict) -> str:
    scene = _finding_scene(f)
    return f"s{scene if scene is not None else 'x'}.{f.get('dimension') or 'none'}"


def assign_ids(findings: list[dict], ledger: list[dict] | None, *, iteration: int | None = None) -> list[dict]:
    """Stamp ``finding_id`` on every finding; return the updated ledger.

    A finding the judge already gave an ``id``/``finding_id`` keeps it. Otherwise
    it inherits the id of the ledger entry on the same cell whose tokens it shares
    most (overlap coefficient >= :data:`ID_OVERLAP`, >= :data:`ID_MIN_SHARED`
    words), each entry claimed at most once per pass; else it gets a new id
    ``s<scene>.<dimension>.<n>``.
    """
    ledger = [dict(e) for e in ledger or [] if isinstance(e, dict) and e.get("id")]
    by_id = {e["id"]: e for e in ledger}
    claimed: set[str] = set()
    for f in findings or []:
        if not isinstance(f, dict):
            continue
        toks = finding_tokens(f)
        slug = _cell_slug(f)
        fid = str(f.get("finding_id") or f.get("id") or "").strip() or None
        if fid is None:
            best, best_score = None, 0.0
            for e in ledger:
                if e.get("cell") != slug or e["id"] in claimed:
                    continue
                prior = set(e.get("tokens") or [])
                shared = len(toks & prior)
                if shared < ID_MIN_SHARED:
                    continue
                score = shared / max(1, min(len(toks), len(prior)))
                if score >= ID_OVERLAP and score > best_score:
                    best, best_score = e, score
            if best is not None:
                fid = best["id"]
            else:
                n = 1 + sum(1 for e in ledger if e.get("cell") == slug)
                fid = f"{slug}.{n}"
                while fid in by_id:
                    n += 1
                    fid = f"{slug}.{n}"
        claimed.add(fid)
        f["finding_id"] = fid
        entry = by_id.get(fid)
        if entry is None:
            entry = {"id": fid, "cell": slug, "first_iteration": iteration}
            ledger.append(entry)
            by_id[fid] = entry
        entry["tokens"] = sorted(toks)[:60]
        entry["last_iteration"] = iteration
    return ledger


# ---------------------------------------------------------------------------
# Per-cell floor: PINNED vs WORKABLE
# ---------------------------------------------------------------------------
#
# The gate is a minimum, so one cell the loop may not edit pins the TRUE score
# forever. Before this, the loop either kept iterating on a pinned score until
# the stall rule (noora: six passes at 2.0, the floor held by a labs-template
# defect the loop may not touch, ace#2337) or stopped the whole run as soon as
# the floor cell was out of scope (smallholder: ``blocked_out_of_scope`` at
# iteration 3, though other cells still had work). A pinned cell is now
# reported and parked — removed from the WORKABLE objective — and the loop keeps
# optimizing the rest. Both scores are reported; convergence is unchanged (every
# gating judge >= the bar on the true score).

PINNED = "pinned"
WORKABLE = "workable"
PASSING = "passing"
#: A cell at or above this is not holding anything down.
CELL_BAR = 4.0


def cell_key(judge: str, scene: Any, dimension: str) -> str:
    return f"{judge}:{scene if scene is not None else '*'}:{dimension}"


def cell_table(
    verdicts: dict[str, Any],
    *,
    run_dir: str | Path | None = None,
    distribution: dict | None = None,
) -> list[dict]:
    """Every gating (judge, scene, dimension) cell readable from the verdicts.

    Per-scene rows come from the verdict files (:func:`scene_scores`) and, for
    the concept judge, the ``distribution.capping_cells`` (CONFIRMED values). A
    dimension whose lowest scene is unknown gets one residual cell (``scene:
    None``) at the dimension's score; when the concept rows come only from the
    capping cells, the residual is the unlisted scenes' lower bound (> the cap
    threshold) and is marked ``bound: true``.
    """
    run = Path(run_dir) if run_dir else None
    cells: list[dict] = []
    for k, v in verdicts.items():
        if k not in JUDGE_OF or v is None or getattr(v, "gate", None) != "gating":
            continue
        judge = JUDGE_OF[k]
        dims = _dim_scores(v)
        raw = None
        if run is not None and judge != "arc":
            p = run / VERDICT_FILE[judge]
            try:
                raw = yaml.safe_load(p.read_text()) if p.exists() else None
            except yaml.YAMLError:
                raw = None
        per = scene_scores(raw) if judge != "arc" else {}
        from_caps: set[str] = set()
        if judge == "concept":
            dist = distribution
            if dist is None and isinstance(raw, dict):
                dist = raw.get("distribution")
            for c in (dist or {}).get("capping_cells") or []:
                if not isinstance(c, dict):
                    continue
                s, d = _scene_key(c.get("scene")), str(c.get("dimension") or "")
                n = _num(c.get("confirmed", c.get("score")))
                if s is None or not d or n is None or d not in dims:
                    continue
                if d not in {dd for r in per.values() for dd in r}:
                    from_caps.add(d)
                per.setdefault(s, {})[d] = n
        for d, score in sorted(dims.items()):
            rows = {s: r[d] for s, r in per.items() if d in r}
            for s, val in sorted(rows.items()):
                cells.append({"judge": judge, "verdict": k, "scene": s, "dimension": d, "score": float(val)})
            if not rows:
                cells.append({"judge": judge, "verdict": k, "scene": None, "dimension": d, "score": float(score)})
            elif d in from_caps:
                cells.append({
                    "judge": judge, "verdict": k, "scene": None, "dimension": d,
                    "score": max(float(score), 3.0), "bound": True,
                })
            elif min(rows.values()) > float(score) + 1e-9:
                cells.append({"judge": judge, "verdict": k, "scene": None, "dimension": d, "score": float(score)})
    for c in cells:
        c["key"] = cell_key(c["judge"], c["scene"], c["dimension"])
    return cells


def _finding_judges(f: dict, dim_judges: dict[str, set[str]]) -> set[str]:
    judges = dim_judges.get(str(f.get("dimension") or ""), set())
    if len(judges) <= 1:
        return judges
    src = str(f.get("source") or "")
    if src == "user_artifact" and "user" in judges:
        return {"user"}
    return judges - {"user"} or judges


def classify_cells(
    cells: list[dict],
    findings: list[dict],
    *,
    bar: float = CELL_BAR,
    dimensions: set[str] | None = None,
) -> dict:
    """``{cells, pinned, workable, workable_score, unattached}`` — the per-cell floor.

    Each finding attaches to the cell of its judge (by dimension), scene and
    dimension; a finding whose scene has no row attaches to the dimension's
    residual cell. A cell below ``bar`` is PINNED when it has findings and every
    one is ``edit_scope: out`` — a cell with no finding is not evidence the loop
    cannot act, so it stays WORKABLE. ``workable_score`` is the minimum over every
    non-pinned cell (``dimensions`` restricts it, e.g. to the product
    dimensions); ``None`` when every scored cell is pinned. Findings are expected
    to carry ``finding_id`` and ``edit_scope`` already (see
    :func:`scripts.ddd.run_pipeline.compute_auto_iterate`).
    """
    dim_judges: dict[str, set[str]] = {}
    rows_by: dict[tuple[str, str], set[int]] = {}
    for c in cells:
        dim_judges.setdefault(c["dimension"], set()).add(c["judge"])
        if c["scene"] is not None:
            rows_by.setdefault((c["judge"], c["dimension"]), set()).add(c["scene"])
    out_cells = [dict(c, findings=[], in_scope=0, out_of_scope=0) for c in cells]
    index = {c["key"]: c for c in out_cells}
    unattached: list[str] = []
    for f in findings or []:
        if not isinstance(f, dict):
            continue
        scene = _finding_scene(f)
        placed = False
        for judge in sorted(_finding_judges(f, dim_judges)):
            dim = str(f.get("dimension"))
            key = cell_key(judge, scene, dim) if scene in rows_by.get((judge, dim), set()) else cell_key(judge, None, dim)
            cell = index.get(key)
            if cell is None:
                continue
            cell["findings"].append(f)
            placed = True
            break
        if not placed and f.get("edit_scope") != OUT:
            unattached.append(str(f.get("finding_id") or ""))
    for c in out_cells:
        fs = c.pop("findings")
        outs = [f for f in fs if f.get("edit_scope") == OUT]
        c["findings"] = [str(f.get("finding_id") or "") for f in fs]
        c["in_scope"] = len(fs) - len(outs)
        c["out_of_scope"] = len(outs)
        # The best an in-scope fix can reach while an out-of-scope defect on the
        # same cell remains: the score the judge gave that defect.
        caps = [n for n in (_num(f.get("score")) for f in outs) if n is not None]
        c["ceiling"] = min(caps) if caps else None
        if c["score"] >= bar:
            c["status"] = PASSING
        elif fs and not (len(fs) - len(outs)):
            c["status"] = PINNED
            owners: list[dict] = []
            for f in outs:
                o = {
                    "owner": f.get("owner"),
                    "issue": f.get("issue"),
                    "surface": f.get("surface"),
                    "via": f.get("edit_scope_via"),
                }
                if o not in owners:
                    owners.append(o)
            c["owners"] = owners
            c["blocked_by"] = [
                {
                    "finding_id": f.get("finding_id"),
                    "text": str(f.get("fix_recommendation") or f.get("detail") or "")[:240],
                    "reason": str(f.get("edit_scope_reason") or "")[:240],
                    "owner": f.get("owner"),
                    "issue": f.get("issue"),
                }
                for f in outs
            ]
        else:
            c["status"] = WORKABLE
    scored = [c for c in out_cells if dimensions is None or c["dimension"] in dimensions]
    free = [c["score"] for c in scored if c["status"] != PINNED]
    return {
        "bar": float(bar),
        "cells": out_cells,
        "pinned": [c["key"] for c in out_cells if c["status"] == PINNED],
        "workable": [c["key"] for c in out_cells if c["status"] == WORKABLE],
        "workable_score": min(free) if free else None,
        "unattached": [u for u in unattached if u],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.ddd.floor")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show", help="print the run's last-assembled gating floor")
    s.add_argument("run_dir")
    d = sub.add_parser(
        "decline",
        help="record that a fixer found a finding's fix outside the loop's edit scope",
    )
    d.add_argument("run_dir")
    d.add_argument(
        "--finding-id",
        default=None,
        help="the finding's stable id (state.findings[].finding_id) — preferred: it "
        "survives the judge re-wording the finding next pass",
    )
    d.add_argument("--match", default=None, help="text the finding's detail/fix contains (fallback)")
    d.add_argument("--reason", required=True, help="where the fix lives and who owns it")
    d.add_argument("--owner", default=None, help="who can make the fix (printed in the blocked report)")
    d.add_argument("--issue", default=None, help="tracking issue, e.g. dimagi/ace#2337")
    d.add_argument("--scene", default=None)
    d.add_argument("--dimension", default=None)
    args = ap.parse_args(argv)
    run = Path(args.run_dir)
    if args.cmd == "show":
        p = run / "run_state.yaml"
        state = yaml.safe_load(p.read_text()) if p.exists() else {}
        print(json.dumps(
            {"gating_floor": (state or {}).get("gating_floor"), "cell_floor": (state or {}).get("cell_floor")},
            indent=1,
            default=str,
        ))
        return 0
    if not (args.finding_id or args.match):
        ap.error("decline needs --finding-id (preferred) or --match")
    rows = load_declined(run)
    entry = {
        k: v
        for k, v in {
            "finding_id": args.finding_id,
            "match": args.match,
            "reason": args.reason,
            "owner": args.owner,
            "issue": args.issue or _issue_in(args.reason),
            "scene": args.scene,
            "dimension": args.dimension,
        }.items()
        if v is not None or k in ("scene", "dimension")
    }
    if entry not in rows:
        rows.append(entry)
    (run / DECLINED_FILE).write_text(json.dumps(rows, indent=1) + "\n")
    print(json.dumps({"wrote": str(run / DECLINED_FILE), "declined": len(rows)}))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
