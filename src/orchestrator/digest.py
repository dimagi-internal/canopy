"""Generate daily digest markdown from recent runs."""
from pathlib import Path

from orchestrator.observations import list_observations
from orchestrator.proposals import list_proposals
from orchestrator.run_log import load_run


def generate_digest(state_dir: Path, write: bool = False) -> str:
    """Generate a markdown digest of recent orchestrator activity."""
    lines = ["# Orchestrator Digest", ""]

    # Recent runs
    runs_dir = state_dir / "runs"
    if runs_dir.exists():
        run_files = sorted(runs_dir.glob("run-*.yaml"))[-5:]
        if run_files:
            lines.append("## Recent Runs")
            lines.append("")
            for rf in run_files:
                run = load_run(rf)
                if run:
                    lines.append(
                        f"- **{run.get('started', '?')}**: "
                        f"{run.get('transcripts_analyzed', 0)} transcripts, "
                        f"{run.get('observations_created', 0)} new observations, "
                        f"{run.get('proposals_implemented', 0)} implemented"
                    )
            lines.append("")

    # Pending observations
    obs_dir = state_dir / "observations"
    pending_obs = list_observations(obs_dir, status="pending")
    if pending_obs:
        pending_obs.sort(key=lambda o: -o.get("frequency", 1))
        lines.append("## Pending Observations")
        lines.append("")
        for obs in pending_obs[:10]:
            lines.append(
                f"- [{obs.get('severity', '?')}] {obs.get('description', '?')} "
                f"(seen {obs.get('frequency', 1)}x)"
            )
        lines.append("")

    # Pending proposals
    proposals_dir = state_dir / "proposals"
    pending_props = list_proposals(proposals_dir, status="pending")
    if pending_props:
        lines.append("## Pending Proposals")
        lines.append("")
        for prop in pending_props:
            lines.append(f"- **{prop.get('type', '?')}**: {prop.get('action', '?')}")
        lines.append("")

    # Recently implemented
    implemented = list_proposals(proposals_dir, status="implemented")
    if implemented:
        lines.append("## Recently Implemented")
        lines.append("")
        for prop in implemented[-5:]:
            lines.append(f"- {prop.get('action', '?')} ({prop.get('target_repo', '?')})")
        lines.append("")

    content = "\n".join(lines)

    if write:
        digest_path = state_dir / "digest.md"
        digest_path.write_text(content, encoding="utf-8")

    return content
