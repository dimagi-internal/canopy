"""The fleet's inbound-email table: every message an agent mailbox receives lands in
exactly ONE row, and every row either WAKES the agent or ARCHIVES the mail under a
Gmail label named after the row (canopy#679, decided by Jonathan 2026-09-24).

This file is the only place the rules live. Two layers act on it:

* **Gmail filters** (canopy ``inbox_filters.apply_filters``): the rows whose match Gmail
  search can express carry ``gmail`` queries. Each becomes a filter that archives, marks
  read and adds the row's label, so the mail never reaches ``in:inbox is:unread`` and no
  turn is ever created for it.
* **The runner** (canopy-web ``runner/canopy_runner/canopy_runner/inbox_rules.py``, a
  VERBATIM copy of this file): for every unread thread it calls ``classify()`` on the
  newest message, headers and body in hand. An archive row is archived + labelled there
  too, which is how the rows Gmail cannot express (headers, Doc-comment bodies) act.

So "which rule did this, and why?" has one answer wherever it acted: the row name is on
the thread as a label, and ``canopy email why <thread>`` re-runs ``classify()`` and prints
the evidence.

**Stdlib only, no canopy imports** — the runner cannot import canopy, so it vendors this
file byte-for-byte. Editing it means re-vendoring: ``tests/test_inbox_rules.py`` pins the
file's hash and says how.

**Fail open.** A row matches only on a POSITIVE marker. Anything no row recognises is
``person`` and wakes the agent, and ``classify()`` turns its own exceptions into
``person`` too. Silently archiving a message a human wrote is the one failure this table
must never produce; a wasted turn is the cheap direction.

**Order matters** — first match wins. The wanted rows sit first so a CloudWatch alarm is
never read as no-reply mail and a Doc comment addressed to the agent is never read as an
ignorable one. Gmail applies every matching filter regardless of order, which is why the
broad ``gmail`` queries below carry explicit exclusions for the wanted rows' senders.
"""
from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass, field
from email.utils import getaddresses
from typing import Callable

WAKE = "wake"
ARCHIVE = "archive"

#: The default row. Not in ``TABLE``: it is what "no row matched" is called.
PERSON = "person"


@dataclass(frozen=True)
class Message:
    """The one message being classified — the NEWEST in its thread, since that is what
    would start the turn. ``headers`` keys are lowercased, first value wins."""

    headers: dict = field(default_factory=dict)
    body: str = ""
    labels: frozenset = frozenset()
    #: The agent's own address, when known. Only used to recognise an @-mention of it.
    mailbox: str = ""

    @property
    def from_(self) -> str:
        return self.headers.get("from", "")

    @property
    def subject(self) -> str:
        return self.headers.get("subject", "")

    @property
    def address(self) -> str:
        """The bare ``From:`` address, lowercased."""
        pairs = getaddresses([self.from_])
        return (pairs[0][1] if pairs else "").strip().lower()


@dataclass(frozen=True)
class Verdict:
    row: str
    bucket: str
    #: The header, address or phrase that matched — what ``canopy email why`` prints.
    evidence: str

    @property
    def label(self) -> str | None:
        """The Gmail label an archive row files the thread under; wake rows add none."""
        return self.row if self.bucket == ARCHIVE else None


@dataclass(frozen=True)
class Row:
    name: str
    bucket: str
    summary: str
    #: ``match(msg) -> evidence`` or ``None``. Must only return evidence on a POSITIVE
    #: marker (see the module docstring on failing open).
    match: Callable[[Message], "str | None"]
    #: Gmail search queries that express this row, one filter each. Empty for rows only
    #: the runner can see (headers, bodies). A query that is a strict SUBSET of the row is
    #: fine — the runner catches the rest; a query WIDER than the row is not.
    gmail: tuple = ()
    #: Queries of filters this row replaces, deleted by ``apply_filters``.
    supersedes: tuple = ()


