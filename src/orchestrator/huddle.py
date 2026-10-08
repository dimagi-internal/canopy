"""Huddle — a team of agents syncs, led by one of them. The PURE half.

Ids, idempotency keys, the `origin_ref` tags canopy-web derives a huddle from, huddle-type
loading / rendering / validation, and the `work` type's gates. No I/O lives here:
`huddle_cli` talks to canopy-web and `huddle_store` to Drive.

A huddle is never stored as a conversation. canopy-web already holds every piece of it — the
round turn's prompt (what the leader asked, critique included), the member's close-out or
transcript (what it answered), and the provenance that hangs the rounds together — so the
engine only TAGS what it dispatches and reads the conversation back from canopy-web's derived
`/api/huddles/` view. The one thing written anywhere new is the clean-outcomes record in the
leader's Drive (`Process State/Huddles/<id>.json`), which the next huddle of the same team
reads so it does not re-raise what the principal already declined.

Spec: docs/superpowers/specs/2026-10-06-huddle-design.md.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import re
from importlib.resources import files

# Round 4 ("resolve") is CONDITIONAL: only the lead (and a proposer who named it) of a proposal
# that drew an `amend` in round 3 gets one. A type may ship fewer rounds than this.
MAX_ROUNDS = 4
MAX_PROPOSALS = 3
MAX_OUTCOMES = 5
# The fence a member wraps its reply in. canopy-web parses the same fence (its own copy).
_FENCE = re.compile(r"```huddle\s*\n(.*?)\n```", re.S)
_VAR = re.compile(r"\{\{(\w+)\}\}")
_TYPE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")

# What a co-signing partner may answer in round 3.
COSIGN, AMEND, DECLINE = "co-sign", "amend", "decline"
# What a round-4 resolution turns a partner's `amend` into. `amend→accepted` counts as a
# co-sign (the lead folded the change in); `amend→rejected` holds the proposal.
AMEND_ACCEPTED, AMEND_REJECTED = "amend→accepted", "amend→rejected"
ACCEPT, REJECT = "accept", "reject"
# What an AGREEMENT THREAD (`canopy thread`, the replacement for the relayed round 4) turns an
# `amend` into when it closes without agreement: declined, out of messages, or out of time. The
# proposal is held, and the reason says who did not agree and why (`thread_held_reason`).
AMEND_NOT_AGREED = "amend→not agreed"


# ── ids, keys, tags ──────────────────────────────────────────────────────────────
def huddle_id(type_: str, team: str, day: dt.date, taken: set[str]) -> str:
    """`<type>-<team>-<YYYYMMDD>`, suffixed `-2`, `-3`… when that id is already taken."""
    base = f"{type_}-{team}-{day:%Y%m%d}"
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def idempotency_key(huddle: str, member: str, round_no: int, attempt: int) -> str:
    """One key per (huddle, member, round, attempt): a re-run of the same dispatch dedupes,
    a deliberate re-send (`--attempt k+1`) is a new turn. Doubles as the thread key, so a
    round never lands in the member's (or the leader's) live `main` session."""
    return f"huddle-{huddle}-{member}-r{int(round_no)}-a{int(attempt)}"


def round_origin_ref(huddle: str, type_: str, round_no: int, member: str, attempt: int) -> dict:
    return {"kind": "huddle_round", "huddle": huddle, "type": type_, "round": int(round_no),
            "member": member, "attempt": int(attempt),
            "thread_key": idempotency_key(huddle, member, round_no, attempt)}


def anchor_origin_ref(huddle: str, type_: str, team: str, leader: str, members) -> dict:
    return {"kind": "huddle", "huddle": huddle, "type": type_, "team": team,
            "leader": leader, "members": list(members)}


def closeout_session_id(huddle: str, member: str, round_no: int) -> str:
    """The `--session-id` a member files its round reply under — stable, so re-filing a
    corrected block replaces the first one instead of adding a second."""
    return f"huddle:{huddle}:{member}:r{int(round_no)}"


# ── huddle types ─────────────────────────────────────────────────────────────────
@dataclasses.dataclass(frozen=True)
class HuddleType:
    name: str
    rounds: dict          # {round_no: template text}
    schema: dict          # schema.json: {"rounds": {"1": {"fields": {...}}, ...}}


def load_type(type_: str) -> HuddleType:
    """A huddle type is DATA shipped as package data under `huddle_types/<type>/`:
    `round<N>.md` templates and `schema.json` for the reply block."""
    if not _TYPE_NAME.match(type_ or ""):
        raise ValueError(f"bad huddle type name {type_!r}")
    root = files("orchestrator.huddle_types").joinpath(type_)
    schema_file = root.joinpath("schema.json")
    if not schema_file.is_file():
        raise ValueError(f"unknown huddle type {type_!r} (no huddle_types/{type_}/schema.json)")
    schema = json.loads(schema_file.read_text(encoding="utf-8"))
    rounds = {}
    for n in range(1, MAX_ROUNDS + 1):
        f = root.joinpath(f"round{n}.md")
        if f.is_file():
            rounds[n] = f.read_text(encoding="utf-8")
    if not rounds:
        raise ValueError(f"huddle type {type_!r} has no round templates")
    return HuddleType(type_, rounds, schema)


def render_round(ht: HuddleType, round_no: int, ctx: dict) -> str:
    """`{{var}}` substitution. A missing var is an error naming it — never a silent blank
    in a prompt an agent will act on."""
    if round_no not in ht.rounds:
        raise ValueError(f"huddle type {ht.name!r} has no round {round_no}")
    tmpl = ht.rounds[round_no]
    missing = sorted({m for m in _VAR.findall(tmpl) if m not in ctx})
    if missing:
        raise ValueError(f"round {round_no} template needs: {', '.join(missing)}")
    return _VAR.sub(lambda m: str(ctx[m.group(1)]), tmpl)


def validate_block(ht: HuddleType, round_no: int, block) -> list[str]:
    """Problems with a reply block against the type's schema; [] means it is usable."""
    if not isinstance(block, dict):
        return ["block is not a JSON object"]
    spec = (ht.schema.get("rounds") or {}).get(str(round_no))
    if spec is None:
        return [f"huddle type {ht.name!r} has no round {round_no}"]
    probs = [f"missing {k}" for k in ("huddle", "round", "member") if k not in block]
    for key, rule in (spec.get("fields") or {}).items():
        if key not in block:
            if rule.get("required"):
                probs.append(f"missing {key}")
            continue
        v = block[key]
        if rule.get("type") == "list":
            if not isinstance(v, list):
                probs.append(f"{key} must be a list")
            elif "max" in rule and len(v) > rule["max"]:
                probs.append(f"{key} has {len(v)} items (max {rule['max']})")
            else:
                probs += _item_problems(key, v, rule)
        elif rule.get("type") == "str" and not isinstance(v, str):
            probs.append(f"{key} must be a string")
    return probs


