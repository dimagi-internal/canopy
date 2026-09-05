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

from orchestrator.agent_email import (
    EmailIdentity,
    dangling_threads,
    owed_replies,
    search_threads,
)

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


def _searcher(threads: list[dict], unread_ids: "set[str] | None" = None):
    """A fake `gog gmail search` that records the query it was handed.

    It answers the sweep's TWO queries differently, the way Gmail does: the
    enumeration query returns everything, and an `is:unread` query returns only
    the threads in `unread_ids` (default: none — a thread is read unless a test
    says otherwise).

    A fake that returned one canned list for every query is what let canopy#608
    through: it made the row `labels` look like a usable proxy for thread state,
    and the real `gog` never populates them that way.
    """
    calls: list[list[str]] = []
    unread = unread_ids or set()

    def runner(cmd, **kw):
        calls.append(cmd)
        rows = ([r for r in threads if r.get("id") in unread]
                if "is:unread" in " ".join(cmd) else threads)
        return type("R", (), {
            "returncode": 0,
            "stdout": json.dumps({"nextPageToken": "", "threads": rows}),
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
    assert dangling_threads(_identity(), now=NOW, runner=runner, reader=reader) == []


def test_a_thread_they_spoke_last_on_is_owed():
    """The ball is with US — the case no turn could previously see."""
    runner = _searcher([{"id": "t1", "date": "2026-07-24 09:00", "messageCount": 2,
                         "from": "Enock <enock@partner.org>", "subject": "Design questions"}])
    reader = _reader({"t1": [
        _msg(ME, "Mon, 20 Jul 2026 09:00:00 +0000"),
        _msg("Enock <enock@partner.org>", "Fri, 24 Jul 2026 09:00:00 +0000"),
    ]})
    owed = dangling_threads(_identity(), now=NOW, runner=runner, reader=reader)
    assert [o["thread_id"] for o in owed] == ["t1"]
    assert owed[0]["last_from"] == "Enock <enock@partner.org>"
    assert owed[0]["subject"] == "Design questions"


def test_our_address_is_matched_inside_a_display_name():
    """`Echo <echo@dimagi-ai.com>` is us. Comparing the raw From: header would not be."""
    runner = _searcher([{"id": "t1", "date": "2026-08-01 09:00", "messageCount": 1,
                         "from": "x", "subject": "s"}])
    reader = _reader({"t1": [_msg(f"Echo <{ME.upper()}>", "Sat, 01 Aug 2026 09:00:00 +0000")]})
    assert dangling_threads(_identity(), now=NOW, runner=runner, reader=reader) == []


# --------------------------------------------------------------------------------------
# the invariant that makes it work at all
# --------------------------------------------------------------------------------------

def test_the_ENUMERATION_query_never_filters_on_unread():
    """THE point. An owed-reply thread produces no new inbound and is usually already
    read — it went unanswered *after* we read it. An enumeration query carrying
    `is:unread` finds exactly the threads this sweep is not for, and none of the ones
    it is.

    Scoped to the FIRST call deliberately: the sweep does ask `is:unread` separately,
    to learn which of the enumerated threads nobody has looked at (canopy#608). That
    is banding, not enumeration, and the distinction is the whole design — so the
    assertion pins the enumeration call rather than the absence of the string."""
    runner = _searcher([])
    dangling_threads(_identity(), now=NOW, runner=runner, reader=_reader({}))
    query = " ".join(runner.calls[0])
    assert "is:unread" not in query
    assert "in:inbox" in query


def test_unread_comes_from_the_thread_level_query_not_the_row_labels():
    """canopy#608 — the bug that told an agent to archive a live ask.

    gog's search row is a PER-MESSAGE projection: one row per thread carrying the
    ORIGINATING message's labels. A long thread whose newest message is unread comes
    back with no UNREAD label at all (measured on a real mailbox: the row also lacked
    INBOX, on the very query `in:inbox` that returned it).

    Reading `unread` off those labels banded a message sent three hours earlier — a
    direct, unanswered request — as `handled`, under a banner reading "Do NOT answer
    these late. Archive them." That is the implicit-miss failure this whole sweep was
    built to catch, produced by the sweep itself.

    So the row below carries NO labels, as the real one does, while the thread IS
    unread. Old code: disposition == "handled". Fixed: "respond".
    """
    row = {"id": "t1", "date": "2026-09-04 09:00", "messageCount": 11,
           "from": "Jonathan <jjackson@dimagi.com>", "subject": "KMC metrics",
           "labels": ["CATEGORY_PERSONAL"]}
    runner = _searcher([row], unread_ids={"t1"})
    reader = _reader({"t1": [
        _msg(ME, "Wed, 26 Aug 2026 09:00:00 +0000"),
        _msg("Jonathan <jjackson@dimagi.com>", "Fri, 04 Sep 2026 09:00:00 +0000"),
    ]})

    owed = dangling_threads(_identity(), now=NOW, runner=runner, reader=reader)

    assert [o["thread_id"] for o in owed] == ["t1"]
    assert owed[0]["unread"] is True
    assert owed[0]["disposition"] == "respond", (
        "a thread the unread query returns must band as respond even though its "
        "search row carries no UNREAD label — that label list is per-message"
    )


def test_a_read_thread_still_bands_handled():
    """The other side of the same discriminator, so the fix cannot pass by calling
    everything unread. Same row shape; this thread is simply not in the unread set."""
    row = {"id": "t1", "date": "2026-07-24 09:00", "messageCount": 2,
           "from": "Enock <enock@partner.org>", "subject": "Design questions",
           "labels": ["CATEGORY_PERSONAL"]}
    runner = _searcher([row], unread_ids=set())
    reader = _reader({"t1": [
        _msg(ME, "Mon, 20 Jul 2026 09:00:00 +0000"),
        _msg("Enock <enock@partner.org>", "Fri, 24 Jul 2026 09:00:00 +0000"),
    ]})

    owed = dangling_threads(_identity(), now=NOW, runner=runner, reader=reader)

    assert [o["thread_id"] for o in owed] == ["t1"]
    assert owed[0]["unread"] is False
    assert owed[0]["disposition"] == "handled"


def test_the_unread_probe_is_scoped_to_the_same_window_and_mailbox():
    """A probe over a different window would misband the edges of the enumeration."""
    runner = _searcher([], unread_ids=set())
    dangling_threads(_identity(), days=30, now=NOW, runner=runner, reader=_reader({}))
    probes = [c for c in runner.calls if "is:unread" in " ".join(c)]
    assert len(probes) == 1, "exactly one unread probe per sweep, not one per thread"
    probe = probes[0]
    assert "newer_than:30d" in " ".join(probe)
    assert probe[probe.index("--account") + 1] == ME


def test_it_reads_as_the_agents_own_mailbox():
    """One mailbox per agent, never shared — the fleet's one hard rule."""
    runner = _searcher([])
    dangling_threads(_identity(), now=NOW, runner=runner, reader=_reader({}))
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
    assert dangling_threads(_identity(), now=NOW, runner=runner, reader=reader) == []
    assert len(dangling_threads(_identity(), now=NOW, runner=runner, reader=reader,
                            include_automated=True)) == 1


def test_age_is_measured_from_their_last_message():
    """The number a human acts on: how long they have been waiting on us."""
    runner = _searcher([{"id": "t1", "date": "2026-07-24 09:00", "messageCount": 1,
                         "from": "x", "subject": "s"}])
    reader = _reader({"t1": [_msg("Enock <enock@partner.org>",
                                  "Fri, 24 Jul 2026 12:00:00 +0000")]})
    assert dangling_threads(_identity(), now=NOW, runner=runner, reader=reader)[0]["age_days"] == 42


def test_the_longest_silence_is_reported_first():
    runner = _searcher([
        {"id": "recent", "date": "2026-09-01 09:00", "messageCount": 1, "from": "x", "subject": "s"},
        {"id": "ancient", "date": "2026-07-24 09:00", "messageCount": 1, "from": "x", "subject": "s"},
    ])
    reader = _reader({
        "recent": [_msg("a@partner.org", "Tue, 01 Sep 2026 12:00:00 +0000")],
        "ancient": [_msg("b@partner.org", "Fri, 24 Jul 2026 12:00:00 +0000")],
    })
    owed = dangling_threads(_identity(), now=NOW, runner=runner, reader=reader)
    assert [o["thread_id"] for o in owed] == ["ancient", "recent"]


def test_a_thread_we_have_never_answered_at_all_is_marked():
    """Worse than a late reply: a counterpart we have never once written back to."""
    runner = _searcher([{"id": "t1", "date": "2026-07-24 09:00", "messageCount": 1,
                         "from": "x", "subject": "s"}])
    reader = _reader({"t1": [_msg("new@partner.org", "Fri, 24 Jul 2026 12:00:00 +0000")]})
    assert dangling_threads(_identity(), now=NOW, runner=runner, reader=reader)[0]["ever_replied"] is False


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

    owed = dangling_threads(_identity(), now=NOW, runner=runner, reader=read)
    assert [o["thread_id"] for o in owed] == ["good"]


def test_an_empty_thread_is_skipped_not_crashed():
    runner = _searcher([{"id": "t1", "date": "2026-08-01 09:00", "messageCount": 0,
                         "from": "x", "subject": "s"}])
    assert dangling_threads(_identity(), now=NOW, runner=runner,
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
    assert "canopy email dangling" in TURN.read_text(), \
        "the fleet turn procedure must name the sweep command, or no agent runs it"


def test_turn_md_puts_the_sweep_before_any_early_return():
    """The whole ACE failure in one assertion: the sweep must be stated to run even
    when there is no new mail, because that is precisely when it matters most."""
    text = TURN.read_text()
    sweep = text.index("canopy email dangling")
    assert any(
        phrase in text[max(0, sweep - 3000):sweep + 3000].lower()
        for phrase in ("before any early return", "even when the inbox is clear",
                       "inbox clear")
    ), "turn.md must say the sweep runs on a quiet inbox, not only when mail arrives"


# --------------------------------------------------------------------------------------
# DETECTION IS NOT A VERDICT — the half that shipped wrong the first time
# --------------------------------------------------------------------------------------
#
# The sweep's first live run surfaced a 79-day thread and confidently proposed a late
# apology. Jonathan, 2026-09-04: "Old events like this weren't not handled. They were not
# good enough in terms of what you had written in response. And I killed the thread. So
# evidence of a dangling thread this old does not mean we should then go respond to it. It
# means we should make sure it's pruned."
#
# That is the opposite verdict from the one the sweep gave, on the same true finding. So
# the disposition is pinned harder than the detection was: a sweep that manufactures
# apologies is one an agent learns to stop running, and then the detection is worth zero.

def _one(thread_id: str, date: str, **kw):
    runner = _searcher([{"id": thread_id, "date": "2026-01-01 00:00", "messageCount": 1,
                         "from": "x", "subject": "s"}])
    reader = _reader({thread_id: [_msg("them@partner.org", date)]})
    return dangling_threads(_identity(), now=NOW, runner=runner, reader=reader, **kw)[0]


def test_the_sweep_itself_never_archives_anything():
    """Detection stays read-only. Pruning is a deliberate, separately-invoked act — the
    sweep prints the archive line, it does not run it."""
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return type("R", (), {"returncode": 0, "stdout": json.dumps(
            {"nextPageToken": "", "threads": [
                {"id": "t", "date": "2026-01-01 00:00", "messageCount": 1,
                 "from": "x", "subject": "s"}]}), "stderr": ""})()

    reader = _reader({"t": [_msg("them@partner.org", "Wed, 17 Jun 2026 12:00:00 +0000")]})
    dangling_threads(_identity(), now=NOW, runner=runner, reader=reader)
    flat = " ".join(" ".join(c) for c in calls)
    assert "modify" not in flat and "archive" not in flat


def test_turn_md_says_handled_threads_are_not_answered_late():
    """The procedure must carry the verdict, not just the query. An agent reading only
    turn.md must not conclude that a 79-day thread wants an apology."""
    text = TURN.read_text().lower()
    sweep = text.index("canopy email dangling")
    window = text[sweep:sweep + 4000]
    assert "handled" in window
    assert "do not answer it late" in window or "not answer" in window


# --------------------------------------------------------------------------------------
# HANDLED-NESS, not age — the discriminator that actually matches how a turn works
# --------------------------------------------------------------------------------------
#
# Jonathan, 2026-09-05, correcting the age heuristic that replaced the first wrong verdict:
# "The issue is less the response window and more whether the thread was handled... The
# primary mode of operating is something comes in, you fire right away, and if we don't
# conclude that session with a response, we didn't intend to come back to it (sometimes I
# forget but this is less common than implicit failure)."
#
# A turn is synchronous. So READ + no reply is a DECISION — the exchange finished, moved to
# Slack, or a human ended it. UNREAD is the failure: nothing ever disposed of it.
#
# The proof that this beats age rather than merely restating it: every thread the first
# live run surfaced was already read. Handled-ness clears all three with no threshold;
# age needed one tuned to 14d to reach the same answer for the wrong reason.

def _row(*, unread: bool, date: str = "Wed, 17 Jun 2026 12:00:00 +0000", **kw):
    """One thread, banded. `unread` drives the thread-level `is:unread` query.

    The row's own `labels` are pinned to the shape a REAL gog row has — a category
    and nothing else, no UNREAD and no INBOX even on an inbox thread — so every
    banding assertion below doubles as a guard on canopy#608: read the labels
    instead of the query and all of these flip.
    """
    runner = _searcher(
        [{"id": "t", "date": "2026-06-17 12:00", "messageCount": 1,
          "from": "x", "subject": "s", "labels": ["CATEGORY_PERSONAL"]}],
        unread_ids={"t"} if unread else set(),
    )
    reader = _reader({"t": [_msg("them@partner.org", date)]})
    return dangling_threads(_identity(), now=NOW, runner=runner, reader=reader, **kw)[0]


def test_a_read_thread_was_DISPOSED_of_however_old_it_is():
    """The 79-day thread. A turn looked at it and closed out without replying — that was
    a decision, not a lapse. Answering reopens what someone closed."""
    r = _row(unread=False)
    assert r["age_days"] == 79
    assert r["disposition"] == "handled"


def test_a_read_thread_is_handled_even_when_it_is_FRESH():
    """Age cannot rescue it: three days old and already concluded (the exchange finished,
    or it moved to Slack) is still not a debt. This is where age got it wrong."""
    assert _row(unread=False, date="Tue, 01 Sep 2026 12:00:00 +0000")["disposition"] == "handled"


def test_an_unread_thread_needs_attention_even_when_it_is_OLD():
    """The implicit failure, and the inverse of the case above: nothing ever looked. Age
    does not convert it into a decision somebody made."""
    r = _row(unread=True)
    assert r["disposition"] == "respond"


def test_an_unread_thread_needs_attention_when_it_is_fresh_too():
    assert _row(unread=True, date="Tue, 01 Sep 2026 12:00:00 +0000")["disposition"] == "respond"


def test_an_old_unread_thread_is_flagged_stale_without_being_dismissed():
    """Stale qualifies the item; it does not demote it. Nobody looked AND the answer is
    probably moot — usually close it rather than answer cold — but it is still the band
    a human should see."""
    r = _row(unread=True)
    assert r["stale"] is True and r["disposition"] == "respond"
    assert _row(unread=True, date="Tue, 01 Sep 2026 12:00:00 +0000")["stale"] is False


def test_stale_never_applies_to_a_handled_thread():
    """A disposed-of thread has no pending answer to go moot."""
    assert _row(unread=False)["stale"] is False


def test_banding_costs_one_query_for_the_whole_sweep_not_one_per_thread():
    """Handled-ness must stay cheap. A per-thread fetch would make the sweep expensive
    enough to skip, and a sweep that gets skipped catches nothing.

    This replaces an assertion that banding came off the search ROW at zero cost. It
    did — and it was wrong (canopy#608); the row's labels are per-message and cannot
    see thread state. Correctness is worth one query. What must NOT return is a cost
    that scales with the inbox, so the bound is asserted over THREE threads: two
    searches total, however many threads are enumerated.
    """
    threads = [{"id": f"t{i}", "date": "2026-06-17 12:00", "messageCount": 1,
                "from": "x", "subject": "s"} for i in range(3)]
    runner = _searcher(threads, unread_ids={"t0"})
    reader = _reader({f"t{i}": [_msg("them@partner.org", "Wed, 17 Jun 2026 12:00:00 +0000")]
                      for i in range(3)})

    owed = dangling_threads(_identity(), now=NOW, runner=runner, reader=reader)

    assert {o["thread_id"]: o["disposition"] for o in owed} == {
        "t0": "respond", "t1": "handled", "t2": "handled"}
    assert len(runner.calls) == 2, "enumeration + one unread probe, regardless of thread count"


def test_a_thread_the_unread_probe_does_not_return_reads_as_handled():
    """Absent evidence of a miss is not evidence of a miss. Defaulting the other way
    would manufacture a NEEDS ATTENTION item out of a gog response shape change.

    Same conservative default as before; it now hangs off the unread probe rather
    than off a `labels` key, which is the thing that could not be trusted.
    """
    runner = _searcher([{"id": "t", "date": "2026-06-17 12:00", "messageCount": 1,
                         "from": "x", "subject": "s"}], unread_ids=set())
    reader = _reader({"t": [_msg("them@partner.org", "Wed, 17 Jun 2026 12:00:00 +0000")]})
    assert dangling_threads(_identity(), now=NOW, runner=runner,
                            reader=reader)[0]["disposition"] == "handled"


def test_owed_replies_still_resolves_for_anything_that_imported_it():
    assert owed_replies is dangling_threads


def test_turn_md_bands_on_handled_ness_rather_than_age():
    text = TURN.read_text().lower()
    window = text[text.index("canopy email dangling"):][:5000]
    assert "unread" in window and "read" in window
    assert "handled" in window
    assert "proxy" in window, "turn.md must admit read-state is a proxy and name its limits"
