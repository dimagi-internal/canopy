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


def anchor_origin_ref(huddle: str, type_: str, team: str, leader: str, members,
                      priorities_brief: str = "") -> dict:
    """The anchor's tags. A huddle planned with a brief carries it, so canopy-web's huddle page
    can say which priority each idea serves ("Serves priority 2: …")."""
    ref = {"kind": "huddle", "huddle": huddle, "type": type_, "team": team,
           "leader": leader, "members": list(members)}
    if priorities_brief:
        ref["priorities_brief"] = priorities_brief
    return ref


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
    # {(round_no, variant): template text} from `round<N>-<variant>.md` — e.g. `round1-nobrief.md`,
    # the pre-brief round 1 a huddle planned without `--priorities-file` still gets.
    variants: dict = dataclasses.field(default_factory=dict)


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
    rounds, variants = {}, {}
    for n in range(1, MAX_ROUNDS + 1):
        f = root.joinpath(f"round{n}.md")
        if f.is_file():
            rounds[n] = f.read_text(encoding="utf-8")
    for f in root.iterdir():
        m = _VARIANT_FILE.match(f.name)
        if m and int(m.group(1)) in rounds:
            variants[(int(m.group(1)), m.group(2))] = f.read_text(encoding="utf-8")
    if not rounds:
        raise ValueError(f"huddle type {type_!r} has no round templates")
    return HuddleType(type_, rounds, schema, variants)


_VARIANT_FILE = re.compile(r"^round(\d+)-([a-z][a-z0-9_]*)\.md$")


def render_round(ht: HuddleType, round_no: int, ctx: dict, variant: str = "") -> str:
    """`{{var}}` substitution. A missing var is an error naming it — never a silent blank
    in a prompt an agent will act on. `variant` picks `round<N>-<variant>.md` when the type
    ships one (else the plain round template)."""
    if round_no not in ht.rounds:
        raise ValueError(f"huddle type {ht.name!r} has no round {round_no}")
    tmpl = ht.variants.get((round_no, variant)) or ht.rounds[round_no]
    missing = sorted({m for m in _VAR.findall(tmpl) if m not in ctx})
    if missing:
        raise ValueError(f"round {round_no} template needs: {', '.join(missing)}")
    return _VAR.sub(lambda m: str(ctx[m.group(1)]), tmpl)


def validate_block(ht: HuddleType, round_no: int, block) -> list[str]:
    """Problems with a reply block against the type's schema; [] means it is usable.

    A round spec is either `{"fields": …}` or `{"shapes": {"<name>": {"fields": …}, …}}` — a
    block is usable when it fits ANY shape (so a huddle planned before a template change still
    validates); otherwise the problems of the shape it was written in are returned: the one
    whose field names it uses most (then the one with fewest problems)."""
    if not isinstance(block, dict):
        return ["block is not a JSON object"]
    spec = (ht.schema.get("rounds") or {}).get(str(round_no))
    if spec is None:
        return [f"huddle type {ht.name!r} has no round {round_no}"]
    probs = [f"missing {k}" for k in ("huddle", "round", "member") if k not in block]
    shapes = [sh.get("fields") or {} for sh in (spec.get("shapes") or {}).values()]
    found = [_field_problems(block, f) for f in shapes or [spec.get("fields") or {}]]
    if any(not f for f in found):
        return probs
    best = max(range(len(found)),
               key=lambda i: (_uses(block, shapes[i]) if shapes else 0, -len(found[i])))
    return probs + found[best]


def _uses(block: dict, fields: dict) -> int:
    """How many of a shape's field names (top-level and per-item) the block uses."""
    n = 0
    for key, rule in fields.items():
        if key not in block:
            continue
        n += 1
        if isinstance(block[key], list):
            names = set(rule.get("item_fields") or []) | set(rule.get("item_types") or {})
            n += sum(1 for item in block[key] if isinstance(item, dict) for f in names if f in item)
    return n


def _field_problems(block: dict, fields: dict) -> list[str]:
    probs = []
    for key, rule in fields.items():
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


def _dig(item: dict, path: str):
    """`item["a"]["b"]` for the dotted path "a.b"; None when any step is missing."""
    for part in path.split("."):
        if not isinstance(item, dict):
            return None
        item = item.get(part)
    return item


def _type_ok(v, want: str) -> bool:
    if want == "int":     # a priority number: 2, or "2" / "#2" from a sloppy writer
        return priority_number(v) is not None
    if want == "dict":
        return isinstance(v, dict)
    if want == "str":
        return isinstance(v, str)
    if want == "list":
        return isinstance(v, list)
    return True


