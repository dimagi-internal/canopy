"""Team shareout briefings — gather a date range of work (Claude Code sessions
+ the author's GitHub PRs) per project, and post the synthesized briefings to
the canopy-web /shareouts feed.

The deterministic parts live here (date math, corpus gathering, payload
shaping, posting). The *synthesis* — turning the corpus into teammate-facing
"what / why / how to leverage" prose — is done by the `canopy:shareout` skill
agent, which reads the gathered corpus and authors the briefings.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

from orchestrator import canopy_web

# Canonical PAT/base-url conventions live in canopy_web; alias for back-compat.
DEFAULT_API = canopy_web.DEFAULT_API
TOKEN_FILE = canopy_web.TOKEN_FILE

# Corpus bounds — keep the gathered JSON small enough to hand to a synthesis
# agent without blowing its context, while preserving the signal (intent +
# what shipped). Prompts carry the "why"; PR titles/bodies carry the "what".
MAX_SESSIONS_PER_PROJECT = 40
MAX_PROMPTS_PER_SESSION = 30
PROMPT_TRUNCATE = 600
MAX_PRS_PER_PROJECT = 50


# ---------------------------------------------------------------------------
# Date range
# ---------------------------------------------------------------------------


UTC = dt.timezone.utc


def _parse_date(s: str) -> dt.date:
    return dt.date.fromisoformat(s.strip())


def _day_start(d: dt.date) -> dt.datetime:
    return dt.datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=UTC)


def _day_end(d: dt.date) -> dt.datetime:
    return dt.datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=UTC)


def resolve_range(
    from_date: str | None = None,
    to_date: str | None = None,
    days: int | None = None,
    now: dt.datetime | None = None,
) -> tuple[dt.datetime, dt.datetime]:
    """Resolve a (start, end) inclusive *timestamp* window (tz-aware UTC).

    Shareouts are stamped to the second, so this returns datetimes:
      - --from/--to (calendar dates) -> whole-day window: from 00:00:00 .. to 23:59:59.
        --from only -> from .. end of today; --to only -> that whole day.
      - --days N    -> a rolling window: now - N days .. now.
      - no args     -> fallback only (yesterday, full day); the real no-arg
        default is `resolve_default_range` (since the last shareout).

    `now` is injectable for testing.
    """
    now = now or dt.datetime.now(UTC)
    today = now.date()

    if from_date or to_date:
        start = _parse_date(from_date) if from_date else None
        end = _parse_date(to_date) if to_date else None
        if start is not None and end is None:
            end = today
        if end is not None and start is None:
            start = end
        if start > end:
            raise ValueError(f"--from ({start}) is after --to ({end})")
        return _day_start(start), _day_end(end)

    if days:
        if days < 1:
            raise ValueError("--days must be >= 1")
        return now - dt.timedelta(days=days), now

    yesterday = today - dt.timedelta(days=1)
    return _day_start(yesterday), _day_end(yesterday)


def fetch_latest_period_end(api_url: str, token: str, timeout: int = 15) -> dt.date | None:
    """Return the most recent existing shareout's period_end (the feed is
    ordered newest-period-first), or None when there are none / on any error."""
    url = f"{api_url.rstrip('/')}/api/shareouts/?limit=1"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        data = json.loads(resp.read().decode("utf-8") or "{}")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, json.JSONDecodeError):
        return None
    items = data.get("items") or []
    if not items:
        return None
    return _parse_ts(items[0].get("period_end"))


def resolve_default_range(
    latest_end: dt.datetime | None, now: dt.datetime | None = None
) -> tuple[dt.datetime, dt.datetime]:
    """The no-argument default: cover from the end *time* of the last shareout
    up to right now (tz-aware UTC). Consecutive shareouts chain exactly —
    next.period_start == prev.period_end — regardless of clock time.

    - latest_end given -> latest_end .. now (clamped so start never exceeds now).
    - latest_end None  -> (now - 24h) .. now (no prior shareout to continue from).

    `now` is injectable for testing.
    """
    now = now or dt.datetime.now(UTC)
    if latest_end is None:
        return now - dt.timedelta(days=1), now
    return (latest_end if latest_end < now else now), now


def _parse_ts(s) -> dt.datetime | None:
    """Parse an ISO timestamp (or date) to a tz-aware UTC datetime, or None."""
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def session_in_range(session: dict, start: dt.datetime, end: dt.datetime) -> bool:
    """True if the session's [first_ts, last_ts] timestamp window intersects
    [start, end]. Sessions missing timestamps are excluded."""
    first = _parse_ts(session.get("first_ts"))
    last = _parse_ts(session.get("last_ts")) or first
    first = first or last
    if first is None or last is None:
        return False
    return first <= end and last >= start


# ---------------------------------------------------------------------------
# Gather
# ---------------------------------------------------------------------------


def _session_digest(session: dict) -> dict:
    """Read a session's transcript and summarize it for synthesis: the human
    prompts (intent → the 'why'), a tool-usage summary, and MCP servers."""
    from orchestrator.transcripts import (
        extract_tool_calls,
        extract_user_messages,
        read_transcript,
    )

    path = Path(session["path"])
    entries = read_transcript(path)

    prompts = [
        p.strip()[:PROMPT_TRUNCATE]
        for p in extract_user_messages(entries)
        if p.strip()
    ][:MAX_PROMPTS_PER_SESSION]

    tool_counts: dict[str, int] = defaultdict(int)
    for call in extract_tool_calls(entries):
        tool_counts[call.get("name", "?")] += 1
    top_tools = sorted(tool_counts.items(), key=lambda kv: kv[1], reverse=True)[:12]

    return {
        "session_id": session.get("session_id"),
        "first_ts": session.get("first_ts"),
        "last_ts": session.get("last_ts"),
        "user_msgs": session.get("user_msgs"),
        "mcp_servers": session.get("mcp_servers", []),
        "prompts": prompts,
        "tool_summary": [{"name": n, "count": c} for n, c in top_tools],
    }


def _pr_in_window(pr: dict, start: dt.datetime, end: dt.datetime) -> bool:
    """A PR belongs to the window it *merged* in: mergedAt within (start, end].

    Merge-time-only anchoring is what makes "what shipped" non-duplicating —
    each merged PR lands in exactly ONE shareout, and a PR opened in one window
    but merged in the next appears only once (when it merged). Unmerged PRs
    (open / closed-without-merge) didn't ship, so they're excluded. Half-open
    on the start so the boundary instant belongs to the previous window."""
    merged = _parse_ts(pr.get("mergedAt"))
    return merged is not None and start < merged <= end


def fetch_prs(repo: str, start: dt.datetime, end: dt.datetime, author: str = "@me") -> list[dict]:
    """Fetch the author's PRs in `repo` that *merged* within the (start, end]
    window, via gh.

    Best-effort: returns [] when gh is unavailable, unauthenticated, or the repo
    isn't on GitHub. The gh `merged:>=` search is a coarse day-resolution
    candidate filter (a superset); `_pr_in_window` then narrows to the precise
    merge timestamp so consecutive shareouts never share a PR.
    """
    if not repo or "/" not in repo:
        return []
    fields = "number,title,url,state,body,createdAt,mergedAt,updatedAt"
    try:
        proc = subprocess.run(
            [
                "gh", "pr", "list",
                "--repo", repo,
                "--author", author,
                "--state", "merged",
                "--search", f"merged:>={start.date().isoformat()}",
                "--json", fields,
                "--limit", str(MAX_PRS_PER_PROJECT),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    try:
        raw = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return []

    out = []
    for pr in raw:
        if not _pr_in_window(pr, start, end):
            continue
        body = (pr.get("body") or "").strip()
        out.append({
            "number": pr.get("number"),
            "title": pr.get("title"),
            "url": pr.get("url"),
            "state": pr.get("state"),
            "merged_at": pr.get("mergedAt"),
            "created_at": pr.get("createdAt"),
            "body": body[:2000],
        })
    return out


def gather(
    *,
    projects_dir: Path,
    repo_map: dict,
    labels: dict,
    start: dt.datetime | dt.date,
    end: dt.datetime | dt.date,
    author: str = "@me",
    project_filter: str | None = None,
    fetch_prs_fn=fetch_prs,
) -> dict:
    """Build the per-project corpus for the [start, end] timestamp window.

    `start`/`end` may be datetimes (the normal path) or plain dates (coerced to
    whole-day bounds). Returns {"period": {start, end}, "projects": {...}}.
    `fetch_prs_fn` is injectable for testing.
    """
    from orchestrator.scanner import scan_all_transcripts

    # Accept dates for convenience; work internally in tz-aware datetimes.
    if not isinstance(start, dt.datetime):
        start = _day_start(start)
    if not isinstance(end, dt.datetime):
        end = _day_end(end)

    sessions = scan_all_transcripts(projects_dir, repo_map, labels)
    in_range = [s for s in sessions if session_in_range(s, start, end)]

    if project_filter:
        suffix = f"/{project_filter}"
        in_range = [s for s in in_range if (s.get("repo") or "").endswith(suffix)]

    groups: dict[str, list[dict]] = defaultdict(list)
    for s in in_range:
        key = s.get("repo") or s.get("project_key") or "unknown"
        groups[key].append(s)

    projects = {}
    for repo, sess in sorted(groups.items()):
        sess.sort(key=lambda s: s.get("last_ts") or "")
        digests = [_session_digest(s) for s in sess[:MAX_SESSIONS_PER_PROJECT]]
        prs = fetch_prs_fn(repo, start, end, author) if "/" in repo else []
        projects[repo] = {
            "session_count": len(sess),
            "sessions": digests,
            "prs": prs,
        }

    return {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "projects": projects,
    }


# ---------------------------------------------------------------------------
# Post
# ---------------------------------------------------------------------------


def build_post_payload(authoring: dict, source: str, produced_by_agent: str = "") -> dict:
    """Expand the agent's authoring doc into a ShareoutBatchIn body.

    Authoring shape (what the skill agent writes):
      {
        "period_start": "YYYY-MM-DD", "period_end": "YYYY-MM-DD",
        "author": "jjackson",
        "rollup": {"title","summary","content","links"} | null,
        "projects": [{"project_slug","title","summary","content","links"}, ...]
      }

    The rollup becomes a shareout with project_slug=null. `source` is stamped
    on every item so a re-run cleanly replaces the prior post (server-side
    idempotency is keyed on project+period+source).

    `produced_by_agent` (a slug like "eva") records that an agent assembled this
    on the author's behalf — the report stays attributed to `author`; this is
    just the producer byline. It is stamped on every item ONLY when non-empty:
    a human run omits the key entirely, so the post stays compatible with a
    canopy-web that predates the field (its ShareoutIn is a StrictModel and
    would reject an unknown `produced_by_agent: ""`).
    """
    ps = authoring["period_start"]
    pe = authoring["period_end"]
    author = authoring.get("author", "")

    def _item(slug, b):
        item = {
            "project_slug": slug,
            "period_start": ps,
            "period_end": pe,
            "title": b["title"],
            "summary": b.get("summary", ""),
            "content": b["content"],
            "links": b.get("links", []),
            "all_prs": b.get("all_prs", []),
            "author": author,
            "source": source,
        }
        if produced_by_agent:
            item["produced_by_agent"] = produced_by_agent
        return item

    items = []
    rollup = authoring.get("rollup")
    if rollup:
        items.append(_item(None, rollup))
    for proj in authoring.get("projects", []):
        items.append(_item(proj["project_slug"], proj))
    return {"shareouts": items}


def _slim_prs(prs: list) -> list:
    """Reduce gather()'s PR dicts to the {number,title,url,state} shape the
    /shareouts API stores in all_prs."""
    return [
        {
            "number": p.get("number"),
            "title": p.get("title", ""),
            "url": p.get("url", ""),
            "state": p.get("state", ""),
        }
        for p in prs
    ]


def fill_all_prs_from_corpus(authoring: dict, corpus: dict) -> dict:
    """Populate each project's `all_prs` from the gathered corpus so the
    author doesn't hand-copy PR lists. Matches a project by repo basename ==
    project_slug (e.g. corpus key 'jjackson/ace' → slug 'ace'). Only fills
    items that don't already carry all_prs. Mutates and returns `authoring`.
    """
    by_slug = {
        repo.split("/")[-1]: data.get("prs", [])
        for repo, data in (corpus.get("projects") or {}).items()
    }
    for proj in authoring.get("projects", []):
        if proj.get("all_prs"):
            continue
        prs = by_slug.get(proj.get("project_slug"))
        if prs:
            proj["all_prs"] = _slim_prs(prs)
    return authoring


def detect_agent_slug(cwd: Path | None = None, env: dict | None = None) -> str:
    """Best-effort: which agent is producing this shareout, as a slug ("").

    Precedence (first hit wins):
      1. `$CANOPY_AGENT_SLUG` — an explicit environment override.
      2. A `config/agent.json` in `cwd` (the fleet agent marker echo/eva/hal
         carry) — its `slug`, else its `name` lowercased.
      3. "" — a human ran it, or the marker isn't reachable from here.

    NOTE: the shareout CLI is usually invoked from the canopy repo dir, so (2)
    rarely fires in practice — the reliable path is the agent passing
    `--produced-by-agent <slug>` explicitly (see the skill). This detection is
    the convenience fallback, not the primary mechanism.
    """
    env = os.environ if env is None else env
    slug = (env.get("CANOPY_AGENT_SLUG") or "").strip()
    if slug:
        return slug

    cwd = cwd or Path.cwd()
    marker = cwd / "config" / "agent.json"
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(data.get("slug") or data.get("name") or "").strip().lower()


def resolve_pat() -> str | None:
    # Same precedence as canopy_web.resolve_token, but returns None instead of
    # raising (this caller treats a missing PAT as "skip the post").
    try:
        return canopy_web.resolve_token(None)
    except RuntimeError:
        return None


def post(payload: dict, api_url: str, token: str, timeout: int = 60) -> tuple[int, dict]:
    """POST a ShareoutBatchIn to canopy-web. Returns (status, body)."""
    url = f"{api_url.rstrip('/')}/api/shareouts/"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8") or "{}")
        except json.JSONDecodeError:
            body = {"error": e.reason}
        return e.code, body
    raw = resp.read()
    return resp.status, (json.loads(raw.decode("utf-8")) if raw else {})


def clear(filters: dict, api_url: str, token: str, timeout: int = 30) -> tuple[int, dict]:
    """POST a clear request to canopy-web. `filters` keys: source, project,
    date_from, date_to (all optional; {} clears all). Returns (status, body)."""
    url = f"{api_url.rstrip('/')}/api/shareouts/clear/"
    data = json.dumps(filters).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8") or "{}")
        except json.JSONDecodeError:
            body = {"error": e.reason}
        return e.code, body
    raw = resp.read()
    return resp.status, (json.loads(raw.decode("utf-8")) if raw else {})


def feed_url(api_url: str) -> str:
    return f"{api_url.rstrip('/')}/shareouts"
