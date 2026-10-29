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
- ``improvement`` — required only when the record carries ``improvement_policy.required``
  (an agent opts its incident turns in; Hal's alarm turns do). What this fire left behind for
  whoever answers the NEXT page: a MERGED PR, ``{"kind": "cause_fix"|"diagnosability", "pr":
  <url>}`` (diagnosability also names its ``signal``: metric | log_line | alarm_definition |
  alarm_description | runbook_query | retune). ``check_improvement`` verifies it with ``gh``
  (its exit code becomes a close-out step); a filed issue, an unmerged PR, or a PR that only
  touches the agent's own prose (``improvement_policy.prose_only``: repo + path globs, the
  agent's config) is rejected. The one exception, ``{"kind": "already_diagnosable", "query":
  <the one command>, "output": <link to its output>}``, holds only when ``cause`` is
  ``confirmed`` with ``identified_by`` equal to that same query. Why: of 24 labs alarm
  incidents since 2026-08-26, ~10 ended "cause unknown, needs someone with AWS SSO" and ~8
  "improved" only Hal's own skill prose — the next page was exactly as hard to answer.

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
import subprocess
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

IMPROVEMENT_KINDS = ("cause_fix", "diagnosability", "already_diagnosable")
SIGNALS = ("metric", "log_line", "alarm_definition", "alarm_description", "runbook_query", "retune")
_PR_URL = re.compile(r"^https?://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)(?:[/?#].*)?$")
_PR_REF = re.compile(r"^([\w.-]+/[\w.-]+)#(\d+)$")
_ISSUE_URL = re.compile(r"^https?://github\.com/[\w.-]+/[\w.-]+/issues/\d+")
_GH_FILES_CAP = 100  # `gh pr view --json files` returns at most this many

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

    problems += improvement_problems(rec, cause)

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


# ---- the improvement: what this fire left for whoever answers the next page -----------------

def _glob_re(pattern: str) -> re.Pattern:
    """A path glob: ``**/`` = any dirs (incl. none), ``**`` = anything, ``*`` = within a segment."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(f"^{out}$")


def prose_only(repo: str, files: list[str], policy: dict) -> bool:
    """True when every changed file is the agent's own prose per ``policy['prose_only']``."""
    if not files:
        return False
    for rule in policy.get("prose_only") or []:
        if not isinstance(rule, dict) or _s(rule.get("repo")).lower() != repo.lower():
            continue
        pats = [_glob_re(_s(p)) for p in rule.get("paths") or [] if _s(p)]
        if pats and all(any(p.match(f) for p in pats) for f in files):
            return True
    return False


def _pr_ref(ref: str) -> tuple[str, str] | None:
    m = _PR_URL.match(ref) or _PR_REF.match(ref)
    return (m.group(1), m.group(2)) if m else None


def _single_command(q: str) -> bool:
    return bool(q) and "\n" not in q and not re.search(r"&&|\|\||;", q)