def _item_problems(key: str, items: list, rule: dict) -> list[str]:
    """Per-item checks of a list field: `item_fields` (required keys), `item_types`
    ({field: int|dict|str|list}), `item_enums` ({field: [allowed]}; a dotted field such as
    `cost_to_jonathan.kind` reaches into a dict), `item_required_when` ({field: {other: value}}
    — e.g. a revised `proposal` is required when `resolution` is `accept`) and `item_unique`
    ([field] — e.g. at most one lever per priority)."""
    probs = []
    seen: dict = {}
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            if rule.get("item_fields"):
                probs += [f"{key}[{i}] missing {f}" for f in rule["item_fields"]]
            continue
        probs += [f"{key}[{i}] missing {f}" for f in rule.get("item_fields") or []
                  if f not in item]
        for f, want in (rule.get("item_types") or {}).items():
            if f in item and not _type_ok(item[f], want):
                probs.append(f"{key}[{i}] {f} must be "
                             + ("a number" if want == "int" else f"a {want}"))
        for f, allowed in (rule.get("item_enums") or {}).items():
            v = _dig(item, f)
            if v is not None and str(v).strip().lower() not in allowed:
                probs.append(f"{key}[{i}] {f} must be one of {'|'.join(allowed)}")
        for f in rule.get("item_unique") or []:
            v = priority_number(item.get(f)) if f == "priority" else item.get(f)
            if v is not None and v in seen.setdefault(f, set()):
                probs.append(f"{key}[{i}] repeats {f} {v} (at most one per {f})")
            seen[f].add(v)
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


# ── the priorities brief ─────────────────────────────────────────────────────────
# The principal's top priorities, written for the team before the huddle (Ada fetches it from
# Eva, who reads the principal's goals): a numbered list, each line
# `N. <priority> — hard dates: <dates or none> — source: <where>`, plus an optional `Not now:`.
# With a brief, members work TOWARD it — round 1 asks for levers on its priorities, round 2's
# `priority` is a brief NUMBER — instead of each guessing the priorities (huddle
# work-fleet-20261006: 3 of 4 members had not read the goals and produced four different lists).
_BRIEF_ITEM = re.compile(r"^\s*#?(\d+)[.):]\s+(.+?)\s*$")


def brief_items(brief: str) -> dict[int, str]:
    """{N: "<the priority line>"} for every numbered line of the brief."""
    out = {}
    for line in (brief or "").splitlines():
        m = _BRIEF_ITEM.match(line)
        if m and int(m.group(1)) not in out:
            out[int(m.group(1))] = m.group(2)
    return out


def brief_section(brief: str, principal: str) -> str:
    """The block every round prompt opens with when the huddle has a brief ('' without one)."""
    text = (brief or "").strip()
    if not text:
        return ""
    who = f"{principal}'s" if principal else "The principal's"
    return (f"## {who} top priorities (the brief — work toward these; do not re-derive them)\n\n"
            f"{text}\n\n")


