"""Pre-build storyboard critique — judge the ARC before anything is built or rendered.

Why this module exists
----------------------
On the first live v1 run the arc-level story decisions — move a scene, the data
scale is too small to make the point — surfaced only at iteration 4, after six
fix batches, seven deploys and ~2.4M judge tokens had been spent polishing
frames inside that arc. The arc judge (``ddd-arc-eval``) only ever ran on
rendered screenshots, so it could not speak until the first render, and by then
the loop was committed to building the story as locked.

Most arc questions do not need pixels: scene ORDER, whether each scene EARNS
its place, and whether the seeded DATA is at the scale and realism that makes a
scene's point can all be read off the locked narrative plus the seed. So
``ddd-arc-eval`` has a storyboard mode that runs on the locked narrative, in
parallel with the gap walk and before the first build, and writes
``<run_dir>/storyboard.json``::

    {
      "narrative_slug": "supply-sophie-rutf",
      "critiqued_at": "2026-09-27T12:00:00Z",
      "one_sentence_story": "Sophie ...",
      "findings": [
        {"scenes": [6], "dimension": "earns_place", "kind": "scope",
         "detail": "Scene 6 (public listing) interrupts the rise from ranking to award.",
         "fix_recommendation": "Cut scene 6, or move it after scene 7.",
         "evidence": ["docs/walkthroughs/supply-sophie-rutf.yaml scene 6"]},
        {"scenes": [1], "dimension": "data_scale", "kind": "seed",
         "detail": "The overview shows 3 rows; the value claim needs a program-sized backlog.",
         "fix_recommendation": "Seed 12+ procurement rounds across 3 commodities.",
         "evidence": ["scripts/walkthroughs/rutf/seed_data.py: 2 tenders, 1 order"]}
      ]
    }

``kind`` routes each finding:

* ``restate`` — the narration says it at the wrong strength or in the wrong
  words (a mechanical restatement). AUTO-APPLIED to the narrative; no gate.
* ``seed``    — the seeded data does not make the scene's point (scale,
  realism). Goes into the gap walk's BUILD batch: seed changes are setup code.
* ``order`` / ``scope`` — move, cut, merge or add a scene. These change the
  story the human approved, so they surface through the ``concept_change``
  gate ONCE, up front, together with any gap-walk ``decision`` gaps.
  After that single ask (``storyboard mark``), later re-walks never re-raise
  them — the rendered arc judge owns the arc from then on.

    python -m scripts.ddd.storyboard validate <storyboard.json> [--spec <spec>]
    python -m scripts.ddd.storyboard mark <run_id> --status asked|applied|clean [--review-id ID]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

KINDS = ("restate", "seed", "order", "scope")
DIMENSIONS = ("scene_order", "earns_place", "data_scale", "data_realism", "restatement")
DECISION_KINDS = ("order", "scope")
ASKED_STATUSES = ("asked", "applied", "clean")


def validate(doc: Any, *, scene_count: int | None = None) -> list[str]:
    problems: list[str] = []
    if not isinstance(doc, dict):
        return ["storyboard.json must be a JSON object"]
    findings = doc.get("findings")
    if not isinstance(findings, list):
        return ["`findings` must be a list (empty when the storyboard is clean)"]
    for i, f in enumerate(findings):
        label = f"findings[{i}]"
        if not isinstance(f, dict):
            problems.append(f"{label}: must be an object")
            continue
        scenes = f.get("scenes")
        if not isinstance(scenes, list) or not scenes or not all(isinstance(s, int) and s >= 1 for s in scenes):
            problems.append(f"{label}: `scenes` must be a non-empty list of 1-based integers")
        elif scene_count and any(s > scene_count for s in scenes):
            problems.append(f"{label}: scenes {scenes} exceed the spec's {scene_count} scenes")
        if f.get("kind") not in KINDS:
            problems.append(f"{label}: `kind` must be one of {KINDS}, got {f.get('kind')!r}")
        if f.get("dimension") not in DIMENSIONS:
            problems.append(f"{label}: `dimension` must be one of {DIMENSIONS}, got {f.get('dimension')!r}")
        for key in ("detail", "fix_recommendation"):
            if not str(f.get(key) or "").strip():
                problems.append(f"{label}: `{key}` is required")
        ev = f.get("evidence")
        if not isinstance(ev, list) or not [e for e in ev if str(e).strip()]:
            problems.append(f"{label}: `evidence` must cite at least one thing the critique read")
    return problems


def route(doc: dict, *, already_asked: bool = False) -> dict[str, list[dict]]:
    """Split a VALID storyboard into restate / seed / decide buckets.

    ``already_asked``: the order/scope questions went to the gate once already —
    they are dropped (reported as ``suppressed``), never re-raised.
    """
    out: dict[str, list[dict]] = {"restate": [], "seed": [], "decide": [], "suppressed": []}
    for f in doc.get("findings") or []:
        if not isinstance(f, dict):
            continue
        kind = f.get("kind")
        row = {
            "scenes": f.get("scenes"),
            "kind": kind,
            "dimension": f.get("dimension"),
            "detail": f.get("detail"),
            "fix_recommendation": f.get("fix_recommendation"),
        }
        if kind == "restate":
            out["restate"].append(row)
        elif kind == "seed":
            out["seed"].append(row)
        elif kind in DECISION_KINDS:
            out["suppressed" if already_asked else "decide"].append(row)
    return out


def already_asked(state: Any) -> bool:
    sb = getattr(state, "storyboard", None) or {}
    return sb.get("status") in ASKED_STATUSES


def mark(state: Any, status: str, *, review_id: str | None = None, decision: Any = None) -> dict:
    if status not in ASKED_STATUSES:
        raise ValueError(f"status must be one of {ASKED_STATUSES}, got {status!r}")
    state.storyboard = {
        "status": status,
        "review_id": review_id,
        "decision": decision,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return state.storyboard


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.ddd.storyboard")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate")
    v.add_argument("path")
    v.add_argument("--spec", default=None)
    m = sub.add_parser("mark")
    m.add_argument("run_id")
    m.add_argument("--status", required=True, choices=list(ASKED_STATUSES))
    m.add_argument("--review-id", default=None)
    args = ap.parse_args(argv)
    if args.cmd == "mark":
        from scripts.ddd.runstate import load, save

        state = load(args.run_id)
        out = mark(state, args.status, review_id=args.review_id)
        save(state)
        print(json.dumps(out))
        return 0
    try:
        doc = json.loads(Path(args.path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"storyboard: cannot read {args.path}: {exc}", file=sys.stderr)
        return 2
    scene_count = None
    if args.spec:
        from scripts.ddd.spec_io import load_spec_raw

        scene_count = len(load_spec_raw(args.spec).get("scenes") or [])
    problems = validate(doc, scene_count=scene_count)
    print(json.dumps({"valid": not problems, "problems": problems}, indent=1))
    return 0 if not problems else 2


if __name__ == "__main__":
    raise SystemExit(_main())
