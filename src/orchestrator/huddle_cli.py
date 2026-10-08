"""`canopy huddle …` — the huddle engine. A team of agents syncs, led by one of them.

Stateless between steps; canopy-web is the state. Every round turn the engine dispatches is
TAGGED (`origin_ref.kind = huddle_round` + huddle/round/member/attempt) and PARENTED on the
huddle's anchor turn, and canopy-web derives the conversation back out of those tags through
`GET /api/huddles/<id>` — the prompt each member got, its reply block (from its close-out, else
its transcript), and the board tasks the huddle produced. Nothing re-records prompts, replies or
round state here. The one write outside canopy-web is the clean-outcomes record in the leader's
Drive (`huddle_store`).

    plan      → huddle id, the anchor turn, one context pack per member, and the principal's
                priorities brief (`--priorities-file`) every prompt opens with  (plan.json)
    prompt    → one member's round-N prompt from its type's template         (<m>-rN.md)
                (round 3 refuses while two proposals overlap and `--merges` does not resolve it)
    dispatch  → a tagged, parented, unpinned round turn
    await     → foreground; exit 0 when every dispatched member settled, 3 = run it again
    status    → per member per round, derived from canopy-web
    resume    → an unfinished huddle < 72 h old, and what to do next
    agree     → one AGREEMENT THREAD per open amend (lead ↔ amender, `canopy thread`);
                the leader then moderates each with `canopy thread run`
    proposals → round 2/3 proposals merged with round-3 co-sign answers and how each
                amend was settled (agreement thread, or a relayed round 4), the leader's
                merges applied, each one's `overlaps`; `asks_of_jonathan`  (props.json)
    file      → gates → board tasks (lead + partners) → Drive record → anchor finished
    view      → the huddle page URL

The leader's procedure (what to run in what order, and the judgment between the steps) is
`plugins/canopy/agent-core/huddle.md`. Spec: docs/superpowers/specs/2026-10-06-huddle-design.md.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

import click

from orchestrator import canopy_web
from orchestrator import huddle as H
from orchestrator.agent_client import AgentClient, CanopyError, _rows

# Seams for tests.
_sleep = time.sleep


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


ROUND_DEADLINE = dt.timedelta(minutes=90)
RESUME_WINDOW = dt.timedelta(hours=72)
FAILED = {"failed", "lost", "error", "expired", "cancelled", "canceled"}
CLOSED_TASK = {"done", "declined"}
ROUND_NAMES = {1: "report", 2: "roundtable", 3: "co-sign", 4: "resolve"}


class HuddleError(click.ClickException):
    """A refusal with a reason; exit 2 by default (a usage problem, not a crash)."""
    exit_code = 2


# ── small helpers ────────────────────────────────────────────────────────────────
def _emit(obj) -> None:
    click.echo(json.dumps(obj, indent=2, default=str))


def _call(method: str, path: str, body=None):
    return canopy_web.call(method, path, body)


def _parse_ts(s) -> dt.datetime | None:
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _read_json(path) -> object:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise HuddleError(f"cannot read {path}: {e}")


def _leader_default() -> str:
    from orchestrator.agent_dispatch import local_agent_slug
    return local_agent_slug()


def _store(repo: str, local: str | None):
    from orchestrator import huddle_store
    if local:
        return huddle_store.LocalHuddleStore(Path(local))
    return huddle_store.DriveHuddleStore(Path(repo))


def _client(slug: str) -> AgentClient:
    return AgentClient({"slug": slug})


def page_url(base_url: str, workspace: str | None, huddle: str) -> str:
    base = (base_url or "").rstrip("/")
    return f"{base}/w/{workspace}/huddles/{huddle}" if workspace else f"{base}/huddles/{huddle}"


def board_url(base_url: str, workspace: str | None, slug: str) -> str:
    base = (base_url or "").rstrip("/")
    return f"{base}/w/{workspace}/agents/{slug}/work" if workspace else f"{base}/agents/{slug}/work"


def _first_line(text: str, limit: int = 90) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line and not line.startswith("<!--"):
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return ""


def _bullets(value) -> str:
    """A critique entry: a string as-is, a list as `- ` bullets, anything else as JSON."""
    if value is None or value == "" or value == []:
        return "none"
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(f"- {v}" for v in value)
    return json.dumps(value, indent=2)


def get_detail(huddle: str) -> dict:
    try:
        return _call("GET", f"/api/huddles/{quote(huddle, safe='')}")
    except CanopyError as e:
        raise HuddleError(f"cannot read huddle {huddle!r} from canopy-web: {e}")


def list_huddles(agent: str = "", limit: int = 200) -> list[dict]:
    path = f"/api/huddles/?limit={int(limit)}" + (f"&agent={quote(agent)}" if agent else "")
    return _rows(_call("GET", path))


def blocks_by(detail: dict) -> dict:
    """{(member, round): block} for every cell that has a parsed reply block."""
    return {(c.get("member"), int(c.get("round") or 0)): c["block"]
            for c in detail.get("cells") or [] if isinstance(c.get("block"), dict)}


def huddle_threads(huddle: str) -> list[dict]:
    """The agreement threads this huddle opened (`GET /api/threads/?parent_key=huddle…`).
    Advisory for reads: a canopy-web without the threads API yields [] with a warning, so a
    huddle that never opened a thread works exactly as before."""
    from orchestrator.thread_cli import list_threads
    try:
        return list_threads(parent_key="huddle", parent_value=huddle)
    except CanopyError as e:
        click.echo(f"[huddle] warning: could not read {huddle}'s agreement threads: {e}", err=True)
        return []


def with_thread_outcomes(props: list[dict], threads: list[dict]) -> list[dict]:
    return [H.apply_thread_outcomes(p, threads) for p in props] if threads else props


def _merges(path) -> dict:
    if not path:
        return {}
    m = _read_json(path)
    if not isinstance(m, dict):
        raise HuddleError(f"{path}: merges must be a JSON object "
                          '({"<kept title>": {"absorbs": ["<title>"], "why": "…"}, '
                          '"<title>": {"distinct_from": ["<title>"], "why": "…"}})')
    return m


def merged_proposals(detail: dict, merges: dict, *, strict: bool = True) -> list[dict]:
    """`collect_proposals` with the leader's merges applied and overlaps marked. A merges.json
    naming a proposal that does not exist is refused (a typo would silently resolve nothing)."""
    props, probs = H.apply_merges(collect_proposals(detail), merges, strict=strict)
    if probs:
        raise HuddleError("; ".join(probs))
    return props


def refuse_unresolved(props: list[dict], what: str) -> None:
    """Exit 2 naming every overlapping pair the leader has not resolved in merges.json."""
    lines = H.unresolved_overlaps(props)
    if lines:
        raise HuddleError(
            f"{what} refused — these proposals overlap (same priority, shared people) and "
            "merges.json does not resolve them:\n  " + "\n  ".join(lines) + "\n"
            "Write merges.json — {\"<kept title>\": {\"absorbs\": [\"<title>\"], \"why\": \"…\"}} "
            "or {\"<title>\": {\"distinct_from\": [\"<title>\"], \"why\": \"…\"}} — and pass "
            "--merges merges.json.")


# ── context packs (plan) ─────────────────────────────────────────────────────────
def pack_text(tasks, projects, turns, since: dt.datetime) -> str:
    """The leader's survey of one member — what it has open, its projects, its recent turns.
    The member is told to correct it, so this is a starting point, not a verdict."""
    open_tasks = [t for t in tasks or [] if str(t.get("status") or "") not in CLOSED_TASK]
    lines = ["Open board tasks:"]
    lines += [f"- {t.get('ext_id')} [{t.get('status')}] {t.get('title')}" for t in open_tasks[:10]]
    if not open_tasks:
        lines.append("- none")
    if len(open_tasks) > 10:
        lines.append(f"- … and {len(open_tasks) - 10} more")
    lines.append("Projects:")
    live = [p for p in projects or [] if (p.get("status") or "active") == "active"]
    lines += [f"- {p.get('ext_id')} {p.get('name')}"
              + (f" — {p['outcome']}" if p.get("outcome") else "") for p in live[:10]]
    if not live:
        lines.append("- none")
    lines.append(f"Turns since {since:%Y-%m-%d}:")
    recent = []
    for t in turns or []:
        ref = t.get("origin_ref") or {}
        if str(ref.get("kind") or "").startswith("huddle"):
            continue
        at = _parse_ts(t.get("created_at"))
        if at is None or at < since:
            continue
        what = _first_line(t.get("prompt") or "") or t.get("report_title") or t.get("origin") or ""
        recent.append(f"- {at:%Y-%m-%d} {t.get('status')} {what}".rstrip())
    lines += recent[:12] or ["- none"]
    return "\n".join(lines)


def _member_pack(slug: str, since: dt.datetime, task_cache: dict) -> str:
    parts = []
    try:
        tasks = _client(slug).list_tasks()
        task_cache[slug] = tasks
    except (CanopyError, RuntimeError) as e:
        tasks = []
        parts.append(f"(could not read {slug}'s board: {str(e)[:160]})")
    try:
        projects = _client(slug).list_projects()
    except (CanopyError, RuntimeError) as e:
        projects = []
        parts.append(f"(could not read {slug}'s projects: {str(e)[:160]})")
    try:
        turns = _rows(_call("GET", f"/api/harness/turns/?agent={quote(slug)}&limit=100"))
    except (CanopyError, RuntimeError) as e:
        turns = []
        parts.append(f"(could not read {slug}'s turns: {str(e)[:160]})")
    return "\n".join([pack_text(tasks, projects, turns, since), *parts])


def refresh_fates(records: list[dict], task_cache: dict) -> list[dict]:
    """A record says `filed` when it was written; the principal decides later, on the board.
    Re-read each filed outcome's task so a decline is known (and not re-raised)."""
    out = []
    for rec in records or []:
        rec = json.loads(json.dumps(rec))
        for o in rec.get("outcomes") or []:
            task = o.get("task") or {}
            if not str(o.get("fate") or "").startswith("filed") or not task.get("agent"):
                continue
            agent = task["agent"]
            if agent not in task_cache:
                try:
                    task_cache[agent] = _client(agent).list_tasks()
                except (CanopyError, RuntimeError):
                    task_cache[agent] = None
            rows = task_cache.get(agent) or []
            live = next((t for t in rows if t.get("ext_id") == task.get("ext_id")), None)
            if live is None:
                continue
            status = str(live.get("status") or "")
            if status == "declined":
                o["fate"] = "declined: " + (str(live.get("review") or "").strip()
                                            or "declined on the board")
            elif status in ("done", "in_progress"):
                o["fate"] = f"filed; {status.replace('_', ' ')}"
            elif status == "suggested":
                o["fate"] = "filed; awaiting decision"
        out.append(rec)
    return out


