"""Per-AGENT bring-up coverage — `canopy agent coverage`.

`canopy agent health` answers "is this agent's workload healthy for its NEXT turn"
(board + inbox, point-in-time). THIS module answers a longitudinal question: "how
much of what we said this agent would be is actually LIVE yet" — which declared
skills have ever fired, which fired and then stopped, and which never fired despite
having the chance.

The fleet is in bring-up, not maintenance: things are built and exercised inside
short bursts (1-2 active days separated by ~6 dark days), then decay when they
can't quite be made to work. So OPPORTUNITY IS COUNTED IN BURSTS SURVIVED, never in
wall-clock days -- a day-based age gate spans whole bursts and suppresses exactly
the most interesting skills. A trough is not a finding: an agent with no bursts
since a skill was written yields no finding at all.

Facts and deterministic buckets only -- WHY a skill never fired (blocked? forgotten?
premature?) is the caller's job (Ada's fleet-persona-coverage skill judges; this
module never does). Read-only by construction: writes to no repo, mailbox, board, or
turn queue.

Same shape as agent_health.py: small probes with injectable dependencies (``call``
for canopy-web, ``now``, ``projects_dir``), composed by ``run_agent_coverage``.
"""
from __future__ import annotations

import re
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from orchestrator import canopy_web
from orchestrator.agent_client import list_agent_slugs
from orchestrator.agent_review import find_turn_transcripts, resolve_agent_repo
from orchestrator.session_sources import (
    collect_web_transcripts,
    corpus_confidence,
    local_transcript_dirs,
    session_sources,
)
from orchestrator.transcripts import read_transcript


DEFAULT_WINDOW_DAYS = 30
DEFAULT_BURST_GAP_DAYS = 2
DEFAULT_MIN_BURSTS = 2
DEFAULT_DECAY_BURSTS = 1
DEFAULT_MIN_TRANSCRIPTS = 3

# Evidence per skill is capped in the report: the bucket is the finding, and a
# skill that fired 200 times does not need 200 citations. `evidence_count` carries
# the true total so the cap is visible, never a silent truncation.
MAX_EVIDENCE_PER_SKILL = 3


def compute_bursts(stamps: list[tuple[datetime, str]], gap_days: int = DEFAULT_BURST_GAP_DAYS) -> list[dict]:
    """Group activity timestamps into bursts of contiguous active days.

    A burst is a run of active days separated by a gap of >= ``gap_days``. This is
    the unit of OPPORTUNITY: "did this skill have a chance to fire?" is answered in
    bursts, not days.
    """
    if not stamps:
        return []
    # date -> set of session ids active that day
    by_day: dict[object, set] = {}
    for ts, sid in stamps:
        by_day.setdefault(ts.date(), set()).add(sid)

    days = sorted(by_day)
    groups: list[list] = [[days[0]]]
    for prev, day in zip(days, days[1:]):
        if (day - prev) >= timedelta(days=gap_days):
            groups.append([day])
        else:
            groups[-1].append(day)

    out = []
    for i, group in enumerate(groups, start=1):
        sessions = set()
        for d in group:
            sessions |= by_day[d]
        out.append({
            "id": i,
            "start": group[0].isoformat(),
            "end": group[-1].isoformat(),
            "active_days": len(group),
            "sessions": len(sessions),
        })
    return out


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    # Mirror agent_health._parse_when: attach the local zone to naive timestamps so
    # every evidence ts is aware -- mixing naive/aware here would shift the `.date()`
    # burst boundaries downstream.
    return dt.astimezone() if dt.tzinfo is None else dt


