"""AI strategic review of transcripts via claude -p."""
import subprocess
from pathlib import Path

import yaml

from orchestrator.prompts import load_prompt
from orchestrator.transcripts import read_transcript


def build_review_prompt(transcript_path: Path) -> str:
    """Build the review prompt using the same transcript rendering as analyze."""
    import json
    entries = read_transcript(transcript_path)

    parts = []
    for entry in entries:
        msg_type = entry.get("type")
        msg = entry.get("message", {})

        if msg_type == "user":
            content = msg.get("content", "") if isinstance(msg, dict) else ""
            if isinstance(content, str) and content:
                parts.append(f"USER: {content}")
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        result_preview = str(block.get("content", ""))[:200]
                        parts.append(f"TOOL RESULT: {result_preview}")
        elif msg_type == "assistant":
            content = msg.get("content", []) if isinstance(msg, dict) else []
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            parts.append(f"ASSISTANT: {block.get('text', '')[:500]}")
                        elif block.get("type") == "tool_use":
                            parts.append(f"TOOL CALL: {block['name']}({json.dumps(block.get('input', {}))})")

    transcript_text = "\n".join(parts)
    return load_prompt("review", transcript_text=transcript_text)


def run_review(
    transcript_path: Path,
    model: str = "sonnet",
    max_budget_usd: float = 1.00,
) -> str | None:
    """Run a strategic AI review. Returns markdown string or None on failure."""
    prompt = build_review_prompt(transcript_path)

    try:
        result = subprocess.run(
            [
                "claude", "-p", prompt,
                "--model", model,
                "--max-budget-usd", str(max_budget_usd),
                "--no-session-persistence",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return None

    if result.returncode != 0:
        return None

    return result.stdout


def save_review(reviews_dir: Path, session_id: str, content: str) -> Path:
    """Save an AI review to disk."""
    reviews_dir.mkdir(parents=True, exist_ok=True)
    path = reviews_dir / f"{session_id}.yaml"
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump({"session_id": session_id, "content": content}, f,
                  default_flow_style=False, sort_keys=False)
    return path


def load_review(reviews_dir: Path, session_id: str) -> str | None:
    """Load an AI review from disk. Returns the markdown content or None."""
    path = reviews_dir / f"{session_id}.yaml"
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data.get("content") if data else None
    except yaml.YAMLError:
        return None
