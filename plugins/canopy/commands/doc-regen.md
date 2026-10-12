---
description: Bring CLAUDE.md + docs to a compact current-state index (detail in topic docs, history left to GitHub), with a size guard
argument-hint: [--dry-run | --apply] [--prune]
allowed-tools: [Read, Glob, Grep, Bash, Agent, Write, Edit, AskUserQuestion]
---

# Doc Regeneration

Restructure CLAUDE.md into a current-state index, move detail into topic docs, drop history (GitHub holds it), verify every reference, and add a size guard.

## Arguments

- `--dry-run` (default): Write all output to `docs/.dry-run/` for review. Nothing is committed.
- `--apply` (optional): Create a branch and PR with the regenerated docs.
- `--prune` (optional, with `--apply`): Also delete SUPERSEDED/CONTRADICTED docs (git keeps them) instead of only listing them in the PR body.

## Process

1. Invoke the `doc-regeneration` skill, passing through any flags above (`$ARGUMENTS`)
2. Execute all five phases (Read → Analyze → Produce → Verify → Deliver)
3. Present the review report to the user
