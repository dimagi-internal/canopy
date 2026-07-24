---
name: verify-findings
description: Re-verify session-review findings against the current state of their target repos. Catches findings whose fix already shipped between when the review was generated and when the user is about to act on them. ALWAYS invoke at the end of session-review (before presenting the table) and before any implementation work on findings.
---

# Verify Findings

Re-check session-review findings against the current state of their
target repos. Flips proposals whose fix already shipped to
`status: obsolete`, so the user doesn't waste time implementing work
that's already done.

## Why this skill exists

The session-review pipeline observes friction in past Claude Code
sessions and emits proposals. Between proposal-time and act-time, the
target repo's main branch keeps moving — often the very fix the
proposal recommends has already shipped. This was the dominant failure
mode of the May 1 ace review: 11 of 17 findings were already in `main`
at versions `0.10.56`–`0.10.69` before the user saw the table.
Implementing them again would have been wasted work.

## What this skill does (canopy ≥ v0.2.78)

**This is now a CLI command.** The verification algorithm lives in
`src/orchestrator/verify_findings.py` and is invoked deterministically
via `canopy verify-findings`. The skill body below is a thin wrapper —
its only job is to invoke the CLI with the right arguments and report
the result.

Earlier versions of this skill described the algorithm in markdown and
asked the agent to re-implement it in Python every run. That cost
~10 min of agent time per cycle and introduced a class of "agent
improvises and gets it wrong" failures. The CLI version replaces all of
that with one subprocess call.

## When to invoke

- **End of session-review (REQUIRED).** Before the agent presents the
  ranked findings table, drop proposals whose verdict is `shipped`,
  annotate `partial` ones, let `open` ones pass through.
- **Before acting on findings (REQUIRED).** When the user signals
  "do these" — `auto-improve`, "implement #5", "do all of them" —
  re-verify the targeted set first. The user may have come back
  hours/days after the review; the world has moved.
- **On demand.** `/canopy:verify-findings <id-prefix>...` lets the
  user re-check specific proposals without running a full review.

## Inputs

Either:

- A space-separated list of proposal-id prefixes (8+ chars each;
  matches `~/.claude/canopy/proposals/<prefix>*.yaml`).
- The literal `--all-pending` to verify every proposal whose status is
  currently `pending`. Default if no other arg is provided.

## How to invoke

Resolve the canopy runtime (via `scripts/canopy-runtime.sh`) and run
the CLI:

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_DIR="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }

# Specific proposals
uv run --project "$CANOPY_DIR" canopy verify-findings <id1> <id2> ... [--json-output]

# Or all pending
uv run --project "$CANOPY_DIR" canopy verify-findings --all-pending [--json-output]
```

Use `--json-output` from session-review's Step 5 (so the agent can parse
the verdicts mechanically). Use the human-readable triage table for
ad-hoc invocations.

## What the CLI does

The implementation mirrors the algorithm this skill used to describe in
prose, but in deterministic Python:

1. **Load proposals** matching the supplied prefixes (or all `pending`).
2. **Group by `target_repo`**, resolving each via
   `orchestrator.repo_paths.resolve_repo_path` — short names (`"ace"`)
   and existing-path strings (`"~/emdash-projects/ace"`) both work.
   Proposals whose target isn't on this machine immediately get verdict
   `unverifiable: target repo not on this machine`.
3. **Build evidence corpus per repo:** fetch origin/main, capture the
   last 14 days of commits, read CHANGELOG.md head, grep the tree for
   backtick-quoted symbols mentioned in the proposals' `action` /
   `motivation`.
4. **One claude -p call per repo** with the corpus + the repo's
   proposal batch — returns a YAML list of verdicts, one per proposal.
5. **Persist `shipped` verdicts** to disk: append a `verified:` block
   and flip `status` to `obsolete`. `partial`/`open`/`unverifiable`
   verdicts don't mutate the YAML — they're for the triage table only.
6. **Emit triage table** (or JSON with `--json-output`):

   ```
   verify-findings: 11 shipped · 1 partial · 5 open · 0 unverifiable (of 17)

   status         id             evidence
   ------------------------------------------------------------------
   shipped        85cbef676ae2   0.10.77 (d968d2e); CHANGELOG hits
   partial        fbeac13427d4   0.10.64 chose HTMX fallback instead
   open           a7f633e915cb   no commit since 2026-05-01; grep ✓
   ```

## Honest limits

- **Equivalent fixes via different solutions register as `partial`,
  not `shipped`.** The LLM judge sees the same evidence the human would
  but errs on the side of recall — read partial-evidence carefully
  before re-implementing.
- **Vague actions are unverifiable.** If the action has no specific
  symbol or file path, there's nothing for grep to confirm. The verdict
  will say `unverifiable` rather than guess.
- **Sub-day commits can race the review.** A fix that lands in the same
  hour as the session-review may already be `shipped` by the time
  verify-findings runs. That's the right outcome — drop the obsolete
  finding.
- **Worktrees are honored via `resolve_repo_path`.** It returns the
  main checkout under whichever emdash root convention this machine
  uses; it does NOT search worktrees, which are per-task and not the
  source of truth for verification.

## Calling from session-review

The session-review agent invokes this skill as Step 5 of its pipeline.
The recommended invocation from the agent:

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_DIR="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
uv run --project "$CANOPY_DIR" canopy verify-findings <ids...> --json-output
```

The agent then parses the JSON `verdicts` array, drops `shipped` rows
from the findings table, and annotates `partial` rows with the evidence
text. `open` and `unverifiable` rows pass through unchanged.