def evidence_from_entries(entries: list[dict], slug: str,
                          skill_names: list[str]) -> dict[str, list[dict]]:
    """Invocation-shaped evidence per skill, from ONE transcript's entries.

    Only ``tool_use`` inputs and ``text`` blocks are scanned. ``tool_result`` content
    is deliberately NOT scanned: `ls skills/` listings and git diffs live there, and
    counting them would mark never-run skills as live. A mention is not an invocation.
    """
    # Require the read path to belong to THIS agent: slug must appear DIRECTLY under
    # repositories/ or worktrees/ before the skills/ segment (repo layout:
    # /repositories/<slug>/skills/..., worktree layout:
    # /worktrees/<slug>/<branch>/skills/... or /worktrees/<slug>/emdash/<branch>/skills/...).
    # Without this, coincidental `/eva/` in fixture paths (e.g. test fixtures) or
    # cross-agent reads of a sibling's identically-named skill file falsely count as THIS
    # agent's skill firing.
    md_res = {n: re.compile(
        rf"/(?:repositories|worktrees)/{re.escape(slug)}/(?:.*/)?skills/{re.escape(n)}/SKILL\.md$"
    ) for n in skill_names}
    slash_res = {n: re.compile(rf"/{re.escape(slug)}:{re.escape(n)}(?![\w-])")
                 for n in skill_names}
    out: dict[str, list[dict]] = {}

    def add(name, ts, kind, line):
        out.setdefault(name, []).append({"ts": ts, "kind": kind, "line": line})

    for entry in entries:
        content = (entry.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        ts = _parse_ts(entry.get("timestamp"))
        if ts is None:
            # Can't place this invocation on the burst timeline -- unusable as
            # evidence rather than a landmine `ts: None` for downstream `.date()`.
            continue
        for c in content:
            if not isinstance(c, dict):
                continue
            ctype = c.get("type")
            if ctype == "tool_use":
                inp = c.get("input") or {}
                name = c.get("name")
                if name == "Skill":
                    ref = str(inp.get("skill") or "")
                    ns, sep, bare = ref.partition(":")
                    if not sep:
                        ns, bare = None, ref
                    # Bare skill name, or namespaced to THIS agent -- reject a
                    # foreign namespace (e.g. "ace:turn" while scanning eva).
                    if bare in md_res and (ns is None or ns == slug):
                        add(bare, ts, "skill_tool_call", ref)
                elif name == "Read":
                    fp = str(inp.get("file_path") or "")
                    for n, rx in md_res.items():
                        if rx.search(fp):
                            add(n, ts, "skill_md_read", fp)
            elif ctype == "text":
                text = str(c.get("text") or "")
                for n, rx in slash_res.items():
                    if rx.search(text):
                        add(n, ts, "slash_invocation", f"/{slug}:{n}")
            # ctype == "tool_result" -> intentionally skipped (see docstring)
    return out


def scan_evidence(paths: list[Path], slug: str, skill_names: list[str], *,
                  reader: Callable = read_transcript) -> dict[str, list[dict]]:
    """Invocation evidence per skill across a transcript corpus."""
    out: dict[str, list[dict]] = {}
    for p in paths:
        found = evidence_from_entries(reader(p), slug, skill_names)
        for name, evs in found.items():
            for e in evs:
                out.setdefault(name, []).append({**e, "transcript": str(p)})
    return out


SUB_SKILL_SUFFIXES = ("-eval", "-qa")


def declared_skills(repo: Path) -> list[str]:
    """Skill names declared in the repo (a dir under skills/ holding a SKILL.md)."""
    root = repo / "skills"
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir() if (d / "SKILL.md").is_file())


def parent_of(name: str, all_names: set[str]) -> Optional[str]:
    """The parent skill for an `-eval`/`-qa` sub-skill, else None.

    Sub-skills are invoked BY their parent, not independently -- judging them
    separately turns ace's 128 skills into a 79-item noise list.
    """
    for suffix in SUB_SKILL_SUFFIXES:
        if name.endswith(suffix):
            parent = name[: -len(suffix)]
            if parent in all_names:
                return parent
    return None


def persona_info(repo: Path) -> dict:
    """persona.md presence/size. Absent is a FACT (echo has none), not an error."""
    p = repo / "persona.md"
    if not p.is_file():
        return {"present": False, "path": None, "bytes": 0}
    return {"present": True, "path": "persona.md", "bytes": p.stat().st_size}


