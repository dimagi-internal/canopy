"""Agent threads — a bounded, moderated, direct conversation between fleet agents. The PURE half.

Ids and keys, the `origin_ref` tag canopy-web derives a thread's messages from, the message
prompt, reply-block extraction/validation, and the moderator's next-step decision. No I/O lives
here: `thread_cli` talks to canopy-web.

A thread is stored by canopy-web (`/api/threads/`), which also ENFORCES its limits server-side:
every message is an ordinary one-shot harness turn tagged
`origin_ref = {"kind": "thread_message", "thread": <id>, "n": <n>, "speaker": <slug>}`, and the
turn-create guard refuses a message to a closed thread, past its deadline, over its budget, out
of order, or to someone who is not a participant. Messages are never stored separately —
canopy-web derives them from the tagged turns, reading each one's reply block from its close-out
(else its transcript), exactly as it reads a huddle round.

No agent ever waits on another inside its own turn. The MODERATOR — a CLI loop the thread's
opener runs (`canopy thread run`) — sends the next message and decides when the thread ends.
`decide()` is that decision, derived from the thread record alone, so the loop is stateless and
resumable: run it again from anywhere and it picks up where the thread is.

First use: the huddle's agreement step. When a teammate answers `amend` on a proposal, the
proposal's author and that teammate settle it directly (kind `agreement`) instead of one relayed
hop. Contract: canopy-web + canopy agree on docs/architecture/agent-threads.md.
"""
from __future__ import annotations

import datetime as dt
import json
import re

OPEN, SETTLED, OUT_OF_BUDGET, TIMED_OUT, CANCELLED = (
    "open", "settled", "out_of_budget", "timed_out", "cancelled")
CLOSE_STATUSES = (SETTLED, OUT_OF_BUDGET, TIMED_OUT, CANCELLED)

AGREE, COUNTER, DECLINE, QUESTION = "agree", "counter", "decline", "question"
POSITIONS = (AGREE, COUNTER, DECLINE, QUESTION)
AGREED, NOT_AGREED = "agreed", "not_agreed"

AGREEMENT = "agreement"
DEFAULT_MAX_MESSAGES = 4
DEFAULT_DEADLINE_MINUTES = 90

# A message turn in one of these states will never reply.
FAILED = {"failed", "lost", "error", "expired", "cancelled", "canceled"}

_THREAD_FENCE = re.compile(r"```thread\s*\n(.*?)\n```", re.S)
_JSON_FENCE = re.compile(r"```json\s*\n(.*?)\n```", re.S)


# ── ids, keys, tags ──────────────────────────────────────────────────────────────
def thread_key(thread: str) -> str:
    """The runner session key for every message of a thread — never a member's `main`."""
    return f"thread:{thread}"


def idempotency_key(thread: str, n: int) -> str:
    """One turn per (thread, message number): a re-sent message dedupes onto the first."""
    return f"thread-{thread}-n{int(n)}"


def closeout_session_id(thread: str, n: int) -> str:
    """The `--session-id` a speaker files its reply under — stable, so re-filing a corrected
    block replaces the first."""
    return f"thread:{thread}:{int(n)}"


def message_origin_ref(thread: str, n: int, speaker: str) -> dict:
    return {"kind": "thread_message", "thread": thread, "n": int(n), "speaker": speaker,
            "thread_key": thread_key(thread)}


# ── small helpers ────────────────────────────────────────────────────────────────
def parse_ts(s) -> dt.datetime | None:
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def agents_of(thread: dict) -> list[str]:
    return [p.get("agent") for p in thread.get("participants") or [] if p.get("agent")]


def role_of(thread: dict, agent: str) -> str:
    for p in thread.get("participants") or []:
        if p.get("agent") == agent:
            return str(p.get("role") or "participant")
    return ""


def messages_used(thread: dict) -> int:
    return max(int(thread.get("messages_used") or 0), len(thread.get("messages") or []))


def messages_of(thread: dict) -> list[dict]:
    return sorted(thread.get("messages") or [], key=lambda m: int(m.get("n") or 0))


def norm_position(raw) -> str:
    """Lenient: "Agrees", "counter-proposal", "Declined", "a question" all land."""
    p = re.sub(r"[^a-z]", "", str(raw or "").lower())
    for want, prefixes in ((AGREE, ("agree",)), (COUNTER, ("counter",)),
                           (DECLINE, ("declin",)), (QUESTION, ("question", "ask"))):
        if p.startswith(prefixes):
            return want
    return p


