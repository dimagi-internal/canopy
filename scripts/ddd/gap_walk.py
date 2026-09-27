"""Pre-render gap walk — is the narrative even buildable against today's product?

The problem, measured
---------------------
On a freshly-built (v1) product the first full render + judge round routinely
learned only that a scene's feature did not exist yet — ~14 minutes and ~600k
judge tokens to discover what reading the target repo's routes, views and
operations would have said in one pass. The judges then scored a screen that
could not show the claim, and every downstream number was about the gap, not
the product.

The contract
------------
``skills/ddd-gap-walk`` has an agent read the target repo against each locked
scene's narration + ``features[]`` and write ``<run_dir>/gaps.json``::

    {
      "narrative_slug": "supply-test-kits",
      "target_repo": "/path/to/connect-labs",
      "walked_at": "2026-09-26T12:00:00Z",
      "covered": [
        {"scene": 1, "evidence": ["connect_labs/supply_chain/views.py:88 commodity_detail"]}
      ],
      "gaps": [
        {"scene": 4, "claim": "Hauwa awards the quote in one click",
         "missing_capability": "no award action on the comparison page",
         "evidence": ["connect_labs/supply_chain/urls.py (no award route)"],
         "kind": "build",               # build | decision
         "build_hint": "POST /procurement/rounds/<id>/award/ + button"}
      ]
    }

Every scene must appear in ``covered`` or ``gaps`` (a walk that skips scenes is
not a walk), and every entry must cite evidence (a path the agent actually
read). ``check`` then answers the loop's one question:

* any STRATEGY gap of kind ``decision`` -> ``decide`` (concept_change: the story
  asks for something nobody has decided the product should do)
* any gap of kind ``build`` or ``restate`` -> ``build`` (ONE batch — product
  code for ``build``, recipe/narration wording for ``restate`` — then re-walk)
* no gaps                        -> ``render``

The accuracy / strategy split (the same one ``finding_class`` applies to judge
findings). "The recipe says 'Deadline' but the page says 'REPLIES BY'" is an
ACCURACY gap: the built page is the authority and restating the recipe to match
it is one determinate change, so it must never open the concept_change gate. A
walker marks it ``kind: restate``; a gap it marked ``decision`` is demoted to
``restate`` when it carries ``finding_class: accuracy``, when ``finding_class``
classifies its claim + missing capability as accuracy, or when its
``build_hint`` itself offers restating the narration / recipe to what is built.
The first live v1 run (0.2.528, M2) opened the gate for three such gaps, each
then resolved by restating the recipe.

    python -m scripts.ddd.gap_walk check <gaps.json> [--spec <spec>] [--run-id <id>]

Exit 0 = render, 1 = build/decide, 2 = invalid gaps.json.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

GAP_KINDS = ("build", "decision", "restate")

# A build_hint that offers restating the story to the product as a resolution.
_RESTATE_OFFER = re.compile(
    r"\b(?:reword|restate|re-?target|rephrase)\b"
    r"|\b(?:change|edit|update|adjust|align)\s+(?:scene\s+\d+'s\s+|the\s+)?"
    r"(?:narration|narrative|recipe|concept_claim|wording|wait_for|hover)\b"
    r"|\b(?:narration|narrative|recipe)\s+(?:drops|omits|stops\s+claiming)\b",
    re.IGNORECASE,
)


def gap_class(gap: dict) -> tuple[str, str]:
    """``(kind, reason)`` — the kind a gap is ROUTED as (build | restate | decision)."""
    kind = gap.get("kind", "build")
    if kind != "decision":
        return kind, "declared"
    explicit = str(gap.get("finding_class") or "").lower()
    if explicit == "strategy":
        return "decision", "explicit finding_class: strategy"
    if explicit == "accuracy":
        return "restate", "explicit finding_class: accuracy"
    hint = str(gap.get("build_hint") or "")
    if _RESTATE_OFFER.search(hint):
        return "restate", "build_hint offers restating the story to what is built"
    from scripts.ddd import finding_class

    cls, why = finding_class.classify(
        {
            "detail": f"{gap.get('claim') or ''}. {gap.get('missing_capability') or ''}",
            "fix_recommendation": hint,
        }
    )
    if cls == finding_class.ACCURACY:
        return "restate", f"accuracy: {why}"
    return "decision", "strategy/unclassified decision"


def validate(doc: Any, *, scene_count: int | None = None) -> list[str]:
    """Problems with a gaps.json document (empty list = valid)."""
    problems: list[str] = []
    if not isinstance(doc, dict):
        return ["gaps.json must be a JSON object"]
    covered = doc.get("covered")
    gaps = doc.get("gaps")
    if not isinstance(covered, list):
        problems.append("`covered` must be a list")
        covered = []
    if not isinstance(gaps, list):
        problems.append("`gaps` must be a list")
        gaps = []
    seen: set[int] = set()
    for where, items in (("covered", covered), ("gaps", gaps)):
        for i, item in enumerate(items):
            label = f"{where}[{i}]"
            if not isinstance(item, dict):
                problems.append(f"{label}: must be an object")
                continue
            scene = item.get("scene")
            if not isinstance(scene, int) or scene < 1:
                problems.append(f"{label}: `scene` must be a 1-based integer")
            else:
                seen.add(scene)
            ev = item.get("evidence")
            if not isinstance(ev, list) or not [e for e in ev if str(e).strip()]:
                problems.append(
                    f"{label}: `evidence` must list at least one path the walk actually read"
                )
            if where == "gaps":
                for key in ("claim", "missing_capability"):
                    if not str(item.get(key) or "").strip():
                        problems.append(f"{label}: `{key}` is required")
                kind = item.get("kind", "build")
                if kind not in GAP_KINDS:
                    problems.append(f"{label}: `kind` must be one of {GAP_KINDS}, got {kind!r}")
    if scene_count:
        missing = sorted(set(range(1, scene_count + 1)) - seen)
        if missing:
            problems.append(
                f"scenes {missing} appear in neither `covered` nor `gaps` — walk every scene"
            )
        extra = sorted(s for s in seen if s > scene_count)
        if extra:
            problems.append(f"scenes {extra} do not exist in the spec ({scene_count} scenes)")
    return problems


def decide(doc: dict) -> dict[str, Any]:
    """The loop's next action from a VALID gaps document."""
    gaps = [g for g in doc.get("gaps") or [] if isinstance(g, dict)]
    routed = [(g, *gap_class(g)) for g in gaps]
    decisions = [g for g, kind, _ in routed if kind == "decision"]
    builds = [g for g, kind, _ in routed if kind == "build"]
    restates = [
        {"scene": g.get("scene"), "claim": g.get("claim"), "why": why}
        for g, kind, why in routed
        if kind == "restate"
    ]
    if decisions:
        return {
            "action": "decide",
            "open_gaps": len(gaps),
            "reason": (
                f"{len(decisions)} scene claim(s) need a product decision before anything can "
                "be built — open the concept_change gate with them (and build the "
                f"{len(builds)} buildable gap(s) and restate the {len(restates)} accuracy "
                "gap(s) in the same pass once decided)."
            ),
            "scenes": sorted({g["scene"] for g in gaps}),
            "restate": restates,
        }
    if builds or restates:
        return {
            "action": "build",
            "open_gaps": len(gaps),
            "reason": (
                f"{len(builds)} scene claim(s) the product cannot show yet — BUILD them — and "
                f"{len(restates)} where the recipe/narration says something the built page "
                "does not — RESTATE them to the page (accuracy; no gate). One batch, one "
                "PR/deploy, then re-walk. Do not render: a judge round would only "
                "re-discover these."
            ),
            "scenes": sorted({g["scene"] for g in gaps}),
            "restate": restates,
        }
    return {"action": "render", "open_gaps": 0, "reason": "every scene's claim is buildable today"}


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.ddd.gap_walk")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("gaps")
    c.add_argument("--spec", default=None, help="spec path; enforces every scene was walked")
    c.add_argument("--run-id", default=None, help="stamp gaps_path/open_gaps on run_state")
    args = ap.parse_args(argv)

    path = Path(args.gaps)
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"gap_walk: cannot read {path}: {exc}", file=sys.stderr)
        return 2
    scene_count = None
    if args.spec:
        from scripts.ddd.spec_io import load_spec_raw

        scene_count = len(load_spec_raw(args.spec).get("scenes") or [])
    problems = validate(doc, scene_count=scene_count)
    if problems:
        print(json.dumps({"action": "invalid", "problems": problems}, indent=1))
        return 2
    result = decide(doc)
    if args.run_id:
        from scripts.ddd.runstate import load, save

        state = load(args.run_id)
        state.gaps_path = str(path.resolve())
        state.open_gaps = result["open_gaps"]
        save(state)
    print(json.dumps(result, indent=1))
    return 0 if result["action"] == "render" else 1


if __name__ == "__main__":
    raise SystemExit(_main())