def prior_text(records: list[dict]) -> str:
    lines = []
    for rec in records or []:
        for o in rec.get("outcomes") or []:
            lines.append(f"- {o.get('title')} (lead {o.get('lead')}) — {o.get('fate')}"
                         f"  [{rec.get('id')}]")
    return "\n".join(lines) or "none yet"


# ── proposals (shared by prompt rounds 3-4, proposals, file) ─────────────────────
def _answer(raw) -> str:
    a = H.norm(raw)
    if a in ("co sign", "cosign", "co signed", "cosigned"):
        return H.COSIGN
    if a.startswith("amend"):
        return H.AMEND
    if a.startswith("decline"):
        return H.DECLINE
    return a


def collect_proposals(detail: dict) -> list[dict]:
    """Every proposal in round-2/3 blocks keyed by (lead, title) — a lead's round-3 revision
    wins — with each partner's round-3 answer, any round-4 resolution of an `amend`, and
    whether the lead answered the critique."""
    cells = sorted((c for c in detail.get("cells") or [] if isinstance(c.get("block"), dict)),
                   key=lambda c: (int(c.get("round") or 0), str(c.get("member"))))
    props: dict = {}
    order: list = []
    r2_answered: dict = {}
    for c in cells:
        rnd, member, block = int(c.get("round") or 0), c.get("member"), c["block"]
        if rnd == 2:
            r2_answered[member] = bool(block.get("critique_answers"))
        if rnd not in (2, 3):
            continue
        for p in block.get("proposals") or []:
            if not isinstance(p, dict) or not p.get("title"):
                continue
            lead = p.get("lead") or member
            key = (lead, H.norm(p["title"]))
            prev = props.get(key, {})
            if rnd == 3 and member not in (lead, prev.get("proposed_by")):
                continue  # only the lead, or whoever proposed it, revises a proposal
            props[key] = {**prev, **p, "lead": lead, "proposed_by": prev.get("proposed_by", member),
                          "round": rnd, "revised": rnd == 3 and bool(prev),
                          "answers": prev.get("answers", {}),
                          "answer_notes": prev.get("answer_notes", {})}
            if key not in order:
                order.append(key)
    for c in cells:
        if int(c.get("round") or 0) != 3:
            continue
        for a in c["block"].get("answers") or []:
            if not isinstance(a, dict):
                continue
            t = H.norm(a.get("title"))
            matches = [k for k in order if k[1] == t and (not a.get("lead") or k[0] == a.get("lead"))]
            for k in matches:
                props[k]["answers"][c.get("member")] = _answer(a.get("answer"))
                if a.get("note"):
                    props[k]["answer_notes"][c.get("member")] = a.get("note")
    _apply_resolutions(cells, props, order)
    out = []
    for k in order:
        p = props[k]
        # the critique went to whoever proposed it — the lead, or the teammate who named it
        p["critique_answered"] = (bool(r2_answered.get(p["lead"]))
                                  or bool(r2_answered.get(p.get("proposed_by")))
                                  or bool(p.get("revised")))
        out.append(p)
    return out