def message_from_gmail(msg: dict, mailbox: str = "") -> Message:
    """Build a ``Message`` from one raw Gmail API message (``gog gmail thread get --json``)."""
    headers: dict = {}
    payload = msg.get("payload") or {}
    for h in payload.get("headers") or []:
        headers.setdefault((h.get("name") or "").lower(), h.get("value") or "")
    return Message(headers=headers, body=_plain_body(payload),
                   labels=frozenset(msg.get("labelIds") or ()), mailbox=mailbox.lower())


def _plain_body(payload: dict) -> str:
    """Every text/plain part, base64url-decoded; ``""`` when there is none."""
    out: list[str] = []

    def walk(part: dict) -> None:
        mime = part.get("mimeType") or ""
        data = (part.get("body") or {}).get("data")
        if data and (mime.startswith("text/plain") or (part is payload and not mime)):
            try:
                out.append(base64.urlsafe_b64decode(data + "==").decode("utf-8", "replace"))
            except (binascii.Error, ValueError):
                pass
        for child in part.get("parts") or ():
            walk(child)

    walk(payload)
    return "\n".join(out)


def _flat(text: str) -> str:
    """Lowercased, whitespace collapsed — Google hard-wraps notification bodies mid-phrase."""
    return re.sub(r"\s+", " ", text or "").lower()


def _words(pattern_words: list[str]) -> re.Pattern:
    """A case-insensitive, word-bounded alternation — Gmail's `subject:(a OR "b c")`."""
    return re.compile(r"\b(?:" + "|".join(re.escape(w) for w in pattern_words) + r")\b", re.I)


# ── wanted/cloudwatch-alarm ────────────────────────────────────────────────────────────
# CloudWatch alarms arrive as `Labs Alerts <no-reply@sns.amazonaws.com>`, and email is
# Hal's only alarm channel — the mail with the shortest useful life in the whole inbox.
# The subject shape is the runner's `alarm_key()`; the runner still coalesces a storm of
# them into one turn per incident after this row says "wake".
_ALARM_SUBJECT = re.compile(r'^\s*(ALARM|OK):\s*"([^"]+)"')


def _cloudwatch_alarm(m: Message) -> str | None:
    # The exact domain, not a suffix: `evilsns.amazonaws.com` must not pass for SNS.
    if m.address.rpartition("@")[2] != "sns.amazonaws.com":
        return None
    hit = _ALARM_SUBJECT.match(m.subject)
    return f'{m.address}, subject {hit.group(1)}: "{hit.group(2)}"' if hit else None


# ── the Google Docs rows ───────────────────────────────────────────────────────────────
# Doc, Sheet and Slides comment mail all comes from this one address, with none of the
# RFC automation headers. What it DOES carry is Google's own statement of why you got it,
# in the lead line and the footer. Measured on every such message in the fleet mailboxes
# (2026-09-24: ace 1 thread, echo 13, eva 18, ada/hal 0):
#   "... because you are mentioned in this thread by X"   — an @-mention of the agent
#   "... because you are a participant in this thread"    — a reply to the agent's
#                                                           comment or suggestion
#   "X mentioned you in a comment" / "X assigned you an action item" / "_Assigned to you_"
# Those are the wanted markers. The owner's default subscription reads "because you are
# subscribed to all discussions", and that is NOT a marker either way: on an agent's own
# draft it carries "dont send - she is not in seattle" and "this section reads as
# obviously AI" — feedback to the agent with no @ in it. So `ignored/doc-comment` needs
# its own positive marker that the comment is for somebody else, and a digest ("New
# activity in the following document", several items at once) is never archived.
_DOCS_COMMENTS = "comments-noreply@docs.google.com"
_ADDRESSED = [
    "you are mentioned in this thread",
    "you are a participant in this thread",
    " mentioned you in ",
    " assigned you ",
    "_assigned to you_",
    " replied to your comment",
]
#: Lead-line events that report a change, not a question: nobody is waiting on the agent.
_DOC_FYI = re.compile(r"\b(?:accepted a suggestion|rejected a suggestion|resolved a comment"
                      r"|marked an action item as done)\b")
#: An action item handed to a NAMED person — "assigned you" is caught as wanted first.
_ASSIGNED_OTHER = re.compile(r"\bassigned (?!you\b)[^.\n]{1,80}? an action item\b|_assigned to (?!you_)[^_\n]{1,80}_")
_MENTION = re.compile(r"@([\w.+-]+@[\w-]+(?:\.[\w-]+)+)")


