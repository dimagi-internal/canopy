"""Construct prompts and invoke claude -p to generate proposals from observations."""
import subprocess
import sys

import yaml
from orchestrator.llm_output import parse_yaml_list

from orchestrator.prompts import load_prompt


def build_proposal_prompt(
    observations: list[dict],
    skill_catalog: str = "",
) -> str:
    """Build the full prompt for proposal generation.

    `skill_catalog` is a pre-formatted string listing existing skills (see
    `orchestrator.skill_catalog.format_for_prompt`) — passed in so the LLM
    can avoid proposing duplicates of skills that already exist.
    """
    observations_yaml = yaml.dump(observations, default_flow_style=False)
    return load_prompt(
        "propose",
        observations_yaml=observations_yaml,
        skill_catalog=skill_catalog or "(catalog unavailable)",
    )


def parse_proposal_output(output: str) -> list[dict]:
    """Parse YAML proposal list from Claude's output (preamble-tolerant)."""
    return parse_yaml_list(output)


def generate_proposals(
    observations: list[dict],
    model: str = "sonnet",
    max_budget_usd: float = 0.50,
    skill_catalog: str = "",
    timeout: int = 240,
) -> list[dict]:
    """Generate proposals by invoking claude -p. Returns list of proposals.

    On failure (timeout, nonzero exit, unparseable output) prints a one-line
    diagnostic to stderr so the caller can tell *why* nothing came back —
    silent empty results were impossible to debug in production.
    """
    prompt = build_proposal_prompt(observations, skill_catalog)

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
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print(f"proposer: claude -p timed out after {timeout}s", file=sys.stderr)
        return []

    if result.returncode != 0:
        stderr_tail = (result.stderr or "")[-500:].strip()
        print(
            f"proposer: claude -p exited {result.returncode}; stderr tail: {stderr_tail!r}",
            file=sys.stderr,
        )
        return []

    if not result.stdout.strip():
        stderr_tail = (result.stderr or "")[-500:].strip()
        print(
            f"proposer: claude -p returned empty stdout (rc=0); stderr tail: {stderr_tail!r}",
            file=sys.stderr,
        )
        return []

    parsed = parse_proposal_output(result.stdout)
    if not parsed:
        snippet = result.stdout.strip()[:300]
        print(
            f"proposer: claude -p returned unparseable output; first 300 chars: {snippet!r}",
            file=sys.stderr,
        )
    return parsed
