"""Scan ~/.claude/projects/ for transcripts and extract metadata."""
import json
from pathlib import Path

from orchestrator.repo_map import resolve_repo
from orchestrator.transcripts import read_transcript, get_session_id


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
