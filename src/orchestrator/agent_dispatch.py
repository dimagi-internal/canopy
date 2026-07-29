"""One-shot dispatch — record the work on the board, then send an agent to do it.

Both halves already existed: `canopy agent add` writes a board task, and
`POST /api/harness/turns/` enqueues a turn the runner claims and spawns a visible
emdash session for. What did not exist was a way to do the two together, so every
caller wrote the raw POST by hand — and the sharp edges lived in prose inside one
agent's skill, where nobody else reads them. Three of them, each of which has
actually bitten:

**1. A missing `thread_key` types the prompt into the CALLER'S session.** The runner
keys a session by `(agent, thread_key)`; with no key it resolves `<agent>:main`,
which for a self-targeted turn is the live conductor session that sent the request.
The prompt lands in that conversation instead of a new one. Handled here by always
sending a thread_key: a one-shot dispatch wants an isolated session by definition, so
there is no case where the caller should have to remember this.

**2. `done` means SPAWNED, not WORKED.** The launch turn flips to `done` within
seconds carrying `result_note: "created session '<name>'"`. That is the runner's job
finishing, not the agent's turn succeeding — no evidence the agent read anything, did
anything, or exited cleanly. `summarize_turn` therefore reports `launched` +
`verified: False`, never "complete". (A 5-second `done` was reported as a verified win
on 2026-07-23; this is that miss, encoded.)

**3. Re-running a dispatch spawns a second session.** The idempotency key is derived
from (agent, title, day) so the same work dispatched twice is one turn.

Deterministic: builds payloads and reads status. Judgment about what to dispatch, and
verification that it worked, stay with the caller.
"""
from __future__ import annotations

import hashlib
import re

TURNS_PATH = "/api/harness/turns/"

# Runner statuses → what a human may honestly say about them.
_LAUNCHED = {"done"}                       # spawned; the agent's own outcome is unknown
_PENDING = {"queued", "claimed", "running"}
# All three are pending — none has finished spawning, so none may claim LAUNCHED.
# But they are not the same claim to a human: `queued` means no runner has taken
# it, while `claimed`/`running` mean one has and the work is under way. Rendering
# the second as "the runner has not spawned it yet" states the exact falsehood
# this command exists to disprove — someone checking whether their dispatch
# landed reads it and concludes it didn't.
_UNCLAIMED = {"queued"}
_FAILED = {"lost", "failed", "error", "expired"}

_SESSION_RX = re.compile(r"created session '([^']+)'")


class DispatchError(Exception):
    """A dispatch that cannot be built correctly — raised, never silently degraded."""


def derive_idempotency_key(slug: str, title: str, day: str) -> str:
    """Stable key for (agent, work, day) so a repeat dispatch is one turn, not two.

    Scoped to the day rather than forever: dispatching the same title tomorrow is
    usually a deliberate re-run, and a permanent key would silently swallow it.
    """
    digest = hashlib.sha256(f"{slug}|{title}|{day}".encode()).hexdigest()[:12]
    return f"dispatch-{(slug or '').strip().lower()}-{digest}"


def build_turn_payload(slug: str, *, prompt: str = "", idempotency_key: str,
                       task_ext_id: str | None = None) -> dict:
    """The `POST /api/harness/turns/` body for a one-shot dispatch."""
    slug = (slug or "").strip()
    if not slug:
        raise DispatchError("dispatch needs a target agent slug")
    if not (idempotency_key or "").strip():
        raise DispatchError("dispatch needs an idempotency key")

    origin_ref = {"thread_key": idempotency_key}   # see §1 above — always, not conditionally
    if task_ext_id:
        origin_ref["task_ext_id"] = task_ext_id

    payload = {
        "agent_slug": slug,
        "origin": "api",
        "idempotency_key": idempotency_key,
        "origin_ref": origin_ref,
    }
    # An absent prompt means "drain your board" — meaningfully different from an
    # empty one, so send nothing rather than "".
    if (prompt or "").strip():
        payload["prompt"] = prompt
    return payload


def summarize_turn(turn: dict) -> dict:
    """Turn a harness turn record into something safe to report.

    Never returns a "completed" state: this layer cannot know whether the agent did
    the work, only whether a session was spawned.
    """
    turn = turn or {}
    status = str(turn.get("status") or "").strip().lower()
    note = str(turn.get("result_note") or "").strip()
    m = _SESSION_RX.search(note)
    session_name = m.group(1) if m else None

    if status in _LAUNCHED:
        state = "launched"
        headline = (f"launched (unverified) — spawned session {session_name}"
                    if session_name else "launched (unverified) — session spawned")
    elif status in _PENDING:
        # One bucket (nothing here is LAUNCHED), two honest headlines.
        state = "queued"
        headline = (f"queued ({status}) — no runner has claimed it yet"
                    if status in _UNCLAIMED
                    else f"queued ({status}) — a runner claimed it and is executing it")
    elif status in _FAILED:
        state = "failed"
        headline = f"FAILED ({status}){' — ' + note if note else ''}"
    else:
        state = "unknown"
        headline = f"unknown status {status!r}{' — ' + note if note else ''}"

    return {
        "id": turn.get("id"),
        "agent_slug": turn.get("agent_slug"),
        "status": status,
        "state": state,
        # Always False here by construction. Verifying means reading the spawned
        # session's own outcome or the agent's next turn record — the caller's job.
        "verified": False,
        "session_name": session_name,
        "result_note": note,
        "headline": headline,
    }