def _apply_resolutions(cells: list[dict], props: dict, order: list) -> None:
    """Fold round-4 resolutions in. Only a proposal's resolver (`H.resolvers_of`: its lead, or
    the teammate who named that lead) counts; when both answered, the lead's wins.
    `accept` with a revised proposal replaces the proposal (same lead and key) and turns each
    open amend into `amend→accepted`; `reject` turns them into `amend→rejected` (held).
    An `accept` without a revised proposal resolves nothing — the amend stays open."""
    picked: dict = {}
    for c in cells:
        if int(c.get("round") or 0) != 4:
            continue
        member = c.get("member")
        for res in c["block"].get("resolutions") or []:
            if not isinstance(res, dict):
                continue
            t = H.norm(res.get("title"))
            for k in order:
                if k[1] != t or (res.get("lead") and k[0] != res.get("lead")):
                    continue
                if member not in H.resolvers_of(props[k]):
                    continue
                if k in picked and picked[k][0] == props[k]["lead"]:
                    continue                       # the lead already answered; it wins
                picked[k] = (member, res)
    for k, (member, res) in picked.items():
        p = props[k]
        amenders = [m for m in H.amenders_of(p) if m != member]   # before a revision edits `with`
        verdict = H.norm(res.get("resolution"))
        revised = res.get("proposal")
        if verdict == H.ACCEPT and isinstance(revised, dict) and revised:
            outcome = H.AMEND_ACCEPTED
            p = props[k] = {**p, **revised, "title": p["title"], "lead": p["lead"],
                            "proposed_by": p.get("proposed_by"), "round": 4, "revised": True,
                            "answers": p["answers"], "answer_notes": p["answer_notes"]}
        elif verdict == H.REJECT:
            outcome = H.AMEND_REJECTED
        else:
            continue
        for m in amenders:
            p["answers"][m] = outcome
        p["resolution"] = {"by": member, "resolution": verdict, "note": res.get("note") or ""}


def resolve_text(props: list[dict], member: str) -> str:
    """Round 4's body for one resolver: each proposal it must resolve, verbatim, then each
    amend on it — who sent it and the note, verbatim."""
    parts = []
    for p in props:
        if member not in H.resolvers_of(p):
            continue
        shown = _shown(p)
        notes = p.get("answer_notes") or {}
        amends = "\n".join(f"- {m}: {json.dumps(notes.get(m) or '(no note given)', ensure_ascii=False)}"
                           for m in H.amenders_of(p) if m != member)
        parts.append(f"### {p['title']} (lead {p['lead']})\n```json\n{json.dumps(shown, indent=2)}\n```\n"
                     f"Amendments:\n{amends}")
    return "\n\n".join(parts)


# ── await states ─────────────────────────────────────────────────────────────────
def cell_state(cell: dict, ht, deadline: dt.datetime | None, now: dt.datetime
               ) -> tuple[str, list[str]]:
    """(state, problems). Settled states: replied, malformed, failed, timed_out.
    A round turn's `done` means its session was SPAWNED, not that the member replied —
    so `done` without a block is still pending."""
    block = cell.get("block")
    if isinstance(block, dict):
        probs = H.validate_block(ht, int(cell.get("round") or 0), block)
        return ("malformed", probs) if probs else ("replied", [])
    status = str(cell.get("status") or "")
    if status in FAILED:
        return "failed", [cell.get("reply_error")] if cell.get("reply_error") else []
    if deadline is not None and now > deadline:
        return "timed_out", []
    return f"pending:{status or 'unknown'}", ([cell["reply_error"]] if cell.get("reply_error") else [])


def round_deadline(cells: list[dict]) -> dt.datetime | None:
    starts = [t for t in (_parse_ts(c.get("created_at")) for c in cells) if t]
    return (min(starts) + ROUND_DEADLINE) if starts else None


def round_states(detail: dict, round_no: int, now: dt.datetime) -> dict:
    ht = H.load_type(detail.get("type") or "work")
    cells = [c for c in detail.get("cells") or [] if int(c.get("round") or 0) == round_no]
    deadline = round_deadline(cells)
    states, problems = {}, {}
    for c in cells:
        st, probs = cell_state(c, ht, deadline, now)
        states[c.get("member")] = st
        if probs:
            problems[c.get("member")] = probs
    return {"states": states, "problems": problems,
            "deadline_at": _iso(deadline) if deadline else None}


def _settled(state: str) -> bool:
    return not state.startswith("pending:")


# ── digest ───────────────────────────────────────────────────────────────────────
def _clip(s, n) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def digest_text(huddle: str, filed: list[dict], held: list[dict], page: str,
                base_url: str, workspace: str | None, brief: str = "",
                asks: list[dict] | None = None) -> str:
    """The principal's email body: one line per filed outcome linking the lead's board, one
    held line, the asks waiting on the principal (each once), the huddle page. Plain text,
    short — the board is where he decides."""
    if not filed:
        return ""
    lines = [f"Huddle {huddle} — {len(filed)} proposal{'s' if len(filed) != 1 else ''} for you", ""]
    for p in filed:
        partners = H.partners_of(p)
        who = p.get("lead", "") + (f" + {', '.join(partners)}" if partners else "")
        if p.get("fails_if"):
            risk = f"would fail if {_clip(p['fails_if'], 70)}"
        else:   # a pre-brief proposal still carries a confidence
            try:
                risk = f"{round(float(p.get('confidence') or 0) * 100)}%"
            except (TypeError, ValueError):
                risk = "?"
        lines.append(f"• {_clip(p.get('title'), 80)} — {who} · serves "
                     f"\"{_clip(H.priority_label(p.get('priority'), brief), 60)}\""
                     f" · {p.get('effort') or '?'} · {risk}")
        lines.append(f"  {board_url(base_url, workspace, p.get('lead', ''))}")
    if held:
        reasons = "; ".join(sorted({str(h.get("held")) for h in held}))
        lines += ["", f"Held: {len(held)} ({_clip(reasons, 80)})"]
    if asks:
        lines += ["", "Waiting on you:"]
        lines += [f"• {_clip(a['ask'], 100)} — {', '.join(a.get('who') or [])}"
                  + (f" (for {_clip(a['for'], 50)})" if a.get("for") else "") for a in asks]
    lines += ["", f"The whole conversation: {page}", "Accept or decline each on the board."]
    return "\n".join(lines)


# ── commands ─────────────────────────────────────────────────────────────────────
@click.group("huddle")
def huddle_group():
    """A team of agents syncs, led by one of them (leader procedure: agent-core/huddle.md)."""


@huddle_group.command("plan")
@click.option("--leader", default="", help="Leading agent's slug (default: this repo's agent).")
@click.option("--team", required=True, help="Team name, e.g. fleet.")
@click.option("--type", "type_", default="work", show_default=True, help="Huddle type.")
@click.option("--members", required=True, help="Comma-separated member slugs.")
@click.option("--days", default=7, show_default=True, type=int, help="Review window.")
@click.option("--principal", default="the principal", show_default=True)
@click.option("--sharing-rule", default="Share freely within the team.", show_default=True)
@click.option("--repo", default=".", show_default=True, help="Leader's repo (Drive identity).")
@click.option("--local", default=None, help="Keep outcome records in this dir, not Drive.")
@click.option("--priorities-file", default=None, type=click.Path(exists=True, dir_okay=False),
              help="The principal's priorities brief (numbered lines). Every round prompt opens "
                   "with it, and proposals serve its numbers.")
