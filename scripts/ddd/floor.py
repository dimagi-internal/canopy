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

    python -m scripts.ddd.floor show <run_dir>
    python -m scripts.ddd.floor decline <run_dir> --match "<text>" --reason "<why>" \
        [--scene N] [--dimension D]
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
    return [d for d in (data if isinstance(data, list) else []) if isinstance(d, dict) and d.get("match")]


def _declined_by(f: dict, declined: list[dict]) -> dict | None:
    text = f"{f.get('detail') or ''} {f.get('fix_recommendation') or ''}".lower()
    for d in declined:
        if d.get("dimension") and d["dimension"] != f.get("dimension"):
            continue
        if d.get("scene") not in (None, "") and _finding_scene(f) not in (None, _scene_key(d["scene"])):
            continue
        if str(d["match"]).lower() in text:
            return d
    return None


def edit_scope(
    f: dict,
    *,
    narrative_locked: bool = False,
    fixed_surfaces: tuple[str, ...] | list[str] = (),
    declined: list[dict] | None = None,
) -> tuple[str, str]:
    """``(IN | OUT, reason)``: can THIS loop make the finding's fix?

    OUT when the finding is ``route: DEFER``; carries ``edit_scope: out`` or a
    ``fix_scope`` of data / registry / external / upstream / seed; tags its fix
    ``[DATA]``, ``[REGISTRY]``, ``[EXTERNAL]``, ``[UPSTREAM]``, ``[SEED]``; is a
    NARRATION fix (``[NARRATION]``, ``route: NARRATION``, ``fix_scope:
    narrative``) under a locked narrative; names a configured
    ``loop.fixed_surfaces`` pattern; or a fixer declined it as outside the loop's
    edit scope (``out_of_scope.json``). Everything else is IN — the loop only
    stops on evidence that it cannot act.
    """
    rec = f"{f.get('fix_recommendation') or ''}"
    text = f"{f.get('detail') or ''} {rec}"
    route = str(f.get("route") or "PRODUCT").upper()
    if route == "DEFER":
        return OUT, "route DEFER (advisory; the loop never acts on it)"
    if str(f.get("edit_scope") or "").lower() == OUT:
        return OUT, str(f.get("edit_scope_reason") or "marked edit_scope: out")
    scope = str(f.get("fix_scope") or "").lower()
    if scope in _OUT_SCOPES:
        return OUT, f"fix_scope {scope}"
    m = _TAG_OUT.search(rec)
    if m:
        return OUT, f"fix tagged [{m.group(1).upper()}]"
    narration = route == "NARRATION" or scope == "narrative" or bool(_TAG_NARRATION.search(rec))
    if narration and narrative_locked:
        return OUT, "a narration edit, and the narrative is locked"
    for pat in fixed_surfaces or ():
        try:
            if re.search(pat, text, re.I):
                return OUT, f"names a fixed surface ({pat})"
        except re.error:
            continue
    hit = _declined_by(f, declined or [])
    if hit:
        return OUT, f"fixer declined: {hit.get('reason') or 'outside the edit scope'}"
    return IN, "in the loop's edit scope"


def classify_floor(
    floor: dict | None,
    findings: list[dict],
    *,
    verdicts: dict[str, Any] | None = None,
    narrative_locked: bool = False,
    fixed_surfaces: tuple[str, ...] | list[str] = (),
    declined: list[dict] | None = None,
) -> dict:
    """``{findings: [...], in_scope, out_of_scope, all_out}`` for the floor cell(s).

    ``all_out`` is True only when the floor HAS findings and every one is out of
    scope: a floor with no finding at all is not evidence the loop cannot act.
    """
    rows = []
    for f in floor_findings(floor, findings, verdicts):
        scope, reason = edit_scope(
            f, narrative_locked=narrative_locked, fixed_surfaces=fixed_surfaces, declined=declined
        )
        rows.append({**f, "edit_scope": scope, "edit_scope_reason": reason})
    out_rows = [r for r in rows if r["edit_scope"] == OUT]
    return {
        "findings": rows,
        "in_scope": len(rows) - len(out_rows),
        "out_of_scope": len(out_rows),
        "all_out": bool(rows) and len(out_rows) == len(rows),
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
    d.add_argument("--match", required=True, help="text the finding's detail/fix contains")
    d.add_argument("--reason", required=True, help="where the fix lives and who owns it")
    d.add_argument("--scene", default=None)
    d.add_argument("--dimension", default=None)
    args = ap.parse_args(argv)
    run = Path(args.run_dir)
    if args.cmd == "show":
        p = run / "run_state.yaml"
        state = yaml.safe_load(p.read_text()) if p.exists() else {}
        print(json.dumps((state or {}).get("gating_floor"), indent=1, default=str))
        return 0
    rows = load_declined(run)
    entry = {
        "match": args.match,
        "reason": args.reason,
        "scene": args.scene,
        "dimension": args.dimension,
    }
    if entry not in rows:
        rows.append(entry)
    (run / DECLINED_FILE).write_text(json.dumps(rows, indent=1) + "\n")
    print(json.dumps({"wrote": str(run / DECLINED_FILE), "declined": len(rows)}))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