def _doc_lead(m: Message) -> str:
    for line in (m.body or "").splitlines():
        if line.strip():
            return _flat(line).strip()
    return ""


def _doc_interaction(m: Message) -> str | None:
    if m.address != _DOCS_COMMENTS:
        return None
    flat = _flat(m.body)
    for marker in _ADDRESSED:
        if marker in flat:
            return f'{_DOCS_COMMENTS}, body says "{marker.strip()}"'
    if m.mailbox and f"@{m.mailbox}" in flat:
        return f"{_DOCS_COMMENTS}, body @-mentions {m.mailbox}"
    return None


def _doc_comment_for_someone_else(m: Message) -> str | None:
    # Only reached when `_doc_interaction` found no addressed marker (rows run in order).
    if m.address != _DOCS_COMMENTS:
        return None
    lead = _doc_lead(m)
    if not lead or lead.startswith("new activity"):
        return None  # a digest, or a body we cannot read: fail open
    fyi = _DOC_FYI.search(lead)
    if fyi:
        return f'{_DOCS_COMMENTS}, an FYI event: "{fyi.group(0)}"'
    flat = _flat(m.body)
    other = _ASSIGNED_OTHER.search(flat)
    if other:
        return f'{_DOCS_COMMENTS}, action item for someone else: "{other.group(0)}"'
    mentioned = sorted({a.lower() for a in _MENTION.findall(m.body or "")})
    if mentioned and m.mailbox not in mentioned:
        return f"{_DOCS_COMMENTS}, addressed to {', '.join(mentioned)} and not the agent"
    return None


# ── ignored/out-of-office ──────────────────────────────────────────────────────────────
# Two Gmail queries, kept verbatim from the rules they replace (auto-reply-ooo and
# auto-reply-ooo-body, whose histories are in canopy git log): the subject list is an
# enumeration that keeps getting outrun, so the second query ANDs a broad subject word
# with a first-person availability phrase in the body. Verified then against eva@'s full
# history: 10/10 matches carried Auto-Submitted: auto-replied, zero false positives. The
# runner adds the header itself, which Gmail filters cannot read.
_OOO_SUBJECTS = ["out of office", "automatic reply", "auto-reply", "autoreply",
                 "away from my email", "away from the office", "offline through",
                 "offline until"]
_OOO_BODY_SUBJECTS = ["offline", "ooo", "out of office", "on leave", "annual leave",
                      "vacation", "holiday", "away", "responsive", "traveling", "travelling",
                      "offsite", "through", "until"]
_OOO_BODY_PHRASES = ["i will be offline", "i am offline", "i will be out of the office",
                     "i am out of the office", "i am currently out of the office",
                     "i will be on leave", "i am on leave", "i am on vacation",
                     "i will be on vacation", "limited access to email",
                     "limited email access", "intermittent access",
                     "checking email intermittently", "check email intermittently",
                     "slower to respond", "slow to respond", "delayed response",
                     "away from my email", "away from the office"]


def _gmail_or(words: list[str]) -> str:
    return " OR ".join(f'"{w}"' if " " in w or "-" in w else w for w in words)


_OOO_Q1 = f"subject:({_gmail_or(_OOO_SUBJECTS)})"
_OOO_Q2 = (f"subject:({_gmail_or(_OOO_BODY_SUBJECTS)}) "
           f"({' OR '.join(chr(34) + p + chr(34) for p in _OOO_BODY_PHRASES)})")
_OOO_SUBJ_RE = _words(_OOO_SUBJECTS)
_OOO_BODY_SUBJ_RE = _words(_OOO_BODY_SUBJECTS)


def _out_of_office(m: Message) -> str | None:
    auto = m.headers.get("auto-submitted", "").strip().lower()
    if auto.startswith("auto-replied"):
        return f"Auto-Submitted: {auto}"
    if m.headers.get("precedence", "").strip().lower() == "auto_reply":
        return "Precedence: auto_reply"
    hit = _OOO_SUBJ_RE.search(m.subject)
    if hit:
        return f'subject contains "{hit.group(0)}"'
    if _OOO_BODY_SUBJ_RE.search(m.subject):
        flat = _flat(m.body)
        for p in _OOO_BODY_PHRASES:
            if p in flat:
                return f'subject marker and body says "{p}"'
    return None


