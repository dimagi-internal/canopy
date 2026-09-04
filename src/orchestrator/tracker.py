"""Self-improvement tracker — logs proposal outcomes for auto-tuning.

Records which observations led to proposals, which proposals were
implemented, and whether they succeeded. Over time, this data lets
the pipeline auto-tune: prioritize high-confidence proposals more
aggressively, deprioritize patterns that don't lead to improvements.
"""
import json
from datetime import datetime, timezone
from pathlib import Path


def record_outcome(
    tracker_path: Path,
    observation_id: str,
    proposal_id: str,
    outcome: str,
    evidence: str = "",
    proposal_type: str = "",
    verification_confidence: str = "",
) -> None:
    """Append an outcome record to the tracker."""
    tracker_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "observation_id": observation_id,
        "proposal_id": proposal_id,
        "outcome": outcome,
        "evidence": evidence,
        "proposal_type": proposal_type,
        "verification_confidence": verification_confidence,
    }
    with open(tracker_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def load_outcomes(tracker_path: Path) -> list[dict]:
    """Load all outcome records."""
    if not tracker_path.exists():
        return []
    outcomes = []
    with open(tracker_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    outcomes.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return outcomes


def compute_success_rates(outcomes: list[dict]) -> dict:
    """Compute success rates by proposal type and verification confidence."""
    by_type: dict[str, dict] = {}
    by_confidence: dict[str, dict] = {}

    for o in outcomes:
        ptype = o.get("proposal_type", "unknown")
        conf = o.get("verification_confidence", "unknown")
        success = o.get("outcome") == "implemented"

        for key, bucket in [(ptype, by_type), (conf, by_confidence)]:
            if key not in bucket:
                bucket[key] = {"total": 0, "success": 0}
            bucket[key]["total"] += 1
            if success:
                bucket[key]["success"] += 1

    rates = {"by_type": {}, "by_confidence": {}}
    for key, data in by_type.items():
        rates["by_type"][key] = data["success"] / data["total"] if data["total"] > 0 else 0
    for key, data in by_confidence.items():
        rates["by_confidence"][key] = data["success"] / data["total"] if data["total"] > 0 else 0

    return rates


def get_prioritization_weights(outcomes: list[dict]) -> dict:
    """Suggest prioritization weights based on track record.

    Returns multipliers for each verification confidence level.
    High success rate → higher weight → prioritized in proposals.
    """
    rates = compute_success_rates(outcomes)
    conf_rates = rates.get("by_confidence", {})

    weights = {}
    for conf in ["high", "medium", "low"]:
        rate = conf_rates.get(conf, 0.5)  # default 50% if no data
        weights[conf] = round(rate * 2, 2)  # scale: 0-1 rate → 0-2 weight

    return weights
