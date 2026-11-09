"""Turn-synthesis — reduce a raw Claude Code transcript to the conversation that
matters: what the human typed and the FINAL assistant reply per turn.

This is the canonical, DRY home for the reduction shared by the `share-session`
uploader (`scripts/share-session/upload.py`) and the harvest engine
(`harvest.py::_ordered_texts` / `strip_session` / `session_digest`), which both
import from here.

A *turn-synthesis* is the list of ``Turn(prompt, response)`` for a session:
the human prompt paired with the final assistant text that followed it. Tool
calls, tool results, sidechains, and harness noise are dropped — so the result
is small, readable, and free of any sensitive tool output.

Stdlib only — `share-session`'s uploader runs under a bare ``python3`` (it adds
``src/`` to ``sys.path`` and imports this module), so it must not depend on the
package's third-party deps.

Two extraction surfaces, one set of helpers:

- ``synthesize(path)`` → ``(session_id, [Turn, ...])``. One Turn per human
  prompt; ``Turn.response`` is the **joined text of the last assistant message**
  before the next prompt (the reply you actually saw). This is what the
  share-session wire format and any "prompt + final reply" view want.
- ``iter_messages(path)`` → ordered ``[("U"|"A", text), ...]`` keeping **every**
  assistant prose block. This is the richer substrate harvest needs for its
  ``full`` strip mode and per-session digests.

Both run through the same prompt-cleaning (noise filter + slash-command
rendering) and assistant-text extraction, so the two never drift again.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

# ── Harness-authored "user" lines that aren't things the human actually typed ──
_NOISE_PREFIXES = (
    "<system-reminder",
    "<command-name",
    "<command-message",
    "<command-args",
    "<local-command-stdout",
    "<local-command-stderr",
    "<local-command-caveat",
    "<task-notification",
    "<system>",
    "Caveat:",
    "[Request interrupted",
    # Auto-compaction continuation summary — Claude Code injects this as a
    # type=user message (also flagged isCompactSummary) when a session runs out
    # of context. It is NOT something the human typed. Dropped by the flag in the
    # iterators below; this prefix is a fallback for transcripts where the flag
    # is absent (older CLI versions).
    "This session is being continued from a previous conversation",
)


def is_noise(text: str) -> bool:
    """True for harness-injected user lines the human did not type."""
    t = text.lstrip()
    return any(t.startswith(p) for p in _NOISE_PREFIXES)


def _is_harness_user(e: dict) -> bool:
    """True for type=user events the harness authored rather than the human:
    sidechain sub-agent turns and auto-compaction continuation summaries."""
    return bool(e.get("isSidechain") or e.get("isCompactSummary"))


_CMD_NAME_RE = re.compile(r"<command-name>\s*(.*?)\s*</command-name>", re.S)
_CMD_ARGS_RE = re.compile(r"<command-args>\s*(.*?)\s*</command-args>", re.S)


def render_slash_command(text: str) -> str | None:
    """If a user message is a slash-command invocation, render it as the human
    typed it (e.g. ``/reload-plugins`` or ``/loop 5m /foo``). The harness wraps
    these in <command-name>/<command-args> tags, so they'd otherwise be dropped
    as noise even though the user typed them. Returns None if not a command.
    """
    m = _CMD_NAME_RE.search(text)
    if not m:
        return None
    name = m.group(1).strip()
    if not name:
        return None
    if not name.startswith("/"):
        name = "/" + name
    a = _CMD_ARGS_RE.search(text)
    args = a.group(1).strip() if a else ""
    return f"{name} {args}".strip()


def clean_prompt(content: object) -> str | None:
    """The human-typed prompt for a ``user`` event's ``message.content``, or None.

    Returns None when the content is not a plain string (list content is a
    tool_result, never a prompt) or is harness noise. Slash commands are
    surfaced as the human typed them rather than dropped.
    """
    if not isinstance(content, str):
        return None  # list content == tool_result; ignore
    cmd = render_slash_command(content)
    if cmd is not None:
        return cmd
    text = content.strip()
    if not text or is_noise(text):
        return None
    return text


def assistant_text(msg: dict) -> str:
    """The joined text blocks of one assistant message (tool_use blocks dropped)."""
    blocks = msg.get("content", [])
    if not isinstance(blocks, list):
        return ""
    texts = [
        b.get("text", "")
        for b in blocks
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    return "".join(texts).strip()


def _events(path: str | Path):
    """Yield parsed JSON events from a Claude .jsonl, tolerating bad lines."""
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def iter_messages(path: str | Path) -> list[tuple[str, str]]:
    """Ordered ``[("U", human_input) | ("A", assistant_block), ...]``.

    Keeps EVERY assistant prose block (not just the final one) — the substrate
    harvest's ``full`` strip mode and digests build on. Tool calls/results,
    sidechains, and harness noise are dropped; slash commands are surfaced.
    """
    out: list[tuple[str, str]] = []
    for e in _events(path):
        if _is_harness_user(e):
            continue
        kind = e.get("type")
        msg = e.get("message") if isinstance(e.get("message"), dict) else {}
        if kind == "user":
            prompt = clean_prompt(msg.get("content"))
            if prompt is not None:
                out.append(("U", prompt))
        elif kind == "assistant":
            for b in msg.get("content", []) or []:
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip():
                    out.append(("A", b["text"].strip()))
    return out


@dataclass
class Turn:
    """A human prompt paired with the final assistant reply that followed it."""

    prompt: str
    response: str  # joined text of the last assistant message; "" if none


def synthesize(path: str | Path) -> tuple[str, list[Turn]]:
    """Reduce a transcript to ``(session_id, [Turn, ...])`` — one Turn per human
    prompt, the response being the final assistant message before the next prompt.

    This mirrors the share-session uploader's reduction exactly so the wire
    format (and the server's dedup hash) is byte-stable.
    """
    session_id = ""
    turns: list[Turn] = []
    cur_prompt: str | None = None
    pending_response = ""

    for e in _events(path):
        kind = e.get("type")
        if kind == "system" and e.get("subtype") == "init":
            session_id = e.get("session_id", "") or session_id
            continue
        if _is_harness_user(e):
            continue
        msg = e.get("message") if isinstance(e.get("message"), dict) else {}

        if kind == "assistant":
            joined = assistant_text(msg)
            if joined:
                pending_response = joined  # latest assistant message wins
            continue

        if kind == "user":
            prompt = clean_prompt(msg.get("content"))
            if prompt is None:
                continue
            if cur_prompt is not None:
                turns.append(Turn(cur_prompt, pending_response))
            cur_prompt = prompt
            pending_response = ""

    if cur_prompt is not None:
        turns.append(Turn(cur_prompt, pending_response))
    return session_id, turns


def timespan(path: str | Path) -> tuple[str | None, str | None]:
    """The first and last event timestamps (ISO-8601 strings) in a raw
    transcript, or ``(None, None)`` if none carry a ``timestamp``.

    Lets a share surface show a session's *when* and *how long* without
    shipping the full transcript — the reduced upload drops per-event
    timestamps, so these are captured here and sent as metadata instead.
    """
    first: str | None = None
    last: str | None = None
    for e in _events(path):
        ts = e.get("timestamp")
        if isinstance(ts, str) and ts:
            if first is None:
                first = ts
            last = ts
    return first, last


def _parse_ts(ts: str):
    """Parse an ISO-8601 timestamp (tolerating a trailing ``Z``), or None."""
    import datetime as _dt

    try:
        return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def active_seconds(path: str | Path, idle_gap_seconds: int = 1800) -> int:
    """Estimated *active* duration of a session, in seconds.

    Sums the gaps between consecutive events, but caps each gap at
    ``idle_gap_seconds`` (default 30 min) so a session left open overnight
    doesn't read as 14 hours of work. This is the honest "how long did this
    take" number; the raw first→last span (see ``timespan``) is wall-clock and
    includes idle time.
    """
    times = []
    for e in _events(path):
        ts = e.get("timestamp")
        if isinstance(ts, str) and ts:
            parsed = _parse_ts(ts)
            if parsed is not None:
                times.append(parsed)
    if len(times) < 2:
        return 0
    total = 0.0
    for a, b in zip(times, times[1:]):
        gap = (b - a).total_seconds()
        if gap > 0:
            total += min(gap, idle_gap_seconds)
    return int(total)


def to_share_jsonl(session_id: str, turns: list[Turn]) -> tuple[bytes, int]:
    """Re-emit a minimal Claude-format .jsonl (init + user/assistant text lines)
    that canopy-web's session parser reads back into exactly these turns.

    Returns ``(bytes, message_count)`` where message_count counts the emitted
    user + assistant lines. NUL bytes are stripped (the server rejects them).
    """
    out = [json.dumps({"type": "system", "subtype": "init", "session_id": session_id})]
    n = 0
    for i, turn in enumerate(turns):
        prompt = turn.prompt.replace("\x00", "")
        out.append(json.dumps({"type": "user", "message": {"content": prompt}}))
        n += 1
        if turn.response:
            response = turn.response.replace("\x00", "")
            out.append(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"id": f"a{i}", "content": [{"type": "text", "text": response}]},
                    }
                )
            )
            n += 1
    return ("\n".join(out) + "\n").encode("utf-8"), n