@click.option("--out", required=True, type=click.Path(dir_okay=False))
def plan_cmd(leader, team, type_, members, days, principal, sharing_rule, repo, local,
             priorities_file, out):
    """Start a huddle: pick its id, tag the anchor turn, build one context pack per member."""
    leader = leader or _leader_default()
    if not leader:
        raise HuddleError("no --leader and this is not an agent repo")
    ht = H.load_type(type_)
    brief = ""
    if priorities_file:
        brief = Path(priorities_file).read_text(encoding="utf-8").strip()
        if not H.brief_items(brief):
            raise HuddleError(f"{priorities_file} has no numbered priorities "
                              "(expected lines like `1. <priority> — hard dates: … — source: …`)")
    else:
        click.echo("[huddle] WARNING: no --priorities-file — members will each GUESS the "
                   f"priorities in round 1 (the pre-brief huddle). Pass {principal}'s brief.",
                   err=True)
    roster = [m.strip() for m in members.split(",") if m.strip() and m.strip() != leader]
    if not roster:
        raise HuddleError("--members names nobody (the leader is not its own member)")
    now = _now()
    try:
        taken = {r.get("id") for r in list_huddles(agent=leader)}
    except CanopyError as e:
        raise HuddleError(f"canopy-web huddle API unreachable — a huddle cannot run without it: {e}")
    hid = H.huddle_id(ht.name, team, now.date(), taken)
    leader_client = _client(leader)
    try:
        workspace = (leader_client.get_agent() or {}).get("workspace") or None
        anchor = leader_client.post_turn(
            cli_session_id=f"huddle:{hid}", title=f"Huddle {hid}", summary="", source="huddle",
            emdash_task_id="",
            origin_ref=H.anchor_origin_ref(hid, ht.name, team, leader, roster, brief))
    except (CanopyError, RuntimeError) as e:
        raise HuddleError(f"could not record the huddle's anchor turn: {e}")
    since = now - dt.timedelta(days=days)
    task_cache: dict = {}
    packs = {m: _member_pack(m, since, task_cache) for m in roster}
    try:
        records = _store(repo, local).recent(team)
    except Exception as e:  # noqa: BLE001 — memory is advisory at plan time; file fails loud
        click.echo(f"[huddle] warning: could not read earlier huddle records: {e}", err=True)
        records = []
    records = refresh_fates(records, task_cache)
    base = canopy_web.resolve_base_url(None)
    plan = {"id": hid, "type": ht.name, "team": team, "leader": leader, "members": roster,
            "principal": principal, "sharing_rule": sharing_rule, "days": days,
            "since": f"{since:%Y-%m-%d}", "started_at": _iso(now),
            "anchor_turn_id": str(anchor.get("id") or ""), "workspace": workspace,
            "base_url": base, "page": page_url(base, workspace, hid),
            "packs": packs, "prior_text": prior_text(records), "prior": records,
            "priorities_brief": brief}
    Path(out).write_text(json.dumps(plan, indent=2), encoding="utf-8")
    _emit({"huddle": hid, "anchor_turn_id": plan["anchor_turn_id"], "members": roster,
           "page": plan["page"], "plan": str(out),
           "priorities": len(H.brief_items(brief)) or None})


def _load_plan(path) -> dict:
    plan = _read_json(path)
    if not isinstance(plan, dict) or not plan.get("id"):
        raise HuddleError(f"{path} is not a huddle plan (run `canopy huddle plan`)")
    return plan


def _critique(path) -> dict:
    if not path:
        return {}
    c = _read_json(path)
    if not isinstance(c, dict):
        raise HuddleError(f"{path}: critique must be a JSON object "
                          '({"<member>": questions, "proposals": {"<title>": critique}})')
    return c


@huddle_group.command("prompt")
@click.option("--plan", "plan_path", required=True, type=click.Path(exists=True))
@click.option("--member", required=True)
@click.option("--round", "round_no", required=True, type=int)
@click.option("--critique", "critique_path", default=None, type=click.Path(exists=True),
              help='JSON: {"<member>": questions, "proposals": {"<title>": critique}}')
@click.option("--merges", "merges_path", default=None, type=click.Path(exists=True),
              help='Rounds 3-4. JSON: {"<kept title>": {"absorbs": [...], "why": ""}, '
                   '"<title>": {"distinct_from": [...], "why": ""}}')
@click.option("--out", required=True, type=click.Path(dir_okay=False))
def prompt_cmd(plan_path, member, round_no, critique_path, merges_path, out):
    """Render one member's round-N prompt."""
    plan = _load_plan(plan_path)
    ht = H.load_type(plan.get("type") or "work")
    hid = plan["id"]
    crit = _critique(critique_path)
    principal = plan.get("principal") or "the principal"
    brief = plan.get("priorities_brief") or ""
    ctx = {"huddle": hid, "member": member, "leader": plan["leader"], "principal": principal,
           "principal_key": (H.norm(principal).split() or ["principal"])[0],
           "round": round_no, "today": f"{_now():%Y-%m-%d}",
           "brief": H.brief_section(brief, principal)}
    if round_no == 1:
        ctx.update(days=plan.get("days", 7), since=plan.get("since", ""),
                   context=(plan.get("packs") or {}).get(member, "(no survey — review yourself)"),
                   prior=plan.get("prior_text") or "none yet",
                   sharing_rule=plan.get("sharing_rule") or "")
    else:
        detail = get_detail(hid)
        blocks = blocks_by(detail)
        if (member, 1) not in blocks:
            raise HuddleError(f"{member} has no round-1 reply in {hid} — it cannot take part "
                              f"in round {round_no} (it is not_reached; see `canopy huddle status`)")
        ctx["critique"] = _bullets(crit.get(member))
        if round_no == 2:
            reports = []
            for m in plan.get("members") or []:
                if (m, 1) in blocks:
                    reports.append(f"### {m}\n```json\n{json.dumps(blocks[(m, 1)], indent=2)}\n```")
            ctx["reports"] = "\n\n".join(reports)
        else:
            props = merged_proposals(detail, _merges(merges_path))
            ctx["earlier"] = earlier_text(blocks, member, round_no)
            if round_no == 4:
                ctx["resolve"] = resolve_text(props, member)
                if not ctx["resolve"]:
                    raise HuddleError(f"nothing for {member} to resolve in round 4: no open "
                                      "`amend` on a proposal it leads or proposed (round 4 is "
                                      "only for those)")
            else:
                refuse_unresolved(props, "round 3")
                ctx.update(round3_sections(props, blocks, member, crit.get("proposals") or {}))
    try:
        text = H.render_round(ht, round_no, ctx, variant="" if brief else "nobrief")
    except ValueError as e:
        raise HuddleError(str(e))
    Path(out).write_text(text, encoding="utf-8")
    _emit({"huddle": hid, "member": member, "round": round_no, "prompt": str(out)})


def _shown(p: dict) -> dict:
    """A proposal as a member sees it: without the engine's bookkeeping."""
    return {k: v for k, v in p.items() if k not in _HIDDEN}


def earlier_text(blocks: dict, member: str, round_no: int) -> str:
    """The member's own round-1 and round-2 blocks, verbatim — what it said before (members
    of work-fleet-20261006 said round 3 lost their own earlier text). In round 3 the round-2
    `proposals` are left out here: they are quoted in full, with the critique, further down."""
    parts = []
    for n in (1, 2):
        b = blocks.get((member, n))
        if not isinstance(b, dict):
            continue
        if n == 2 and round_no == 3 and b.get("proposals"):
            b = {k: v for k, v in b.items() if k != "proposals"}
            note = " (your `proposals` are quoted below, each with its critique)"
        else:
            note = ""
        parts.append(f"Round {n}{note}:\n```json\n{json.dumps(b, indent=2, ensure_ascii=False)}\n```")
    return "\n\n".join(parts) or "nothing on record."


