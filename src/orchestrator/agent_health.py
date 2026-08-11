"""Per-AGENT work-state readiness — `canopy agent health`.

`canopy agent doctor` answers "can this MACHINE run the agent" (identity, secrets,
gog auth, canopy-web registration). THIS module answers the next question: "is the
agent's WORKLOAD in a healthy state for its next turn" — stale needs-you items
sitting on the board, stuck or recently-failed harness turns, turn recency, and
inbox hygiene (unread junk that would pollute inbox-triage into burning a turn on
non-work).

Facts and deterministic signals only — junk VERDICTS are the caller's job (Ada's
fleet-audit skill judges borderline mail; this module never does). Read-only by
construction: it writes to no mailbox, no board, no turn queue. Junk findings feed
the fleet filter set in ``inbox_filters.FILTERS`` — the remediation path is "add a
rule there and re-run `canopy email apply-filters --all`", never per-mailbox
hand-cleanup.

Same shape as agent_doctor.py: small probes with injectable dependencies
(``call`` for canopy-web, ``runner`` for gog), composed by ``run_agent_health``.
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from typing import Callable, Optional

from orchestrator import canopy_web
from orchestrator.agent_client import list_agent_slugs

DEFAULT_STALE_NEEDS_YOU_DAYS = 7.0
DEFAULT_STALE_INBOX_DAYS = 3.0
FAILED_TURN_WINDOW_HOURS = 48.0

MAILBOX_DOMAIN = "dimagi-ai.com"  # fleet convention: <slug>@dimagi-ai.com

_NOREPLY_RE = re.compile(r"\b(no-?reply|donotreply|do-not-reply|mailer-daemon|postmaster)\b", re.I)
_CALENDAR_RE = re.compile(r"^(accepted|declined|tentative|updated invitation|invitation):", re.I)
_SECURITY_RE = re.compile(r"security alert", re.I)
_CATEGORY_LABELS = {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_UPDATES", "CATEGORY_FORUMS"}


# ---------- shared helpers ----------

def _parse_when(value: Optional[str]) -> Optional[datetime]:
    """Parse canopy-web ISO timestamps ('…Z') and gog's 'YYYY-MM-DD HH:MM' (local time)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.strptime(value, "%Y-%m-%d %H:%M")
        except ValueError:
            return None
    # gog prints local naive times; fromisoformat also parses them (space separator, py3.11+)
    # but leaves them naive — attach the local zone so ages compare against aware `now`.
    return dt.astimezone() if dt.tzinfo is None else dt


def _age_days(value: Optional[str], now: datetime) -> Optional[float]:
    dt = _parse_when(value)
    if dt is None:
        return None
    return round((now - dt).total_seconds() / 86400.0, 1)


# ---------- inbox ----------

def junk_signals(thread: dict) -> list[str]:
    """Deterministic junk SIGNALS for one unread thread — never a verdict."""
    signals = []
    if _NOREPLY_RE.search(thread.get("from") or ""):
        signals.append("noreply_sender")
    if _CATEGORY_LABELS & set(thread.get("labels") or []):
        signals.append("category_label")
    if _CALENDAR_RE.match((thread.get("subject") or "").strip()):
        signals.append("calendar_response")
    if _SECURITY_RE.search(thread.get("subject") or ""):
        signals.append("security_alert")
    return signals


