"""Pre-send review rail — a receipt keyed to the BODY, not to the agent's memory.

WHY THIS EXISTS (Eva, 2026-07-15; Jonathan): every agent's `agent-turn-review` skill says
"re-run this whole review on every revision of a draft, not only the first." That
instruction is prose, and prose fails under load in one specific, reproducible way: the
agent reviews draft v1, then revises twice as new findings land, and each revision feels
like *improving already-reviewed work* rather than *a new draft needing review*. The
turn's own close-checklist then reports "review ran ✅" — truthfully, about v1 — and v3
ships unreviewed. On the turn that motivated this, re-running the review on the final body
caught a named shortlist target missing from the email entirely.

The fleet's stated principle is "invariants are hooks, not memory" — prose relies on the
model choosing to comply. So this makes the failure IMPOSSIBLE rather than discouraged:
the receipt is keyed to a fingerprint of the normalized body, so revising the body moves
the fingerprint and the stale receipt stops matching. You cannot carry v1's review to v3.

DESIGN NOTES
- It is a DENY RAIL, not an approval gate. No human is in this loop and no modal blocks
  the turn (`agent-core/gating-baseline.json`: "a PreToolUse 'ask' is a blocking modal
  that stalls autonomous work"). The agent self-corrects and keeps going.
- It lives in `send()` rather than in each agent's PreToolUse hook ON PURPOSE. The
  baseline rails already force ALL agent mail through `canopy email send`, so enforcing
  here ships once via /canopy:update and reaches every agent with no per-agent hook
  backport. The gating hook is copied per repo; this is not.
- `dry_run` is deliberately exempt. Dry-run is HOW an agent iterates and verifies
  recipients; gating it would make the rail actively harmful and get it ripped out.
- There is no bypass env var, by design. An escape hatch is exactly what a turn under
  load reaches for. The cost of compliance is one command, so there is no need for one.
- The receipt records what the review CAUGHT. That is the self-improvement surface:
  `caught` entries aggregate into the fleet loop (canopy:agent-review / fleet-align) to
  show which reviews actually earn their keep, and which drafts keep failing the same way.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

# Imported lazily by agent_email to avoid a circular import at module load.

RECEIPTS_ENV = "CANOPY_REVIEW_RECEIPTS_DIR"


def receipts_root() -> Path:
    """Where receipts live. Env-overridable for tests and unusual installs.

    Deliberately OUTSIDE any agent repo: agents run in emdash worktrees, and a receipt is
    ephemeral per-draft state, not something to track in git.
    """
    override = os.environ.get(RECEIPTS_ENV)
    if override:
        return Path(override)
    return Path.home() / ".canopy" / "review-receipts"


def _normalize(text: str) -> str:
    """Fingerprint the body as the SEND PATH will render it.

    Delegates to agent_email.normalize so a receipt recorded from a body file matches the
    body that actually goes out. If these two ever disagree, the rail blocks a draft that
    WAS reviewed — the fastest way to make the fleet hate it.
    """
    from orchestrator.agent_email import normalize  # local: avoids circular import
    return normalize(text)


def fingerprint(body_text: str) -> str:
    """Stable, content-specific id for a draft body."""
    return hashlib.sha256(_normalize(body_text).encode("utf-8")).hexdigest()


def _path_for(slug: str, fp: str) -> Path:
    return receipts_root() / slug / f"{fp}.json"


# ── Commitment scan: §6 as a gate, not a checklist line ──────────────────────
# Why this is enforced by the tool: on 2026-07-23 a review ran, caught three real
# body defects, recorded "clean" — and still shipped "Happy to walk anyone through
# it live", a real-time human session the agent has no way to hold. The RULE was
# already written (§6: name the executable mechanism or cut it). What failed was
# applying it to every instance — a completeness problem, and completeness is
# exactly what prose cannot enforce. So the tool enumerates the phrases and
# refuses the receipt until each is ruled; the send stays blocked by the gate
# rather than by whether the reviewer remembered to look at the sign-off line.
_COMMITMENT_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"walk\s+(?:you|him|her|them|anyone|someone|folks|the\s+team)\b[^.\n]{0,40}\bthrough\b",
     "real-time human"),
    (r"\bhop\s+on\b|\bjump\s+on\b|\bon\s+a\s+call\b|\bset\s+up\s+a\s+call\b|\bget\s+on\s+a\s+call\b",
     "real-time human"),
    (r"\bin\s+person\b|\bface[-\s]to[-\s]face\b", "real-time human"),
    (r"\bsync\s+(?:up\s+)?with\b|\bloop\s+in\b|\bcheck\s+with\b|\brun\s+it\s+by\b"
     r"|\bcoordinate\s+with\b|\balign\s+with\b|\breach\s+out\s+to\b", "human dependency"),
    (r"\bhappy\s+to\b|\bglad\s+to\b", "offer"),
)


def scan_commitments(body_text: str) -> list[dict]:
    """Every commitment-class phrase in the body, with surrounding context.

    Recall-biased on purpose: a false positive costs a two-second
    ``grounded:re-render`` ruling; a missed offer ships to the recipients.
    """
    hits: list[dict] = []
    for pattern, kind in _COMMITMENT_PATTERNS:
        for m in re.finditer(pattern, body_text, re.IGNORECASE):
            start, end = max(0, m.start() - 40), min(len(body_text), m.end() + 60)
            hits.append({
                "kind": kind,
                "match": m.group(0).strip(),
                "context": " ".join(body_text[start:end].split()),
            })
    return hits


def unruled_commitments(body_text: str, rulings=None) -> list[dict]:
    """Commitment hits not accounted for by a ruling.

    A ruling is ``"<substring>=grounded:<mechanism>"`` or ``"<substring>=cut"``; it
    covers any hit whose match or surrounding context contains that substring.
    """
    keys = [r.split("=", 1)[0].strip().lower() for r in (rulings or []) if "=" in r]
    out: list[dict] = []
    for hit in scan_commitments(body_text):
        haystack = f"{hit['match']} {hit['context']}".lower()
        if not any(k and k in haystack for k in keys):
            out.append(hit)
    return out


def commitment_block_message(hits: list[dict]) -> str:
    """The refusal: enumerate what must be ruled, and how."""
    lines = [
        f"REFUSED: {len(hits)} commitment-class phrase(s) in this body are unruled.",
        "",
        "Each asserts something you will DO. Name the executable mechanism, or cut it",
        "(canopy:agent-turn-review §6). Rule every hit below, then re-record:",
        "",
    ]
    for h in hits:
        lines.append(f'  • [{h["kind"]}] "{h["match"]}"')
        lines.append(f'      …{h["context"]}…')
    lines += [
        "",
        '  --commitment "<substring>=grounded:<how you will actually do it>"',
        '  --commitment "<substring>=cut"',
        "",
        "GROUNDED for an agent: re-render, reply on the thread, open a PR, produce a doc.",
        "NOT grounded: anything needing you to be a person in real time — a live",
        "walkthrough, a call, meeting someone, or syncing with a human.",
    ]
    return "\n".join(lines)


def record(slug: str, body_text: str, *, caught=None, verdict: str = "clean",
           reviewer: str = "agent-turn-review", commitments=None) -> Path:
    """Record that `body_text` was reviewed. Returns the receipt path.

    `caught` is the list of things the review actually found (and you then fixed). An
    empty list is a legitimate answer — but per the review skill's own §13, "none found"
    is only valid AFTER reading the draft back, not as a default.

    Refuses to issue while any commitment-class phrase is unruled (see the scan above):
    no receipt means no send, so §6 is enforced by the gate rather than remembered.
    """
    unruled = unruled_commitments(body_text, commitments)
    if unruled:
        from orchestrator.agent_email import AgentEmailError  # local: circular import
        raise AgentEmailError(commitment_block_message(unruled))
    fp = fingerprint(body_text)
    p = _path_for(slug, fp)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "slug": slug,
        "fingerprint": fp,
        "reviewed_at": time.time(),
        "reviewer": reviewer,
        "verdict": verdict,
        "caught": list(caught or []),
        "commitments": list(commitments or []),
    }
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return p


def lookup(slug: str, body_text: str) -> dict | None:
    """Return the receipt for this exact body, or None."""
    p = _path_for(slug, fingerprint(body_text))
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None  # a corrupt receipt is no receipt — fail closed


def rail_message(slug: str, body_text: str) -> str:
    fp = fingerprint(body_text)
    return (
        f"BLOCKED: this exact body has no pre-send review receipt "
        f"(fingerprint {fp[:12]}).\n"
        f"Your agent-turn-review skill requires a review of the CURRENT draft — a review "
        f"of an earlier revision does not carry over, which is the whole point: revise the "
        f"body and the fingerprint moves.\n"
        f"Do this:\n"
        f"  1. Run the `agent-turn-review` skill against the body you are about to send: "
        f"re-read the original request, extract each discrete ask, confirm the draft does "
        f"exactly that, check every commitment is executable, then read it back for "
        f"repetition.\n"
        f"  2. Record it, naming what it caught (\"none\" only after you actually read it "
        f"back):\n"
        f"     canopy email review-receipt --repo . --body-file <the same file> "
        f"--caught \"<what you found>\"\n"
        f"  3. Re-run the send.\n"
        f"`--dry-run` never needs a receipt — iterate and verify recipients there freely."
    )


def require(slug: str, body_text: str) -> dict:
    """Raise unless this exact body has been reviewed. Returns the receipt."""
    from orchestrator.agent_email import AgentEmailError  # local: circular import
    got = lookup(slug, body_text)
    if got is None:
        raise AgentEmailError(rail_message(slug, body_text))
    return got