def round3_sections(props: list[dict], blocks: dict, member: str, per: dict) -> dict:
    """Round 3's `asks` (joint work naming the member) and `own` (its round-2 proposals with
    the critique; one the leader merged into another says so)."""
    asks = []
    for p in props:
        if member in H.partners_of(p):
            head = f"### {p['title']} (lead {p['lead']})"
        elif p["lead"] == member and H.named_lead(p):
            head = (f"### {p['title']} (lead {p['lead']} — {p['proposed_by']} named YOU "
                    "as lead; answer it like any joint ask)")
        else:
            continue
        asks.append(f"{head}\n```json\n{json.dumps(_shown(p), indent=2)}\n```\n"
                    f"Critique: {_bullets(per.get(p['title']))}")
    into = {H.norm(t): p for p in props for t in p.get("absorbed") or []}
    own = []
    for p in (blocks.get((member, 2)) or {}).get("proposals") or []:
        if not isinstance(p, dict) or not p.get("title"):
            continue
        kept = into.get(H.norm(p["title"]))
        tail = (f"MERGED into \"{kept['title']}\" (lead {kept['lead']}): "
                f"{kept.get('merge_why') or 'the same work'}. Do not revise it; answer the merged "
                "proposal above — `amend` it with anything of yours it lost." if kept
                else f"Critique: {_bullets(per.get(p['title']))}")
        own.append(f"### {p['title']}\n```json\n{json.dumps(p, indent=2)}\n```\n{tail}")
    if not asks and not own:
        raise HuddleError(f"nothing for {member} in round 3: no proposal asks it to "
                          "co-sign and it proposed none")
    return {"asks": "\n\n".join(asks) or "none — no teammate proposed joint work with you.",
            "own": "\n\n".join(own) or "none — you proposed nothing in round 2."}


def _is_mode_refusal(err: Exception) -> bool:
    msg = str(err).lower()
    return ("-> 403" in msg or "-> 422" in msg) and "mode" in msg


@huddle_group.command("dispatch")
@click.option("--plan", "plan_path", required=True, type=click.Path(exists=True))
@click.option("--member", required=True)
@click.option("--round", "round_no", required=True, type=int)
@click.option("--prompt-file", required=True, type=click.Path(exists=True))
@click.option("--attempt", default=1, show_default=True, type=int)
@click.option("--mode", type=click.Choice(["auto", "manual", "none"]), default="auto",
              show_default=True, help="Requested turn mode; `none` lets canopy-web decide.")
def dispatch_cmd(plan_path, member, round_no, prompt_file, attempt, mode):
    """Send one member its round turn — tagged, parented on the anchor, never pinned."""
    from orchestrator.agent_dispatch import TURNS_PATH, build_turn_payload
    plan = _load_plan(plan_path)
    hid, leader = plan["id"], plan["leader"]
    text = Path(prompt_file).read_text(encoding="utf-8")
    if not text.strip():
        raise HuddleError(f"{prompt_file} is empty")
    key = H.idempotency_key(hid, member, round_no, attempt)
    payload = build_turn_payload(member, prompt=text, idempotency_key=key, sender=leader,
                                 turn_mode=None if mode == "none" else mode)
    payload["origin_ref"].update(H.round_origin_ref(hid, plan.get("type") or "work",
                                                    round_no, member, attempt))
    if plan.get("anchor_turn_id"):
        payload["parent"] = {"turn": plan["anchor_turn_id"]}
    fallback = False
    try:
        turn = _call("POST", TURNS_PATH, payload)
    except CanopyError as e:
        if "turn_mode" not in payload or not _is_mode_refusal(e):
            raise HuddleError(f"dispatch to {member} failed: {e}")
        click.echo(f"[huddle] canopy-web refused turn_mode={payload['turn_mode']} for {member} "
                   f"({str(e)[:160]}); re-sending without a mode — its own rules decide.",
                   err=True)
        payload.pop("turn_mode")
        fallback = True
        try:
            turn = _call("POST", TURNS_PATH, payload)
        except CanopyError as e2:
            raise HuddleError(f"dispatch to {member} failed: {e2}")
    _emit({"turn_id": turn.get("id"), "status": turn.get("status"), "member": member,
           "round": round_no, "attempt": attempt, "idempotency_key": key,
           "mode_fallback": fallback})


@huddle_group.command("await")
@click.option("--huddle", "hid", required=True)
@click.option("--round", "round_no", required=True, type=int)
@click.option("--budget-seconds", default=540, show_default=True, type=int,
              help="How long this call waits before exiting 3 (run it again).")
@click.option("--poll", default=30, show_default=True, type=int)
def await_cmd(hid, round_no, budget_seconds, poll):
    """Wait in the FOREGROUND for a round. Exit 0 = every dispatched member settled
    (replied / malformed / failed / timed_out); exit 3 = still out, run it again."""
    started = time.monotonic()
    while True:
        detail = get_detail(hid)
        res = round_states(detail, round_no, _now())
        if not res["states"]:
            raise HuddleError(f"nothing dispatched for round {round_no} of {hid}")
        settled = all(_settled(s) for s in res["states"].values())
        if settled or time.monotonic() - started >= budget_seconds:
            _emit({"huddle": hid, "round": round_no, "settled": settled, **res})
            if not settled:
                sys.exit(3)
            return
        out = sum(1 for s in res["states"].values() if not _settled(s))
        click.echo(f"[huddle] round {round_no}: {out} member(s) still out", err=True)
        _sleep(max(1, poll))


@huddle_group.command("status")
@click.option("--huddle", "hid", required=True)
def status_cmd(hid):
    """One line per member: each round's state and where its reply came from."""
    detail = get_detail(hid)
    now = _now()
    rounds = sorted({int(c.get("round") or 0) for c in detail.get("cells") or []})
    per: dict = {}
    for n in rounds:
        res = round_states(detail, n, now)
        for c in detail.get("cells") or []:
            if int(c.get("round") or 0) != n:
                continue
            st = res["states"].get(c.get("member"), "?")
            src = c.get("reply_source") or "none"
            per.setdefault(c.get("member"), []).append(
                f"r{n} {st}" + (f" ({src})" if src != "none" else "")
                + (f" a{c.get('attempt')}" if int(c.get("attempt") or 1) > 1 else ""))
    click.echo(f"{hid} — {detail.get('type')} · team {detail.get('team')} · leader "
               f"{detail.get('leader')} · {'finished' if detail.get('finished') else 'in flight'}")
    for m in detail.get("members") or sorted(per):
        click.echo(f"  {m:<10} " + ("  ".join(per.get(m, [])) or "not dispatched"))


def next_steps(detail: dict, now: dt.datetime) -> list[str]:
    hid = detail.get("id")
    cells = detail.get("cells") or []
    if not cells:
        return [f"round 1: `canopy huddle prompt` + `dispatch` every member of {hid}"]
    last = max(int(c.get("round") or 0) for c in cells)
    res = round_states(detail, last, now)
    steps = []
    for c in cells:
        if int(c.get("round") or 0) != last:
            continue
        st = res["states"].get(c.get("member"), "")
        if st == "failed":
            steps.append(f"re-dispatch {c.get('member')} round {last} with "
                         f"--attempt {int(c.get('attempt') or 1) + 1} (check runner health first)")
    if any(not _settled(s) for s in res["states"].values()):
        steps.append(f"canopy huddle await --huddle {hid} --round {last}")
    elif not steps:
        if last == 2:
            steps.append("round 2 settled — `canopy huddle proposals`, resolve every overlap in "
                         "merges.json, then round 3 with `--merges merges.json`")
        elif last < 3:
            steps.append(f"round {last} settled — continue with round {last + 1}")
        elif last == 3:
            steps.append("round 3 settled — `canopy huddle proposals`; if any proposal has an "
                         "`amend`, `canopy huddle agree` and `canopy thread run` each thread to "
                         "its close, else `canopy huddle file`")
        else:
            steps.append(f"round {last} settled — `canopy huddle proposals` then "
                         "`canopy huddle file`")
    return steps


