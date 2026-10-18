---
description: Bring CLAUDE.md + docs to a compact current-state index (detail in topic docs, history left to GitHub), with size and history guards; incremental on an already-regenerated repo
argument-hint: [--dry-run | --apply] [--prune]
allowed-tools: [Read, Glob, Grep, Bash, Agent, Write, Edit, AskUserQuestion]
---

# Doc Regeneration

Restructure CLAUDE.md into a current-state index, move detail into topic docs, drop history (GitHub holds it), verify every reference, and add size and history guards. On a repo that was already regenerated, review only what changed since the last regen, plus a full reference and history check.

## Arguments

- `--dry-run` (default): Write all output to `docs/.dry-run/` for review. Nothing is committed.
- `--apply` (optional): Create a branch and PR with the regenerated docs. The commit and PR title describe what the diff actually changes.
- `--prune` (optional, with `--apply`): Also delete SUPERSEDED/CONTRADICTED docs (git keeps them) instead of only listing them in the PR body.

Full vs incremental scope is detected by the skill's `regen_status.py`, not passed as a flag.

## Process

1. Invoke the `doc-regeneration` skill, passing through any flags above (`$ARGUMENTS`)
2. Execute all five phases (Read → Analyze → Produce → Verify → Deliver), using the skill's scripts for status, references and history
3. Present the review report to the user, including any step not performed and why
