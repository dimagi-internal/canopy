"""The owed-reply sweep — threads where the BALL IS WITH US (agent-core/turn.md Step 2).

Why this exists, and why it is a test rather than a paragraph.

A turn driven by *new inbound* is structurally incapable of noticing the one failure
that actually costs a relationship: a thread where someone asked us something and we
never answered. Such a thread generates no new mail **by definition** — the counterpart
is waiting on us — so the quieter the inbox, the more certain the miss. ACE went 42 days
without replying to a partner who had answered all three of its questions and attached
the file it asked for (dimagi-internal/ace#1931); the deliverable he was waiting for had
been finished for a week and simply sat there. Nobody noticed because nothing in a turn
ever looked.

ACE's fix made its own aging section *reachable* on a quiet inbox. That fix lives only in
ACE's repo, and every other agent lacks even the section to reach — echo, eva and hal had
zero owed-reply sweep between them, and so did this file's own turn.md. So the sweep is
built HERE, once, as engine + procedure, and every agent inherits it.

Prose already failed at this exact spot once: ACE's section was headed "every turn" and was
skipped anyway, because a line 95 lines above it ended the turn first. Hence tests, not text.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from orchestrator.agent_email import EmailIdentity, owed_replies, search_threads

ME = "echo@dimagi-ai.com"
NOW = dt.datetime(2026, 9, 4, 12, 0, 0, tzinfo=dt.timezone.utc)


def _identity() -> EmailIdentity:
    return EmailIdentity(slug="echo", account=ME, client="canopy")


def _msg(sender: str, date: str, *, automated: bool = False, subject: str = "A question") -> dict:
    return {
        "message_id": f"m-{date}-{sender}",
        "from": sender,
        "to": ME,
        "cc": "",
        "subject": subject,
        "date": date,
        "snippet": "",
        "body_text": "",
        "attachments": [],
        "auto_submitted": "auto-generated" if automated else "",
        "precedence": "",
        "sender": "",
        "is_automated": automated,
    }


def _searcher(threads: list[dict]):
    """A fake `gog gmail search` that records the query it was handed."""
    calls: list[list[str]] = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return type("R", (), {
            "returncode": 0,
            "stdout": json.dumps({"nextPageToken": "", "threads": threads}),
            "stderr": "",
        })()

    runner.calls = calls
    return runner


def _reader(by_thread: dict[str, list[dict]]):
    def read(identity, thread_id, **kw):
        return {"thread_id": thread_id, "messages": by_thread[thread_id], "reply_all": {}}
    return read


# --------------------------------------------------------------------------------------
# the core discrimination: who spoke last
# --------------------------------------------------------------------------------------

def test_a_thread_we_answered_last_is_not_owed():
    """The ball is with THEM. This is the case a normal turn already handles."""
    runner = _searcher([{"id": "t1", "date": "2026-08-01 09:00", "messageCount": 2,
                         "from": "Enock <enock@partner.org>", "subject": "Design questions"}])
    reader = _reader({"t1": [
        _msg("Enock <enock@partner.org>", "Fri, 24 Jul 2026 09:00:00 +0000"),
        _msg(ME, "Sat, 01 Aug 2026 09:00:00 +0000"),
    ]})
    assert owed_replies(_identity(), now=NOW, runner=runner, reader=reader) == []


def test_a_thread_they_spoke_last_on_is_owed():
    """The ball is with US — the case no turn could previously see."""
    runner = _searcher([{"id": "t1", "date": "2026-07-24 09:00", "messageCount": 2,
                         "from": "Enock <enock@partner.org>", "subject": "Design questions"}])
    reader = _reader({"t1": [
        _msg(ME, "Mon, 20 Jul 2026 09:00:00 +0000"),
        _msg("Enock <enock@partner.org>", "Fri, 24 Jul 2026 09:00:00 +0000"),
    ]})
    owed = owed_replies(_identity(), now=NOW, runner=runner, reader=reader)
    assert [o["thread_id"] for o in owed] == ["t1"]
    assert owed[0]["last_from"] == "Enock <enock@partner.org>"
    assert owed[0]["subject"] == "Design questions"


def test_our_address_is_matched_inside_a_display_name():
    """`Echo <echo@dimagi-ai.com>` is us. Comparing the raw From: header would not be."""
    runner = _searcher([{"id": "t1", "date": "2026-08-01 09:00", "messageCount": 1,
                         "from": "x", "subject": "s"}])
    reader = _reader({"t1": [_msg(f"Echo <{ME.upper()}>", "Sat, 01 Aug 2026 09:00:00 +0000")]})
    assert owed_replies(_identity(), now=NOW, runner=runner, reader=reader) == []


# --------------------------------------------------------------------------------------
# the invariant that makes it work at all
# --------------------------------------------------------------------------------------

def test_the_sweep_never_filters_on_unread():
    """THE point. An owed-reply thread produces no new inbound and is usually already
    read — it went unanswered *after* we read it. A query carrying `is:unread` finds
    exactly the threads this sweep is not for, and none of the ones it is."""
    runner = _searcher([])
    owed_replies(_identity(), now=NOW, runner=runner, reader=_reader({}))
    query = " ".join(runner.calls[0])
    assert "is:unread" not in query
    assert "in:inbox" in query


def test_it_reads_as_the_agents_own_mailbox():
    """One mailbox per agent, never shared — the fleet's one hard rule."""
    runner = _searcher([])
    owed_replies(_identity(), now=NOW, runner=runner, reader=_reader({}))
    cmd = runner.calls[0]
    assert cmd[cmd.index("--account") + 1] == ME


