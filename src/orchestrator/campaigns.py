"""Campaign persistence — track multi-day improvement arcs.

Inspired by Citadel's campaign system. An improvement campaign tracks
the lifecycle of a specific improvement from observation through
implementation and verification.

Campaigns survive across sessions via markdown files at:
~/.claude/canopy/campaigns/

States: active → implementing → verifying → completed | failed | stalled
"""
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

import yaml


def create_campaign(
    title: str,
    observation_ids: list[str],
    proposal_ids: list[str],
    description: str = "",
) -> dict:
    """Create a new campaign tracking an improvement arc."""
    return {
        "id": uuid4().hex[:12],
        "title": title,
        "description": description,
        "status": "active",
        "created": date.today().isoformat(),
        "updated": datetime.now(timezone.utc).isoformat(),
        "observation_ids": observation_ids,
        "proposal_ids": proposal_ids,
        "phases": [
            {"name": "observed", "completed": True, "ts": datetime.now(timezone.utc).isoformat()},
        ],
        "notes": [],
    }


def save_campaign(campaign: dict, campaigns_dir: Path) -> Path:
    """Save a campaign to a YAML file."""
    campaigns_dir.mkdir(parents=True, exist_ok=True)
    path = campaigns_dir / f"{campaign['id']}.yaml"
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(campaign, f, default_flow_style=False, sort_keys=False)
    return path


def load_campaign(path: Path) -> dict | None:
    """Load a campaign from a YAML file."""
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except (yaml.YAMLError, OSError):
        return None


def list_campaigns(
    campaigns_dir: Path,
    status: str | None = None,
) -> list[dict]:
    """List campaigns, optionally filtered by status."""
    if not campaigns_dir.exists():
        return []
    results = []
    for path in sorted(campaigns_dir.glob("*.yaml")):
        campaign = load_campaign(path)
        if campaign is None:
            continue
        if status and campaign.get("status") != status:
            continue
        results.append(campaign)
    return results


def advance_campaign(
    campaign: dict,
    phase_name: str,
    note: str = "",
) -> dict:
    """Advance a campaign to the next phase."""
    updated = {**campaign}
    updated["phases"].append({
        "name": phase_name,
        "completed": True,
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    updated["updated"] = datetime.now(timezone.utc).isoformat()
    if note:
        updated["notes"].append(note)

    # Auto-set status based on phase
    if phase_name == "completed":
        updated["status"] = "completed"
    elif phase_name == "failed":
        updated["status"] = "failed"
    elif phase_name == "implementing":
        updated["status"] = "implementing"
    elif phase_name == "verifying":
        updated["status"] = "verifying"

    return updated
