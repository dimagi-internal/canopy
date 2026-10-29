"""The closeout verdict: whether an unattended, incident-style turn may say it is done.

A turn used to END on a free-typed "✅ Session complete — safe to close." and nothing checked
it. The rule (agent-core/turn.md) was "complete is a claim: write it only when nothing YOU SAID
you would do is still undone" — which checks the turn's own promises, not whether the incident
is closed out. Across Hal's CloudWatch alarm turns 2026-08-26..10-29, 36 ended "complete" and
about 12 had actually closed the incident. The rest left the alarm still firing with the cause
unconfirmed and the next step owned by "someone with SSO", a merged-but-not-live fix, a
decision for Jonathan buried inside a "complete", promised future work, filed issues nobody
was assigned, and a close-out script whose success was inferred from the tail of its output.

So the turn no longer gets to type the claim. It records FACTS, and this module computes the
verdict from them:

- ``incident`` — the incident's CURRENT state, ``OK`` | ``ALARM`` | ``UNKNOWN``, plus
  ``checked_via`` naming how it was checked (``describe_alarm``, ``alarm_mail:<id>``, …). A
  state with no ``checked_via`` was assumed, and is read as ``UNKNOWN``.
- ``cause`` — ``confirmed`` | ``likely`` | ``unknown``, with an ``evidence`` link (required for
  ``confirmed``).
- ``remedy`` — ``none_needed`` | ``shipped_verified_live`` | ``merged_not_live`` | ``proposed``
  | ``none``.
- ``open_items`` — everything still open, each with an OWNER that is one of:
  ``{"agent": slug, "task": <board task>, "recheck_at": <when>}``,
  ``{"person": <who>, "decision": <the one decision asked of them>}``, or
  ``{"dispatched": <session/turn id>}``. "someone", a role, or no owner is rejected.
- ``closeout_steps`` — each close-out step and its EXIT CODE (an int, not a reading of a tail).

Verdict, in order:

1. **INCOMPLETE** — any rejected/missing fact: an unowned item, an unverified close-out step, a
   ``confirmed`` cause without evidence. The turn keeps working or records owners.
2. **NEEDS_YOU** — any item owned by a person. The status line carries exactly the ask(s).
   Ranked above CLOSED on purpose: a session auto-closed on CLOSED would bury the ask.
3. **CLOSED** — incident ``OK`` (checked), cause ``confirmed`` or remedy ``none_needed``, remedy
   in {``none_needed``, ``shipped_verified_live``}, every item owned.
4. **MONITORING** — not closed, at least one open item, and every agent-owned item has a
   scheduled re-check (dispatched items count as owned).
5. **INCOMPLETE** — otherwise (e.g. still firing and nobody owns the next step).

The status line the turn ends on is printed FROM the verdict, and ends with a machine token
``[closeout: <VERDICT>]`` that canopy-web lifts off the turn's result note. The Stop hook
``plugins/canopy/hooks/closeout_guard.py`` blocks an unattended stop that still says "safe to
close" when the latest recorded verdict is not CLOSED. FRAMEWORK tier; stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field

SCHEMA = "canopy.closeout/v1"
CLOSED, MONITORING, NEEDS_YOU, INCOMPLETE = "CLOSED", "MONITORING", "NEEDS_YOU", "INCOMPLETE"
VERDICTS = (CLOSED, MONITORING, NEEDS_YOU, INCOMPLETE)

INCIDENT_STATES = ("OK", "ALARM", "UNKNOWN")
CAUSES = ("confirmed", "likely", "unknown")
REMEDIES = ("none_needed", "shipped_verified_live", "merged_not_live", "proposed", "none")
CLOSING_REMEDIES = ("none_needed", "shipped_verified_live")

#: An owner that names nobody. Matched against a person owner's name (and a bare-string owner).
_VAGUE = re.compile(
    r"\b(some(one|body)|any(one|body)|whoever|tbd|tba|unassigned|unowned|nobody|n/?a|"
    r"the team|a human|a person|ops|on-?call|someone with)\b|^\s*(\?+|-+|none|unknown)?\s*$",
    re.IGNORECASE)

#: The machine-readable token at the end of every status line. canopy-web parses this exact
#: shape off result_note (packages/canopy_transcript/closeout.py) — change both together.
TOKEN = "[closeout: {verdict}]"
TOKEN_RE = re.compile(r"\[closeout: (CLOSED|MONITORING|NEEDS_YOU|INCOMPLETE)\]")

ROOT = os.path.expanduser("~/.canopy/closeout")
_SID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


@dataclass
class Verdict:
    verdict: str
    problems: list[str] = field(default_factory=list)
    asks: list[str] = field(default_factory=list)      # "<person>: <decision>"
    monitors: list[str] = field(default_factory=list)  # "<agent> re-checks <what> (<task>) at <when>"

    @property
    def status_line(self) -> str:
        return status_line(self)

    def as_dict(self) -> dict:
        return {"verdict": self.verdict, "problems": self.problems, "asks": self.asks,
                "monitors": self.monitors, "status_line": self.status_line}


def _s(v) -> str:
    return str(v).strip() if v is not None else ""


def _owner_problem(item: dict, i: int) -> tuple[str, str | None]:
    """(kind, problem) for one open item's owner. kind ∈ agent|person|dispatched|"" ."""
    what = _s(item.get("what")) or f"open item #{i + 1}"
    owner = item.get("owner")
    if isinstance(owner, str) or not owner:
        who = _s(owner) or "nobody"
        return "", f"'{what}' is owned by {who!r} — name an owner: this agent + a board task + a " \
                   "re-check time, a named person + the one decision asked of them, or a " \
                   "dispatched session/turn id"
    if not isinstance(owner, dict):
        return "", f"'{what}': owner must be an object"
    if _s(owner.get("dispatched")):
        return "dispatched", None
    if _s(owner.get("person")) or "decision" in owner:
        person = _s(owner.get("person"))
        if not person or _VAGUE.search(person):
            return "", f"'{what}' is owned by {person or 'nobody'!r} — a person owner must be " \
                       "a named person (e.g. @jjackson), not a role or 'someone'"
        if not _s(owner.get("decision")):
            return "", f"'{what}' is owned by {person} but names no decision — say the one " \
                       "thing asked of them"
        return "person", None
    if _s(owner.get("agent")) or "task" in owner:
        if not _s(owner.get("agent")):
            return "", f"'{what}': an agent owner names the agent"
        if not _s(owner.get("task")):
            return "", f"'{what}' is owned by {owner.get('agent')} with no board task — put it " \
                       "on the board (filing a GitHub issue is not owning it)"
        return "agent", None
    return "", f"'{what}': owner has none of agent/person/dispatched"