# ── the reply block ──────────────────────────────────────────────────────────────
def _bare_objects(text: str) -> list[str]:
    """Every top-level JSON object embedded in free text, in order."""
    dec = json.JSONDecoder()
    out, i = [], 0
    while True:
        i = text.find("{", i)
        if i < 0:
            return out
        try:
            _, end = dec.raw_decode(text, i)
        except ValueError:
            i += 1
            continue
        out.append(text[i:end])
        i = end


def extract_block(text: str, thread: str, n: int) -> tuple[dict | None, str]:
    """The LAST ```thread block in `text` naming this thread and message number, and '' — or
    None and why not. Same leniency as canopy-web's parser: a ```json fence or a bare JSON
    object with those keys also counts (a fenced ```thread block wins when both exist). A block
    copied from another thread, or from an earlier message, never counts."""
    err = ""
    for candidates in (_THREAD_FENCE.findall(text or ""), _JSON_FENCE.findall(text or ""),
                       _bare_objects(text or "")):
        for raw in reversed(candidates):
            try:
                b = json.loads(raw)
            except ValueError as e:
                err = err or f"reply block is not valid JSON: {e}"
                continue
            if not isinstance(b, dict) or "thread" not in b:
                err = err or "reply block is not a thread block"
                continue
            try:
                bn = int(b.get("n") or 0)
            except (TypeError, ValueError):
                bn = 0
            if str(b.get("thread")) == thread and bn == int(n):
                return b, ""
            err = err or f"block names thread {b.get('thread')!r} message {b.get('n')!r}"
    return None, err or "no thread block found"


def validate_block(block, thread: str, n: int, speaker: str) -> list[str]:
    """Problems with a reply block; [] means it is usable."""
    if not isinstance(block, dict):
        return ["block is not a JSON object"]
    probs = [f"missing {k}" for k in ("thread", "n", "from", "says", "position") if k not in block]
    if "thread" in block and str(block["thread"]) != thread:
        probs.append(f"thread is {block['thread']!r}, not {thread!r}")
    if "n" in block:
        try:
            if int(block["n"]) != int(n):
                probs.append(f"n is {block['n']!r}, not {n}")
        except (TypeError, ValueError):
            probs.append("n must be a number")
    if "from" in block and speaker and str(block["from"]) != speaker:
        probs.append(f"from is {block['from']!r}, but message {n} was {speaker}'s")
    if "says" in block and not (isinstance(block["says"], str) and block["says"].strip()):
        probs.append("says must be non-empty prose")
    if "position" in block and norm_position(block["position"]) not in POSITIONS:
        probs.append(f"position must be one of {'|'.join(POSITIONS)}")
    if block.get("proposal") not in (None, {}) and not isinstance(block.get("proposal"), dict):
        probs.append("proposal must be a JSON object")
    return probs


def message_state(msg: dict, thread: dict, now: dt.datetime) -> tuple[str, list[str]]:
    """(state, problems) for one message. Settled: replied | malformed | failed | timed_out.
    A message turn's `done` means its session SPAWNED, not that the speaker replied — so `done`
    without a block is still pending."""
    block = msg.get("block")
    tid, n, speaker = thread.get("id") or "", int(msg.get("n") or 0), msg.get("speaker") or ""
    if isinstance(block, dict):
        probs = validate_block(block, tid, n, speaker)
        return ("malformed", probs) if probs else ("replied", [])
    status = str(msg.get("status") or "")
    if status in FAILED:
        return "failed", [msg["reply_error"]] if msg.get("reply_error") else []
    deadline = parse_ts(thread.get("deadline_at"))
    if deadline is not None and now >= deadline:
        return "timed_out", []
    return f"pending:{status or 'unknown'}", ([msg["reply_error"]] if msg.get("reply_error") else [])


def settled_state(state: str) -> bool:
    return not state.startswith("pending:")


# ── the message prompt ───────────────────────────────────────────────────────────
_POSITION_WORDS = {AGREE: "agrees", COUNTER: "suggests a change", DECLINE: "doesn't agree",
                   QUESTION: "asks"}


def transcript_text(thread: dict, now: dt.datetime) -> str:
    """The whole thread so far, verbatim: who spoke, what it said, its position, and any
    proposal it put on the table. A message that never arrived says so (and why)."""
    parts = []
    for m in messages_of(thread):
        n, who = int(m.get("n") or 0), m.get("speaker") or "?"
        state, probs = message_state(m, thread, now)
        if state == "replied":
            b = m["block"]
            pos = norm_position(b.get("position"))
            part = (f"#### Message {n} — {who} ({role_of(thread, who) or 'participant'}) · "
                    f"position: {pos}\n{b.get('says', '').strip()}")
            if isinstance(b.get("proposal"), dict) and b["proposal"]:
                part += f"\nProposal on the table:\n```json\n{json.dumps(b['proposal'], indent=2)}\n```"
        elif settled_state(state):
            why = f" ({'; '.join(p for p in probs if p)})" if any(probs) else ""
            part = f"#### Message {n} — {who}: no usable reply — {state}{why}"
        else:
            part = f"#### Message {n} — {who}: still writing"
        parts.append(part)
    return "\n\n".join(parts) or "(nothing yet — you open the thread)"


