# Agent-Based Improve Pipeline

> **Status: shipped (`a6991a6`) — `/canopy:improve` is the agent-dispatch orchestrator; pipeline stops after proposals. Historical record, not current-state.**

Replace the `claude -p` subprocess implementation pipeline with native Claude Code
agent dispatch. Analysis and proposal generation happen directly in-context.
Implementation happens via parallel agents with worktree isolation.

## Problem

The current `canopy improve` pipeline shells out to `claude -p` subprocesses for
all three stages (analyze, propose, implement). When run from inside Claude Code:

1. **Implementation silently fails** — `claude -p --no-session-persistence` makes
   changes that vanish. Exit code 0 doesn't mean code was committed.
2. **No visibility** — subprocess output is captured, never shown to the user.
3. **Redundant API calls** — Claude calling a CLI that calls Claude back.
4. **Lost context** — each subprocess is isolated; cross-transcript patterns
   can only be caught by post-hoc dedup, not during analysis.

## Design

### Skill as orchestrator

The `/canopy:improve` skill becomes the primary pipeline orchestrator. It runs
six phases directly in the Claude Code session:

**Phase 1 — Discover:** Run `uv run canopy sessions list --json-output` for
transcript metadata. Filter out already-processed sessions (read run logs from
`~/.claude/canopy/runs/`). Take last N (default 10).

**Phase 2 — Analyze (in-context):** Read `registry.yaml` for ecosystem context.
Read each transcript JSONL. Analyze all transcripts together for friction, gaps,
missing capabilities, recurring patterns. Deduplicate against existing
observations on disk. Write observation YAML files.

**Phase 3 — Propose (in-context):** Read pending observations sorted by
frequency + severity. Generate concrete proposals with verification plans.
Write proposal YAML files. Present summary table to user.

**Phase 4 — User gate:** Show proposals, ask which to implement (all, some,
none). Stop here for `observe` or `dry-run` modes.

**Phase 5 — Implement (parallel agents):** For each approved proposal, dispatch
an Agent with `isolation: "worktree"`. Each agent gets the proposal YAML,
observation context, registry context, and target repo path. Instructions:
read code, implement, test, commit on branch, open PR via `gh pr create`.
Agents targeting different repos run in parallel.

**Phase 6 — Verify & report:** Collect agent results. Check that PR URLs were
returned. Update proposal YAML status (implemented with PR URL, or failed with
reason). Update observation status. Write run log. Show final summary with PR
links.

### File changes

| File | Change |
|------|--------|
| `plugins/canopy/skills/improve/SKILL.md` | Complete rewrite — full orchestration logic |
| `plugins/canopy/commands/improve.md` | Update to reflect new flow |
| `src/orchestrator/pipeline.py` | Remove implementation stage; `run_cycle()` stops at propose |
| `src/orchestrator/implementer.py` | Delete |
| Tests referencing implementer | Update or remove |

### What stays the same

- `scanner.py`, `transcripts.py` — transcript discovery and parsing
- `observations.py`, `proposals.py` — data models and storage
- `analyzer.py`, `proposer.py` — kept for CLI-only fallback
- `circuit_breaker.py`, `rate_limiter.py` — still used by CLI path
- `registry.yaml`, all other modules — unchanged

### Data format

No changes to observation/proposal YAML format. New optional field on proposals:
`pr_url` (string) when implemented via agent.

### CLI compatibility

`uv run canopy improve` from the command line still works via the Python pipeline
for analyze + propose. It stops at propose (no implementation). Implementation
requires a Claude Code session where agents are available.
