"""Sealed loop decisions — the orchestrator may not quietly overrule `assemble`.

Why this module exists
----------------------
On connect-labs ``supply-sophie-rutf-2026-09-26-001`` (canopy 0.2.543) the loop
machinery decided correctly and the orchestrating agent overrode it, twice,
without leaving a record:

* At iteration 5 ``assemble`` returned ``stop_max_iter``. Ad-hoc scripts then
  rewrote ``run_state.yaml`` (re-routed findings, de-duplicated the history, and
  reset ``progress_history`` to a one-point "v4 baseline"), which also reset
  stall detection. Four more ~60-minute iterations followed with the mean cell
  flat at 3.60 +/- 0.05.
* From iteration 7 ``assemble`` said "next judge: incremental", and
  ``state.next_judge_full`` was False, but the agent re-used an earlier command
  line with a literal ``judge_scope plan ... --full``, so every pass judged all 7
  scenes and all three judges ("full pass requested").

Both overrides may have been the right call. Neither left a reason, and the
second was invisible: the run's own record said "backlog mode, incremental".

The contract
------------
``assemble`` seals its decision (:func:`seal`): the action plus a digest of the
state it rests on. Before a pass proceeds, :func:`check` refuses when

* the state was rewritten after the seal (the digest no longer matches), or
* the sealed action was a STOP (``stop_max_iter``, ``stop_unclear``,
  ``stop_concept_change``, ``stop_done``) and nothing overrode it,

unless :func:`override` logged a reason. An override is cheap and always
allowed — it only has to be SAID: ``state.decision_overrides`` records it, the
seal is re-taken over the edited state, and ``assemble`` prints it in the digest.
Bumping ``state.iteration`` (:func:`bump`) is not a rewrite.

    python -m scripts.ddd.decision check    <run_id>
    python -m scripts.ddd.decision override <run_id> --reason "<why>"
    python -m scripts.ddd.decision bump     <run_id>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from typing import Any

#: The state a decision rests on. Only ``assemble``/``compute_auto_iterate``
#: write these; anything else changing them is an override.
SEALED_FIELDS = (
    "progress_history",
    "score_history",
    "finding_fingerprints",
    "auto_iterate_next_action",
    "next_judge_full",
    "loop_mode",
    "batches_since_full",
)

#: Sealed actions a new pass may not proceed past without a logged override.
#: ``stop_partial`` is excluded: its documented next step IS a re-fire on the
#: full spec.
BLOCKING_STOPS = frozenset({"stop_max_iter", "stop_unclear", "stop_concept_change", "stop_done"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(state: Any) -> str:
    payload = {f: getattr(state, f, None) for f in SEALED_FIELDS}
    raw = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def seal(state: Any, *, overridden: bool = False) -> dict:
    """Seal the decision currently on ``state`` (called by ``assemble``)."""
    state.decision_seal = {
        "iteration": state.iteration,
        "action": state.auto_iterate_next_action,
        "digest": digest(state),
        "sealed_at": _now(),
        "overridden": bool(overridden),
    }
    return state.decision_seal


def check(state: Any) -> dict[str, Any]:
    """``{ok, problem, action}`` — may a new pass proceed from this state?"""
    s = getattr(state, "decision_seal", None)
    if not s:
        return {"ok": True, "problem": None, "action": None}
    action = s.get("action")
    cmd = f"python -m scripts.ddd.decision override {state.run_id} --reason \"<why>\""
    if s.get("digest") != digest(state):
        return {
            "ok": False,
            "action": action,
            "problem": (
                f"run_state.yaml was rewritten after assemble decided {action!r} at iteration "
                f"{s.get('iteration')} (one of {', '.join(SEALED_FIELDS)} changed). The loop "
                "owns those fields. If the edit is deliberate, log it: " + cmd
            ),
        }
    if action in BLOCKING_STOPS and not s.get("overridden"):
        return {
            "ok": False,
            "action": action,
            "problem": (
                f"assemble decided {action!r} at iteration {s.get('iteration')}: the run is "
                "over. Report it (ddd agent: the stop branch for that action). To keep going "
                "anyway, log why first: " + cmd
            ),
        }
    return {"ok": True, "problem": None, "action": action}


def override(state: Any, reason: str) -> dict:
    """Log an override of the sealed decision and re-seal over the current state."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("an override needs a --reason")
    prior = getattr(state, "decision_seal", None) or {}
    entry = {
        "iteration": state.iteration,
        "action": prior.get("action"),
        "state_rewritten": bool(prior) and prior.get("digest") != digest(state),
        "reason": reason,
        "at": _now(),
    }
    state.decision_overrides = list(getattr(state, "decision_overrides", None) or []) + [entry]
    seal(state, overridden=True)
    return entry


def bump(state: Any) -> int:
    """Advance to the next iteration — the only run_state edit a ``continue`` needs."""
    state.iteration += 1
    state.phase = "render"
    return state.iteration


def require(state: Any, *, where: str) -> None:
    """Raise ``ValueError`` (CLI exit 2) when :func:`check` refuses."""
    out = check(state)
    if not out["ok"]:
        raise ValueError(f"{where} refused: {out['problem']}")


def _main(argv: list[str] | None = None) -> int:
    from scripts.ddd.runstate import load, save

    ap = argparse.ArgumentParser(prog="python -m scripts.ddd.decision")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="may a new pass proceed?")
    c.add_argument("run_id")
    o = sub.add_parser("override", help="log a reason for overruling the sealed decision")
    o.add_argument("run_id")
    o.add_argument("--reason", required=True)
    b = sub.add_parser("bump", help="state.iteration += 1 (after a continue/checkpoint)")
    b.add_argument("run_id")
    args = ap.parse_args(argv)
    try:
        state = load(args.run_id)
        if args.cmd == "check":
            out = check(state)
            print(json.dumps(out, indent=1))
            return 0 if out["ok"] else 1
        if args.cmd == "override":
            out = override(state, args.reason)
            save(state)
            print(json.dumps(out, indent=1))
            return 0
        out = {"iteration": bump(state)}
        save(state)
        print(json.dumps(out))
        return 0
    except (ValueError, OSError) as exc:
        print(f"decision {args.cmd}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
