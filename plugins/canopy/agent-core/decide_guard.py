#!/usr/bin/env python3
"""The fleet's decide-don't-poll rail — ONE implementation, all agents.

Each agent's `hooks/decide_guard.py` is a thin loader that execs this file out of
the INSTALLED canopy plugin, exactly like `agent-core/gating_guard.py`. Nothing
here is agent-specific, so a calibration fix reaches the fleet through
`/canopy:update` instead of N pull requests.

## What it enforces

An agent must not end a session by handing back a call it was equipped to make.
Every agent in the fleet already carries this rule in prose — the turn close-out
says *"DECIDE, don't poll… a clear call handed back as a question is work you
didn't finish"* — and prose is exactly what CLAUDE.md says does not survive load.

## Why prose was not enough, measured

The rule had TWO homes in hal and neither fired. The close-out checklist that
carries it is behind `turn_close_guard.py`, whose property #1 is that it engages
ONLY on turn entry points — ad-hoc sessions "must never be nagged". That is right
for the close-out CEREMONY (workspace-refresh, skill self-check) and wrong for
this one rule, which applies to every session an agent has. Net effect: the rule
with the most evidence behind it had zero enforcement on the sessions where the
human actually talks to the agent directly.

Measured 2026-08-27, hal, an ad-hoc "can you debug" request: hal diagnosed a
canopy-web bug to the exact line, then ended with *"Want me to ship the fix as a
PR?"*; after shipping and merging it, ended AGAIN with *"Want me to kick the
deploy?"*. Both were unambiguous, both were dev actions that ship without
approval, and the fix sat inert on `main` in between. Jonathan: *"stop asking me
when you know how to get to the right good endstate."* Same root as 2026-08-13
(*"Why are you asking me? just do the right thing if its clear?"*) and 2026-08-14.

## Shape

Deliberately NARROW where `turn_close_guard` is deliberately SCOPED: it engages on
any session, but only on one shape — a final message that CLOSES by OFFERING to do
work. It does not judge whether the ask was legitimate; it cannot. It blocks once
and makes the agent answer the question the rule already poses: whose call is this
actually?

Three properties, the same three its sibling has and for the same reasons:

1. **One shape only.** A closing offer-to-act, and only one the agent has NOT
   already answered. A genuine fork stated in prose ("I'd close it rather than
   merge — ok?") is the SANCTIONED form and does not match. Neither does an
   outbound gate — sending, replying, publishing, posting, sharing — carved out
   because putting something in front of a person always waits. Nor does an
   offer that carries its own answer: a stated rationale ("yours to authorize")
   or a stated default ("default is next turn"). Those ARE the call, made.
2. **Block at most once per session.** Worst case is one extra beat on a
   legitimate ask, never a wedged session.
3. **Fail open, always.** Every unexpected condition exits 0.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# Per-agent state, so two agents on one box cannot consume each other's single
# block. `CANOPY_AGENT_HOME` is set by the loader from its own repo; the fallback
# keeps this file runnable standalone (tests, a manual invocation).
STATE_DIR = Path(
    os.environ.get("CANOPY_AGENT_HOME") or os.path.expanduser("~/.canopy")
).expanduser() / "decide-guard"

# How much of the tail to consider. The rule is about how a message CLOSES, not
# what it mentions in passing: "I considered asking whether you wanted X" halfway
# through a report is not handing the call back. 600 chars is roughly the last
# paragraph or two.
TAIL_CHARS = 600

# Closing offers to act. Each is a way of saying "I know what to do and I am
# going to make you say it" — the exact move the memory forbids.
#
# What is deliberately NOT here matters as much. A bare "ok?" or "sound good?"
# appended to a stated recommendation is the SANCTIONED inline-prose ask from
# the fleet's "recommend and act, ask inline when it is a real fork" rule;
# matching it would train the agent to ignore this
# rail, which is worse than the gap. So is a question about Jonathan's taste,
# priorities, or risk appetite — those have no fixed grammar and belong to the
# judgment this rail prompts rather than to its matcher.
_OFFER_RE = re.compile(
    r"(?:"
    r"want\s+me\s+to"
    r"|shall\s+i\b"
    r"|should\s+i\b"
    r"|would\s+you\s+like\s+me\s+to"
    r"|do\s+you\s+want\s+me\s+to"
    r"|let\s+me\s+know\s+if\s+you(?:'d|\s+would)?\s+(?:like|want)"
    r"|say\s+the\s+word"
    r"|if\s+you(?:'d|\s+would)?\s+(?:like|want)\s+me\s+to"
    r"|happy\s+to\s+.{0,40}\bif\s+you"
    r")",
    re.IGNORECASE | re.DOTALL,
)

# The carve-out: OUTBOUND. Putting something in front of a person — or on a
# surface other people read — is the one class of action that ALWAYS waits for a
# human. It is every agent's CLAUDE.md § hard guardrail, verbatim: "every outbound
# action (sending on a channel, public writes) requires explicit human approval."
# An agent asking here is working CORRECTLY, and a rail that pushes it to act
# anyway is worse than no rail at all — it argues against a hard guardrail, citing
# § Shipping, which relaxes code review and nothing else.
#
# WIDENED 2026-08-27, before the fleet spread. This shipped as an EMAIL-only
# carve-out keyed to the mail gate's machinery (a `bin/<slug>-email` shim, a named
# draft, an address) — right for a code agent, wrong for the rest of the fleet.
# Measured against the engine as shipped, every non-mail outbound gate blocked:
# "want me to publish this digest to the board?" (ada), "should I share it with
# Shayoni and Natalia?" (eva), "want me to publish it to the site?" (echo). Hal's
# mail was carved out because hal's outbound IS mail; ada's is the board.
#
# Matched by SHAPE, not by a list of surfaces — a surface list trails the tool
# surface the same way `gating-baseline.json` documents its verb list doing. The
# shape is a DELIVERY VERB standing as the offer's OBJECT: "want me to <deliver>".
#
# Scope is the load-bearing half. Tested against the whole 600-char tail, a bare
# "sent" turns "I sent the PR through CI and it passed. Want me to merge it?" into
# a carve-out and the rail goes quiet on a true positive. So this is read ONLY in
# the window the offer opens — from the end of "want me to" to the end of that
# sentence — which is exactly where the offer's object lives.
_OUTBOUND_OBJECT_RE = re.compile(
    r"\b(?:send|sends|sending|sent"
    r"|repl(?:y|ies|ying)|respond(?:s|ing)?"
    r"|forward(?:s|ing)?|email(?:s|ing)?"
    r"|publish(?:es|ing)?|post(?:s|ing)?|shar(?:e|es|ing)"
    r"|announc(?:e|es|ing)|notif(?:y|ies|ying)|submit(?:s|ting)?)\b",
    re.IGNORECASE,
)

# How far past the offer to look for its object. Bounded so a tail with no "?" —
# "Say the word and I'll send it." — still terminates somewhere sensible.
OBJECT_CHARS = 200

# The mail gate's own machinery, unchanged from the shipped version. Specific
# enough to read over the whole tail without over-carving: a `bin/<slug>-email`
# shim, a named draft, an address, a reply-all. These usually sit in the sentence
# BEFORE the offer ("Draft is ready via bin/hal-email. Should I?"), which is why
# they keep the wider scope while the verbs above do not.
_OUTBOUND_CONTEXT_RE = re.compile(
    r"(?:bin/[a-z0-9_-]+-email|\bdraft(?:ed|s)?\b.{0,60}\b(?:email|reply|message)"
    r"|\b(?:email|reply)\b.{0,60}\bdraft(?:ed|s)?\b|reply-all|@[\w.-]+\.\w+)",
    re.IGNORECASE | re.DOTALL,
)


# The second carve-out: an offer the agent has ALREADY SETTLED.
#
# The rail's premise is that an offer-to-act means the call was never made. That
# premise fails in two shapes, and both are the fleet's SANCTIONED behaviour:
#
#   1. The agent states WHY it is the human's — "a production deploy is
#      outward-facing and yours to authorize", "one-line change, product call".
#      The call WAS made: the answer is "not mine". Blocking that argues with a
#      conclusion the agent reached deliberately, and the reason text then tells
#      it § Shipping clears deploys — which is exactly the claim the agent just
#      examined and rejected for this deploy.
#   2. The agent states a DEFAULT — "say the word and I'll pick it up now;
#      default is next turn". Nothing is being handed back at all: it has
#      decided, and is offering an override. That is "recommend and act".
#
# Measured 2026-08-27 over the last 40 sessions per agent, against the engine as
# shipped: ace 8/40 blocked, hal 4/40, ada 3/19, eva 4/40. Of ~22 blocks, ~7 were
# these two shapes — on FOUR different agents. Not one agent's quirk; the rail's.
#
# DIRECTION IS LOAD-BEARING, and asymmetric on purpose.
#
# A rationale is stated BEFORE the offer it justifies, so it is read BACKWARD
# only. Read forward too, a later sentence about a DIFFERENT item silences a real
# one — the live case: "Say the word and I'll take it. Still owed, and only you
# can do it: full Claude Code restart." That "only you can do it" belongs to the
# restart, not to the offer, and the offer is a true positive.
#
# A default is stated AFTER the offer it overrides, so it is read FORWARD only.
# Each alternative must assert "this one is the human's". A bare mention of the
# human is NOT enough: `needs you` was in this list for one measured pass and it
# matched "whether anything here needs you. My read is nothing does" — a sentence
# saying the exact OPPOSITE, which then silenced a true positive on two agents.
# An over-loose alternative here costs a missed nudge silently, so every pattern
# below carries its own verb or noun of decision.
_SETTLED_RATIONALE_RE = re.compile(
    r"(?:\byour\s+call\b|\byours\s+to\s+\w+"
    r"|\bonly\s+you\s+can\b|\bnot\s+mine\b|\byou\s+alone\b"
    r"|\b(?:product|judgment|business|policy)\s+call\b"
    r"|\bturns?\s+on\s+your\b"
    r"|\bfor\s+you\s+to\s+(?:decide|call|weigh|authorize|approve))",
    re.IGNORECASE | re.DOTALL,
)
_STATED_DEFAULT_RE = re.compile(
    r"(?:default\s+is\b|by\s+default\b|otherwise\s+I(?:'ll|\s+will)\b"
    r"|if\s+(?:I\s+hear\s+nothing|you\s+say\s+nothing|not)\b"
    r"|unless\s+you\s+(?:say|tell|object)\b|either\s+way\s+I(?:'ll|\s+will)\b)",
    re.IGNORECASE | re.DOTALL,
)

# How far to look on each side for the settling evidence. One sentence or so.
SETTLED_CHARS = 220


def _already_settled(tail: str, offer: "re.Match") -> bool:
    """True if this offer carries its own answer — a rationale, or a default."""
    before = tail[max(0, offer.start() - SETTLED_CHARS) : offer.start()]
    if _SETTLED_RATIONALE_RE.search(before):
        return True
    after = tail[offer.end() : offer.end() + SETTLED_CHARS]
    return bool(_STATED_DEFAULT_RE.search(after))


def _offers_something_outbound(tail: str, offer: "re.Match") -> bool:
    """True if this offer's object is a delivery — the one ask that always waits."""
    if _OUTBOUND_CONTEXT_RE.search(tail):
        return True
    window = tail[offer.end() : offer.end() + OBJECT_CHARS]
    sentence_end = window.find("?")
    if sentence_end != -1:
        window = window[:sentence_end]
    return bool(_OUTBOUND_OBJECT_RE.search(window))