def _item_problems(key: str, items: list, rule: dict) -> list[str]:
    """Per-item checks of a list field: `item_fields` (required keys), `item_enums`
    ({field: [allowed]}) and `item_required_when` ({field: {other: value}} — e.g. a revised
    `proposal` is required when `resolution` is `accept`)."""
    probs = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            if rule.get("item_fields"):
                probs += [f"{key}[{i}] missing {f}" for f in rule["item_fields"]]
            continue
        probs += [f"{key}[{i}] missing {f}" for f in rule.get("item_fields") or []
                  if f not in item]
        for f, allowed in (rule.get("item_enums") or {}).items():
            if f in item and str(item[f]).strip().lower() not in allowed:
                probs.append(f"{key}[{i}] {f} must be one of {'|'.join(allowed)}")
        for f, when in (rule.get("item_required_when") or {}).items():
            if f in item and item[f]:
                continue
            if all(str(item.get(k, "")).strip().lower() == v for k, v in when.items()):
                cond = " and ".join(f"{k} is {v}" for k, v in when.items())
                probs.append(f"{key}[{i}] missing {f} (required when {cond})")
    return probs


def extract_block(text: str, huddle: str, round_no: int) -> tuple[dict | None, str]:
    """The LAST ```huddle block in `text` naming this huddle and round, and '' — or None
    and why not. A block copied from an earlier huddle (or round) never counts."""
    err = ""
    for raw in reversed(_FENCE.findall(text or "")):
        try:
            b = json.loads(raw)
        except ValueError as e:
            err = err or f"reply block is not valid JSON: {e}"
            continue
        if not isinstance(b, dict):
            err = err or "reply block is not a JSON object"
            continue
        try:
            rnd = int(b.get("round") or 0)
        except (TypeError, ValueError):
            rnd = 0
        if str(b.get("huddle")) == huddle and rnd == int(round_no):
            return b, ""
        err = err or f"block names huddle {b.get('huddle')!r} round {b.get('round')!r}"
    return None, err


