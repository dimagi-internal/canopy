"""The fleet's inbound-email table: every message lands in exactly one row.

Doc-comment fixtures are synthetic but keep the exact phrasing Google uses (lead line,
footer), as measured across every Doc-comment message in the fleet mailboxes. Only a tag of
the agent wakes it; every other Docs notification is archived.
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
# Only a TAG wakes the agent: an @-mention of it, or an action item assigned to it. Every other notification from Google's comment address is archived.

@pytest.mark.parametrize("lead,footer", [
    ("Pat Reviewer (pat@example.org) mentioned you in a comment in the following\ndocument",
     "You have received this email because you are mentioned in this thread by\nPat Reviewer."),
    ("Pat Reviewer (pat@example.org) assigned you an action item in the following document",
     "You have received this email because you are mentioned in this thread by\nPat Reviewer."),
    # A later reply in a thread the agent was tagged in: Google still says why.
    ("Pat Reviewer replied to a comment in the following document",
     "You have received this email because you are mentioned in this thread by\nPat Reviewer."),
])
def test_doc_comments_that_tag_the_agent_wake(lead, footer):
    v = R.classify(doc(lead, "Pat Reviewer\n| quoted\nplease update the numbers", footer))
    assert v.row == "wanted/doc-interaction" and v.bucket == "wake"


def test_an_at_mention_of_the_agent_wakes_even_without_googles_phrasing():
    m = doc("Pat Reviewer added a comment to the following document",
            f"Pat Reviewer\n@{AGENT} can you check this?", SUBSCRIBED)
    assert row(m) == "wanted/doc-interaction"


@pytest.mark.parametrize("lead,content,footer", [
    ("Pat Reviewer accepted a suggestion in the following document", "Add: “a”\n_Accepted suggestion_", SUBSCRIBED),
    ("Pat Reviewer resolved a comment in the following document", "tone\n_Marked as resolved_", SUBSCRIBED),
    ("Pat Reviewer marked an action item as done in the following document", "ship it", SUBSCRIBED),
    ("Pat Reviewer assigned Sam Other an action item in the following document",
     "@sam@example.org is this still on?\n_Assigned to Sam Other_", SUBSCRIBED),
    ("Pat Reviewer added a comment to the following document",
     "@sam@example.org @kim@example.org could you send this to your contact?", SUBSCRIBED),
    # The owner's default subscription on the agent's own Doc, tagging nobody (eva@:
    # "what does this mean?", "check if eha clinic same as eha?").
    ("Pat Reviewer added a comment to the following document", "dont send - she is not in town", SUBSCRIBED),
    ("Pat Reviewer added a suggestion to the following document", "Delete paragraph", SUBSCRIBED),
    ("New activity in the following document", "@sam@example.org resolved\n_Accepted suggestion_", SUBSCRIBED),
    # A reply to a thread the agent only took part in is not a tag.
    ("Pat Reviewer replied to a comment in the following document", "I didnt quite get this",
     "You have received this email because you are a participant in this thread."),
])
def test_doc_notifications_that_do_not_tag_the_agent_are_archived(lead, content, footer):
    v = R.classify(doc(lead, content, footer))
    assert v.row == "ignored/doc-comment" and v.label == "ignored/doc-comment"


def test_an_unreadable_doc_comment_is_archived_as_a_doc_comment_not_as_no_reply():
    assert row(msg(frm=DOCS, body="")) == "ignored/doc-comment"


# --- the Doc a tagged comment is on --------------------------------------------------------
# The layout of a real notification (eva@ thread 1a20810ba581b0a9), text/plain part.
FID = "1QDxfhS_gWTYbu31_vhT6gdFxin1WL3XmX8q_B-_QAhE"
REAL = (
    "Shayoni Mazumdar added a comment to the following document\r\n"
    "GDHF outreach drafts \u2014 Shayoni's 23 Nov list  \r\n"
    f"(https://docs.google.com/document/d/{FID}/edit?disco=doco.I7zRfRBEMbUpGaSUsMVuLQ&usp=comment_email_document"
    "&ts=6b041cdb&usp_dm=false&tab=t.plx9zoff0any)  \r\n[Shared externally]\r\n\r\n1 comment\r\n\r\n.\r\n"
    "Shayoni Mazumdar\r\n| 'talked to jackie most recently';\r\n{content}\r\n\r\nOpen  \r\n"
    f"(https://docs.google.com/document/d/{FID}/edit?disco=doco.I7zRfRBEMbUpGaSUsMVuLQ&usp=comment_email_discussion"
    "&ts=6b041cdb&usp_dm=false)\r\n\r\nGoogle LLC, 1600 Amphitheatre Parkway, Mountain View, CA 94043, USA\r\n\r\n"
    "You have received this email because you are subscribed to all discussions  \r\n"
    "on GDHF outreach drafts \u2014 Shayoni's 23 Nov list  \r\n"
    f"(https://docs.google.com/document/d/{FID}/edit?disco=doco.I7zRfRBEMbUpGaSUsMVuLQ&usp=comment_email_document"
    "&ts=6b041cdb&usp_dm=false&tab=t.plx9zoff0any).  \r\nChange what Google sends you  \r\n"
    "(https://docs.google.com/document/u/101388914857441342761/docos/notify?ouid=101388914857441342761"
    f"&id={FID}&title=GDHF+outreach+drafts&resourcekey).  \r\n"
    "You can reply to this email to reply to the discussion.")
REAL_FROM = '"Shayoni Mazumdar (Google Docs)" <comments-noreply@docs.google.com>'


def test_the_doc_is_read_off_googles_footer_link():
    m = msg(frm=REAL_FROM, body=REAL.format(content=f"@{AGENT} what does this mean?"))
    assert row(m) == "wanted/doc-interaction"
    assert R.doc_comment_of(m) == {"file_id": FID, "kind": "document",
                                   "comment_hint": "doco.I7zRfRBEMbUpGaSUsMVuLQ",
                                   "commenter_name": "Shayoni Mazumdar"}


def test_a_link_pasted_into_the_comment_never_names_the_doc():
    other = "1OTHERotherOTHERotherOTHERotherOTHERother00"
    pasted = (f"@{AGENT} see https://docs.google.com/document/u/0/docos/notify?id={other} and "
              f"https://docs.google.com/document/d/{other}/edit?disco=AAAAevil")
    got = R.doc_comment_of(msg(frm=REAL_FROM, body=REAL.format(content=pasted)))
    assert got["file_id"] == FID and got["comment_hint"] == "doco.I7zRfRBEMbUpGaSUsMVuLQ"


@pytest.mark.parametrize("frm,body", [
    ("Jane Doe <jane@example.org>", REAL.format(content="hi")),     # not Google's address
    (REAL_FROM, "Shayoni Mazumdar added a comment\nno footer link"),  # no footer: no Doc
])
def test_no_doc_without_googles_address_and_footer(frm, body):
    assert R.doc_comment_of(msg(frm=frm, body=body)) is None


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
VENDORED_SHA256 = "b62bd1343ed04941edf33dee62acc97efdc93a117bf2aa645577f4e5e03702ce"


def test_vendored_copy_pin():
    digest = hashlib.sha256(Path(R.__file__).read_bytes()).hexdigest()
    assert digest == VENDORED_SHA256, (
        "inbox_rules.py changed: re-vendor it into canopy-web "
        "(runner/canopy_runner/canopy_runner/inbox_rules.py) and update BOTH pins to "
        f"{digest} — see the comment above VENDORED_SHA256.")



def test_a_lookalike_sns_domain_is_not_an_alarm():
    m = msg(frm="Alerts <no-reply@evilsns.amazonaws.com>", subject='ALARM: "x" in US East')
    assert row(m) != "wanted/cloudwatch-alarm"