REASON = """\
You ended by OFFERING to do something rather than doing it.

Before you stop, answer the question your operating model already poses:
**whose call is this actually?**

  * Is it clear from the code, the request, and what you already found?
  * Is it a dev action — branch / commit / PR / merge / deploy / repo ops? Those
    ship with NO approval (CLAUDE.md § Shipping). "It's a deploy" is not a reason
    to ask.
  * Is the end state one you know how to reach?

If yes to those — **do it now**, in this session, and report the reasoning as the
rationale for what you did. Say what you scoped OUT and why, so it can be widened;
that is not the same as asking permission to start.

It genuinely IS the human's call only when it turns on their taste, their
priorities, or cost/risk they alone can weigh — or when it puts something in front
of a person: sending, replying, publishing, posting, sharing — that is your
CLAUDE.md § hard guardrail, and this rail never overrides it.

If that's this, say so in one line and stop again; this rail blocks only once per
session and will not interrupt you twice.

Measured cost of getting this wrong: 2026-08-27, two full round-trips on a bug the
agent had already diagnosed to the line and could ship to without approval.\
"""


def _read_payload() -> dict:
    """Parse the Stop-event JSON on stdin. Anything unexpected -> {}."""
    try:
        return json.loads(sys.stdin.read()) or {}
    except Exception:
        return {}


