"""Activity corpus builder and reader."""

from pathlib import Path
from typing import Any

import yaml


def create_corpus_entry(
    entry_id: str,
    domain: str,
    goal: str,
    initial_prompt: str,
    expected_servers: list[str],
    expected_tool_sequence: list[dict],
    outcome: str = "success",
    failure_reason: str | None = None,
    tags: list[str] | None = None,
    prompts: list[str] | None = None,
) -> dict[str, Any]:
    """Create a corpus entry dict."""
    from datetime import date

    entry = {
        "id": entry_id,
        "domain": domain,
        "created": date.today().isoformat(),
        "goal": goal,
        "complexity": "multi-server" if len(expected_servers) > 1 else "single-server",
        "outcome": outcome,
        "failure_reason": failure_reason,
        "initial_prompt": initial_prompt,
        "prompts": prompts or [initial_prompt],
        "expected_servers": expected_servers,
        "expected_tool_sequence": expected_tool_sequence,
        "expected_outcome": {
            "type": "task_completed",
            "validation": goal,
        },
        "tags": tags or [],
        "eval_results": [],
    }
    return entry


def save_corpus_entry(entry: dict, corpus_dir: Path) -> Path:
    """Save a corpus entry to a YAML file."""
    domain_dir = corpus_dir / entry["domain"]
    domain_dir.mkdir(parents=True, exist_ok=True)
    path = domain_dir / f"{entry['id']}.yaml"
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(entry, f, default_flow_style=False, sort_keys=False)
    return path


def load_corpus_entry(path: Path) -> dict:
    """Load a corpus entry from a YAML file."""
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def list_corpus_entries(corpus_dir: Path, domain: str | None = None) -> list[Path]:
    """List all corpus entry files, optionally filtered by domain."""
    if domain:
        search_dir = corpus_dir / domain
        if not search_dir.exists():
            return []
        return sorted(search_dir.glob("*.yaml"))
    return sorted(corpus_dir.rglob("*.yaml"))
