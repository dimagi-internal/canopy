#!/usr/bin/env python3
"""CI gate: engine source paths with user-facing authoring implications must be
shipped together with their teaching SKILL.md updates.

Context: in the 14 canopy PRs shipped on 2026-06-01, only #108, #111, #113,
and #115 explicitly touched SKILL.md when they should have. The others left
their best practices undocumented for weeks — PRs #100, #101, #102, #105,
#112, #114 each shipped new spec-author surface (Scene.url, must_succeed,
prefix syntax, snapshot flags, scroll_to cursor glide, Scene.viewport)
without updating SKILL.md. Future agents doing /canopy:ddd inherited the
engine fixes but not the authoring patterns, so the same gaps got audited
and patched in #115 + #116. This gate prevents the next drift cycle.

Usage (as a CLI from GitHub Actions):

    python .github/scripts/docs_sync_check.py \\
        --pr-number "$PR_NUMBER" \\
        --repo "$GITHUB_REPOSITORY"

The script reads changed files + PR body from the GitHub REST API via `gh
api`. It exits 0 on pass (including the opt-out path) and 1 on a real miss.
It prints a markdown-friendly failure block to stdout that GitHub Actions
surfaces in the job log.

It also exits 0 — with a loud SKIPPED message — when the GitHub API cannot be
read at all, because a gate that could not look has made no finding, and a red
check on an unread diff is read as "my PR is wrong" (canopy #496).

For testing, the pure logic lives in `check_docs_sync(changed, pr_body)`
which takes plain inputs and returns a structured result — no subprocess
required.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Iterable


# Mapping of engine source path → required teaching SKILL.md docs.
#
# When a PR touches a key here, every value path MUST also be in the PR's
# changed-files set (or the PR body must carry a `Docs-not-needed:` opt-out
# marker). The three categories below match the structural enforcement plan:
#
#   1. Spec model surface (new Action verb or new Action field) → ddd-spec
#      teaches the verb/field to authors; walkthrough teaches the same on the
#      interactive-recording side.
#   2. Recorder primitives (the engine that interprets actions) — same audience
#      as (1); when a new primitive lands, both author-facing docs need to know.
#   3. record_video CLI flag set → ddd-run orchestrator skill carries the
#      canonical default flag set.
#   4. Concept-eval rubric dimensions → ddd-concept-eval SKILL.md documents the
#      weight, routing, and scope.
TRIGGER_PATHS: dict[str, list[str]] = {
    "scripts/ddd/schemas/models.py": [
        "plugins/canopy/skills/ddd-spec/SKILL.md",
        "plugins/canopy/skills/walkthrough/SKILL.md",
    ],
    # The canonical spec-author surface (UnifiedSpec / Scene / Action) lives
    # here since the narrative-substrate refactor; scripts/ddd/schemas/models.py
    # above is now a re-export shim. Same audience, same required docs.
    "scripts/narrative/models.py": [
        "plugins/canopy/skills/ddd-spec/SKILL.md",
        "plugins/canopy/skills/walkthrough/SKILL.md",
    ],
    "scripts/walkthrough/_lib/recorder.py": [
        "plugins/canopy/skills/ddd-spec/SKILL.md",
        "plugins/canopy/skills/walkthrough/SKILL.md",
    ],
    # The timing model: every dwell/settle/timeout knob (config.py) and the
    # per-scene loop that consumes them (orchestrator.py). Authors tune these
    # through the spec (video_pace / video_recorder_config / hold actions /
    # video_hold_seconds); the walkthrough SKILL's "Recording time & dead
    # space" section is the authoritative map and must move in lockstep.
    "scripts/walkthrough/_lib/config.py": [
        "plugins/canopy/skills/walkthrough/SKILL.md",
    ],
    "scripts/walkthrough/_lib/orchestrator.py": [
        "plugins/canopy/skills/walkthrough/SKILL.md",
    ],
    "scripts/walkthrough/record_video.py": [
        "plugins/canopy/skills/ddd-run/SKILL.md",
    ],
    "plugins/canopy/skills/ddd-concept-eval/rubric.yaml": [
        "plugins/canopy/skills/ddd-concept-eval/SKILL.md",
    ],
}

OPT_OUT_PREFIX = "Docs-not-needed:"


@dataclass
class CheckResult:
    """Outcome of a docs-sync check."""

    passed: bool
    # When the opt-out marker was honored, this captures the reason text for
    # logging — `passed` is True in that case.
    opt_out_reason: str | None = None
    # Per-trigger findings — one entry per source path that was touched but
    # whose required docs weren't all updated.
    missing: list["TriggerMiss"] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return not self.passed


@dataclass
class TriggerMiss:
    trigger: str
    missing_docs: list[str]


def has_opt_out_marker(pr_body: str | None) -> tuple[bool, str | None]:
    """Detect a `Docs-not-needed: <reason>` line in the PR body.

    Returns (present, reason). When present is True, reason is the trimmed
    text after the marker.
    """
    if not pr_body:
        return False, None
    for raw_line in pr_body.splitlines():
        line = raw_line.strip()
        if line.startswith(OPT_OUT_PREFIX):
            reason = line[len(OPT_OUT_PREFIX) :].strip()
            return True, reason or "(no reason given)"
    return False, None


def check_docs_sync(
    changed: Iterable[str],
    pr_body: str | None,
    trigger_paths: dict[str, list[str]] | None = None,
) -> CheckResult:
    """Pure logic: given changed files + PR body, produce a CheckResult.

    No subprocess, no I/O — fully unit-testable.
    """
    triggers = trigger_paths if trigger_paths is not None else TRIGGER_PATHS
    changed_set = set(changed)

    misses: list[TriggerMiss] = []
    for trigger, required_docs in triggers.items():
        if trigger not in changed_set:
            continue
        missing = [d for d in required_docs if d not in changed_set]
        if missing:
            misses.append(TriggerMiss(trigger=trigger, missing_docs=missing))

    if not misses:
        return CheckResult(passed=True)

    # Misses exist — check for the opt-out marker before failing.
    opted_out, reason = has_opt_out_marker(pr_body)
    if opted_out:
        return CheckResult(passed=True, opt_out_reason=reason, missing=misses)

    return CheckResult(passed=False, missing=misses)


def format_failure_message(result: CheckResult) -> str:
    """Human-readable failure block for the GitHub Actions log."""
    lines: list[str] = []
    lines.append(
        "docs-sync: this PR changed source paths that have user-facing"
        " authoring implications, but didn't touch the corresponding skill"
        " docs."
    )
    lines.append("")
    for miss in result.missing:
        lines.append(f"  - {miss.trigger} changed -> also update:")
        for doc in miss.missing_docs:
            lines.append(f"      {doc}")
    lines.append("")
    lines.append(
        "Why this matters: PRs #100, #101, #102, #105, #112, #114 each shipped"
        " new spec-author surface (Scene.url, must_succeed, prefix syntax,"
        " snapshot flags, scroll_to cursor glide, Scene.viewport) without"
        " updating SKILL.md. Future agents doing /canopy:ddd inherited the"
        " engine fixes but not the authoring patterns, so the same gaps got"
        " audited and patched in #115 + #116."
    )
    lines.append("")
    lines.append("To pass:")
    lines.append("  1. Update the listed SKILL.md file(s) to teach the new surface, OR")
    lines.append(
        '  2. Add a line "Docs-not-needed: <one-sentence reason>" to the PR body'
    )
    lines.append(
        "     if this is genuinely an engine-internal change (refactor, perf fix,"
    )
    lines.append("     bug fix that doesn't change the authoring contract).")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI / subprocess plumbing. The `gh` calls themselves only run on GitHub
# Actions, but the retry/skip behaviour around them is unit-tested with a
# stubbed subprocess runner — it is the part that decides a PR's fate when the
# API is down, so it does not get to be the untested part.
# ---------------------------------------------------------------------------


class PRContextUnavailable(RuntimeError):
    """The GitHub API could not be read, so the gate has nothing to evaluate.

    Distinct from a docs-sync MISS. A miss is a finding about the PR; this is
    the absence of the input the finding would be computed from, and the two
    must not look alike to a reader of the Checks tab.
    """


# The GitHub API is not always up. Retries absorb a blip; the skip path below
# absorbs an outage. Kept small — this runs inline in a PR check.
_GH_ATTEMPTS = 3
_GH_BACKOFF_SECONDS = 2.0


def _run_gh(cmd: list[str], *, attempts: int = _GH_ATTEMPTS, sleep=time.sleep) -> str:
    """Run a `gh` command, retrying transient failures, and return stdout.

    Raises PRContextUnavailable (never CalledProcessError) once the attempts
    are spent, carrying gh's own stderr so the log says what actually broke.
    """
    last_err = ""
    for attempt in range(1, attempts + 1):
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            return proc.stdout
        last_err = (proc.stderr or proc.stdout or "").strip()
        if attempt < attempts:
            print(
                f"docs-sync: GitHub API call failed (attempt {attempt}/{attempts}),"
                f" retrying: {last_err.splitlines()[0] if last_err else 'no detail'}"
            )
            sleep(_GH_BACKOFF_SECONDS * attempt)

    raise PRContextUnavailable(
        f"`{' '.join(cmd)}` failed {attempts}x: {last_err or 'no detail'}"
    )


def fetch_pr_context(
    pr_number: str, repo: str, *, attempts: int = _GH_ATTEMPTS, sleep=time.sleep
) -> tuple[list[str], str]:
    """Pull changed file paths + PR body from the GitHub REST API.

    REST rather than `gh pr view --json` (GraphQL): during the 2026-08-17
    incident that produced #496, GraphQL returned 503 while REST stayed up.
    Both are retried; on persistent failure this raises PRContextUnavailable
    and the caller SKIPS the gate rather than failing a PR it never read.
    """
    files_out = _run_gh(
        [
            "gh",
            "api",
            "--paginate",
            f"repos/{repo}/pulls/{pr_number}/files",
            # REST calls this field `filename`; the GraphQL `files` connection
            # this replaced called it `path`. Carrying `.path` over returns a
            # column of nulls -- a 200 that yields no filenames, which without
            # the empty-list guard below would read as "no trigger paths
            # touched" and pass every PR silently.
            "--jq",
            ".[].filename",
        ],
        attempts=attempts,
        sleep=sleep,
    )
    changed = [line for line in files_out.splitlines() if line.strip()]

    # A pull request always changes at least one file, so an empty list is not
    # "nothing was touched" — it is a read that did not work (a truncated page,
    # a jq path that stopped matching, an API returning 200 with no content).
    # Treating it as a clean pass would make the gate silently approve exactly
    # the PRs it exists to catch, so it is reported as unavailable instead.
    if not changed:
        raise PRContextUnavailable(
            f"the API returned no changed files for PR #{pr_number};"
            " a PR always changes at least one file, so this is a failed read,"
            " not an empty diff"
        )

    pr_body = _run_gh(
        ["gh", "api", f"repos/{repo}/pulls/{pr_number}", "--jq", ".body"],
        attempts=attempts,
        sleep=sleep,
    )

    return changed, pr_body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pr-number",
        required=False,
        default=os.environ.get("PR_NUMBER"),
        help="PR number (or set PR_NUMBER env var)",
    )
    parser.add_argument(
        "--repo",
        required=False,
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="owner/repo (or set GITHUB_REPOSITORY env var)",
    )
    parser.add_argument(
        "--changed-files",
        help="Whitespace-, newline- or comma-separated list of changed files "
             "(test helper).",
    )
    parser.add_argument(
        "--pr-body",
        help="PR body text passed directly (test helper).",
    )
    args = parser.parse_args(argv)

    if args.changed_files is not None:
        # Test / local invocation — skip gh entirely.
        #
        # Split on ANY whitespace as well as commas. This used to be
        # comma-or-newline only, which made the natural local invocation —
        # pasting `git diff --name-only` output, which is space-separated once
        # it goes through `tr` or `$(...)` — silently produce ONE path
        # containing spaces. That matched no trigger path, so the gate printed
        # its own success line ("no trigger paths touched") having evaluated
        # nothing, and the PR body then claimed a pass the gate never gave.
        # A gate whose failure mode is a confident false PASS is worse than no
        # gate: CI caught it on the very next run, after the merge.
        changed = [
            p.strip()
            for p in re.split(r"[,\s]+", args.changed_files)
            if p.strip()
        ]
        pr_body = args.pr_body or ""
    else:
        if not args.pr_number or not args.repo:
            print(
                "ERROR: --pr-number and --repo (or PR_NUMBER and"
                " GITHUB_REPOSITORY env) are required when --changed-files"
                " is not given.",
                file=sys.stderr,
            )
            return 2
        try:
            changed, pr_body = fetch_pr_context(args.pr_number, args.repo)
        except PRContextUnavailable as exc:
            # A gate that cannot look must skip LOUDLY, not fail. A red check
            # on a PR whose diff was never read is worse than no check: the
            # safe reading of red is "my PR is wrong", so it costs someone an
            # edit to a correct diff to satisfy a gate that never evaluated it.
            print(f"docs-sync SKIPPED: could not read PR context — {exc}")
            print(
                "::warning::docs-sync could not reach the GitHub API;"
                " the gate was skipped, NOT passed. Re-run this job once the"
                " API recovers if you want the docs check to actually run."
            )
            return 0

    result = check_docs_sync(changed, pr_body)

    if result.passed:
        if result.opt_out_reason is not None:
            # Opted out — log the reason so reviewers can spot misuse.
            triggered = ", ".join(m.trigger for m in result.missing)
            print(
                f"docs-sync skipped: {result.opt_out_reason}"
                f" (triggered by: {triggered})"
            )
        else:
            print("docs-sync: ok (no trigger paths touched, or all docs updated)")
        return 0

    # Failure path — print the structured message and exit 1.
    msg = format_failure_message(result)
    print(msg)
    # GitHub Actions annotation for visibility in the PR's Checks tab.
    summary = "; ".join(
        f"{m.trigger} missing {','.join(m.missing_docs)}" for m in result.missing
    )
    print(f"::error::docs-sync gate failed: {summary}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