# ── the `work` type's gates ──────────────────────────────────────────────────────
def norm(s) -> str:
    """Case/punctuation-insensitive form for matching titles and priorities."""
    return re.sub(r"\W+", " ", str(s or "").lower()).strip()


def partners_of(p: dict) -> list[str]:
    return [m for m in (p.get("with") or []) if m and m != p.get("lead")]


def named_lead(p: dict) -> bool:
    """True when someone other than the lead proposed this and named it lead — the lead never
    consented, so it must answer in round 3 like a partner."""
    by = p.get("proposed_by")
    return bool(by and p.get("lead") and by != p.get("lead"))


def cosigners_of(p: dict) -> list[str]:
    """Everyone whose round-3 `co-sign` the gate needs: the partners, plus the lead when a
    teammate named it lead."""
    out = partners_of(p)
    if named_lead(p) and p["lead"] not in out:
        out.append(p["lead"])
    return out


def amenders_of(p: dict) -> list[str]:
    """The co-signers whose round-3 answer is a still-open `amend`."""
    ans = p.get("answers") or {}
    return [m for m in cosigners_of(p) if ans.get(m) == AMEND]


def resolvers_of(p: dict) -> list[str]:
    """Who gets round 4 for this proposal: its lead, plus the teammate who proposed it when
    that teammate named someone else lead — each only for an amend that is not its own. [] when
    nothing is amended (no amend → no round 4)."""
    amenders = amenders_of(p)
    out = []
    for r in (p.get("lead"), p.get("proposed_by") if named_lead(p) else None):
        if r and r not in out and any(a != r for a in amenders):
            out.append(r)
    return out


def _name(slug) -> str:
    return str(slug or "").strip().capitalize()


def thread_held_reason(p: dict, member: str) -> str:
    """"Eva and Echo didn't agree: <why>" — the plain reason an amend settled in an agreement
    thread holds its proposal."""
    t = (p.get("threads") or {}).get(member) or {}
    other = t.get("author") or p.get("lead")
    return f"{_name(other)} and {_name(member)} didn't agree: {t.get('why') or 'no reason given'}"


def agreement_author(p: dict, amender: str) -> str:
    """Who answers an amend in an agreement thread: the proposal's lead — or, when the lead is
    the one amending (a teammate named it lead), the teammate who proposed it."""
    lead = p.get("lead") or ""
    if amender == lead:
        return p.get("proposed_by") or ""
    return lead


def apply_thread_outcomes(p: dict, threads: list[dict]) -> dict:
    """Fold agreement-thread outcomes into one proposal's open amends (returns a new dict).

    A thread belongs to an amend when its `parent` names this proposal (title + lead) and its
    participants include the amender; the newest wins. Settled AGREED → `amend→accepted`
    (adopting the thread's proposal, same title and lead, when it carries one); closed any other
    way → `amend→not agreed` (held: "Eva and Echo didn't agree: <why>"); OPEN → the amend stays
    open. Only an amend still open is touched — a round-4 resolution is never overridden.
    Every matched thread is recorded in `p["threads"][amender]`."""
    from orchestrator import thread as T
    p = {**p, "answers": dict(p.get("answers") or {}), "threads": dict(p.get("threads") or {})}
    title, lead = norm(p.get("title")), p.get("lead")
    mine = [t for t in threads or []
            if norm((t.get("parent") or {}).get("title")) == title
            and (t.get("parent") or {}).get("lead") in (None, "", lead)]
    mine.sort(key=lambda t: str(t.get("created_at") or ""))
    for m in amenders_of(p):
        ts = [t for t in mine if m in T.agents_of(t)]
        if not ts:
            continue
        t = ts[-1]
        author = next((a for a in T.agents_of(t) if a != m), agreement_author(p, m))
        result = T.result_of(t)
        p["threads"][m] = {"id": t.get("id"), "status": t.get("status"), "result": result,
                           "author": author, "why": T.why_of(t) if result != T.OPEN else ""}
        if result == T.AGREED:
            revised = (t.get("outcome") or {}).get("proposal")
            if isinstance(revised, dict) and revised:
                keep = {k: p[k] for k in ("title", "lead", "proposed_by", "answers",
                                          "answer_notes", "threads") if k in p}
                p = {**p, **revised, **keep, "revised": True}
            p["answers"][m] = AMEND_ACCEPTED
        elif result == T.NOT_AGREED:
            p["answers"][m] = AMEND_NOT_AGREED
    return p