def _kind_rules(thread: dict, speaker: str) -> str:
    if thread.get("kind") != AGREEMENT:
        return ("The thread settles when every participant's latest position is `agree`; any "
                "`decline` ends it without agreement.")
    others = ", ".join(a for a in agents_of(thread) if a != speaker) or "the other participant"
    return (f"This is an AGREEMENT thread between you and {others}. It settles AGREED the moment "
            "the latest positions of both of you are `agree` — and then the LATEST proposal put "
            "on the table is what gets adopted, so if you change the idea, put the WHOLE revised "
            "proposal (same title) in `proposal`. Any `decline` ends it NOT agreed. Running out "
            "of messages or time ends it not agreed too, so converge — don't re-argue.")


def render_message_prompt(thread: dict, speaker: str, n: int, now: dt.datetime) -> str:
    """The prompt for message `n`, to `speaker`. Carries the purpose, the context verbatim,
    the WHOLE thread so far verbatim, the speaker's role, the messages left, and the exact
    reply block + close-out command. (The dispatched-by footer is added at dispatch.)"""
    tid = thread["id"]
    budget = int(thread.get("max_messages") or DEFAULT_MAX_MESSAGES)
    left_after = max(0, budget - int(n))
    role = role_of(thread, speaker) or "participant"
    who = ", ".join(f"{p.get('agent')} ({p.get('role') or 'participant'})"
                    for p in thread.get("participants") or [])
    moderator = thread.get("moderator") or "the moderator"
    deadline = parse_ts(thread.get("deadline_at"))
    parent = thread.get("parent") or {}
    parent_line = ("\nPart of: " + ", ".join(f"{k} {v}" for k, v in parent.items()) + "\n"
                   if parent else "")
    left_line = (f"After this message {left_after} more may be sent"
                 if left_after else "This is the LAST message the thread allows — make it count")
    if deadline:
        left_line += f"; the thread closes at {deadline:%Y-%m-%d %H:%M} UTC"
    left_line += "."
    block = json.dumps({"thread": tid, "n": int(n), "from": speaker, "says": "<what you say to "
                        "the others — plain prose>", "position": "agree|counter|decline|question",
                        "proposal": {}}, ensure_ascii=False)
    return f"""Thread {tid} — message {n} of at most {budget}. You are {speaker}, the {role}.
{moderator} moderates; it sends each message and decides when the thread ends.

READ-ONLY TURN: this is one message in a short, direct conversation between fleet agents. No
emails, replies, PRs or board writes, and do not run your inbox or board steps. Your one write is
filing your reply block as your close-out. How to answer a thread: canopy
`plugins/canopy/agent-core/thread.md`.

Purpose: {thread.get('purpose') or '(none given)'}
Participants: {who}{parent_line}
## Context (verbatim)

{thread.get('context') or '(none)'}

## The thread so far (verbatim)

{transcript_text(thread, now)}

## Your message

You are the {role}. {_kind_rules(thread, speaker)}

Positions: `agree` (you accept the latest proposal on the table — or yours, if you put one in
`proposal`), `counter` (you want a change — say it, and put the WHOLE revised proposal in
`proposal`), `decline` (you cannot agree — say why), `question` (you need an answer first — ask it).
{left_line}
ANY message may be the last: the thread ends the moment the end condition holds (for agreement,
when both sides' latest positions are `agree`), so nobody may get another turn. Put EVERYTHING the
others need in THIS message — never promise something "in my next message", and never assume a
later message will reach you.
Speak only to the purpose; quote nothing private outside its audience.

```thread
{block}
```

(`proposal` is optional — leave it `{{}}` when you are not changing the idea.)

Put the block LAST in your final message, then file it as your close-out (block in a file):

    canopy agent turn --slug {speaker} --session-id "{closeout_session_id(tid, n)}" --title "thread {tid} message {n}" --summary "$(cat <file>)"
"""


