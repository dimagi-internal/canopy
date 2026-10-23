"""Discover, read, and parse Claude Code transcript JSONL files."""
import json
from pathlib import Path


def read_transcript(path: Path) -> list[dict]:
    """Read a transcript JSONL file, filtering out file-history-snapshots."""
    if not path.exists():
        return []
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            if entry.get("type") != "file-history-snapshot":
                entries.append(entry)
    return entries


def extract_user_messages(entries: list[dict]) -> list[str]:
    """Extract human-authored user messages (not tool results)."""
    messages = []
    for entry in entries:
        if entry.get("type") != "user":
            continue
        msg = entry.get("message", {})
        if isinstance(msg, dict):
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                messages.append(content)
            # Skip tool_result content blocks
    return messages


def extract_tool_calls(entries: list[dict]) -> list[dict]:
    """Extract tool calls from assistant messages, paired with their results."""
    # First pass: collect all tool calls
    calls = {}
    for entry in entries:
        if entry.get("type") != "assistant":
            continue
        msg = entry.get("message", {})
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                calls[block["id"]] = {
                    "name": block["name"],
                    "input": block.get("input", {}),
                    "result": None,
                }

    # Second pass: match tool results
    for entry in entries:
        if entry.get("type") != "user":
            continue
        msg = entry.get("message", {})
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tool_id = block.get("tool_use_id")
                if tool_id in calls:
                    calls[tool_id]["result"] = block.get("content", "")
                    # The harness already recorded whether the call failed. Keep it:
                    # every consumer that instead re-derives failure by grepping the
                    # result text inherits the false positives that guessing implies
                    # (a `PASS …` line, a `-> 200`, a URL containing `-404-`).
                    # None when the block omits it — older transcripts predate the
                    # flag, and "unknown" must stay distinguishable from "succeeded".
                    calls[tool_id]["is_error"] = block.get("is_error")

    return list(calls.values())


def extract_assistant_text(entries: list[dict]) -> list[str]:
    """Extract text blocks from assistant messages."""
    texts = []
    for entry in entries:
        if entry.get("type") != "assistant":
            continue
        msg = entry.get("message", {})
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
    return texts


def get_session_id(entries: list[dict]) -> str | None:
    """Extract session ID from the last-prompt entry."""
    for entry in entries:
        if entry.get("type") == "last-prompt":
            return entry.get("sessionId")
    return None
