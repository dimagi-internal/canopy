---
description: Audit project docs for staleness and coverage gaps, then regenerate CLAUDE.md
argument-hint: [--dry-run | --apply] [--prune]
allowed-tools: [Read, Glob, Grep, Bash, Agent, Write, Edit, AskUserQuestion]
---

# Doc Regeneration

Audit project documentation against actual project state and regenerate CLAUDE.md.

## Arguments

- `--dry-run` (default): Write all output to `docs/.dry-run/` for review. Nothing is committed.
- `--apply` (optional): Create a branch and PR with the regenerated docs.
- `--prune` (optional, with `--apply`): Also archive/delete SUPERSEDED/CONTRADICTED docs instead of only listing them in the PR body for confirmation.

## Process

1. Invoke the `doc-regeneration` skill, passing through any flags above (`$ARGUMENTS`)
2. Execute all four phases (Read Everything → Analyze → Produce Output → Deliver)
3. Present the review report to the user
