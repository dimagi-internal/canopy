"""Session log writer and reader for orchestrator usage tracking."""
import json
from pathlib import Path
from typing import Any


def append_log_entry(log_file: Path, entry: dict[str, Any]) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def read_session_log(log_file: Path) -> list[dict]:
    if not log_file.exists():
        return []
    entries = []
    with open(log_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def group_by_session(entries: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for entry in entries:
        sid = entry.get("session_id", "unknown")
        groups.setdefault(sid, []).append(entry)
    return groups


def classify_sessions(grouped: dict[str, list[dict]]) -> dict[str, list[str]]:
    result = {"single_server": [], "multi_server": []}
    for session_id, entries in grouped.items():
        servers = {e.get("server") for e in entries}
        if len(servers) > 1:
            result["multi_server"].append(session_id)
        else:
            result["single_server"].append(session_id)
    return result


def find_transcript_path(session_id: str, project_path: str) -> Path:
    """Construct Claude Code transcript path. Mangles / to - with leading -."""
    mangled = project_path.lstrip("/").replace("/", "-")
    return Path.home() / ".claude" / "projects" / f"-{mangled}" / f"{session_id}.jsonl"
