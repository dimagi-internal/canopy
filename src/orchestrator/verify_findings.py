"""Deterministic CLI implementation of the verify-findings algorithm.

Replaces the SKILL.md "agent reads markdown then re-implements in Python" loop
with a pure CLI command. The agent calls `canopy verify-findings <ids>` and
gets a triage table back; the LLM-as-judge step is invoked exactly once per
target repo (batched), not per proposal.

Workflow:
  1. Load proposals matching the supplied id-prefixes (or all `pending` if
     `--all-pending`).
  2. Group by `target_repo` (resolved via `repo_paths.resolve_repo_path` so
     short names and legacy hardcoded paths both work). Proposals whose
     target repo isn't on this machine immediately get verdict
     `unverifiable: target repo not on this machine`.
  3. Per resolved repo: fetch origin/main, capture last-14-days commits,
     CHANGELOG head, and a focused grep for symbols mentioned in any of
     the repo's proposals.
  4. One claude -p call per repo with all proposals + corpus → returns
     verdict YAML for each proposal.
  5. For each `shipped` verdict, append a `verified:` block to the proposal
     YAML and flip status to `obsolete`.
  6. Return the verdicts as a list of dicts; the CLI layer prints the
     triage table.
"""
from __future__ import annotations

import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml
from orchestrator.llm_output import parse_yaml_list

from orchestrator.paths import CANOPY_DIR
from orchestrator.prompts import load_prompt
from orchestrator.repo_evidence import (
    SYMBOL_RX as _SYMBOL_RX,
    changelog_head as _changelog_head,
    git_log_recent as _git_log_recent,
    grep_repo as _grep_repo,
)
from orchestrator.repo_paths import resolve_repo_path

PROPOSALS_DIR = CANOPY_DIR / "proposals"


def load_proposals(
    id_prefixes: list[str] | None,
    all_pending: bool,
) -> list[dict]:
    """Read the proposal YAMLs matching the given prefixes."""
    proposals: list[dict] = []
    if not PROPOSALS_DIR.exists():
        return proposals

    if all_pending:
        for path in sorted(PROPOSALS_DIR.glob("*.yaml")):
            try:
                d = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (yaml.YAMLError, OSError):
                continue
            if d and d.get("status") == "pending":
                d["_path"] = str(path)
                proposals.append(d)
        return proposals

    for prefix in id_prefixes or []:
        if len(prefix) < 8:
            continue
        for path in sorted(PROPOSALS_DIR.glob(f"{prefix}*.yaml")):
            try:
                d = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (yaml.YAMLError, OSError):
                continue
            if d:
                d["_path"] = str(path)
                proposals.append(d)
    return proposals


def _extract_symbols(proposals: list[dict]) -> list[str]:
    """Pull backtick-quoted identifiers from each proposal's action text.

    These are the things to grep for: file paths, function names, env vars,
    config keys. The LLM verdict step has access to the full proposal text;
    grep just gives it concrete evidence about whether the symbol is present
    in the current tree.
    """
    out: set[str] = set()
    for p in proposals:
        for field in ("action", "motivation"):
            text = p.get(field) or ""
            for m in _SYMBOL_RX.finditer(text):
                sym = m.group(1).strip()
                if sym and len(sym) <= 80 and "/" not in sym[:1]:
                    out.add(sym)
    return sorted(out)[:30]


def build_corpus(repo: Path, proposals: list[dict]) -> dict:
    """Build the evidence dict the LLM verdict prompt consumes."""
    return {
        "commits": _git_log_recent(repo) or "(no commits in window)",
        "changelog": _changelog_head(repo) or "(no CHANGELOG.md)",
        "grep_results": _grep_repo(repo, _extract_symbols(proposals)) or "(no symbols extracted)",
    }


def _build_verdict_prompt(corpus: dict, proposals: list[dict]) -> str:
    """Render the verdict prompt for one repo's batch of proposals."""
    minimal = [
        {
            "id": p.get("id"),
            "type": p.get("type"),
            "action": p.get("action"),
            "motivation": (p.get("motivation") or "")[:500],
            "created": str(p.get("created", "")),
        }
        for p in proposals
    ]
    return load_prompt(
        "verify-findings",
        commits=corpus["commits"],
        changelog=corpus["changelog"],
        grep_results=corpus["grep_results"],
        proposals_yaml=yaml.dump(minimal, default_flow_style=False, sort_keys=False),
    )


def _parse_verdict_output(output: str) -> list[dict]:
    """Verdict list out of the grader's answer (preamble-tolerant)."""
    return parse_yaml_list(output)


