"""Resolve the "obvious" DDD narrative to work on.

When `/canopy:ddd` is invoked with no explicit narrative_slug/run_id, the orchestrator
should pick up whatever narrative was most recently being worked on instead of
erroring or asking. This module scans deterministic local signals — the target
the DDD runs root (external; plus the legacy in-repo `.canopy/ddd/runs/`), its `docs/walkthroughs/*.yaml`
specs, and the current git branch + recent commit subjects — ranks the
candidate narratives, and prints a JSON resolution the agent acts on:

    {"decision": "resume", "narrative_slug": "...", "run_id": "...", ...}   # in-flight run
    {"decision": "new",    "narrative_slug": "...", "spec_path": "...", ...} # fresh run on latest spec
    {"decision": "ask",    "candidates": [...], ...}                  # genuinely ambiguous / nothing

The agent uses `confidence`: on "high" it announces the pick and proceeds; on
"ambiguous"/"none" it confirms with the user (top candidate pre-selected).

Pure scan + rank — no network, no side effects. `narrative_slug`/`run_id` passed
explicitly short-circuit the scan.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml

# A run is "terminal" (done — don't auto-resume) at these phases.
TERMINAL_PHASES = {"uploaded", "promoted"}

# Prefer a git-branch match only when the candidate was touched within this
# window — so an old narrative whose slug happens to appear in the branch name
# doesn't get resurrected over genuinely recent work.
BRANCH_MATCH_WINDOW_S = 14 * 24 * 3600

# Two candidates within this window of each other count as "too close to call"
# → ambiguous (the agent confirms rather than guessing).
AMBIGUITY_WINDOW_S = 6 * 3600


def _git(args: list[str], repo_root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", *args],
            cwd=str(repo_root),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def _slug_tokens(slug: str) -> list[str]:
    """Distinctive (len>=4) tokens of a slug, for fuzzy git-context matching."""
    return [t for t in re.split(r"[^a-z0-9]+", slug.lower()) if len(t) >= 4]


def _scan_runs(ddd_dir: Path) -> dict[str, dict[str, Any]]:
    """Map narrative_slug slug → newest run summary, from runs/*/run_state.yaml."""
    by_narrative_slug: dict[str, dict[str, Any]] = {}
    # Runs live outside the repo now; older ones remain in the in-repo dir, so
    # scan both or resuming a pre-split run silently finds nothing.
    from scripts.ddd.runstate import _legacy_runs_dir, _resolve_runs_dir

    roots = [_resolve_runs_dir(ddd_dir), _legacy_runs_dir(ddd_dir)]
    state_files = [f for r in roots if r.is_dir() for f in r.glob("*/run_state.yaml")]
    if not state_files:
        return by_narrative_slug
    for state_file in state_files:
        try:
            raw = yaml.safe_load(state_file.read_text()) or {}
        except (yaml.YAMLError, OSError):
            continue
        narrative_slug = (raw.get("narrative_slug") or "").strip()
        run_id = (raw.get("run_id") or state_file.parent.name).strip()
        if not narrative_slug:
            continue
        mtime = state_file.stat().st_mtime
        prev = by_narrative_slug.get(narrative_slug)
        if prev is None or mtime > prev["run_mtime"]:
            by_narrative_slug[narrative_slug] = {
                "latest_run_id": run_id,
                "phase": (raw.get("phase") or "").strip() or None,
                "run_mtime": mtime,
            }
    return by_narrative_slug


def _scan_specs(repo_root: Path) -> dict[str, dict[str, Any]]:
    """Map narrative_slug slug → spec summary, from docs/walkthroughs/*.yaml."""
    by_narrative_slug: dict[str, dict[str, Any]] = {}
    specs_dir = repo_root / "docs" / "walkthroughs"
    if not specs_dir.is_dir():
        return by_narrative_slug
    for spec_file in specs_dir.glob("*.yaml"):
        slug = spec_file.stem
        by_narrative_slug[slug] = {
            "spec_path": str(spec_file),
            "spec_mtime": spec_file.stat().st_mtime,
        }
    return by_narrative_slug