# ── sender rows ────────────────────────────────────────────────────────────────────────
def _from_any(*addresses: str) -> Callable[[Message], "str | None"]:
    wanted = {a.lower() for a in addresses}
    return lambda m: m.address if m.address in wanted else None


_DRIVE_SHARES = ("drive-shares-dm-noreply@google.com", "drive-shares-noreply@google.com")

# Calendar: a share invite's From: is SPOOFED to the human sharer and only Sender: says
# Google, so a From:-only rule would either miss it or archive that person's real mail.
_CALENDAR_SENDERS = ("calendar-notification@google.com", "calendar-noreply@google.com")
_CALENDAR_SHARE_SUBJECTS = ["invitation to join shared calendar",
                            "invitation to view shared calendar"]


def _calendar(m: Message) -> str | None:
    sender = (getaddresses([m.headers.get("sender", "")]) or [("", "")])[0][1].lower()
    if sender in _CALENDAR_SENDERS:
        return f"Sender: {sender}"
    if m.address in _CALENDAR_SENDERS:
        return m.address
    s = m.subject.lower()
    for phrase in _CALENDAR_SHARE_SUBJECTS:
        if phrase in s:
            return f'subject contains "{phrase}"'
    return None


# ── ignored/other-auto-header ──────────────────────────────────────────────────────────
# RFC 3834 and friends, which Gmail filters cannot read at all (canopy#653). The same
# signals the runner's `is_automated` read before this table existed.
def _auto_header(m: Message) -> str | None:
    auto = m.headers.get("auto-submitted", "").strip().lower()
    if auto and auto != "no":
        return f"Auto-Submitted: {auto}"
    prec = m.headers.get("precedence", "").strip().lower()
    if prec in {"bulk", "list", "junk", "auto_reply"}:
        return f"Precedence: {prec}"
    xar = m.headers.get("x-autoreply", "").strip().lower()
    if xar and xar != "no":
        return f"X-Autoreply: {xar}"
    return None


# ── ignored/no-reply-sender ────────────────────────────────────────────────────────────
# A no-reply WORD anywhere in the address, delimited like Gmail tokenises it: Gmail's
# `from:noreply` matches `comments-noreply@docs.google.com` and
# `drive-shares-dm-noreply@google.com` too (measured 2026-09-24: every Doc comment and
# Drive share in echo@ and eva@ had been archived by the old `automated-noreply` filter
# for exactly this reason). Mirroring Gmail's reading keeps the two layers from
# disagreeing; the Docs/Drive/SNS senders are matched by earlier rows, and the Gmail
# query excludes them explicitly because Gmail does not stop at the first filter.
_NOREPLY_WORD = re.compile(
    r"(?:^|[<\s\"._+-])(noreply|no-reply|donotreply|do-not-reply|mailer-daemon|postmaster)"
    r"(?=[@._+-])", re.I)


def _no_reply(m: Message) -> str | None:
    addr = m.address
    # A Doc comment neither Docs row could place is unclassified, and unclassified wakes:
    # never let the no-reply word in its address archive it by the back door.
    if not addr or addr == _DOCS_COMMENTS:
        return None
    return addr if _NOREPLY_WORD.search(addr) else None


def _marketing(m: Message) -> str | None:
    for cat in ("CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL"):
        if cat in m.labels:
            return f"Gmail category {cat.split('_', 1)[1].lower()}"
    return None


_NOREPLY_Q = ('from:(noreply OR no-reply OR donotreply OR "do-not-reply" OR mailer-daemon '
              'OR postmaster) -from:sns.amazonaws.com -from:docs.google.com '
              '-from:(drive-shares-dm-noreply@google.com OR drive-shares-noreply@google.com) '
              '-from:(calendar-notification@google.com OR calendar-noreply@google.com)')
_SES_RECEIPTS_Q = 'from:no-reply@sns.amazonaws.com subject:"Amazon SES Email Event Notification"'