def list_gog_accounts(*, runner=subprocess.run) -> list[dict]:
    """Accounts authed on THIS machine, from `gog auth list --json`."""
    try:
        r = runner(["gog", "auth", "list", "--json"],
                   capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    try:
        return json.loads(r.stdout or "{}").get("accounts") or []
    except ValueError:
        return []


def resolve_mailbox(slug: str, accounts: list[dict]) -> Optional[tuple[str, str]]:
    """(mailbox, gog_client) for an agent slug, or None if not authed on this machine."""
    want = f"{slug.lower()}@{MAILBOX_DOMAIN}"
    for a in accounts:
        if (a.get("email") or "").lower() == want:
            return a["email"], a.get("client") or ""
    return None


def probe_inbox(mailbox: str, client: str, *, runner=subprocess.run,
                now: datetime, stale_days: float = DEFAULT_STALE_INBOX_DAYS) -> dict:
    """Unread threads with ages + junk signals. Degrades LOUD: an unreadable inbox
    sets `error` (and the caller flags inbox_unreachable) — never a silent empty list."""
    cmd = ["gog", "gmail", "search", "is:unread", "--account", mailbox,
           "--json", "--max", "100"]
    if client:
        cmd += ["--client", client]
    try:
        r = runner(cmd, capture_output=True, text=True, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return {"unread": [], "error": f"gog unavailable: {e}"}
    if r.returncode != 0:
        detail = (r.stderr.strip() or r.stdout.strip())[:200]
        return {"unread": [], "error": f"gog gmail search failed: {detail}"}
    try:
        threads = json.loads(r.stdout or "{}").get("threads") or []
    except ValueError:
        return {"unread": [], "error": "gog gmail search returned non-JSON"}
    unread = []
    for t in threads:
        age = _age_days(t.get("date"), now)
        unread.append({
            "thread_id": t.get("id"),
            "from": t.get("from"),
            "subject": t.get("subject"),
            "age_days": age,
            "junk_signals": junk_signals(t),
            "stale": age is not None and age > stale_days,
        })
    return {"unread": unread, "error": None}


# ---------- board ----------

def probe_board(slug: str, *, call: Callable = canopy_web.call, now: datetime,
                stale_needs_you_days: float = DEFAULT_STALE_NEEDS_YOU_DAYS) -> dict:
    """Board/turn facts from canopy-web: needs-you ages, harness-turn anomalies, and
    turn recency.

    `latest_turn_at` now means WHEN THE AGENT LAST RAN — canopy-web serves it off the
    harness turn queue, the same rows this function reads below for anomalies. Until
    2026-08-11 it came from a separate close-out table that only got a row when an
    agent survived long enough to package itself, so it read weeks stale (or null)
    against a busy queue, and this docstring told you to ignore it for exactly that
    reason. It is a real signal again.

    Still surfaced rather than FLAGGED here: what counts as too-quiet is per-agent
    (a scheduled agent runs hourly, an on-demand one may idle for days), so the
    judgment belongs to the caller reading the report, not to a threshold in here."""
    detail = call("GET", f"/api/agents/{slug}/")
    # The inbox is a pure Item query now (canopy-web #304 deleted the needs_you
    # aggregation): read the agent's OPEN items directly. The endpoint returns a
    # bare list, each item carrying `kind` (review|question) + `created_at`.
    open_items = call("GET", f"/api/agents/{slug}/items/?state=open")
    if isinstance(open_items, dict):  # tolerate a paginated envelope if the API grows one
        open_items = open_items.get("items") or []
    turns = call("GET", f"/api/harness/turns/?agent={slug}")
    if isinstance(turns, dict):  # tolerate a paginated envelope if the API grows one
        turns = turns.get("items") or []

    turn_age = _age_days(detail.get("latest_turn_at"), now)
    items = []
    for i in open_items or []:
        age = _age_days(i.get("created_at"), now)
        items.append({
            "type": i.get("kind"), "title": i.get("title"), "age_days": age,
            "stale": age is not None and age > stale_needs_you_days,
        })

    anomalies = []
    for t in turns:
        status = t.get("status")
        if status in ("claimed", "running"):
            lease = _parse_when(t.get("lease_expires_at"))
            anomalies.append({"id": t.get("id"), "status": status,
                              "past_lease": lease is not None and lease < now})
        elif status in ("failed", "lost"):
            finished_age = _age_days(t.get("finished_at") or t.get("created_at"), now)
            if finished_age is not None and finished_age * 24 <= FAILED_TURN_WINDOW_HOURS:
                anomalies.append({"id": t.get("id"), "status": status, "past_lease": False})

    return {
        "latest_turn_at": detail.get("latest_turn_at"),
        "turn_age_days": turn_age,
        "turn_count": detail.get("turn_count"),
        "needs_you": items,
        "harness_turns": anomalies,
    }


# ---------- assembly ----------

def health_report(slug: str, *, call: Callable = canopy_web.call,
                  runner=subprocess.run, now: Optional[datetime] = None,
                  stale_needs_you_days: float = DEFAULT_STALE_NEEDS_YOU_DAYS,
                  stale_inbox_days: float = DEFAULT_STALE_INBOX_DAYS) -> dict:
    """One agent's readiness: board + inbox facts, derived flags, ready bool."""
    now = now or datetime.now(timezone.utc)
    board = probe_board(slug, call=call, now=now,
                        stale_needs_you_days=stale_needs_you_days)

    resolved = resolve_mailbox(slug, list_gog_accounts(runner=runner))
    if resolved is None:
        inbox = {"unread": [], "error": f"no gog account for {slug}@{MAILBOX_DOMAIN} on this machine"}
    else:
        inbox = probe_inbox(*resolved, runner=runner, now=now, stale_days=stale_inbox_days)

    flags = []
    if any(i["stale"] for i in board["needs_you"]):
        flags.append("stale_needs_you")
    if any(t["status"] in ("claimed", "running") and t["past_lease"]
           for t in board["harness_turns"]):
        flags.append("stuck_turn")
    if any(t["status"] in ("failed", "lost") for t in board["harness_turns"]):
        flags.append("failed_turn")
    if inbox["error"] is not None:
        flags.append("inbox_unreachable")
    elif any(u["stale"] for u in inbox["unread"]):
        flags.append("stale_inbox")

    return {"agent": slug, "ready": not flags, "flags": flags,
            "board": board, "inbox": inbox}


def run_agent_health(slug: Optional[str] = None, *, call: Callable = canopy_web.call,
                     runner=subprocess.run, now: Optional[datetime] = None,
                     stale_needs_you_days: float = DEFAULT_STALE_NEEDS_YOU_DAYS,
                     stale_inbox_days: float = DEFAULT_STALE_INBOX_DAYS) -> dict:
    """Probe one agent (slug) or sweep the whole registered fleet (slug=None)."""
    now = now or datetime.now(timezone.utc)
    slugs = [slug] if slug else list_agent_slugs(call)
    agents = [health_report(s, call=call, runner=runner, now=now,
                            stale_needs_you_days=stale_needs_you_days,
                            stale_inbox_days=stale_inbox_days)
              for s in slugs]
    return {"ok": all(a["ready"] for a in agents), "agents": agents}