def compute(record: dict) -> Verdict:
    """The verdict for a closeout record. Pure — no I/O."""
    problems: list[str] = []
    rec = record if isinstance(record, dict) else {}

    incident = rec.get("incident") if isinstance(rec.get("incident"), dict) else {}
    state = _s(incident.get("state")).upper() or "UNKNOWN"
    if state not in INCIDENT_STATES:
        problems.append(f"incident.state {state!r} is not one of {', '.join(INCIDENT_STATES)}")
        state = "UNKNOWN"
    if state != "UNKNOWN" and not _s(incident.get("checked_via")):
        problems.append(f"incident.state {state} was not checked — record checked_via (how you "
                        "read the CURRENT state), or say UNKNOWN")
        state = "UNKNOWN"

    cause = rec.get("cause") if isinstance(rec.get("cause"), dict) else {"status": rec.get("cause")}
    cause_status = _s(cause.get("status")).lower() or "unknown"
    if cause_status not in CAUSES:
        problems.append(f"cause {cause_status!r} is not one of {', '.join(CAUSES)}")
        cause_status = "unknown"
    if cause_status == "confirmed" and not _s(cause.get("evidence")):
        problems.append("cause is 'confirmed' with no evidence link")

    remedy = _s(rec.get("remedy")).lower() or "none"
    if remedy not in REMEDIES:
        problems.append(f"remedy {remedy!r} is not one of {', '.join(REMEDIES)}")
        remedy = "none"

    steps = rec.get("closeout_steps") or []
    if not isinstance(steps, list) or not steps:
        problems.append("no closeout_steps recorded — record each close-out step's exit code")
        steps = []
    for st in steps:
        st = st if isinstance(st, dict) else {}
        name = _s(st.get("name")) or "a close-out step"
        code = st.get("exit_code")
        if isinstance(code, bool) or not isinstance(code, int):
            problems.append(f"{name} has no exit code — run it and record its exit status, not "
                            "a reading of its output")
        elif code != 0:
            problems.append(f"{name} exited {code}")

    asks, monitors, unscheduled = [], [], []
    items = rec.get("open_items") or []
    if not isinstance(items, list):
        problems.append("open_items must be a list")
        items = []
    for i, item in enumerate(items):
        item = item if isinstance(item, dict) else {"what": _s(item)}
        kind, problem = _owner_problem(item, i)
        if problem:
            problems.append(problem)
            continue
        what, owner = _s(item.get("what")) or f"open item #{i + 1}", item["owner"]
        if kind == "person":
            asks.append(f"{_s(owner['person'])}: {_s(owner['decision'])}")
        elif kind == "agent":
            when = _s(owner.get("recheck_at"))
            if when:
                monitors.append(f"{_s(owner['agent'])} re-checks {what} ({_s(owner['task'])}) at {when}")
            else:
                unscheduled.append(what)
        else:
            monitors.append(f"{what} — dispatched to {_s(owner['dispatched'])}")

    if problems:
        return Verdict(INCOMPLETE, problems, asks, monitors)
    if asks:
        return Verdict(NEEDS_YOU, [], asks, monitors)
    closable = (state == "OK" and (cause_status == "confirmed" or remedy == "none_needed")
                and remedy in CLOSING_REMEDIES)
    if closable:
        return Verdict(CLOSED, [], [], monitors)
    if items and not unscheduled:
        return Verdict(MONITORING, [], [], monitors)

    why = []
    if state != "OK":
        why.append(f"the incident is {state}")
    if remedy not in CLOSING_REMEDIES:
        why.append(f"remedy is {remedy}")
    elif cause_status != "confirmed" and remedy != "none_needed":
        why.append(f"cause is {cause_status}")
    gap = (f"agent-owned item(s) with no re-check time: {', '.join(unscheduled)}" if unscheduled
           else "nothing open is owned to finish it")
    return Verdict(INCOMPLETE, [f"not closed ({'; '.join(why)}) and {gap}"], [], monitors)


