"""Load, save, deduplicate, and query observation YAML files."""
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml


def create_observation(
    obs_type: str,
    description: str,
    severity: str,
    session_id: str,
    related_servers: list[str] | None = None,
    lifecycle_stage: str | None = None,
) -> dict[str, Any]:
    """Create a new observation dict."""
    return {
        "id": uuid4().hex[:12],
        "type": obs_type,
        "description": description,
        "severity": severity,
        "frequency": 1,
        "sessions": [session_id],
        "related_servers": related_servers or [],
        "lifecycle_stage": lifecycle_stage,
        "status": "pending",
        "created": date.today().isoformat(),
    }


def save_observation(obs: dict, obs_dir: Path) -> Path:
    """Save an observation to a YAML file."""
    obs_dir.mkdir(parents=True, exist_ok=True)
    path = obs_dir / f"{obs['id']}.yaml"
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(obs, f, default_flow_style=False, sort_keys=False)
    return path


def load_observation(path: Path) -> dict | None:
    """Load an observation from a YAML file. Returns None on parse error."""
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except (yaml.YAMLError, OSError):
        return None


def list_observations(
    obs_dir: Path,
    obs_type: str | None = None,
    status: str | None = None,
) -> list[dict]:
    """List observations, optionally filtered by type and/or status."""
    if not obs_dir.exists():
        return []
    results = []
    for path in sorted(obs_dir.glob("*.yaml")):
        obs = load_observation(path)
        if obs is None:
            continue
        if obs_type and obs.get("type") != obs_type:
            continue
        if status and obs.get("status") != status:
            continue
        results.append(obs)
    return results


def find_matching_observation(
    new_obs: dict,
    existing: list[dict],
) -> dict | None:
    """Find an existing observation that matches the new one.
    Matching is by type + related_servers + lifecycle_stage.
    """
    for obs in existing:
        if (
            obs.get("type") == new_obs.get("type")
            and set(obs.get("related_servers", [])) == set(new_obs.get("related_servers", []))
            and obs.get("lifecycle_stage") == new_obs.get("lifecycle_stage")
            and obs.get("status") == "pending"
        ):
            return obs
    return None


def merge_observation(existing: dict, session_id: str) -> dict:
    """Merge a new sighting into an existing observation."""
    merged = {**existing}
    merged["frequency"] = existing["frequency"] + 1
    merged["sessions"] = existing["sessions"] + [session_id]
    if merged["frequency"] >= 5 and merged["severity"] == "low":
        merged["severity"] = "high"
    elif merged["frequency"] >= 3 and merged["severity"] == "low":
        merged["severity"] = "medium"
    return merged
