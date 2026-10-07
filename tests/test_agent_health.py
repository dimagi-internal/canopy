"""Tests for `canopy agent health` — work-state readiness probe (board + inbox facts)."""
import json
import subprocess
from datetime import datetime, timezone

from orchestrator.agent_health import (
    health_report,
    junk_signals,
    probe_board,
    probe_inbox,
    resolve_mailbox,
    run_agent_health,
)

NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)


# ---------- junk signals (deterministic, never verdicts) ----------

def test_junk_signals_detects_each_pattern():
    assert "noreply_sender" in junk_signals(
        {"from": "GitHub <noreply@github.com>", "subject": "x", "labels": []})
    assert "noreply_sender" in junk_signals(
        {"from": "Do-Not-Reply <do-not-reply@corp.com>", "subject": "x", "labels": []})
    assert "category_label" in junk_signals(
        {"from": "a@b.c", "subject": "x", "labels": ["UNREAD", "CATEGORY_PROMOTIONS"]})
    assert "category_label" in junk_signals(
        {"from": "a@b.c", "subject": "x", "labels": ["CATEGORY_UPDATES"]})
    assert "calendar_response" in junk_signals(
        {"from": "a@b.c", "subject": "Accepted: weekly sync @ Mon", "labels": []})
    assert "security_alert" in junk_signals(
        {"from": "Google <no-reply@accounts.google.com>",
         "subject": "Critical security alert", "labels": []})


def test_junk_signals_empty_for_real_mail():
    assert junk_signals(
        {"from": "Fiorenzo Conte <fconte@dimagi.com>",
         "subject": "questions about users stories", "labels": ["UNREAD", "INBOX"]}) == []


# ---------- mailbox resolution from gog auth ----------

def test_resolve_mailbox_matches_slug_and_reports_missing():
    accounts = [
        {"email": "echo@dimagi-ai.com", "client": "echo"},
        {"email": "eva@dimagi-ai.com", "client": "canopy"},
    ]
    assert resolve_mailbox("echo", accounts) == ("echo@dimagi-ai.com", "echo")
    assert resolve_mailbox("eva", accounts) == ("eva@dimagi-ai.com", "canopy")
    assert resolve_mailbox("nope", accounts) is None


# ---------- inbox probe ----------

def _gog_runner(payload, returncode=0):
    def runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, returncode=returncode,
                                           stdout=json.dumps(payload), stderr="")
    return runner


def test_probe_inbox_ages_threads_and_flags_stale():
    payload = {"threads": [
        {"id": "t1", "date": "2026-07-13 09:00", "from": "fconte@dimagi.com",
         "subject": "real work", "labels": ["UNREAD", "INBOX"]},
        {"id": "t2", "date": "2026-06-23 04:01", "from": "Expensify <concierge@expensify.com>",
         "subject": "Welcome to New Expensify", "labels": ["UNREAD", "CATEGORY_UPDATES", "INBOX"]},
    ]}
    inbox = probe_inbox("echo@dimagi-ai.com", "echo", runner=_gog_runner(payload),
                        now=NOW, stale_days=3)
    assert inbox["error"] is None
    assert len(inbox["unread"]) == 2
    fresh, old = inbox["unread"]
    assert fresh["stale"] is False and old["stale"] is True
    assert old["junk_signals"] == ["category_label"]
    assert old["age_days"] > 20


def test_probe_inbox_degrades_loud_on_gog_failure():
    inbox = probe_inbox("x@y.z", "c", runner=_gog_runner({}, returncode=1),
                        now=NOW, stale_days=3)
    assert inbox["error"] is not None
    assert inbox["unread"] == []


# ---------- board probe ----------

def _board_call(agent_detail, open_items, harness_turns):
    def call(method, path, body=None, **kw):
        assert method == "GET"
        if "/tasks/?ask=open" in path:  # tasks with an open ask — a bare list
            return open_items
        if "/harness/turns/" in path:
            return harness_turns
        return agent_detail
    return call


def test_probe_board_flags_stale_items_and_turns():
    board = probe_board(
        "echo",
        call=_board_call(
            {"slug": "echo", "turn_count": 4, "latest_turn_at": "2026-07-01T00:00:00Z"},
            [  # open asks — a bare list of tasks, each carrying `ask_kind` + `created_at`
                {"ask_kind": "review", "title": "old", "created_at": "2026-06-20T00:00:00Z"},
                {"ask_kind": "question", "title": "new", "created_at": "2026-07-13T00:00:00Z"},
            ],
            [  # list envelope (the live API returns a bare list)
                {"id": "a", "status": "done", "finished_at": "2026-07-13T00:00:00Z",
                 "lease_expires_at": "2026-07-13T00:15:00Z"},
                {"id": "b", "status": "claimed", "created_at": "2026-07-14T00:00:00Z",
                 "lease_expires_at": "2026-07-14T00:15:00Z"},  # past lease vs NOW
                {"id": "c", "status": "failed", "finished_at": "2026-07-13T20:00:00Z",
                 "lease_expires_at": None},  # failed inside 48h window
                {"id": "d", "status": "failed", "finished_at": "2026-07-01T00:00:00Z",
                 "lease_expires_at": None},  # failed long ago — ignored
            ],
        ),
        now=NOW, stale_needs_you_days=7,
    )
    assert board["turn_age_days"] > 13  # surfaced as info; never a flag
    ages = {i["title"]: i["stale"] for i in board["needs_you"]}
    assert ages == {"old": True, "new": False}
    anomalies = {t["id"]: t for t in board["harness_turns"]}
    assert set(anomalies) == {"b", "c"}          # done + old-failed excluded
    assert anomalies["b"]["past_lease"] is True