def call_llm_for_verdicts(
    corpus: dict,
    proposals: list[dict],
    model: str = "sonnet",
    max_budget_usd: float = 0.50,
    timeout: int = 120,
) -> list[dict]:
    """Invoke claude -p to grade a batch of proposals against one repo's corpus."""
    if not proposals:
        return []
    prompt = _build_verdict_prompt(corpus, proposals)
    try:
        result = subprocess.run(
            ["claude", "-p", prompt,
             "--model", model,
             "--max-budget-usd", str(max_budget_usd),
             "--no-session-persistence"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print(f"verify-findings: claude -p timed out after {timeout}s",
              file=sys.stderr)
        return []
    if result.returncode != 0:
        print(f"verify-findings: claude -p exited {result.returncode}; "
              f"stderr tail: {(result.stderr or '')[-500:].strip()!r}",
              file=sys.stderr)
        return []
    return _parse_verdict_output(result.stdout)


def update_proposal_yaml(proposal: dict, verdict: dict) -> bool:
    """Persist a `shipped` verdict back to the proposal file.

    Returns True if the file was updated. Other verdicts don't mutate the
    file — they're for the triage table only, and the agent may still want
    to act on `partial`/`open` cases.
    """
    if verdict.get("verdict") != "shipped":
        return False
    path = Path(proposal.get("_path", ""))
    if not path.exists():
        return False
    try:
        d = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return False
    d["status"] = "obsolete"
    d["verified"] = {
        "date": datetime.now(timezone.utc).date().isoformat(),
        "by": "verify-findings",
        "shipped_at": verdict.get("shipped_at"),
        "shipped_in_version": verdict.get("shipped_in_version"),
        "evidence": verdict.get("evidence"),
    }
    path.write_text(yaml.dump(d, default_flow_style=False, sort_keys=False), encoding="utf-8")
    return True


def verify(
    id_prefixes: list[str] | None = None,
    all_pending: bool = False,
    model: str = "sonnet",
    max_budget_usd: float = 0.50,
) -> dict:
    """Top-level entry point. Returns a dict with verdicts + summary counts."""
    proposals = load_proposals(id_prefixes, all_pending)
    if not proposals:
        return {"verdicts": [], "summary": {"shipped": 0, "partial": 0, "open": 0,
                                            "unverifiable": 0, "total": 0}}

    # Group by resolved local path (None → unverifiable).
    by_repo: dict[Path | None, list[dict]] = defaultdict(list)
    for p in proposals:
        target = p.get("target_repo") or ""
        local = resolve_repo_path(target)
        by_repo[local].append(p)

    verdicts: list[dict] = []

    # Unresolved proposals: short-circuit with `unverifiable`.
    for p in by_repo.pop(None, []):
        verdicts.append({
            "id": p.get("id"),
            "verdict": "unverifiable",
            "evidence": (
                f"target repo {p.get('target_repo')!r} not on this machine "
                "(no match under any known emdash root)"
            ),
            "shipped_at": None,
            "shipped_in_version": None,
            "_path": p.get("_path"),
        })

    # Resolved repos: fetch + corpus + LLM verdict per repo.
    for local, group in by_repo.items():
        if local is None:
            continue
        try:
            subprocess.run(["git", "-C", str(local), "fetch", "origin", "main"],
                           capture_output=True, timeout=15)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
        corpus = build_corpus(local, group)
        llm_verdicts = call_llm_for_verdicts(
            corpus, group, model=model, max_budget_usd=max_budget_usd,
        )
        # Index by id; proposals not graded by the LLM fall back to unverifiable.
        graded = {v.get("id"): v for v in llm_verdicts if isinstance(v, dict) and v.get("id")}
        for p in group:
            v = graded.get(p.get("id"))
            if not v:
                v = {
                    "id": p.get("id"),
                    "verdict": "unverifiable",
                    "evidence": "(LLM did not grade this proposal — see stderr)",
                    "shipped_at": None,
                    "shipped_in_version": None,
                }
            v["_path"] = p.get("_path")
            verdicts.append(v)

    # Persist `shipped` verdicts back to the YAMLs.
    for v in verdicts:
        prop = next((p for p in proposals if p.get("id") == v.get("id")), None)
        if prop:
            update_proposal_yaml(prop, v)

    summary = {"shipped": 0, "partial": 0, "open": 0, "unverifiable": 0,
               "total": len(verdicts)}
    for v in verdicts:
        kind = v.get("verdict", "unverifiable")
        summary[kind] = summary.get(kind, 0) + 1

    return {"verdicts": verdicts, "summary": summary}