@huddle_group.command("resume")
@click.option("--leader", default="", help="Leading agent's slug (default: this repo's agent).")
def resume_cmd(leader):
    """The newest unfinished huddle this leader started < 72 h ago, and what to do next."""
    leader = leader or _leader_default()
    now = _now()
    rows = [r for r in list_huddles(agent=leader)
            if r.get("leader") == leader and not r.get("finished")
            and (t := _parse_ts(r.get("created_at"))) is not None and now - t < RESUME_WINDOW]
    if not rows:
        _emit({"huddle": None})
        return
    row = max(rows, key=lambda r: _parse_ts(r.get("created_at")))
    detail = get_detail(row["id"])
    rounds = {}
    for c in detail.get("cells") or []:
        rounds.setdefault(str(c.get("round")), {})[c.get("member")] = None
    for n in list(rounds):
        rounds[n] = round_states(detail, int(n), now)["states"]
    _emit({"huddle": row["id"], "type": detail.get("type"), "team": detail.get("team"),
           "anchor_turn_id": detail.get("anchor_turn_id"), "rounds": rounds,
           "next": next_steps(detail, now)})


def round1_blocks(detail: dict) -> dict:
    return {m: b for (m, n), b in blocks_by(detail).items() if n == 1}


@huddle_group.command("proposals")
@click.option("--huddle", "hid", required=True)
@click.option("--merges", "merges_path", default=None, type=click.Path(exists=True),
              help="The leader's merges.json (absorbs / distinct_from).")
@click.option("--principal", default="Jonathan", show_default=True,
              help="Whose asks `asks_of_jonathan` collects (round-1 needs `from` this name).")
@click.option("--out", default=None, type=click.Path(dir_okay=False))
def proposals_cmd(hid, merges_path, principal, out):
    """Round-2/3 proposals merged with round-3 co-sign answers and how each amend settled —
    `file`'s input, after the leader merges duplicates and ranks them. An amend settled AGREED
    in its agreement thread is `amend→accepted` (the thread's proposal adopted); one that closed
    any other way is `amend→not agreed` (held); an OPEN thread leaves it `amend`. `threads`
    names each amend's thread; `needs_agreement` lists amends with no thread yet (run
    `canopy huddle agree`); `resolvers` is who would owe a relayed round 4 instead.

    `overlaps` names the other proposals with the same priority and a shared person;
    `unresolved_overlaps` the ones `--merges` has not resolved (round 3, `agree` and `file`
    refuse while any remain). With `--out`, the file is the proposal list and stdout carries
    `asks_of_jonathan` — round-1 needs addressed to the principal plus every proposal's
    `cost_to_jonathan`, de-duplicated."""
    detail = get_detail(hid)
    props = with_thread_outcomes(merged_proposals(detail, _merges(merges_path)),
                                 huddle_threads(hid))
    for p in props:
        p["resolvers"] = H.resolvers_of(p)
        # open amends with no agreement thread yet — `canopy huddle agree` opens them
        p["needs_agreement"] = [m for m in H.amenders_of(p) if m not in (p.get("threads") or {})]
    asks = H.asks_of_principal(round1_blocks(detail), props, principal)
    if out:
        Path(out).write_text(json.dumps(props, indent=2), encoding="utf-8")
        _emit({"huddle": hid, "proposals": len(props), "out": str(out),
               "unresolved_overlaps": H.unresolved_overlaps(props),
               "asks_of_jonathan": asks})
    else:
        _emit(props)


_HIDDEN = ("answers", "answer_notes", "critique_answered", "proposed_by", "round", "revised",
           "resolution", "resolvers", "threads", "needs_agreement", "overlaps",
           "unresolved_overlaps", "distinct_from")


def agreement_context(p: dict, amender: str) -> str:
    """What an agreement thread opens with: the proposal verbatim and the amend verbatim."""
    shown = _shown(p)
    note = (p.get("answer_notes") or {}).get(amender) or "(no note given)"
    return (f"The proposal (lead {p.get('lead')}), verbatim:\n```json\n"
            f"{json.dumps(shown, indent=2, ensure_ascii=False)}\n```\n\n"
            f"{amender} answered `amend` — in, with changes. Its change request, verbatim:\n"
            f"{json.dumps(note, ensure_ascii=False)}"
            f"{_others_line(p, amender)}")


_ANSWER_WORDS = {"co-sign": "already said yes (co-signed)", "amend": "asked for changes too",
                 "decline": "said no", "amend→accepted": "had changes, now agreed",
                 "amend→rejected": "had changes, not agreed"}


def _others_line(p: dict, amender: str) -> str:
    """Who else the proposal involves and where they stand — so nobody in the thread invents a
    side conversation for them (first live thread, 2026-10-08: the author told the asker "Ace's
    side is a separate thread" when Ace had simply co-signed)."""
    lead = p.get("lead")
    others = [m for m in H.partners_of(p) if m not in (lead, amender)]
    if not others:
        return ""
    answers = p.get("answers") or {}
    threads = p.get("threads") or {}
    parts = []
    for m in others:
        a = str(answers.get(m) or "")
        where = _ANSWER_WORDS.get(a, "has not answered")
        if m in threads:
            where += f" and is settling it in its own thread ({threads[m].get('id')})"
        else:
            where += "; there is NO thread with them"
        parts.append(f"{m} {where}")
    return ("\n\nOthers on this proposal (not in this thread): " + "; ".join(parts) + ".")


@huddle_group.command("agree")
@click.option("--plan", "plan_path", required=True, type=click.Path(exists=True))
@click.option("--max-messages", default=4, show_default=True, type=int)
@click.option("--deadline-minutes", default=90, show_default=True, type=int)
@click.option("--dry-run", is_flag=True, help="List the threads it would open; open none.")
@click.option("--merges", "merges_path", default=None, type=click.Path(exists=True),
              help="The leader's merges.json — the same one round 3 was prompted with.")
def agree_cmd(plan_path, max_messages, deadline_minutes, dry_run, merges_path):
    """Open one AGREEMENT THREAD per open `amend`: the proposal's author (its lead) and the
    teammate who amended it settle the change directly. Idempotent — canopy-web hands back the
    open thread for the same proposal and pair. Then run `canopy thread run --thread <id>` on
    each, in the foreground, until it exits 0."""
    from orchestrator import thread as T
    from orchestrator.thread_cli import open_thread
    plan = _load_plan(plan_path)
    hid, leader = plan["id"], plan["leader"]
    threads = huddle_threads(hid)
    props = merged_proposals(get_detail(hid), _merges(merges_path))
    refuse_unresolved(props, "agree")
    props = with_thread_outcomes(props, threads)
    rows = []
    for p in props:
        for m in H.amenders_of(p):
            author = H.agreement_author(p, m)
            if not author or author == m:
                continue
            row = {"title": p["title"], "lead": p["lead"], "author": author, "asker": m}
            existing = (p.get("threads") or {}).get(m)
            if existing and existing.get("result") != T.OPEN:
                continue   # settled already — `proposals` carries its outcome
            if dry_run:
                rows.append({**row, "thread": (existing or {}).get("id")})
                continue
            purpose = _clip(f"Settle {m}'s change to \"{p['title']}\" (lead {p['lead']})", 300)
            try:
                th = open_thread(
                    kind=T.AGREEMENT, purpose=purpose,
                    participants=[{"agent": author, "role": "author"},
                                  {"agent": m, "role": "asker"}],
                    moderator=leader,
                    parent={"huddle": hid, "title": p["title"], "lead": p["lead"]},
                    context=agreement_context(p, m), max_messages=max_messages,
                    deadline_minutes=deadline_minutes)
            except CanopyError as e:
                raise HuddleError(f"could not open the agreement thread for {p['title']!r} "
                                  f"({author} ↔ {m}): {e}")
            rows.append({**row, "thread": th.get("id"), "status": th.get("status"),
                         "run": f"canopy thread run --thread {th.get('id')}"})
    _emit({"huddle": hid, "dry_run": dry_run, "threads": rows})


