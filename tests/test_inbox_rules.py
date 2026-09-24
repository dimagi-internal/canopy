"""The fleet's inbound-email table (canopy#679): every message lands in exactly one row.

Doc-comment fixtures are synthetic but keep the exact phrasing Google uses (lead line,
footer), measured on every Doc-comment message in the fleet mailboxes on 2026-09-24:
52 messages → 5 wanted, 20 ignored, 27 unclassified (wake).
"""
import base64
import hashlib
from pathlib import Path

import pytest

from orchestrator import inbox_rules as R

AGENT = "eva@example-agents.com"


def msg(frm="Jane Doe <jane@example.org>", subject="hello", body="", labels=(), **headers):
    h = {"from": frm, "subject": subject}
    h.update({k.replace("_", "-"): v for k, v in headers.items()})
    return R.Message(headers=h, body=body, labels=frozenset(labels), mailbox=AGENT)


def row(m):
    return R.classify(m).row


DOCS = '"Pat Reviewer (Google Docs)" <comments-noreply@docs.google.com>'


def doc(lead, content, footer):
    return msg(frm=DOCS, subject="Draft plan... - a comment",
               body=f"{lead}\nDraft plan (https://docs.google.com/document/d/x)\n1 comment\n.\n"
                    f"{content}\nOpen (https://docs.google.com/x)\n"
                    f"Google LLC, 1600 Amphitheatre Parkway\n{footer}\n"
                    "You can reply to this email to reply to the discussion.")


SUBSCRIBED = ("You have received this email because you are subscribed to all discussions\n"
              "on Draft plan (https://docs.google.com/document/d/x).")


# --- person: the fail-open default ---------------------------------------------------------

def test_a_person_wakes():
    v = R.classify(msg())
    assert (v.row, v.bucket, v.label) == ("person", "wake", None)


def test_a_matcher_error_wakes():
    class Boom(R.Message):
        @property
        def address(self):
            raise RuntimeError("bad header")
    v = R.classify(Boom(headers={"from": "x"}))
    assert v.row == "person" and v.bucket == "wake" and "classifier error" in v.evidence


# --- wanted/cloudwatch-alarm ---------------------------------------------------------------

@pytest.mark.parametrize("subject", ['ALARM: "labs-web-cpu-high" in US East (N. Virginia)',
                                     'OK: "labs-web-cpu-high" in US East (N. Virginia)'])
def test_cloudwatch_alarms_wake_despite_a_no_reply_sender(subject):
    m = msg(frm="Labs Alerts <no-reply@sns.amazonaws.com>", subject=subject)
    assert row(m) == "wanted/cloudwatch-alarm"


def test_ses_receipts_from_the_same_sender_are_archived():
    m = msg(frm="Labs email events <no-reply@sns.amazonaws.com>",
            subject="Amazon SES Email Event Notification")
    v = R.classify(m)
    assert v.row == "ignored/no-reply-sender" and v.label == "ignored/no-reply-sender"


def test_a_human_writing_alarm_shaped_subject_is_a_person():
    assert row(msg(subject='OK: "the deploy" in staging')) == "person"


# --- the Docs rows -------------------------------------------------------------------------

@pytest.mark.parametrize("lead,footer", [
    ("Pat Reviewer (pat@example.org) mentioned you in a comment in the following\ndocument",
     "You have received this email because you are mentioned in this thread by\nPat Reviewer."),
    ("Pat Reviewer (pat@example.org) assigned you an action item in the following document",
     "You have received this email because you are mentioned in this thread by\nPat Reviewer."),
    ("Pat Reviewer replied to a comment in the following document",
     "You have received this email because you are a participant in this thread."),
])
def test_doc_interactions_addressed_to_the_agent_wake(lead, footer):
    v = R.classify(doc(lead, "Pat Reviewer\n| quoted\nplease update the numbers", footer))
    assert v.row == "wanted/doc-interaction" and v.bucket == "wake"


