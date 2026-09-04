"""Load, save, and query transcript labels."""
from pathlib import Path

import yaml

QUALITY_VALUES = [
    "unlabeled", "went-well", "had-friction", "disaster",
    "skip-coding", "skip-setup", "good-for-eval",
]

DEFAULT_LABEL = {
    "quality": "unlabeled",
    "use_case_tags": [],
    "eval_candidate": False,
    "notes": "",
}


def load_labels(path: Path) -> dict:
    """Load all labels from a YAML file."""
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except yaml.YAMLError:
        return {}


def save_label(
    path: Path,
    session_id: str,
    quality: str | None = None,
    use_case_tags: list[str] | None = None,
    eval_candidate: bool | None = None,
    notes: str | None = None,
) -> None:
    """Save or update a label for a session. Merges with existing data."""
    labels = load_labels(path)
    existing = labels.get(session_id, {**DEFAULT_LABEL})

    if quality is not None:
        existing["quality"] = quality
    if use_case_tags is not None:
        existing["use_case_tags"] = use_case_tags
    if eval_candidate is not None:
        existing["eval_candidate"] = eval_candidate
    if notes is not None:
        existing["notes"] = notes

    labels[session_id] = existing
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(labels, f, default_flow_style=False, sort_keys=False)


def get_label(labels: dict, session_id: str) -> dict:
    """Get label for a session, returning defaults if not found."""
    return labels.get(session_id, {**DEFAULT_LABEL})