def priority_number(v) -> int | None:
    """A proposal's / lever's priority as a brief number: 2, "2", "#2", "2. Funders…"."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    m = re.match(r"^\s*(?:priority\s*)?#?(\d+)\b", str(v or ""), re.I)
    return int(m.group(1)) if m else None


def priority_label(v, brief: str) -> str:
    """"priority 2: <its line>" for a brief number, else the free-text priority as given."""
    n = priority_number(v)
    items = brief_items(brief)
    if n is not None and n in items:
        return f"priority {n}: {items[n].split(' — ')[0].strip()}"
    return str(v or "")


# ── merge before agreement ───────────────────────────────────────────────────────
# Two proposals that serve the same priority with overlapping people are probably the same
# work (work-fleet-20261006: Ace's and Eva's IDM-demo proposals were one piece of work, both
# were co-signed and filed, and the principal had to decline one). The leader resolves every
# overlap in `merges.json` before round 3 — `absorbs` (keep one, drop the others, partners
# become the union) or `distinct_from` (they really are different) — and round 3, `agree` and
# `file` refuse while one is unresolved.
_OWN_KEYS = ("overlaps", "unresolved_overlaps")


def _priority_key(v):
    n = priority_number(v)
    return ("n", n) if n is not None else ("t", norm(v))


def people_of(p: dict) -> set[str]:
    return {m for m in [p.get("lead"), *(p.get("with") or [])] if m}


def find_overlaps(props: list[dict]) -> list[dict]:
    """Each proposal with `overlaps` (other titles: same priority AND a shared person — lead or
    `with`) and `unresolved_overlaps` (those not resolved by a `distinct_from`). New dicts."""
    out = []
    for p in props:
        key, people = _priority_key(p.get("priority")), people_of(p)
        distinct = {norm(t) for t in p.get("distinct_from") or []}
        over, open_ = [], []
        for q in props:
            if q is p or norm(q.get("title")) == norm(p.get("title")):
                continue
            if key[1] in (None, "") or _priority_key(q.get("priority")) != key:
                continue
            if not people & people_of(q):
                continue
            over.append(q.get("title"))
            if norm(q.get("title")) not in distinct and norm(p.get("title")) not in {
                    norm(t) for t in q.get("distinct_from") or []}:
                open_.append(q.get("title"))
        out.append({**{k: v for k, v in p.items() if k not in _OWN_KEYS},
                    "overlaps": over, "unresolved_overlaps": open_})
    return out


def apply_merges(props: list[dict], merges: dict | None, *, strict: bool = True
                 ) -> tuple[list[dict], list[str]]:
    """Fold the leader's `merges.json` into the proposals, then mark overlaps. Returns
    (proposals, problems).

    `{"<kept>": {"absorbs": ["<title>", …], "why": "…"}, "<title>": {"distinct_from": [...],
    "why": "…"}}`: an absorbed proposal is dropped; the kept one's `with` becomes the union of
    both proposals' people (minus its lead), it gains each absorbed lead's part in
    `ask_of_partners` (unless it already asks something of them) and records `absorbed` +
    `merge_why`. `distinct_from` is recorded on the proposal (both directions count).
    `strict` (before round 3, where every title must exist) reports a title that matches no
    proposal; `file` runs it lenient — the leader may already have dropped one."""
    out = [dict(p) for p in props]
    idx = {norm(p.get("title")): p for p in out}
    probs: list[str] = []
    gone: set[str] = set()
    for kept_title, m in (merges or {}).items():
        kp = idx.get(norm(kept_title))
        if not isinstance(m, dict):
            probs.append(f"merges[{kept_title!r}] must be an object")
            continue
        if kp is None:
            if strict:
                probs.append(f"merges: no proposal titled {kept_title!r}")
            continue
        for t in m.get("absorbs") or []:
            a = idx.get(norm(t))
            if a is None or a is kp:
                if strict or a is kp:
                    probs.append(f"merges: {kept_title!r} cannot absorb {t!r}"
                                 + (" (itself)" if a is kp else " (no such proposal)"))
                continue
            if norm(t) in gone:
                continue
            gone.add(norm(t))
            lead = kp.get("lead")
            kp["with"] = [x for x in dict.fromkeys([*(kp.get("with") or []), a.get("lead"),
                                                    *(a.get("with") or [])]) if x and x != lead]
            asks = dict(a.get("ask_of_partners") or {})
            asks.update(kp.get("ask_of_partners") or {})
            if a.get("lead") and a.get("lead") != lead and a.get("lead") not in asks:
                asks[a["lead"]] = (f"your part of the merged work (you proposed "
                                   f"\"{a.get('title')}\" for the same priority)")
            kp["ask_of_partners"] = {k: v for k, v in asks.items() if k != lead}
            kp["absorbed"] = list(dict.fromkeys([*(kp.get("absorbed") or []), a.get("title")]))
            if m.get("why"):
                kp["merge_why"] = m["why"]
        for t in m.get("distinct_from") or []:
            other = idx.get(norm(t))
            if other is None:
                if strict:
                    probs.append(f"merges: {kept_title!r} distinct_from {t!r}: no such proposal")
                continue
            kp["distinct_from"] = list(dict.fromkeys([*(kp.get("distinct_from") or []), t]))
    kept = [p for p in out if norm(p.get("title")) not in gone]
    return find_overlaps(kept), probs


def unresolved_overlaps(props: list[dict]) -> list[str]:
    """One line per unresolved overlapping pair — what a refusal names."""
    lines, seen = [], set()
    for p in props:
        for t in p.get("unresolved_overlaps") or []:
            pair = frozenset((norm(p.get("title")), norm(t)))
            if pair in seen:
                continue
            seen.add(pair)
            lines.append(f"\"{p.get('title')}\" (lead {p.get('lead')}) ↔ \"{t}\" — same priority "
                         f"{p.get('priority')!r}, shared people")
    return lines


# ── one ask list for the principal ───────────────────────────────────────────────
_ASK_IDS = re.compile(r"\b[TP]\d+\b")


def is_principal(who, principal: str) -> bool:
    """Whether a round-1 `needs[].from` names the principal ("jonathan", "Jonathan Jackson")."""
    w = norm(who)
    names = {"principal", norm(principal)}
    if norm(principal):
        names.add(norm(principal).split()[0])
    return bool(w) and (w in names or w.split()[0] in names)


def asks_of_principal(r1_blocks: dict, proposals: list[dict], principal: str) -> list[dict]:
    """Everything the team needs from the principal, once: round-1 `needs` addressed to them
    and each proposal's `cost_to_jonathan` (kind ≠ none), as `{"ask", "who", "for"}`
    (`kind` too: `need`, or the cost's kind). Near-identical asks merge — same normalized
    text, or the same task/project id (T22, P3) — and keep every member asking."""
    raw = []
    for member, b in sorted((r1_blocks or {}).items()):
        for n in (b or {}).get("needs") or []:
            if isinstance(n, dict) and is_principal(n.get("from"), principal) and n.get("ask"):
                raw.append({"ask": str(n["ask"]).strip(), "who": [member], "for": "",
                            "kind": "need"})
    for p in proposals or []:
        c = p.get("cost_to_jonathan")
        if not isinstance(c, dict) or norm(c.get("kind")) in ("", "none"):
            continue
        raw.append({"ask": str(c.get("detail") or c.get("kind")).strip(),
                    "who": [m for m in [p.get("lead")] if m], "for": p.get("title") or "",
                    "kind": norm(c.get("kind"))})
    out: list[dict] = []
    for a in raw:
        ids = set(_ASK_IDS.findall(a["ask"]))
        same = next((o for o in out if norm(o["ask"]) == norm(a["ask"])
                     or (ids and ids & set(_ASK_IDS.findall(o["ask"])))), None)
        if same is None:
            out.append(a)
            continue
        same["who"] = list(dict.fromkeys([*same["who"], *a["who"]]))
        fors = [f for f in (same["for"], a["for"]) if f]
        same["for"] = "; ".join(dict.fromkeys("; ".join(fors).split("; "))) if fors else ""
        if same["kind"] == "need" and a["kind"] != "need":
            same["kind"] = a["kind"]
    return out


# ── the gates ────────────────────────────────────────────────────────────────────
def _serves_stated_priority(priority, r1_priorities) -> bool:
    want = norm(priority)
    if not want:
        return False
    return any(want == x or want in x for x in (norm(p) for p in r1_priorities))


def _priority_problem(priority, r1_priorities, brief_numbers) -> str:
    """'' when the proposal serves a priority it may serve: a brief number when the huddle has
    a brief, else (pre-brief huddles) a priority some member stated in round 1."""
    n = priority_number(priority)
    if brief_numbers and n is not None:
        return "" if n in brief_numbers else f"priority {n} is not in the brief"
    if _serves_stated_priority(priority, r1_priorities):
        return ""
    if brief_numbers:
        return f"priority is not a brief number: {priority!r}"
    return f"priority not stated in any round-1 report: {priority!r}"


def work_gates(proposals: list[dict], r1_priorities, prior_declined: list[dict],
               brief_numbers=None) -> tuple[list[dict], list[dict]]:
    """Split proposals into (filed, held). Each held one carries `held`: the reason.

    - a joint proposal files only when EVERY partner co-signed (an unresolved `amend` holds,
      and so does one the lead rejected in round 4 or an agreement thread closed without
      agreement; one accepted either way counts as a co-sign),
      and so must a lead someone else named (`proposed_by` ≠ `lead`);
    - it must serve a priority — a number in the brief (`brief_numbers`) when the huddle has
      one, else one some member stated in round 1 — and live in a project;
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
        elif off_brief := _priority_problem(p.get("priority"), r1_priorities, brief_numbers):
            reason = off_brief
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
        for k in ("kind", "cost_to_jonathan", "fails_if", "absorbed"):
            if p.get(k):
                out[k] = p[k]
        return out

    return {"version": 1,
            **{k: meta.get(k) for k in ("id", "type", "team", "leader", "members",
                                        "leader_turn", "started_at", "priorities_brief")},
            "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "outcomes": ([row(p, "filed") for p in filed]
                         + [row(p, f"held: {p['held']}") for p in held]),
            "not_reached": list(not_reached)}