def test_an_at_mention_of_the_agent_wakes_even_without_googles_phrasing():
    m = doc("Pat Reviewer added a comment to the following document",
            f"Pat Reviewer\n@{AGENT} can you check this?", SUBSCRIBED)
    assert row(m) == "wanted/doc-interaction"


@pytest.mark.parametrize("lead,content", [
    ("Pat Reviewer accepted a suggestion in the following document", "Add: “a”\n_Accepted suggestion_"),
    ("Pat Reviewer resolved a comment in the following document", "tone\n_Marked as resolved_"),
    ("Pat Reviewer marked an action item as done in the following document", "ship it"),
    ("Pat Reviewer assigned Sam Other an action item in the following document",
     "@sam@example.org is this still on?\n_Assigned to Sam Other_"),
    ("Pat Reviewer added a comment to the following document",
     "@sam@example.org @kim@example.org could you send this to your contact?"),
])
def test_doc_events_for_someone_else_are_archived(lead, content):
    v = R.classify(doc(lead, content, SUBSCRIBED))
    assert v.row == "ignored/doc-comment" and v.label == "ignored/doc-comment"


@pytest.mark.parametrize("lead,content", [
    # Feedback on the agent's own draft, @-ing nobody — measured instances say
    # "dont send - she is not in seattle" and "this section reads as obviously AI".
    ("Pat Reviewer added a comment to the following document", "dont send - she is not in town"),
    ("Pat Reviewer replied to a comment in the following document", "I didnt quite get this"),
    ("Pat Reviewer added a suggestion to the following document", "Delete paragraph"),
    ("New activity in the following document", "@sam@example.org resolved\n_Accepted suggestion_"),
])
def test_doc_comments_that_cannot_be_placed_wake(lead, content):
    """No positive marker that the comment is for someone else → never archive it."""
    v = R.classify(doc(lead, content, SUBSCRIBED))
    assert v.row == "person" and v.bucket == "wake"


def test_an_unreadable_doc_comment_wakes_rather_than_hitting_the_no_reply_row():
    assert row(msg(frm=DOCS, body="")) == "person"


def test_drive_shares_are_archived():
    m = msg(frm='"Pat (via Google Sheets)" <drive-shares-dm-noreply@google.com>',
            subject='Spreadsheet shared with you: "Budget"')
    assert row(m) == "ignored/doc-share"


# --- header and sender rows ----------------------------------------------------------------

def test_out_of_office_by_header_subject_and_body():
    assert row(msg(auto_submitted="auto-replied")) == "ignored/out-of-office"
    assert row(msg(subject="Automatic reply: hello")) == "ignored/out-of-office"
    assert row(msg(subject="less responsive through Sept 25",
                   body="I am checking email intermittently this week.")) == "ignored/out-of-office"


def test_a_subject_word_alone_is_not_out_of_office():
    """`offline` without the body half would archive "let's take this offline"."""
    assert row(msg(subject="let's take this offline", body="call me?")) == "person"


def test_other_auto_headers_are_archived():
    assert row(msg(auto_submitted="auto-generated")) == "ignored/other-auto-header"
    assert row(msg(precedence="bulk")) == "ignored/other-auto-header"
    assert row(msg(x_autoreply="yes")) == "ignored/other-auto-header"
    assert row(msg(auto_submitted="no")) == "person"


def test_calendar_by_sender_even_when_from_is_the_human():
    m = msg(frm="Pat <pat@example.org>", subject="Invitation: sync @ Tue",
            sender="Google Calendar <calendar-notification@google.com>")
    assert row(m) == "ignored/calendar"