TABLE: tuple = (
    Row("wanted/cloudwatch-alarm", WAKE,
        "a CloudWatch ALARM:/OK: notification from SNS", _cloudwatch_alarm),
    Row("wanted/doc-interaction", WAKE,
        "a Google Docs comment, reply or action item addressed to the agent", _doc_interaction),
    Row("ignored/out-of-office", ARCHIVE,
        "an out-of-office / auto-reply", _out_of_office,
        gmail=(_OOO_Q1, _OOO_Q2),
        supersedes=(
            'subject:(offline OR ooo OR "out of office" OR "on leave" OR "annual leave" '
            'OR vacation OR holiday OR away) '
            '("i will be offline" OR "i am offline" OR "i will be out of the office" '
            'OR "i am out of the office" OR "i am currently out of the office" '
            'OR "i will be on leave" OR "i am on leave" OR "i am on vacation" '
            'OR "i will be on vacation" OR "limited access to email" '
            'OR "away from my email" OR "away from the office")',
        )),
    Row("ignored/doc-comment", ARCHIVE,
        "a Google Docs event for someone else: an FYI, or a comment @-ing only others",
        _doc_comment_for_someone_else),
    Row("ignored/doc-share", ARCHIVE,
        "a Google Drive 'shared with you' notification", _from_any(*_DRIVE_SHARES),
        gmail=("from:(drive-shares-dm-noreply@google.com OR drive-shares-noreply@google.com)",)),
    Row("ignored/calendar", ARCHIVE,
        "a Google Calendar notification (invites, shares)", _calendar,
        gmail=('subject:("invitation to join shared calendar" OR '
               '"invitation to view shared calendar")',
               "from:(calendar-notification@google.com OR calendar-noreply@google.com)")),
    Row("ignored/github", ARCHIVE,
        "a GitHub notification (agents work GitHub through gh, never email)",
        _from_any("notifications@github.com"), gmail=("from:notifications@github.com",)),
    Row("ignored/connect-devops", ARCHIVE,
        "a Connect platform FYI ('This inbox is not monitored')",
        _from_any("connect-devops@dimagi.com"), gmail=("from:connect-devops@dimagi.com",)),
    Row("ignored/vendor", ARCHIVE,
        "Google Cloud marketing, Expensify",
        _from_any("googlecloud@google.com", "concierge@expensify.com",
                  "notifications@expensify.com"),
        gmail=("from:googlecloud@google.com",
               "from:(concierge@expensify.com OR notifications@expensify.com)")),
    Row("ignored/other-auto-header", ARCHIVE,
        "a machine-written message by its RFC headers", _auto_header),
    Row("ignored/no-reply-sender", ARCHIVE,
        "a no-reply sender (SES event receipts included)", _no_reply,
        gmail=(_NOREPLY_Q, _SES_RECEIPTS_Q),
        supersedes=(
            'from:(noreply OR no-reply OR donotreply OR "do-not-reply" OR mailer-daemon '
            'OR postmaster) -from:sns.amazonaws.com',
            'from:(noreply OR no-reply OR donotreply OR "do-not-reply" OR mailer-daemon '
            'OR postmaster)',
        )),
    Row("ignored/marketing", ARCHIVE,
        "Gmail's Promotions / Social categories", _marketing,
        gmail=("category:promotions", "category:social")),
)

ROWS = {r.name: r for r in TABLE}


def classify(msg: Message) -> Verdict:
    """The one row ``msg`` lands in. Never raises: a matcher error is ``person``."""
    try:
        for row in TABLE:
            evidence = row.match(msg)
            if evidence:
                return Verdict(row.name, row.bucket, evidence)
    except Exception as exc:  # noqa: BLE001 — fail open, the table must never drop mail
        return Verdict(PERSON, WAKE, f"classifier error ({type(exc).__name__}: {exc}) — waking")
    return Verdict(PERSON, WAKE, "no row matched")


def archive_labels() -> list[str]:
    """Every label an archive row can apply — what a mailbox must have before filters."""
    return [r.name for r in TABLE if r.bucket == ARCHIVE]
