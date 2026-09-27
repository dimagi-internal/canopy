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

    python -m scripts.ddd.gap_walk check <gaps.json> [--spec <spec>] [--run-id <id>] \
        [--storyboard <storyboard.json>]

With ``--storyboard`` the pre-build storyboard critique (:mod:`scripts.ddd.storyboard`)
comes out in the SAME answer: its ``restate`` findings join the restate list,
``seed`` findings join the build batch, and ``order``/``scope`` findings join the
``decide`` gate — once. With ``--run-id`` a storyboard with no order/scope
questions is marked consumed here; one with questions is marked by the
orchestrator after it posts the gate (``storyboard mark --status asked``).
Later re-walks ignore a consumed storyboard.

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


def decide(doc: dict, storyboard: dict | None = None) -> dict[str, Any]:
    """The loop's next action from a VALID gaps document (+ optional routed storyboard).

    ``storyboard`` is :func:`scripts.ddd.storyboard.route` output. Its buckets
    fold into the same three actions, so the gap walk and the storyboard
    critique produce ONE next step.
    """
    result = _decide_gaps(doc, storyboard or {})
    if storyboard is not None:
        result["storyboard"] = storyboard
    return result


def _decide_gaps(doc: dict, sb: dict) -> dict[str, Any]:
    sb_restate = list(sb.get("restate") or [])
    sb_seed = list(sb.get("seed") or [])
    sb_decide = list(sb.get("decide") or [])
    gaps = [g for g in doc.get("gaps") or [] if isinstance(g, dict)]
    routed = [(g, *gap_class(g)) for g in gaps]
    decisions = [g for g, kind, _ in routed if kind == "decision"]
    builds = [g for g, kind, _ in routed if kind == "build"]
    restates = [
        {"scene": g.get("scene"), "claim": g.get("claim"), "why": why}
        for g, kind, why in routed
        if kind == "restate"
    ]
    restates += [
        {"scenes": r["scenes"], "claim": r["detail"], "why": "storyboard restatement", "fix": r["fix_recommendation"]}
        for r in sb_restate
    ]
    scenes = sorted({g["scene"] for g in gaps} | {s for r in sb_restate + sb_seed + sb_decide for s in r.get("scenes") or []})
    sb_note = ""
    if sb_decide:
        sb_note = f" Storyboard: {len(sb_decide)} scene order/scope question(s) go in the same gate — asked once, up front."
    if decisions or sb_decide:
        return {
            "action": "decide",
            "open_gaps": len(gaps),
            "reason": (
                f"{len(decisions)} scene claim(s) need a product decision before anything can "
                "be built — open the concept_change gate with them (and build the "
                f"{len(builds) + len(sb_seed)} buildable gap(s) and restate the {len(restates)} "
                "accuracy gap(s) in the same pass once decided)." + sb_note
            ),
            "scenes": scenes,
            "restate": restates,
            "seed": sb_seed,
            "decisions": sb_decide,
        }
    if builds or restates or sb_seed:
        return {
            "action": "build",
            "open_gaps": len(gaps),
            "reason": (
                f"{len(builds)} scene claim(s) the product cannot show yet — BUILD them — "
                f"{len(sb_seed)} scene(s) whose seeded data does not make the point — SEED them — and "
                f"{len(restates)} where the recipe/narration says something the built page "
                "does not — RESTATE them to the page (accuracy; no gate). One batch, one "
                "PR/deploy, then re-walk. Do not render: a judge round would only "
                "re-discover these."
            ),
            "scenes": scenes,
            "restate": restates,
            "seed": sb_seed,
        }
    return {"action": "render", "open_gaps": 0, "reason": "every scene's claim is buildable today"}


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.ddd.gap_walk")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("gaps")
    c.add_argument("--spec", default=None, help="spec path; enforces every scene was walked")
    c.add_argument("--run-id", default=None, help="stamp gaps_path/open_gaps on run_state")
    c.add_argument("--storyboard", default=None, help="storyboard.json from ddd-arc-eval storyboard mode")
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
    state = None
    if args.run_id:
        from scripts.ddd.runstate import load

        state = load(args.run_id)
    routed = None
    if args.storyboard:
        from scripts.ddd import storyboard as sbmod

        try:
            sb_doc = json.loads(Path(args.storyboard).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"gap_walk: cannot read {args.storyboard}: {exc}", file=sys.stderr)
            return 2
        sb_problems = sbmod.validate(sb_doc, scene_count=scene_count)
        if sb_problems:
            print(json.dumps({"action": "invalid", "problems": [f"storyboard: {p}" for p in sb_problems]}, indent=1))
            return 2
        if state is not None and sbmod.already_asked(state):
            routed = {"restate": [], "seed": [], "decide": [], "suppressed": [], "consumed": True}
        else:
            routed = sbmod.route(sb_doc)
    result = decide(doc, routed)
    if state is not None:
        from scripts.ddd.runstate import save

        state.gaps_path = str(path.resolve())
        state.open_gaps = result["open_gaps"]
        if routed is not None and not routed.get("consumed") and not routed["decide"]:
            from scripts.ddd import storyboard as sbmod

            sbmod.mark(state, "applied" if (routed["restate"] or routed["seed"]) else "clean")
        save(state)
    print(json.dumps(result, indent=1))
    return 0 if result["action"] == "render" else 1


if __name__ == "__main__":
    raise SystemExit(_main())