# ---------- report assembly: flags + ready ----------

def _full_call(latest_turn="2026-07-14T09:00:00Z", items=(), turns=()):
    return _board_call(
        {"slug": "echo", "turn_count": 4, "latest_turn_at": latest_turn},
        list(items),
        list(turns),
    )


def _accounts_runner(accounts, threads):
    """One runner serving both `gog auth list --json` and `gog gmail search`."""
    def runner(cmd, **kw):
        payload = {"accounts": accounts} if "auth" in cmd else {"threads": threads}
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")
    return runner


def test_health_report_ready_when_clean():
    rep = health_report("echo", call=_full_call(), now=NOW,
                        runner=_accounts_runner(
                            [{"email": "echo@dimagi-ai.com", "client": "echo"}], []))
    assert rep["flags"] == [] and rep["ready"] is True


def test_health_report_collects_flags():
    rep = health_report(
        "echo",
        call=_full_call(
            latest_turn="2026-06-01T00:00:00Z",
            items=[{"ask_kind": "review", "title": "old", "created_at": "2026-06-01T00:00:00Z"}],
            turns=[{"id": "x", "status": "failed", "finished_at": "2026-07-14T00:00:00Z",
                    "lease_expires_at": None}],
        ),
        now=NOW,
        runner=_accounts_runner(
            [{"email": "echo@dimagi-ai.com", "client": "echo"}],
            [{"id": "t", "date": "2026-06-23 04:01", "from": "noreply@x.com",
              "subject": "hi", "labels": ["UNREAD"]}]),
    )
    assert rep["ready"] is False
    # A long-ago last turn is NOT a flag — turn packaging is manual, recency is info only.
    assert set(rep["flags"]) == {"stale_needs_you", "failed_turn", "stale_inbox"}


def test_health_report_missing_mailbox_is_inbox_unreachable():
    rep = health_report("ghost", call=_full_call(), now=NOW,
                        runner=_accounts_runner([], []))
    assert "inbox_unreachable" in rep["flags"]
    assert rep["ready"] is False
    assert rep["inbox"]["error"]


def test_health_report_never_turned_agent_is_not_flagged():
    # Turn packaging is manual, so an agent that never packaged a turn is NOT unhealthy:
    # no stale_turn flag exists, and an otherwise-clean agent still reads ready.
    rep = health_report("echo", call=_full_call(latest_turn=None), now=NOW,
                        runner=_accounts_runner(
                            [{"email": "echo@dimagi-ai.com", "client": "echo"}], []))
    assert "stale_turn" not in rep["flags"]
    assert rep["flags"] == [] and rep["ready"] is True
    assert rep["board"]["turn_age_days"] is None  # still surfaced as info


# ---------- fleet sweep ----------

def test_run_agent_health_sweeps_paginated_fleet():
    def call(method, path, body=None, **kw):
        if path.rstrip("/").endswith("/api/agents") and "offset" not in path:
            return {"items": [{"slug": "echo"}, {"slug": "eva"}], "total": 3,
                    "offset": 0, "limit": 2}
        if "offset=2" in path:
            return {"items": [{"slug": "hal"}], "total": 3, "offset": 2, "limit": 2}
        if "/tasks/?ask=open" in path:
            return []
        if "/harness/turns/" in path:
            return []
        return {"slug": "x", "turn_count": 1, "latest_turn_at": "2026-07-14T09:00:00Z"}

    out = run_agent_health(call=call, now=NOW,
                           runner=_accounts_runner(
                               [{"email": e, "client": "canopy"} for e in
                                ("echo@dimagi-ai.com", "eva@dimagi-ai.com", "hal@dimagi-ai.com")],
                               []))
    assert [a["agent"] for a in out["agents"]] == ["echo", "eva", "hal"]
    assert out["ok"] is True


def test_run_agent_health_single_slug():
    out = run_agent_health(slug="echo", call=_full_call(), now=NOW,
                           runner=_accounts_runner(
                               [{"email": "echo@dimagi-ai.com", "client": "echo"}], []))
    assert len(out["agents"]) == 1 and out["agents"][0]["agent"] == "echo"