def status_line(v: Verdict) -> str:
    token = TOKEN.format(verdict=v.verdict)
    if v.verdict == CLOSED:
        return f"✅ Session complete — safe to close. {token}"
    if v.verdict == NEEDS_YOU:
        return f"⏸ Session paused — waiting on you for: {'; '.join(v.asks)}. {token}"
    if v.verdict == MONITORING:
        return f"🔁 Session monitoring — not closed: {'; '.join(v.monitors)}. {token}"
    return f"⚠️ Session incomplete — {'; '.join(v.problems)}. {token}"


# ---- the record on disk (read by the Stop hook) ----------------------------------------------

def record_path(session_id: str, *, root: str | None = None) -> str:
    if not session_id or not _SID.match(session_id):
        raise ValueError(f"not a session id: {session_id!r}")
    return os.path.join(root or ROOT, f"{session_id}.json")


def write(record: dict, session_id: str, *, turn_id: str = "", root: str | None = None,
          now: float | None = None) -> dict:
    """Compute the verdict and store record+verdict for `session_id` (atomic, 0600)."""
    v = compute(record)
    doc = {"schema": SCHEMA, "session_id": session_id, "turn_id": turn_id,
           "recorded_at": time.time() if now is None else now, "record": record, **v.as_dict()}
    path = record_path(session_id, root=root)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".closeout-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return doc


def read(session_id: str, *, root: str | None = None) -> dict | None:
    try:
        with open(record_path(session_id, root=root), encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None
