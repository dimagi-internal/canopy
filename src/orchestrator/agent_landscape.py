"""The agent landscape of ONE workspace tree — every agent in a workspace and its descendants,
who owns it, what it can do, whether it is active, and whether a given conductor agent may run
it. Plus a skill-based membership rule for team work (huddles).

Scope is canopy-web's `GET /api/workspaces/<slug>/agent-topology`: one tree, never the whole
system. That is the point — any workspace with its own conductor agent (Ada for dimagi) reads
its own tree with the same command (Jonathan, 2026-10-16). Admin and above on that workspace.

Origin: Jonathan, 2026-10-16 ("Re: AI Learning Champions: next steps") — "an easy way to
understand the agentic landscape within Canopy dynamically", and "we do NOT want all agents to
be participating in the huddles unless they are bringing specific skills / value".
"""
from __future__ import annotations

import datetime as dt
import re

# Skills every agent carries — plumbing, not a reason to bring an agent into a work group.
BOILERPLATE = {
    "agent-turn-review", "setup", "shipping", "task-tracker", "gdoc-writer", "turn", "ask",
    "manager-sync", "answer-caller", "update", "doctor", "email-communicator", "inbox-triage",
    "self-review", "canopy-publish", "contact-memory", "capability-report", "gdoc-review",
}
MIN_MATCHED_SKILLS = 2
ACTIVE_DAYS = 14


def _rows(r):
    if isinstance(r, dict):
        return r.get("items") or r.get("results") or []
    return r or []


def default_root(call, slug: str) -> str:
    """The root of the tree `slug`'s agent record lives in (its organisation)."""
    rec = call("GET", f"/api/agents/{slug}/") or {}
    home = str(rec.get("workspace") or "")
    for w in _rows(call("GET", "/api/workspaces/")):
        if w.get("slug") == home:
            return str(w.get("root") or home)
    return home


def may_run(edges: list, conductor: str, slug: str) -> bool:
    """The conductor's login gets the WHOLE agent as its admin/owner. An editor-basis edge is a
    workspace role, not the agent owner's say-so — that agent needs its owner's OK."""
    return any(e.get("source") == conductor and e.get("target") == slug and e.get("access") == "full"
               and e.get("basis") in ("admin", "owner") for e in edges or [])


def fetch(call, conductor: str, workspace: str, now: dt.datetime | None = None) -> dict:
    """{"root", "workspaces", "agents": [...]} for `workspace` and its descendants."""
    now = now or dt.datetime.now(dt.timezone.utc)
    since = (now - dt.timedelta(days=ACTIVE_DAYS)).isoformat()
    topo = call("GET", f"/api/workspaces/{workspace}/agent-topology") or {}
    edges = topo.get("edges") or []
    agents = []
    for a in topo.get("agents") or []:
        slug = a.get("slug")
        if not slug:
            continue
        try:
            skills = _rows(call("GET", f"/api/agents/{slug}/skills/"))
        except Exception:  # noqa: BLE001 — an unreadable catalog is "no skills", not a crash
            skills = []
        try:
            turns = _rows(call("GET", f"/api/harness/turns/?agent={slug}&limit=200"))
        except Exception:  # noqa: BLE001
            turns = []
        stamps = sorted(str(t.get("created_at") or "") for t in turns if t.get("created_at"))
        agents.append({
            "slug": slug, "name": a.get("name") or slug, "workspace": a.get("workspace") or "",
            "owner": a.get("owner_email") or "", "may_run": may_run(edges, conductor, slug),
            "skills": [{"name": s.get("name"), "description": s.get("description") or ""} for s in skills],
            "turns_recent": sum(1 for x in stamps if x >= since),
            "last_turn": stamps[-1][:10] if stamps else "",
        })
    return {"root": topo.get("root") or workspace, "workspaces": topo.get("workspaces") or [],
            "agents": agents}


def matched_skills(skills: list, keywords: list) -> list:
    """The agent's non-boilerplate skills whose NAME hits a keyword. Names only: descriptions
    are prose and made a 129-skill agent a match for every group."""
    kw = {k.lower() for k in keywords}
    return sorted({s["name"] for s in skills or [] if s.get("name") and s["name"] not in BOILERPLATE
                   and set(re.findall(r"[a-z0-9]+", s["name"].lower())) & kw})


def select_members(agents: list, groups: list, conductor: str) -> dict:
    """Who each group needs: {"members", "by_group", "could_bring", "left_out"}. A member brings
    ≥2 skills named for a group AND the conductor may run it; the conductor is never a member."""
    by_group, members, could = [], set(), {}
    for g in groups:
        rows = []
        for a in agents:
            if a["slug"] == conductor:
                continue
            hits = matched_skills(a.get("skills"), g.get("keywords") or [])
            if len(hits) < MIN_MATCHED_SKILLS:
                continue
            rows.append({"slug": a["slug"], "skills": hits})
            if a.get("may_run"):
                members.add(a["slug"])
            else:
                could.setdefault(a["slug"], {"slug": a["slug"], "owner": a.get("owner", ""),
                                             "groups": []})["groups"].append(g["name"])
        by_group.append({"group": g["name"], "agents": rows})
    runnable = {a["slug"] for a in agents if a.get("may_run") and a["slug"] != conductor}
    return {"members": sorted(members), "by_group": by_group,
            "could_bring": sorted(could.values(), key=lambda r: r["slug"]),
            "left_out": sorted(runnable - members)}


def render(land: dict, conductor: str, selection: dict | None = None) -> str:
    out = [f"# Agent landscape — {land['root']} and its sub-workspaces "
           f"({', '.join(w.get('slug', '') for w in land['workspaces']) or land['root']})", "",
           f"| Agent | Workspace | Owner | {conductor} may run | Skills | Turns (14d) | Last turn |",
           "|---|---|---|---|---|---|---|"]
    for a in sorted(land["agents"], key=lambda r: r["slug"]):
        out.append(f"| {a['slug']} | {a['workspace']} | {a['owner'] or '?'} | "
                   f"{'yes' if a['may_run'] else 'no'} | {len(a['skills'])} | {a['turns_recent']} | "
                   f"{a['last_turn'] or '—'} |")
    if selection:
        out += ["", "## Who each group needs", ""]
        for g in selection["by_group"]:
            who = ", ".join(f"{x['slug']} ({', '.join(x['skills'][:4])})" for x in g["agents"])
            out.append(f"- **{g['group']}** — {who or 'nobody yet'}")
        out += ["", f"Members: {', '.join(selection['members']) or 'none'}"]
        if selection["left_out"]:
            out.append(f"Left out (no matching skills): {', '.join(selection['left_out'])}")
        for c in selection["could_bring"]:
            out.append(f"Could bring value, needs its owner's OK: {c['slug']} ({c['owner'] or '?'}) — "
                       + "; ".join(c["groups"]))
    return "\n".join(out)
