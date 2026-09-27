"""Park the scenes a pending gate decision affects; keep working on the rest.

Why this module exists
----------------------
An unattended run resolves a ``concept_change`` gate to ``defer``
(:mod:`scripts.ddd.gates`) and — before this module — TERMINATED on it. On the
first live v1 run that meant one strategy question about two scenes halted
fixing and judging for all seven, although most of the backlog sat on scenes
the question did not touch.

A pending decision now parks only the scenes its findings name. The loop keeps
fixing and judging every other scene; mechanical findings on a parked scene are
withheld (their direction may be about to change). When the review is resolved
the decision is re-integrated and the scenes are unparked. Convergence still
requires every scene: parked scenes are rendered and judged each pass and are
never excluded from ``compute_convergence``.

State lives on ``RunState.parked`` — a list of entries::

    {review_id, review_url, gate, scenes: [int], reason, iteration,
     parked_at, status: pending|resolved, decision, resolved_by, resolved_at}

and ``RunState.park_request`` — what ``compute_auto_iterate`` asks to park when
it returns ``park_and_continue`` (the orchestrator posts the review, then calls
``park``).

    python -m scripts.ddd.parking park <run_id> --review-id ID [--review-url URL]
    python -m scripts.ddd.parking poll <run_id>          # non-blocking; unparks resolved
    python -m scripts.ddd.parking status <run_id>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from typing import Any, Callable

_RANGE = re.compile(r"(\d+)\s*[-–]\s*(\d+)")
_INT = re.compile(r"\d+")
_MAX_RANGE = 50


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def scene_refs(raw: object) -> set[int]:
    """Every scene a finding's ``scene`` names.

    Per-scene findings carry ``4`` or ``"4: title"`` -> ``{4}``. Arc findings
    carry sequences, one leading number list per clause (clauses split on ``;``
    and ``vs``): ``"2-3 round 1 audit vs 4-7 round 2 award"`` ->
    ``{2,3,4,5,6,7}``; ``"1/3/7 overview; 4/5 comparison"`` -> ``{1,3,4,5,7}``;
    ``"1 opening vs 7 close"`` -> ``{1,7}``. Numbers inside a clause's words
    (``round 1``) are not scenes.
    """
    text = str(raw if raw is not None else "").strip()
    if not text:
        return set()
    m = re.match(r"^(\d+)\s*:", text)
    if m:
        return {int(m.group(1))}
    refs: set[int] = set()
    for clause in re.split(r";|\bvs\.?(?=\s)", text):
        head = re.match(r"^\s*([\d\s/,&+–-]+)", clause)
        if not head:
            continue
        chunk = head.group(1)
        for a, b in _RANGE.findall(chunk):
            lo, hi = int(a), int(b)
            if lo < hi and hi - lo <= _MAX_RANGE:
                refs.update(range(lo, hi + 1))
        for n in _INT.findall(_RANGE.sub(" ", chunk)):
            refs.add(int(n))
    return refs


def affected_scenes(findings: list[dict]) -> set[int]:
    out: set[int] = set()
    for f in findings or []:
        if isinstance(f, dict):
            out |= scene_refs(f.get("scene"))
    return out


def pending(state: Any) -> list[dict]:
    return [p for p in (getattr(state, "parked", None) or []) if p.get("status") == "pending"]


def parked_scenes(state: Any) -> set[int]:
    out: set[int] = set()
    for p in pending(state):
        out |= {int(s) for s in p.get("scenes") or []}
    return out


def is_parked(finding: dict, parked: set[int]) -> bool:
    """A finding touching ANY parked scene is withheld (conservative)."""
    return bool(parked and (scene_refs(finding.get("scene")) & parked))


def mark(findings: list[dict], parked: set[int]) -> list[dict]:
    """Stamp ``parked: true`` on findings that touch a parked scene (clears it elsewhere)."""
    out: list[dict] = []
    for f in findings or []:
        g = dict(f)
        if is_parked(g, parked):
            g["parked"] = True
        else:
            g.pop("parked", None)
        out.append(g)
    return out


def park(
    state: Any,
    *,
    review_id: str,
    review_url: str | None = None,
    gate: str = "concept_change",
    scenes: list[int] | None = None,
    reason: str = "",
) -> dict:
    """Record a parked decision. Consumes ``state.park_request`` when scenes are omitted."""
    request = getattr(state, "park_request", None) or {}
    chosen = sorted({int(s) for s in (scenes if scenes is not None else request.get("scenes") or [])})
    if not chosen:
        raise ValueError("nothing to park: no scenes given and no park_request on the run")
    for p in pending(state):
        if p.get("review_id") == review_id:
            p["scenes"] = sorted(set(p.get("scenes") or []) | set(chosen))
            state.park_request = None
            return p
    entry = {
        "review_id": review_id,
        "review_url": review_url,
        "gate": gate,
        "scenes": chosen,
        "reason": reason or request.get("reason") or "",
        "iteration": getattr(state, "iteration", None),
        "parked_at": _now(),
        "status": "pending",
        "decision": None,
        "resolved_by": None,
        "resolved_at": None,
    }
    state.parked = list(getattr(state, "parked", None) or []) + [entry]
    state.park_request = None
    return entry


def resolve(state: Any, review_id: str, decision: Any, *, resolved_by: str = "human") -> dict | None:
    """Mark a parked decision resolved -> its scenes rejoin the loop."""
    for p in pending(state):
        if p.get("review_id") == review_id:
            p["status"] = "resolved"
            p["decision"] = decision
            p["resolved_by"] = resolved_by
            p["resolved_at"] = _now()
            return p
    return None


def poll(state: Any, fetch: Callable[[str], dict]) -> list[dict]:
    """Non-blocking: ask the review surface about every pending entry once.

    ``fetch(review_id) -> {"status": ..., "response_json": ...}`` (default:
    :func:`scripts.ddd.review.get_review`). A fetch error leaves the entry
    parked — never a crash, never an unpark on no evidence.
    """
    resolved: list[dict] = []
    for p in list(pending(state)):
        try:
            data = fetch(p["review_id"]) or {}
        except Exception:
            continue
        if data.get("status") == "resolved":
            entry = resolve(state, p["review_id"], data.get("response_json"), resolved_by="human")
            if entry:
                resolved.append(entry)
    return resolved


def _main(argv: list[str] | None = None) -> int:
    from scripts.ddd.runstate import load, save

    ap = argparse.ArgumentParser(prog="python -m scripts.ddd.parking")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pk = sub.add_parser("park")
    pk.add_argument("run_id")
    pk.add_argument("--review-id", required=True)
    pk.add_argument("--review-url", default=None)
    pk.add_argument("--gate", default="concept_change")
    pk.add_argument("--scene", type=int, action="append", default=None)
    po = sub.add_parser("poll")
    po.add_argument("run_id")
    st = sub.add_parser("status")
    st.add_argument("run_id")
    args = ap.parse_args(argv)
    try:
        state = load(args.run_id)
        if args.cmd == "park":
            entry = park(
                state,
                review_id=args.review_id,
                review_url=args.review_url,
                gate=args.gate,
                scenes=args.scene,
            )
            save(state)
            print(json.dumps(entry, indent=1))
            return 0
        if args.cmd == "poll":
            from scripts.ddd.review import get_review

            resolved = poll(state, lambda rid: get_review(rid))
            save(state)
            print(
                json.dumps(
                    {"resolved": resolved, "still_parked": sorted(parked_scenes(state))}, indent=1
                )
            )
            return 0
        print(json.dumps({"parked": pending(state), "scenes": sorted(parked_scenes(state))}, indent=1))
        return 0
    except (ValueError, OSError) as exc:
        print(f"parking {args.cmd}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