def _git(repo: Path, args: list[str], runner) -> str:
    try:
        r = runner(["git", "-C", str(repo), *args],
                   capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def skill_git_facts(repo: Path, name: str, now: datetime, *,
                    runner=subprocess.run) -> dict:
    """When a skill landed, when it was last touched, how often -- from git.

    `commits`/`last_touched_days` are CONTEXT (a skill edited yesterday that never
    fired is a sharper story), never a bucket: buckets are firing behavior.

    ONE `--follow` call, not three `--diff-filter=A` calls: `--diff-filter=A`
    classifies a `git mv` destination as a fresh "add" at the new path, losing
    the skill's real history (and undercounting `commits`) across a rename.
    `--follow` walks the file across renames instead -- it takes exactly one
    pathspec, which is all we ever pass here, so that limitation doesn't bite.
    `git log` prints newest-first, so the FIRST line is the most recent touch
    and the LAST line is the oldest commit that ever touched this path.
    """
    path = f"skills/{name}/SKILL.md"
    log = _git(repo, ["log", "--follow", "--format=%aI", "--", path], runner)
    if not log:
        return {"added_at": None, "age_days": None,
                "last_touched_days": None, "commits": 0}
    lines = log.splitlines()
    last = lines[0]
    # Deliberately the OLDEST line, even across a delete-then-re-add: for a
    # skill that was removed and later re-added, this reports the ORIGINAL
    # creation date, overstating age rather than understating it. Overstating
    # credits the skill with MORE opportunity, which surfaces it for a human
    # to judge -- the safe failure direction. Do not "fix" this back to the
    # most recent add; that is the silent-suppression bug this exists to avoid.
    added_at = lines[-1]

    def age(v):
        dt = _parse_ts(v)
        return None if dt is None else round((now - dt).total_seconds() / 86400.0, 1)

    return {"added_at": added_at, "age_days": age(added_at),
            "last_touched_days": age(last), "commits": len(lines)}


def burst_of(ts: Optional[datetime], bursts: list[dict]) -> Optional[int]:
    """The id of the burst containing ``ts`` (None if it falls in a dark stretch)."""
    if ts is None:
        return None
    d = ts.date()
    for b in bursts:
        if date.fromisoformat(b["start"]) <= d <= date.fromisoformat(b["end"]):
            return b["id"]
    return None


def classify(*, parent: Optional[str], used_bursts: list[int],
             opportunity_bursts: list[int], corpus_adequate: bool,
             min_bursts: int = DEFAULT_MIN_BURSTS,
             decay_bursts: int = DEFAULT_DECAY_BURSTS) -> str:
    """One bucket for one skill. First match wins, in this order.

    Positive evidence short-circuits EVERY negative gate: absence of evidence is
    not evidence of absence, but presence of evidence IS evidence of presence.
    `used_bursts` being non-empty is PROOF the skill fired, so `live`/`decayed`
    are decided before `no_opportunity` (the opportunity-burst-count gate) and
    before `insufficient_evidence` (the corpus-size gate) -- both of those exist
    only to gate NEGATIVE claims ("never fired"), never to suppress a positive
    one. A skill built and used inside the same burst
    (`opportunity_bursts=[3]`, `used_bursts=[3]`, below `min_bursts`) is `live`,
    not `no_opportunity`.

    Opportunity is BURSTS, never days.
    """
    if parent:
        return "sub_skill"
    if used_bursts:
        recent = set(opportunity_bursts[-decay_bursts:]) if decay_bursts else set()
        if recent & set(used_bursts):
            return "live"
        return "decayed"
    if len(opportunity_bursts) < min_bursts:
        return "no_opportunity"
    if not corpus_adequate:
        return "insufficient_evidence"
    return "never_live"


def _activity_stamps(paths: list[Path], reader: Callable) -> list[tuple[datetime, str]]:
    """(timestamp, session_id) for every entry in the corpus -- the burst input."""
    stamps = []
    for p in paths:
        for entry in reader(p):
            ts = _parse_ts(entry.get("timestamp"))
            if ts is not None:
                stamps.append((ts, entry.get("sessionId") or str(p)))
    return stamps


def _agent_activity(slug: str, call: Callable) -> dict:
    """Auxiliary canopy-web telemetry (turn/task counts).

    Best-effort by design: bursts/evidence/buckets are computed purely from git +
    transcripts, so an unreachable canopy-web must not abort the whole coverage
    report over this supplementary block. But swallowing the failure to a bare
    `{}` would be indistinguishable from "this agent genuinely has zero turns" --
    across a fleet sweep, a canopy-web outage would then silently read as every
    agent having no activity. Keep the failure as a FACT instead.
    """
    try:
        detail = call("GET", f"/api/agents/{slug}/") or {}
    except Exception as e:
        return {"error": str(e)}
    return {k: detail.get(k) for k in
            ("turn_count", "task_count", "latest_turn_at")}


def coverage_report(slug: str, *, call: Callable = canopy_web.call,
                    now: Optional[datetime] = None,
                    projects_dir: Optional[Path] = None,
                    window_days: int = DEFAULT_WINDOW_DAYS,
                    burst_gap_days: int = DEFAULT_BURST_GAP_DAYS,
                    min_bursts: int = DEFAULT_MIN_BURSTS,
                    decay_bursts: int = DEFAULT_DECAY_BURSTS,
                    min_transcripts: int = DEFAULT_MIN_TRANSCRIPTS,
                    reader: Callable = read_transcript) -> dict:
    """One agent's bring-up coverage: declared surface vs. what actually fired."""
    now = now or datetime.now(timezone.utc)
    repo = resolve_agent_repo(slug)
    if repo is None or not Path(repo).exists():
        return {"agent": slug, "error": f"cannot resolve an agent repo for '{slug}'"}
    repo = Path(repo)

    if projects_dir:
        # An explicit caller-supplied dir (tests, mainly) scans ONLY that dir --
        # the function stays injectable without going through the source seam.
        paths = find_turn_transcripts(repo, hours=window_days * 24, projects_dir=projects_dir)
        confidence = "whole-corpus"
        source_names = [str(projects_dir)]
        web_stats = {}
        unreadable = {}
    else:
        # No override: scan EVERY readable local source and merge. This is the
        # cross-user fix -- `agent_review._belongs_to_agent` already handles
        # cross-user paths correctly (a worktree under another user's home still
        # matches the repo/worktree rule); only the enumeration was missing.
        sources = session_sources()
        paths = []
        for d in local_transcript_dirs(sources):
            paths += find_turn_transcripts(repo, hours=window_days * 24, projects_dir=d)
        # Off-machine turns (cloud runner) from canopy-web, minus sessions already
        # on local disk. A failed listing/fetch flips the source -> half-blind.
        web = collect_web_transcripts(sources, repo.name, window_days * 24,
                                      local_session_ids={p.stem for p in paths})
        paths += web.transcripts
        confidence = corpus_confidence(sources)
        source_names = [s.name for s in sources if s.readable]
        web_stats = web.stats
        unreadable = {s.name: s.reason for s in sources if not s.readable}
    stamps = _activity_stamps(paths, reader)
    bursts = compute_bursts(stamps, gap_days=burst_gap_days)
    adequate = len(paths) >= min_transcripts

    names = declared_skills(repo)
    all_names = set(names)
    evidence = scan_evidence(paths, slug, names, reader=reader)

    rows = []
    for name in names:
        parent = parent_of(name, all_names)
        git = skill_git_facts(repo, name, now)
        born = burst_of(_parse_ts(git["added_at"]), bursts)
        if born is None:
            # Predates the window (or landed in a dark stretch): it existed for all
            # bursts we can see. Fair -- do not credit it with less opportunity.
            opportunity = [b["id"] for b in bursts]
        else:
            opportunity = [b["id"] for b in bursts if b["id"] >= born]
        evs = evidence.get(name, [])
        used = sorted({b for b in (burst_of(e["ts"], bursts) for e in evs) if b})
        bucket = classify(parent=parent, used_bursts=used,
                          opportunity_bursts=opportunity, corpus_adequate=adequate,
                          min_bursts=min_bursts, decay_bursts=decay_bursts)
        row = {"name": name, "bucket": bucket, "born_burst": born,
               "opportunity_bursts": opportunity, "used_bursts": used,
               "live": bucket == "live",
               "evidence_count": len(evs),
               "evidence": [{"transcript": e.get("transcript"),
                             "ts": e["ts"].isoformat() if e["ts"] else None,
                             "kind": e["kind"], "line": e["line"]}
                            for e in evs[:MAX_EVIDENCE_PER_SKILL]],
               **git}
        if parent:
            row["parent"] = parent
        rows.append(row)

    return {
        "agent": slug,
        "window_days": window_days,
        "corpus": {"transcripts": len(paths), "entries": len(stamps),
                   "adequate": adequate, "sources": source_names,
                   **({"web": web_stats} if web_stats else {}),
                   **({"unreadable": unreadable} if unreadable else {})},
        "confidence": confidence,
        "persona": persona_info(repo),
        "activity": _agent_activity(slug, call),
        "bursts": bursts,
        "skills": rows,
    }


def run_agent_coverage(slug: Optional[str] = None, *, call: Callable = canopy_web.call,
                       now: Optional[datetime] = None, **kw) -> dict:
    """Probe one agent (slug) or sweep the whole registered fleet (slug=None)."""
    now = now or datetime.now(timezone.utc)
    slugs = [slug] if slug else list_agent_slugs(call)
    agents = [coverage_report(s, call=call, now=now, **kw) for s in slugs]
    ok = all(not a.get("error") and a.get("corpus", {}).get("adequate")
             and a.get("confidence") != "half-blind" for a in agents)
    return {"ok": ok, "agents": agents}