def improvement_problems(rec: dict, cause: dict) -> list[str]:
    """Why the record's ``improvement`` does not count (empty = it counts, or none is required)."""
    policy = rec.get("improvement_policy") if isinstance(rec.get("improvement_policy"), dict) else {}
    if not policy.get("required"):
        return []
    imp = rec.get("improvement")
    if not isinstance(imp, dict) or not imp:
        return ["no improvement recorded — an incident turn is not done until a MERGED PR fixes the "
                "cause or makes the next fire easier to diagnose (kind cause_fix | diagnosability: "
                "a metric, log line, alarm definition/description, runbook query or retune). When "
                "the cause is unknown, ship the missing signal"]
    kind = _s(imp.get("kind")).lower()
    if kind not in IMPROVEMENT_KINDS:
        return [f"improvement.kind {kind or 'missing'!r} is not one of {', '.join(IMPROVEMENT_KINDS)}"]
    if kind == "already_diagnosable":
        query, out = _s(imp.get("query")), _s(imp.get("output"))
        bad = []
        if not _single_command(query):
            bad.append("already_diagnosable needs the ONE query/command that diagnosed this fire "
                       "(a single command, not a sequence)")
        if not re.match(r"^https?://", out):
            bad.append("already_diagnosable needs a link to that query's output excerpt")
        if (_s(cause.get("status")).lower() != "confirmed"
                or " ".join(_s(cause.get("identified_by")).split()) != " ".join(query.split())):
            bad.append("already_diagnosable only holds when this record's cause is 'confirmed' "
                       "with identified_by = that same query — else the fire was not diagnosable "
                       "in one step, so ship the signal that would have made it so")
        return bad
    if kind == "diagnosability" and _s(imp.get("signal")).lower() not in SIGNALS:
        return [f"a diagnosability improvement names its signal: one of {', '.join(SIGNALS)}"]
    ref = _s(imp.get("pr"))
    if not ref:
        return [f"improvement ({kind}) names no PR — record its merged PR URL"]
    if _ISSUE_URL.match(ref):
        return [f"improvement {ref} is a filed issue, not a merged PR — an issue helps nobody "
                "answer the next page until something ships"]
    if not _pr_ref(ref):
        return [f"improvement {ref!r} is not a GitHub PR URL"]
    chk = imp.get("checked") if isinstance(imp.get("checked"), dict) else None
    if not chk:
        return [f"improvement {ref} was not checked — record it via `canopy closeout record`, "
                "which verifies it with gh"]
    if chk.get("error"):
        return [f"improvement {ref} could not be verified as a PR: {chk['error']}"]
    if _s(chk.get("state")).upper() != "MERGED":
        return [f"improvement {ref} is {_s(chk.get('state')).upper() or 'not merged'}, not MERGED — "
                "the turn is not done until it merges"]
    files = [f for f in chk.get("files") or [] if isinstance(f, str)]
    if len(files) < _GH_FILES_CAP and prose_only(_s(chk.get("repo")), files, policy):
        return [f"improvement {ref} only changes the agent's own skill/prose files "
                f"({', '.join(files[:5])}) — that helps one agent's prompt, not whoever answers "
                "the page; ship the signal (connect-labs instrumentation, alarm description, "
                "runbook query) instead"]
    return []


def check_improvement(record: dict, run=None) -> dict:
    """Verify the record's improvement PR with ``gh`` (I/O). Returns a NEW record whose
    ``improvement.checked`` is what gh said — anything the turn typed there is discarded — and
    whose ``closeout_steps`` gain the check's exit code. No PR to check → record unchanged."""
    rec = json.loads(json.dumps(record if isinstance(record, dict) else {}))
    imp = rec.get("improvement")
    if not isinstance(imp, dict):
        return rec
    imp.pop("checked", None)
    ref = _s(imp.get("pr"))
    if not ref or _s(imp.get("kind")).lower() == "already_diagnosable" or _ISSUE_URL.match(ref):
        return rec
    pr = _pr_ref(ref)
    if not pr:
        return rec
    repo, num = pr
    run = run or _run
    code, out, err = run(["gh", "pr", "view", num, "-R", repo, "--json", "state,mergedAt,url,files"])
    try:
        doc = json.loads(out) if code == 0 else None
    except ValueError:
        doc = None
    if isinstance(doc, dict):
        imp["checked"] = {"repo": repo, "number": int(num), "url": _s(doc.get("url")),
                          "state": _s(doc.get("state")).upper(), "merged_at": doc.get("mergedAt"),
                          "files": [_s(f.get("path")) for f in doc.get("files") or []
                                    if isinstance(f, dict)]}
    else:
        imp["checked"] = {"repo": repo, "number": int(num),
                          "error": (_s(err).splitlines() or [f"gh exited {code}"])[0][:200]}
    steps = rec.get("closeout_steps") if isinstance(rec.get("closeout_steps"), list) else []
    rec["closeout_steps"] = steps + [{"name": f"gh pr view {repo}#{num} (improvement merged?)",
                                      "exit_code": code}]
    return rec


def _run(args, timeout=60):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)


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