def _assistant_text(record: dict) -> str:
    """The plain-text parts of an assistant record, joined. '' for anything else.

    Text blocks only: a tool_use block is an ACTION, and a message whose last act
    was a tool call has not finished handing anything back.
    """
    if record.get("type") != "assistant":
        return ""
    message = record.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    return "\n".join(parts)


def hands_back_a_call(text: str) -> bool:
    """True if this message CLOSES by offering to act.

    Scoped to the tail for the reason given at TAIL_CHARS, and exempted for the
    outbound gate, which is the one ask that is always correct.
    """
    if not text:
        return False
    tail = text[-TAIL_CHARS:]
    offer = _OFFER_RE.search(tail)
    if not offer:
        return False
    if _offers_something_outbound(tail, offer):
        return False
    if _already_settled(tail, offer):
        return False
    return True


def final_assistant_text(path: str) -> str:
    """The text of the last assistant message in the transcript, or ''.

    Scans the whole file rather than seeking backwards: transcripts are JSONL with
    no index, records vary wildly in size, and a wrong answer here is a rail that
    fires on the wrong message. One corrupt line must not blind the scan.
    """
    latest = ""
    try:
        with open(path, "r", errors="replace") as fh:
            for line in fh:
                if '"assistant"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                if not isinstance(record, dict):
                    continue
                text = _assistant_text(record)
                if text.strip():
                    latest = text
    except Exception:
        # Unreadable transcript -> we cannot judge -> do not block.
        return ""
    return latest


def already_blocked(session_id: str) -> bool:
    """True if this session has been blocked before.

    Records the block as a side effect, so the caller gets at-most-once semantics
    from one call. An unwritable state dir reports "already blocked", which fails
    open rather than risking a session that can never stop. Its own directory, not
    turn-close's: the two rails must be able to fire once each.
    """
    if not session_id:
        return True
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        marker = STATE_DIR / f"{session_id}.blocked"
        if marker.exists():
            return True
        marker.touch()
        return False
    except Exception:
        return True


def main() -> int:
    payload = _read_payload()
    transcript = payload.get("transcript_path")
    if not transcript:
        return 0

    if not hands_back_a_call(final_assistant_text(transcript)):
        return 0

    if already_blocked(payload.get("session_id", "")):
        return 0

    # `decision: block` is the documented Stop-hook contract; `reason` is what
    # Claude sees. Exit 0 — the JSON, not the exit code, carries the decision.
    json.dump({"decision": "block", "reason": REASON}, sys.stdout)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Belt and braces: this hook may never be the reason a session cannot end.
        sys.exit(0)