def resolve(
    ddd_dir: Path,
    repo_root: Path,
    *,
    narrative_slug: str | None = None,
    run_id: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Resolve which narrative `/canopy:ddd` should run. See module docstring."""
    now = time.time() if now is None else now

    # Explicit run_id always wins → resume it.
    if run_id:
        return {
            "decision": "resume",
            "narrative_slug": narrative_slug,
            "run_id": run_id,
            "confidence": "high",
            "reason": f"explicit --resume {run_id}",
            "candidates": [],
        }

    runs = _scan_runs(ddd_dir)
    specs = _scan_specs(repo_root)

    # Explicit narrative_slug → resume its newest non-terminal run, else start fresh.
    if narrative_slug:
        r = runs.get(narrative_slug)
        if r and r["phase"] not in TERMINAL_PHASES:
            return {
                "decision": "resume",
                "narrative_slug": narrative_slug,
                "run_id": r["latest_run_id"],
                "phase": r["phase"],
                "confidence": "high",
                "reason": f"explicit narrative_slug '{narrative_slug}', resuming in-progress run (phase={r['phase']})",
                "candidates": [],
            }
        return {
            "decision": "new",
            "narrative_slug": narrative_slug,
            "spec_path": specs.get(narrative_slug, {}).get("spec_path"),
            "confidence": "high",
            "reason": f"explicit narrative_slug '{narrative_slug}', no in-progress run — start fresh",
            "candidates": [],
        }

    # No explicit target → build the candidate set from runs ∪ specs.
    slugs = set(runs) | set(specs)
    # `symbolic-ref` resolves the branch name even on an unborn branch (no
    # commits yet); `rev-parse --abbrev-ref` returns "HEAD" there.
    branch = _git(["symbolic-ref", "--short", "HEAD"], repo_root) or _git(
        ["rev-parse", "--abbrev-ref", "HEAD"], repo_root
    )
    commits = _git(["log", "--oneline", "-15"], repo_root)
    context_blob = f"{branch}\n{commits}".lower()

    candidates: list[dict[str, Any]] = []
    for slug in slugs:
        r = runs.get(slug, {})
        s = specs.get(slug, {})
        last_activity = max(
            r.get("run_mtime", 0.0),
            s.get("spec_mtime", 0.0),
        )
        tokens = _slug_tokens(slug)
        candidates.append({
            "narrative_slug": slug,
            "latest_run_id": r.get("latest_run_id"),
            "phase": r.get("phase"),
            "spec_path": s.get("spec_path"),
            "last_activity": last_activity,
            "branch_match": any(t in branch.lower() for t in tokens),
            "context_match": any(t in context_blob for t in tokens),
            "source": "run+spec" if r and s else ("run" if r else "spec"),
        })

    if not candidates:
        return {
            "decision": "ask",
            "narrative_slug": None,
            "confidence": "none",
            "reason": "no DDD runs or docs/walkthroughs specs found in this repo",
            "candidates": [],
            "git": {"branch": branch},
        }

    candidates.sort(key=lambda c: c["last_activity"], reverse=True)

    # A git-branch match within the recency window is the strongest signal — the
    # worktree is literally named after the narrative being worked on.
    branch_recent = [
        c for c in candidates
        if c["branch_match"] and (now - c["last_activity"]) < BRANCH_MATCH_WINDOW_S
    ]
    if branch_recent:
        top = branch_recent[0]
        reason = f"git branch '{branch}' matches narrative '{top['narrative_slug']}'"
        confidence = "high"
    else:
        top = candidates[0]
        runner_up = candidates[1] if len(candidates) > 1 else None
        close = (
            runner_up is not None
            and (top["last_activity"] - runner_up["last_activity"]) < AMBIGUITY_WINDOW_S
        )
        confidence = "ambiguous" if close else "high"
        reason = "most recently active narrative" + (
            " (several touched recently — confirm)" if close else ""
        )

    # Resume an in-flight run; otherwise start a fresh run on the narrative.
    resume = top["latest_run_id"] is not None and top["phase"] not in TERMINAL_PHASES
    decision = "resume" if resume else "new"

    return {
        "decision": decision,
        "narrative_slug": top["narrative_slug"],
        "run_id": top["latest_run_id"] if resume else None,
        "phase": top["phase"],
        "spec_path": top["spec_path"],
        "confidence": confidence,
        "reason": reason,
        "candidates": [
            {
                "narrative_slug": c["narrative_slug"],
                "latest_run_id": c["latest_run_id"],
                "phase": c["phase"],
                "spec_path": c["spec_path"],
                "branch_match": c["branch_match"],
                "source": c["source"],
            }
            for c in candidates[:5]
        ],
        "git": {"branch": branch},
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="scripts.ddd.resolve_narrative",
        description="Resolve the obvious DDD narrative to run when none is passed explicitly.",
    )
    parser.add_argument("--ddd-dir", required=True, help="The repo's .canopy/ddd directory")
    parser.add_argument("--repo-root", required=True, help="Target repo root (for specs + git context)")
    parser.add_argument("--narrative-slug", default=None, help="Explicit narrative slug (short-circuits the scan)")
    parser.add_argument("--run-id", default=None, help="Explicit run_id to resume (short-circuits the scan)")
    args = parser.parse_args(argv)

    result = resolve(
        Path(args.ddd_dir),
        Path(args.repo_root),
        narrative_slug=args.narrative_slug,
        run_id=args.run_id,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
