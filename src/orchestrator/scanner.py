"""Scan ~/.claude/projects/ for transcripts and extract metadata."""
import json
import re
from pathlib import Path

from orchestrator.repo_map import resolve_repo
from orchestrator.transcripts import read_transcript, get_session_id


SLASH_NAME_RE = re.compile(r"<command-name>\s*(?P<name>[^<\s]+)\s*</command-name>")
SLASH_ARGS_RE = re.compile(r"<command-args>(?P<args>.*?)</command-args>", re.S)


def summarize_prompt(first_msg: str, width: int = 72) -> str:
    """One legible line describing what a session was ASKED to do.

    A slash-command prompt opens with a fixed preamble
    (`<command-message>`/`<command-name>`/`<command-args>`), so truncating the raw
    text to a display width cuts inside the preamble and every turn of one agent
    renders identically — the scope in `<command-args>` is always the part
    discarded. Measured 2026-09-04: six live hal turns on six different items all
    rendered as `"<command-message>hal:turn</command-message>\n<comma..."`.

    That matters because `agent-core/turn.md` sends a turn here to answer "is a
    sibling already on this?", and several turns of the SAME agent is the only case
    where the roster is needed — precisely the case it could not distinguish.

    So pull the command name and its args out and render `/name args`, falling back
    to the plain prompt when there is no command preamble. Bounded to the passed
    string (the caller's first user message) — never a whole-file scan, which would
    match sibling scopes quoted into a transcript by the duplicate check itself
    (`live-turns.sh`'s fifth failure).
    """
    text = (first_msg or "").strip()
    name = SLASH_NAME_RE.search(text)
    if not name:
        return _clip(" ".join(text.split()), width)
    args_m = SLASH_ARGS_RE.search(text)
    args = " ".join((args_m.group("args") if args_m else "").split())
    label = name.group("name")
    return _clip(f"{label} {args}".strip(), width)


def _clip(text: str, width: int) -> str:
    return text[:width] + "..." if len(text) > width else text


def scan_transcript(path: Path) -> dict:
    """Extract metadata from a single transcript file."""
    entries = read_transcript(path)
    project_key = path.parent.name

    # Count lines (raw, not filtered)
    line_count = sum(1 for _ in open(path, encoding="utf-8"))

    # Extract metadata
    user_msgs = 0
    first_msg = ""
    first_ts = None
    last_ts = None
    mcp_servers = set()
    mcp_call_count = 0

    for entry in entries:
        ts = entry.get("timestamp")
        if ts:
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts

        if entry.get("type") == "user":
            msg = entry.get("message", {})
            if isinstance(msg, dict):
                content = msg.get("content", "")
                if isinstance(content, str) and content:
                    user_msgs += 1
                    if not first_msg:
                        first_msg = content[:500]

        elif entry.get("type") == "assistant":
            msg = entry.get("message", {})
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        name = block.get("name", "")
                        if name.startswith("mcp__"):
                            parts = name.split("__", 2)
                            if len(parts) >= 2:
                                mcp_servers.add(parts[1])
                            mcp_call_count += 1

    session_id = get_session_id(entries) or path.stem

    return {
        "session_id": session_id,
        "path": str(path),
        "project_key": project_key,
        "lines": line_count,
        "user_msgs": user_msgs,
        "first_msg": first_msg,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "mcp_servers": sorted(mcp_servers),
        "mcp_call_count": mcp_call_count,
    }


def scan_all_transcripts(
    projects_dir: Path,
    repo_map: dict | None = None,
    labels: dict | None = None,
) -> list[dict]:
    """Scan all transcript files under projects_dir."""
    repo_map = repo_map or {}
    labels = labels or {}
    results = []

    if not projects_dir.exists():
        return results

    for project_dir in sorted(projects_dir.iterdir()):
        if not project_dir.is_dir():
            continue
        for jsonl in sorted(project_dir.glob("*.jsonl")):
            try:
                meta = scan_transcript(jsonl)
                # Direct lookup first; fall back to emdash-path inference so
                # worktree sessions whose hook never captured them still
                # resolve to the right `owner/repo`. Surfaced when a strict
                # `repo == "jjackson/ace"` filter found only 2 of 8 known
                # ace worktree sessions because the others' worktrees were
                # deleted before the hook fired.
                meta["repo"] = resolve_repo(repo_map, project_dir.name)
                meta["label"] = labels.get(meta["session_id"], {
                    "quality": "unlabeled",
                    "use_case_tags": [],
                    "eval_candidate": False,
                    "notes": "",
                })
                results.append(meta)
            except Exception:
                continue

    return results
