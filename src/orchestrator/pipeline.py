"""Orchestrate the full improvement cycle."""
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from orchestrator.analyzer import analyze_transcript
from orchestrator.circuit_breaker import CircuitBreaker
from orchestrator.observations import (
    create_observation,
    find_matching_observation,
    list_observations,
    merge_observation,
    save_observation,
    load_observation,
)
from orchestrator.proposals import (
    create_proposal,
    list_proposals,
    save_proposal,
    update_proposal_status,
)
from orchestrator.proposer import generate_proposals
from orchestrator.rate_limiter import RateLimiter
from orchestrator.run_log import create_run_entry, save_run, get_last_run_ts
from orchestrator.scanner import scan_all_transcripts


@dataclass
class CycleConfig:
    max_transcripts: int = 10
    max_proposals: int = 3
    observe_only: bool = False
    dry_run: bool = False
    model: str = "sonnet"
    analysis_budget: float = 0.50
    proposal_budget: float = 0.50
    max_failures: int = 3
    max_calls_per_hour: int = 30


def run_cycle(
    state_dir: Path,
    config: CycleConfig | None = None,
) -> dict:
    """Run one full improvement cycle. Returns the run log entry.

    The cycle stops at proposals. Do not add an implement stage that shells out
    to `claude -p`: from inside Claude Code its changes vanish while the exit
    code stays 0. Implementation is done by the `/canopy:improve` skill
    (plugins/canopy/skills/improve/SKILL.md), which dispatches worktree-isolated
    agents and checks that each returned a PR URL.
    """
    config = config or CycleConfig()
    run = create_run_entry()

    obs_dir = state_dir / "observations"
    proposals_dir = state_dir / "proposals"
    runs_dir = state_dir / "runs"

    breaker = CircuitBreaker(max_failures=config.max_failures)
    limiter = RateLimiter(max_calls_per_hour=config.max_calls_per_hour)

    # 1. Collect transcripts via scanner (direct discovery)
    projects_dir = Path.home() / ".claude" / "projects"
    last_ts = get_last_run_ts(runs_dir)
    processed = {
        s for r in (runs_dir.glob("run-*.yaml") if runs_dir.exists() else [])
        for s in _load_processed_sessions(r)
    }

    all_transcripts = scan_all_transcripts(projects_dir)
    transcripts = []
    for t in all_transcripts:
        sid = t["session_id"]
        if sid in processed:
            continue
        transcripts.append(t)
    transcripts = transcripts[:config.max_transcripts]

    # 2. Analyze each transcript
    all_new_observations = []
    analyzed_count = 0
    for t in transcripts:
        if breaker.is_open:
            run["errors"].append(f"Circuit breaker open: {breaker.open_reason}")
            break
        if not limiter.can_proceed():
            run["errors"].append(f"Rate limit reached: {limiter.summary()}")
            break

        limiter.record_call()
        try:
            observations = analyze_transcript(
                Path(t["path"]),
                model=config.model,
                max_budget_usd=config.analysis_budget,
            )
            breaker.record_success()
        except Exception as e:
            breaker.record_failure(f"Analysis error for {t['session_id']}: {e}")
            observations = []

        run["processed_sessions"].append(t["session_id"])
        analyzed_count += 1

        for obs_data in observations:
            obs = create_observation(
                obs_type=obs_data.get("type", "gap"),
                description=obs_data.get("description", ""),
                severity=obs_data.get("severity", "medium"),
                session_id=t["session_id"],
                related_servers=obs_data.get("related_servers", []),
                lifecycle_stage=obs_data.get("lifecycle_stage"),
            )
            all_new_observations.append(obs)

    run["transcripts_analyzed"] = analyzed_count

    # 3. Deduplicate against existing observations
    existing = list_observations(obs_dir, status="pending")
    for new_obs in all_new_observations:
        match = find_matching_observation(new_obs, existing)
        if match:
            merged = merge_observation(match, new_obs["sessions"][0])
            save_observation(merged, obs_dir)
            run["observations_merged"] = run.get("observations_merged", 0) + 1
        else:
            save_observation(new_obs, obs_dir)
            existing.append(new_obs)
            run["observations_created"] = run.get("observations_created", 0) + 1

    if config.observe_only:
        run["circuit_breaker_tripped"] = breaker.is_open
        run["rate_limit_summary"] = limiter.summary()
        run["completed"] = datetime.now(timezone.utc).isoformat()
        save_run(run, runs_dir)
        return run

    # 4-5. Prioritize and propose
    pending = list_observations(obs_dir, status="pending")
    severity_order = {"high": 0, "medium": 1, "low": 2}
    pending.sort(key=lambda o: (-o.get("frequency", 1), severity_order.get(o.get("severity"), 1)))

    this_run_proposal_ids = []
    if pending:
        proposals_raw = generate_proposals(
            pending[:config.max_proposals * 2],
            model=config.model,
            max_budget_usd=config.proposal_budget,
        )

        # Sort proposals by verification confidence — high first
        confidence_order = {"high": 0, "medium": 1, "low": 2}
        proposals_raw.sort(
            key=lambda p: confidence_order.get(
                (p.get("verification") or {}).get("confidence", "low"), 2
            )
        )

        for p_data in proposals_raw[:config.max_proposals]:
            proposal = create_proposal(
                proposal_type=p_data.get("type", "new_tool"),
                action=p_data.get("action", ""),
                target_repo=p_data.get("target_repo", ""),
                ownership=p_data.get("ownership", "self"),
                motivation=p_data.get("motivation", ""),
                observation_id=p_data.get("observation_id", ""),
                complexity=p_data.get("complexity", "medium"),
                verification=p_data.get("verification"),
            )
            save_proposal(proposal, proposals_dir)
            this_run_proposal_ids.append(proposal["id"])
            run["proposals_generated"] = run.get("proposals_generated", 0) + 1

    # Implementation is handled by Claude Code agents via the /canopy:improve
    # skill, not by the Python pipeline. The pipeline stops after proposals.

    # Report
    run["circuit_breaker_tripped"] = breaker.is_open
    run["rate_limit_summary"] = limiter.summary()
    run["completed"] = datetime.now(timezone.utc).isoformat()
    save_run(run, runs_dir)
    return run


def _load_processed_sessions(run_path: Path) -> list[str]:
    """Load processed session IDs from a run log file."""
    try:
        from orchestrator.run_log import load_run
        r = load_run(run_path)
        return r.get("processed_sessions", [])
    except Exception:
        return []