@pytest.mark.parametrize("frm,expected", [
    ("GitHub <notifications@github.com>", "ignored/github"),
    ("Connect <connect-devops@dimagi.com>", "ignored/connect-devops"),
    ("Google Cloud <googlecloud@google.com>", "ignored/vendor"),
    ("Expensify <concierge@expensify.com>", "ignored/vendor"),
    ("Labs <noreply@labs.example.org>", "ignored/no-reply-sender"),
    ("Google Cloud <CloudPlatform-noreply@google.com>", "ignored/no-reply-sender"),
    ("MAILER-DAEMON@example.org", "ignored/no-reply-sender"),
    ("Noreen Replyson <noreen@example.org>", "person"),
])
def test_sender_rows(frm, expected):
    assert row(msg(frm=frm)) == expected


def test_marketing_categories():
    assert row(msg(labels=["CATEGORY_PROMOTIONS"])) == "ignored/marketing"
    assert row(msg(labels=["CATEGORY_UPDATES"])) == "person"


# --- the table's shape ---------------------------------------------------------------------

def test_rows_are_unique_wanted_first_and_every_archive_row_is_labelled():
    names = [r.name for r in R.TABLE]
    assert len(names) == len(set(names))
    buckets = [r.bucket for r in R.TABLE]
    assert buckets == sorted(buckets, key=lambda b: b != R.WAKE), "wake rows must match first"
    for r in R.TABLE:
        assert r.name.startswith("wanted/" if r.bucket == R.WAKE else "ignored/")
        assert not (r.bucket == R.WAKE and r.gmail), "a wake row never becomes a Gmail filter"
    assert R.archive_labels() == [r.name for r in R.TABLE if r.bucket == R.ARCHIVE]


def test_message_from_gmail_decodes_the_newest_message():
    body = base64.urlsafe_b64encode(b"Pat mentioned you in a comment").decode().rstrip("=")
    raw = {"labelIds": ["INBOX", "UNREAD"],
           "payload": {"mimeType": "multipart/alternative",
                       "headers": [{"name": "From", "value": DOCS},
                                   {"name": "Subject", "value": "Doc - hi"}],
                       "parts": [{"mimeType": "text/plain", "body": {"data": body}},
                                 {"mimeType": "text/html", "body": {"data": "PGI-"}}]}}
    m = R.message_from_gmail(raw, "EVA@example-agents.com")
    assert m.address == "comments-noreply@docs.google.com"
    assert m.body == "Pat mentioned you in a comment"
    assert m.mailbox == "eva@example-agents.com" and "INBOX" in m.labels
    assert row(m) == "wanted/doc-interaction"


def test_module_is_stdlib_only_so_the_runner_can_vendor_it():
    src = Path(R.__file__).read_text()
    assert "from orchestrator" not in src and "import orchestrator" not in src


# --- vendoring -----------------------------------------------------------------------------

#: sha256 of src/orchestrator/inbox_rules.py. canopy-web vendors this file VERBATIM as
#: runner/canopy_runner/canopy_runner/inbox_rules.py and pins the same hash in its
#: tests/test_inbox_rules_vendored.py. Changed the table? Then:
#:   1. update this hash;
#:   2. copy the file into canopy-web at that path and update its pin to the same value;
#:   3. ship both. Until canopy-web ships, the runner classifies with the old table.
VENDORED_SHA256 = "69a23cb125fd4b7b882248e40d2ce5d4a0477a72dc8facdb4f94897ff90b8b9a"


def test_vendored_copy_pin():
    digest = hashlib.sha256(Path(R.__file__).read_bytes()).hexdigest()
    assert digest == VENDORED_SHA256, (
        "inbox_rules.py changed: re-vendor it into canopy-web "
        "(runner/canopy_runner/canopy_runner/inbox_rules.py) and update BOTH pins to "
        f"{digest} — see the comment above VENDORED_SHA256.")



def test_a_lookalike_sns_domain_is_not_an_alarm():
    m = msg(frm="Alerts <no-reply@evilsns.amazonaws.com>", subject='ALARM: "x" in US East')
    assert row(m) != "wanted/cloudwatch-alarm"