# ── the moderator's decision ─────────────────────────────────────────────────────
def _first_speaker(thread: dict) -> str:
    agents = agents_of(thread)
    if thread.get("kind") == AGREEMENT:
        for p in thread.get("participants") or []:
            if str(p.get("role") or "").lower() == "author" and p.get("agent"):
                return p["agent"]
    return agents[0] if agents else ""


def _after(thread: dict, speaker: str) -> str:
    agents = agents_of(thread)
    if speaker not in agents:
        return _first_speaker(thread)
    return agents[(agents.index(speaker) + 1) % len(agents)]


def _latest_proposal(replied: list[dict]) -> dict:
    for m in reversed(replied):
        p = m["block"].get("proposal")
        if isinstance(p, dict) and p:
            return p
    return {}


def _settle(thread: dict, replied: list[dict]) -> dict | None:
    """The close an exchange has earned, or None while it is still live."""
    for m in replied:
        if norm_position(m["block"].get("position")) == DECLINE:
            return {"status": SETTLED,
                    "outcome": {"result": NOT_AGREED,
                                "why": f"{m['speaker']} declined: {m['block'].get('says', '').strip()}",
                                "declined_by": m["speaker"]}}
    latest = {}
    for m in replied:
        latest[m["speaker"]] = norm_position(m["block"].get("position"))
    agents = agents_of(thread)
    if len(agents) >= 2 and all(latest.get(a) == AGREE for a in agents):
        out = {"result": AGREED, "why": replied[-1]["block"].get("says", "").strip()}
        prop = _latest_proposal(replied)
        if prop:
            out["proposal"] = prop
        return {"status": SETTLED, "outcome": out}
    return None


def decide(thread: dict, now: dt.datetime) -> dict:
    """What the moderator does next, from the thread record alone (so it is resumable):

    - `{"action": "closed"}` — the thread is already closed; nothing to do.
    - `{"action": "wait", "n", "speaker"}` — the latest message is still being written.
    - `{"action": "close", "status", "outcome"}` — settle it (agreed / not agreed), or close it
      out_of_budget / timed_out.
    - `{"action": "send", "n", "to"}` — send message n to that participant.

    Turn order: message 1 → the author (kind=agreement; else the first participant), then the
    next participant in turn. A message that never arrived (failed / malformed / timed out) uses
    its budget — canopy-web counts every tagged turn — and the same speaker is asked again.
    """
    status = thread.get("status") or OPEN
    if status != OPEN:
        return {"action": "closed", "status": status, "outcome": thread.get("outcome") or {}}
    msgs = messages_of(thread)
    states = [(m, *message_state(m, thread, now)) for m in msgs]
    if states and not settled_state(states[-1][1]):
        last = states[-1][0]
        return {"action": "wait", "n": int(last.get("n") or 0), "speaker": last.get("speaker"),
                "state": states[-1][1]}
    replied = [m for m, st, _ in states if st == "replied"]
    done = _settle(thread, replied)
    if done:
        return {"action": "close", **done}
    deadline = parse_ts(thread.get("deadline_at"))
    if deadline is not None and now >= deadline:
        return {"action": "close", "status": TIMED_OUT,
                "outcome": {"result": NOT_AGREED, "why": "ran out of time"}}
    budget = int(thread.get("max_messages") or DEFAULT_MAX_MESSAGES)
    used = messages_used(thread)
    if used >= budget:
        return {"action": "close", "status": OUT_OF_BUDGET,
                "outcome": {"result": NOT_AGREED, "why": "ran out of messages"}}
    if not msgs:
        to = _first_speaker(thread)
    else:
        last, st, _ = states[-1]
        to = last.get("speaker") if st != "replied" else _after(thread, last.get("speaker"))
    if not to:
        return {"action": "close", "status": CANCELLED,
                "outcome": {"result": NOT_AGREED, "why": "the thread has no participants"}}
    return {"action": "send", "n": used + 1, "to": to}


# ── reading a closed thread ──────────────────────────────────────────────────────
def result_of(thread: dict) -> str:
    """`open`, `agreed`, or `not_agreed` — the one word a caller (a huddle) acts on."""
    if (thread.get("status") or OPEN) == OPEN:
        return OPEN
    out = thread.get("outcome") or {}
    if thread.get("status") == SETTLED and out.get("result") == AGREED:
        return AGREED
    return NOT_AGREED


def why_of(thread: dict) -> str:
    out = thread.get("outcome") or {}
    why = str(out.get("why") or "").strip()
    if why:
        return why
    return {OUT_OF_BUDGET: "ran out of messages", TIMED_OUT: "ran out of time",
            CANCELLED: "the thread was cancelled"}.get(thread.get("status") or "", "no reason given")