# --------------------------------------------------------------------------------------
# noise, ordering, and the fields a human acts on
# --------------------------------------------------------------------------------------

def test_an_automated_last_message_is_not_an_owed_reply():
    """Nobody is waiting on a reply to a Drive share notification."""
    runner = _searcher([{"id": "t1", "date": "2026-08-01 09:00", "messageCount": 1,
                         "from": "x", "subject": "shared a file with you"}])
    reader = _reader({"t1": [
        _msg("drive-shares-dm-noreply@google.com", "Sat, 01 Aug 2026 09:00:00 +0000",
             automated=True),
    ]})
    assert owed_replies(_identity(), now=NOW, runner=runner, reader=reader) == []
    assert len(owed_replies(_identity(), now=NOW, runner=runner, reader=reader,
                            include_automated=True)) == 1


def test_age_is_measured_from_their_last_message():
    """The number a human acts on: how long they have been waiting on us."""
    runner = _searcher([{"id": "t1", "date": "2026-07-24 09:00", "messageCount": 1,
                         "from": "x", "subject": "s"}])
    reader = _reader({"t1": [_msg("Enock <enock@partner.org>",
                                  "Fri, 24 Jul 2026 12:00:00 +0000")]})
    assert owed_replies(_identity(), now=NOW, runner=runner, reader=reader)[0]["age_days"] == 42


def test_the_longest_silence_is_reported_first():
    runner = _searcher([
        {"id": "recent", "date": "2026-09-01 09:00", "messageCount": 1, "from": "x", "subject": "s"},
        {"id": "ancient", "date": "2026-07-24 09:00", "messageCount": 1, "from": "x", "subject": "s"},
    ])
    reader = _reader({
        "recent": [_msg("a@partner.org", "Tue, 01 Sep 2026 12:00:00 +0000")],
        "ancient": [_msg("b@partner.org", "Fri, 24 Jul 2026 12:00:00 +0000")],
    })
    owed = owed_replies(_identity(), now=NOW, runner=runner, reader=reader)
    assert [o["thread_id"] for o in owed] == ["ancient", "recent"]


def test_a_thread_we_have_never_answered_at_all_is_marked():
    """Worse than a late reply: a counterpart we have never once written back to."""
    runner = _searcher([{"id": "t1", "date": "2026-07-24 09:00", "messageCount": 1,
                         "from": "x", "subject": "s"}])
    reader = _reader({"t1": [_msg("new@partner.org", "Fri, 24 Jul 2026 12:00:00 +0000")]})
    assert owed_replies(_identity(), now=NOW, runner=runner, reader=reader)[0]["ever_replied"] is False


def test_one_unreadable_thread_does_not_sink_the_sweep():
    """A sweep that dies on thread 3 of 40 silently hides threads 4-40 — the same
    class of blindness it was built to remove."""
    runner = _searcher([
        {"id": "bad", "date": "2026-08-01 09:00", "messageCount": 1, "from": "x", "subject": "s"},
        {"id": "good", "date": "2026-07-24 09:00", "messageCount": 1, "from": "x", "subject": "s"},
    ])
    good = [_msg("b@partner.org", "Fri, 24 Jul 2026 12:00:00 +0000")]

    def read(identity, thread_id, **kw):
        if thread_id == "bad":
            raise RuntimeError("gog fell over")
        return {"thread_id": thread_id, "messages": good, "reply_all": {}}

    owed = owed_replies(_identity(), now=NOW, runner=runner, reader=read)
    assert [o["thread_id"] for o in owed] == ["good"]


def test_an_empty_thread_is_skipped_not_crashed():
    runner = _searcher([{"id": "t1", "date": "2026-08-01 09:00", "messageCount": 0,
                         "from": "x", "subject": "s"}])
    assert owed_replies(_identity(), now=NOW, runner=runner,
                        reader=_reader({"t1": []})) == []


def test_search_threads_asks_gog_for_every_page():
    """Gmail paginates; a first-page-only sweep is a sweep with a silent horizon."""
    runner = _searcher([])
    search_threads(_identity(), "in:inbox", runner=runner)
    assert "--all" in runner.calls[0]


# --------------------------------------------------------------------------------------
# the procedure, pinned — prose at this spot has failed once already
# --------------------------------------------------------------------------------------

TURN = Path(__file__).resolve().parents[1] / "plugins" / "canopy" / "agent-core" / "turn.md"


def test_turn_md_mandates_the_sweep():
    assert "canopy email owed" in TURN.read_text(), \
        "the fleet turn procedure must name the sweep command, or no agent runs it"


def test_turn_md_puts_the_sweep_before_any_early_return():
    """The whole ACE failure in one assertion: the sweep must be stated to run even
    when there is no new mail, because that is precisely when it matters most."""
    text = TURN.read_text()
    sweep = text.index("canopy email owed")
    assert any(
        phrase in text[max(0, sweep - 3000):sweep + 3000].lower()
        for phrase in ("before any early return", "even when the inbox is clear",
                       "inbox clear")
    ), "turn.md must say the sweep runs on a quiet inbox, not only when mail arrives"