def _check(name: str, value: str) -> str:
    from orchestrator.agent_cli import check_task_field
    return check_task_field(name, value)


def _task_fields(p: dict, plan: dict, page: str) -> tuple[dict, list[dict]]:
    """The lead's task and each partner's — validated BEFORE anything is written: the fleet
    rejects an over-long field rather than truncating it, so the leader shortens it in
    outcomes.json and re-runs."""
    principal = plan.get("principal") or ""
    steps = [str(s) for s in (p.get("plan") or []) if str(s).strip()]
    partners = H.partners_of(p)
    try:
        conf = float(p.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    serves = H.priority_label(p.get("priority"), plan.get("priorities_brief") or "")
    notes = [f"From huddle {plan['id']} ({page})."]
    if partners:
        notes.append(f"Joint with {', '.join(partners)} (co-signed).")
    if p.get("absorbed"):
        notes.append(f"Merged in: {'; '.join(p['absorbed'])}.")
    if p.get("success_measure"):
        notes.append(f"Success: {p['success_measure']}")
    if p.get("effort"):
        notes.append(f"Effort: {p['effort']}")
    cost = p.get("cost_to_jonathan")
    if isinstance(cost, dict) and H.norm(cost.get("kind")) not in ("", "none"):
        notes.append(f"Needs from {principal or 'the principal'}: "
                     f"{cost.get('detail') or cost.get('kind')}")
    if p.get("fails_if"):
        notes.append(f"Would fail if: {p['fails_if']}")
    if p.get("too_late"):
        notes.append("Too late for a hard date in the brief (see why).")
    lead_task = {"title": _check("title", str(p.get("title") or "")),
                 "next_action": _check("next_action", steps[0] if steps else ""),
                 "status": "suggested", "owner": _check("owner", principal),
                 "assigned": _check("assigned", p.get("lead") or ""),
                 "confidence": "high" if conf >= 0.8 else "low",
                 "rationale": f"Serves: {serves}\n\n{p.get('why') or ''}".strip(),
                 "plan": "\n".join(f"- {s}" for s in steps),
                 "notes": "\n".join(notes), "origin": "huddle",
                 "source_url": _check("source_url", page),
                 "links": [{"label": "Huddle", "url": page}]}
    asks = p.get("ask_of_partners") or {}
    partner_tasks = []
    for m in partners:
        partner_tasks.append({
            "agent": m,
            "title": _check("title", f"{p.get('title')} — {m}'s part (lead {p.get('lead')})"),
            "next_action": _check("next_action", str(asks.get(m) or "")),
            "status": "suggested", "owner": _check("owner", principal),
            "assigned": _check("assigned", m),
            "confidence": lead_task["confidence"],
            "rationale": f"Your part of {p.get('lead')}'s \"{p.get('title')}\" "
                         f"(huddle {plan['id']}). Serves: {serves}",
            "notes": f"Lead: {p.get('lead')}. From huddle {plan['id']} ({page}).",
            "origin": "huddle", "source_url": page,
            "links": [{"label": "Huddle", "url": page}]})
    return lead_task, partner_tasks


_TRAILING_PARENS = re.compile(r"\s*\(([^()]*)\)\s*$")
_PROJECT_EXT_ID = re.compile(r"\(\s*(P\d+)\s*\)", re.I)


def _project_key(name: str) -> str:
    """A project name compared loosely: no trailing "(…)" suffix, case/whitespace folded."""
    return " ".join(_TRAILING_PARENS.sub("", str(name or "")).split()).casefold()


def _resolve_project(client: AgentClient, name: str, hid: str, page: str, *, create: bool,
                     by_ext_id: bool, projects_cache: dict) -> dict:
    """The agent's project row for `name`, or {} — never a duplicate of one it has.

    Members write names like "Spark cascade demo (P3)" or "connect-labs reliability (T15/T37)":
    (a) a parenthesised `P<N>` that exists on this board wins (`by_ext_id`: only the lead's
    board — the id is the lead's); (b) else names match with the trailing "(…)" stripped and
    case/whitespace folded; (c) else, with `create`, a new project under the stripped name."""
    if client.slug not in projects_cache:
        projects_cache[client.slug] = client.list_projects()
    rows = projects_cache[client.slug]
    if by_ext_id:
        ids = {i.upper() for i in _PROJECT_EXT_ID.findall(str(name or ""))}
        for pr in rows:
            if str(pr.get("ext_id") or "").upper() in ids:
                return pr
    want = _project_key(name)
    for pr in rows:
        if want and _project_key(pr.get("name")) == want:
            return pr
    if not create:
        return {}
    # The canopy-web half only. The Drive `Projects/<name>/` half belongs to the LEAD's Drive,
    # which the leader cannot write — the lead makes it on its first work turn.
    clean = " ".join(_TRAILING_PARENS.sub("", str(name or "")).split()) or str(name).strip()
    made = client.create_project(name=clean, notes=f"From huddle {hid}.",
                                 links=[{"label": "Huddle", "url": page}])
    rows.append(made)
    return made


# Re-written onto a task this huddle already filed, so a re-run restates them. canopy-web
# finds a huddle's outputs BY `source_url`, so this is not cosmetic.
_REPATCH_ON_REUSE = ("source_url", "rationale", "plan")


def _filed_here(t: dict, page: str, title: str) -> bool:
    """A task this huddle already filed under this title. Tasks filed before source_url was
    written carry source_url "" — those are recognised by their Huddle link."""
    if str(t.get("title") or "") != title:
        return False
    if t.get("source_url") == page:
        return True
    return not t.get("source_url") and any(
        isinstance(link, dict) and link.get("url") == page for link in t.get("links") or [])


def _find_or_add_task(client: AgentClient, task: dict, page: str) -> dict:
    """Reuse a task already filed for this huddle with this title (re-patching its
    source_url/rationale/plan); else create the next T<N> with every field at once."""
    from orchestrator.agent_cli import next_task_ext_id
    tasks = client.list_tasks()
    found = next((t for t in tasks if _filed_here(t, page, task["title"])), None)
    if found is not None:
        ext_id = found.get("ext_id")
        client.patch_task(ext_id, **{k: task[k] for k in _REPATCH_ON_REUSE if k in task})
        return {"agent": client.slug, "ext_id": ext_id,
                "project": found.get("project_ext_id") or task.get("project", ""), "reused": True}
    created = client.create_tasks([{**task, "ext_id": next_task_ext_id(tasks)}])
    if not created or not created[0].get("ext_id"):
        raise RuntimeError(f"{client.slug}: creating {task['title']!r} returned no task")
    return {"agent": client.slug, "ext_id": created[0]["ext_id"],
            "project": task.get("project", ""), "reused": False}


def not_reached(plan: dict, detail: dict) -> list[dict]:
    ht = H.load_type(plan.get("type") or "work")
    r1 = {c.get("member"): c for c in detail.get("cells") or [] if int(c.get("round") or 0) == 1}
    out = []
    for m in plan.get("members") or []:
        c = r1.get(m)
        if c is None:
            out.append({"member": m, "why": "never dispatched"})
            continue
        st, probs = cell_state(c, ht, round_deadline(list(r1.values())), _now())
        if st != "replied":
            out.append({"member": m, "why": st + (f": {'; '.join(probs)[:160]}" if probs else "")})
    return out


@huddle_group.command("file")
@click.option("--plan", "plan_path", required=True, type=click.Path(exists=True))
@click.option("--outcomes", "outcomes_path", required=True, type=click.Path(exists=True),
              help="The leader's ranked, merged list (start from `canopy huddle proposals`).")
@click.option("--repo", default=".", show_default=True, help="Leader's repo (Drive identity).")
@click.option("--local", default=None, help="Write the record to this dir, not Drive.")
@click.option("--digest-out", default=None, type=click.Path(dir_okay=False),
              help="Also write the principal's email body here.")
@click.option("--dry-run", is_flag=True, help="Apply the gates and show what would be filed.")
@click.option("--merges", "merges_path", default=None, type=click.Path(exists=True),
              help="The leader's merges.json (needed only if outcomes.json was not built by "
                   "`proposals --merges`).")
def file_cmd(plan_path, outcomes_path, repo, local, digest_out, dry_run, merges_path):
    """Gate the outcomes, file survivors as suggested tasks (lead + each partner), write the
    Drive record, mark the anchor finished. Idempotent: a re-run reuses the tasks."""
    plan = _load_plan(plan_path)
    hid = plan["id"]
    proposals = _read_json(outcomes_path)
    if not isinstance(proposals, list):
        raise HuddleError(f"{outcomes_path} must be a JSON list of proposals")
    # The leader's merges (lenient: outcomes.json may already have dropped a title), then the
    # same overlap refusal round 3 applies — two filed proposals for the same work is the
    # failure this exists to stop.
    proposals, probs = H.apply_merges(proposals, _merges(merges_path), strict=False)
    if probs:
        raise HuddleError("; ".join(probs))
    refuse_unresolved(proposals, "file")
    detail = get_detail(hid)
    # An agreement thread that closed after `proposals` ran still counts: fold outcomes in
    # again (only amends still open are touched).
    proposals = with_thread_outcomes(proposals, huddle_threads(hid))
    blocks = blocks_by(detail)
    r1_priorities = {pr for (m, n), b in blocks.items() if n == 1
                     for pr in (b.get("priorities") or []) if isinstance(pr, str)}
    brief = plan.get("priorities_brief") or ""
    prior_declined = [o for rec in plan.get("prior") or [] for o in rec.get("outcomes") or []
                      if str(o.get("fate") or "").startswith("declined")]
    filed, held = H.work_gates(proposals, r1_priorities, prior_declined,
                               brief_numbers=set(H.brief_items(brief)))
    base, ws = plan.get("base_url") or canopy_web.resolve_base_url(None), plan.get("workspace")
    page = plan.get("page") or page_url(base, ws, hid)
    prepared = [(p, *_task_fields(p, plan, page)) for p in filed]   # validates every field
    missing = not_reached(plan, detail)
    asks = H.asks_of_principal(round1_blocks(detail), filed, plan.get("principal") or "")
    digest = digest_text(hid, filed, held, page, base, ws, brief, asks)
    summary = {"huddle": hid, "page": page, "dry_run": dry_run,
               "held": [{"title": p.get("title"), "lead": p.get("lead"), "held": p["held"]}
                        for p in held],
               "not_reached": missing, "asks_of_jonathan": asks, "digest": digest}
    if dry_run:
        _emit({**summary, "filed": [{"title": p.get("title"), "lead": p.get("lead"),
                                     "partners": H.partners_of(p), "task": lead,
                                     "partner_tasks": parts}
                                    for p, lead, parts in prepared]})
        return
    created: list[dict] = []
    projects_cache: dict = {}
    try:
        for p, lead_task, partner_tasks in prepared:
            lead = _client(p["lead"])
            project = p.get("project") or {}
            name = project.get("name") if isinstance(project, dict) else str(project)
            lead_project = _resolve_project(lead, name, hid, page, create=True, by_ext_id=True,
                                            projects_cache=projects_cache)
            lead_task["project"] = str(lead_project.get("ext_id") or "")
            # partners look up the lead's REAL project name on their own boards
            name = lead_project.get("name") or name
            p["task"] = _find_or_add_task(lead, lead_task, page)
            created.append(p["task"])
            p["partner_tasks"] = []
            for pt in partner_tasks:
                agent = pt.pop("agent")
                pc = _client(agent)
                pt["project"] = str(_resolve_project(
                    pc, name, hid, page, create=False, by_ext_id=False,
                    projects_cache=projects_cache).get("ext_id") or "")
                pt["links"] = pt["links"] + [{"label": f"Lead task ({p['lead']} {p['task']['ext_id']})",
                                              "url": board_url(base, ws, p["lead"])}]
                ref = _find_or_add_task(pc, pt, page)
                p["partner_tasks"].append(ref)
                created.append(ref)
    except (CanopyError, RuntimeError) as e:
        click.echo(json.dumps({"error": f"filing stopped: {e}", "tasks_so_far": created},
                              indent=2), err=True)
        raise click.ClickException(f"filing stopped (re-run is safe — tasks are reused): {e}")
    meta = {**plan, "leader_turn": plan.get("anchor_turn_id")}
    record = H.outcome_record(meta, filed, held, missing)
    try:
        where = _store(repo, local).write(record)
    except Exception as e:  # noqa: BLE001 — any store failure must stop the success report
        click.echo(json.dumps({"error": f"Drive record NOT written: {e}",
                               "tasks_created": created}, indent=2))
        raise click.ClickException(
            f"tasks were filed but the outcome record was NOT written ({e}). "
            "Fix the store and re-run `canopy huddle file` — tasks are reused, not duplicated.")
    try:
        _client(plan["leader"]).post_turn(
            cli_session_id=f"huddle:{hid}", title=f"Huddle {hid}",
            summary=digest or f"Huddle {hid}: nothing filed ({len(held)} held).",
            source="huddle", emdash_task_id="", origin_ref={"finished_at": _iso(_now())})
        finished = True
    except (CanopyError, RuntimeError) as e:
        click.echo(f"[huddle] warning: could not mark the anchor finished: {e}", err=True)
        finished = False
    if digest_out:
        Path(digest_out).write_text(digest, encoding="utf-8")
    _emit({**summary, "record": where, "anchor_finished": finished,
           "filed": [{"title": p.get("title"), "lead": p.get("lead"),
                      "partners": H.partners_of(p), "task": p["task"],
                      "partner_tasks": p["partner_tasks"]} for p in filed]})


@huddle_group.command("view")
@click.option("--huddle", "hid", required=True)
def view_cmd(hid):
    """Print the huddle's canopy-web page URL."""
    detail = get_detail(hid)
    ws = None
    try:
        ws = (_client(detail.get("leader") or "").get_agent() or {}).get("workspace") or None
    except (CanopyError, RuntimeError):
        pass
    click.echo(page_url(canopy_web.resolve_base_url(None), ws, hid))
