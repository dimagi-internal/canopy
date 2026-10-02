"""Project handoff between agents — the evidence half of `canopy agent handoff`.

A project moving from one agent to another loses everything that is not in the
artifacts: why the framing changed, what the principal rejected, which edit was
his and which was the agent's. That context lives in the SOURCE agent's session
transcripts, and the receiving agent has no index to them — it does not know the
session ids, and the worktree directory names are the source agent's, not its own.

So the first job of a handoff is to FIND those sessions, by the refs the work is
known by (a Gmail thread id, a Doc id, a folder id), and hand the receiver a list
it can read. Everything here is read-only.

(Origin: 2026-10-02 — the first agent-to-agent transfer, ACE → Eva on a funder
concept note. The principal asked for it as "a first class canopy operation";
until then the receiver had to grep ~/.claude/projects by hand to find the
session that did the work.)
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

PROJECTS_ROOT = Path(os.path.expanduser("~/.claude/projects"))


def _first_prompt(path: Path, limit: int = 200) -> str:
    """The session's first real user prompt — what it was dispatched to do."""
    try:
        with path.open(encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("type") != "user":
                    continue
                content = (row.get("message") or {}).get("content")
                if isinstance(content, str) and content.strip():
                    return " ".join(content.split())[:limit]
    except OSError:
        pass
    return ""


def _is_from_agent(path: Path, slug: str, first_prompt: str) -> bool:
    """A session belongs to `slug` if it ran in one of its worktrees or was
    dispatched as one of its slash commands. Both are needed: a resumed session
    keeps its directory but not its argv, and a plain prompt has no command."""
    if f"-worktrees-{slug}-" in path.parent.name or path.parent.name.endswith(f"-{slug}"):
        return True
    return f"<command-name>/{slug}:" in first_prompt or first_prompt.startswith(f"/{slug}:")


def find_sessions(refs, from_slug: str, root: Path | None = None,
                  exclude_session: str = "") -> list[dict]:
    """Every top-level session transcript that mentions any of `refs`.

    Newest first. `from_agent` marks the sessions the source agent ran — the ones
    the receiver must read; the rest are mentions (a sibling's duplicate check, a
    conductor sweep) worth a glance at most. Subagent transcripts are skipped:
    their parent session already matches and is the one to read.
    """
    root = root or PROJECTS_ROOT
    needles = [r for r in (str(x).strip() for x in refs) if r]
    if not needles or not root.is_dir():
        return []
    out = []
    for path in root.glob("*/*.jsonl"):
        if exclude_session and path.stem == exclude_session:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        matched = [n for n in needles if n in text]
        if not matched:
            continue
        first = _first_prompt(path)
        stat = path.stat()
        out.append({
            "session_id": path.stem,
            "path": str(path),
            "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "size_bytes": stat.st_size,
            "matched_refs": matched,
            "from_agent": _is_from_agent(path, from_slug, first),
            "first_prompt": first,
        })
    # from_agent first; within each group, the session that matched MORE refs leads,
    # then the newest. Recency alone ranks a session that merely mentioned the thread
    # (a sibling's duplicate check) above the one that did the work — it did, on the
    # first real run.
    def rank(s):
        return (len(s["matched_refs"]), s["modified"])
    src = sorted([s for s in out if s["from_agent"]], key=rank, reverse=True)
    rest = sorted([s for s in out if not s["from_agent"]], key=rank, reverse=True)
    return src + rest


def _purpose(first_prompt: str) -> str:
    """A short line on what the session was for — the slash command it ran, or
    the opening of the prompt."""
    import re
    m = re.search(r"<command-name>(/[^<]+)</command-name>", first_prompt)
    return m.group(1) if m else first_prompt[:80]


def handoff_notes(from_slug: str, to_slug: str, sessions: list[dict], note: str = "",
                  today: str | None = None) -> str:
    """The receiving project's notes: where it came from and what to read."""
    today = today or datetime.now(timezone.utc).date().isoformat()
    lines = [f"Handed off from {from_slug} to {to_slug} on {today}."]
    if note:
        lines.append(note.strip())
    src = [s for s in sessions if s["from_agent"]]
    if src:
        lines.append(f"Source sessions ({from_slug}) — read before acting:")
        lines += [f"- {s['session_id']} ({s['modified'][:10]}, matched "
                  f"{len(s['matched_refs'])} ref(s)): {_purpose(s['first_prompt'])}"
                  for s in src[:10]]
    else:
        lines.append(f"No {from_slug} session on this machine mentions the refs — "
                     "it may have run on another runner; ask before assuming there is none.")
    return "\n".join(lines)
