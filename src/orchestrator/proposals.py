"""Load, save, and query improvement proposal YAML files."""
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml


def create_proposal(
    proposal_type: str,
    action: str,
    target_repo: str,
    ownership: str,
    motivation: str,
    observation_id: str,
    complexity: str = "medium",
    verification: dict | None = None,
) -> dict[str, Any]:
    """Create a new proposal dict."""
    return {
        "id": uuid4().hex[:12],
        "type": proposal_type,
        "action": action,
        "target_repo": target_repo,
        "ownership": ownership,
        "motivation": motivation,
        "observation_id": observation_id,
        "complexity": complexity,
        "verification": verification or {
            "type": "observational",
            "test_description": "",
            "confidence": "low",
        },
        "status": "pending",
        "failure_reason": None,
        "created": date.today().isoformat(),
    }


def save_proposal(proposal: dict, proposals_dir: Path) -> Path:
    """Save a proposal to a YAML file."""
    proposals_dir.mkdir(parents=True, exist_ok=True)
    path = proposals_dir / f"{proposal['id']}.yaml"
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(proposal, f, default_flow_style=False, sort_keys=False)
    return path


def load_proposal(path: Path) -> dict | None:
    """Load a proposal from a YAML file. Returns None on parse error."""
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except (yaml.YAMLError, OSError):
        return None


def list_proposals(
    proposals_dir: Path,
    status: str | None = None,
) -> list[dict]:
    """List proposals, optionally filtered by status."""
    if not proposals_dir.exists():
        return []
    results = []
    for path in sorted(proposals_dir.glob("*.yaml")):
        proposal = load_proposal(path)
        if proposal is None:
            continue
        if status and proposal.get("status") != status:
            continue
        results.append(proposal)
    return results


def update_proposal_status(
    path: Path,
    status: str,
    reason: str | None = None,
) -> None:
    """Update a proposal's status on disk."""
    proposal = load_proposal(path)
    proposal["status"] = status
    if reason:
        proposal["failure_reason"] = reason
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(proposal, f, default_flow_style=False, sort_keys=False)