def _serves_stated_priority(priority, r1_priorities) -> bool:
    want = norm(priority)
    if not want:
        return False
    return any(want == x or want in x for x in (norm(p) for p in r1_priorities))


def work_gates(proposals: list[dict], r1_priorities, prior_declined: list[dict]
               ) -> tuple[list[dict], list[dict]]:
    """Split proposals into (filed, held). Each held one carries `held`: the reason.

    - a joint proposal files only when EVERY partner co-signed (an unresolved `amend` holds,
      and so does one the lead rejected in round 4 or an agreement thread closed without
      agreement; one accepted either way counts as a co-sign),
      and so must a lead someone else named (`proposed_by` ≠ `lead`);
    - it must serve a priority some member stated in round 1, and live in a project;
    - the leader's critique must have been answered;
    - nothing the principal declined before, unless it brings `new_evidence`;
    - at most MAX_OUTCOMES filed, in the order given (the leader ranks before filing).
    """
    declined = {norm(d.get("title")): d for d in prior_declined or []
                if str(d.get("fate", "")).startswith("declined")}
    filed, held = [], []
    for p in proposals:
        ans = p.get("answers") or {}
        partners = cosigners_of(p)
        refused = [m for m in partners if ans.get(m) == DECLINE]
        rejected = [m for m in partners if ans.get(m) == AMEND_REJECTED]
        not_agreed = [m for m in partners if ans.get(m) == AMEND_NOT_AGREED]
        amended = [m for m in partners if ans.get(m) == AMEND]
        talking = [m for m in amended
                   if ((p.get("threads") or {}).get(m) or {}).get("result") == "open"]
        missing = [m for m in partners if ans.get(m) not in (COSIGN, AMEND_ACCEPTED)]
        project = p.get("project") or {}
        project_name = project.get("name") if isinstance(project, dict) else project
        reason = ""
        if refused:
            reason = f"{refused[0]} declined"
        elif rejected:
            reason = f"amend rejected by lead ({', '.join(rejected)})"
        elif not_agreed:
            reason = thread_held_reason(p, not_agreed[0])
        elif talking:
            reason = ("amend still being settled in thread "
                      f"{p['threads'][talking[0]].get('id')} ({', '.join(talking)})")
        elif amended:
            reason = f"amend unresolved ({', '.join(amended)})"
        elif missing:
            reason = f"{missing[0]} has not co-signed"
        elif not _serves_stated_priority(p.get("priority"), r1_priorities):
            reason = f"priority not stated in any round-1 report: {p.get('priority')!r}"
        elif not str(project_name or "").strip():
            reason = "no project named"
        elif not p.get("critique_answered"):
            reason = "critique not answered"
        elif norm(p.get("title")) in declined and not p.get("new_evidence"):
            reason = (f"declined before ({declined[norm(p.get('title'))].get('fate')}) "
                      "and no new evidence")
        elif len(filed) >= MAX_OUTCOMES:
            reason = f"over the {MAX_OUTCOMES}-outcome cap"
        if reason:
            held.append({**p, "held": reason})
        else:
            filed.append(p)
    return filed, held


def outcome_record(meta: dict, filed: list[dict], held: list[dict],
                   not_reached: list[dict]) -> dict:
    """The clean-outcomes Drive record (spec §1). No messages, prompts or round state."""
    def row(p, fate):
        out = {"title": p.get("title"), "lead": p.get("lead"), "partners": partners_of(p),
               "priority": p.get("priority"), "project": p.get("project"),
               "task": p.get("task"), "cosign": dict(p.get("answers") or {}),
               "why": p.get("why"), "fate": fate}
        if p.get("partner_tasks"):
            out["partner_tasks"] = p["partner_tasks"]
        return out

    return {"version": 1,
            **{k: meta.get(k) for k in ("id", "type", "team", "leader", "members",
                                        "leader_turn", "started_at")},
            "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "outcomes": ([row(p, "filed") for p in filed]
                         + [row(p, f"held: {p['held']}") for p in held]),
            "not_reached": list(not_reached)}
