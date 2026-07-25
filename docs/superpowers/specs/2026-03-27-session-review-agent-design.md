# Session Review Agent Design

> **Status: shipped — historical record, not current-state.**

**Date:** 2026-03-27
**Status:** Draft

## Problem

Reviewing Claude Code sessions for friction, gaps, and improvement opportunities is
currently manual — you either run `/canopy:select-session` one at a time or run
`/canopy:improve` which is fully autonomous with no synthesis step. There's no way to
say "review the last 10 sessions and tell me what to fix" and get back a prioritized
table with confidence scores and staleness detection.

Additionally, there's no way to know if a session invoked a stale version of a
skill/agent, or whether a previous improvement attempt already targeted the same
friction. This matters when iterating ad hoc on skills — you need to know if the pain
you're seeing is already addressed in a newer version.

## Solution

A new `session-review` agent that:

1. Reviews N recent sessions in batch
2. Detects stale skill/agent versions via explicit capture metadata
3. Cross-references existing observations and proposals to avoid duplicate work
4. Produces a ranked synthesis table with confidence intervals
5. Optionally auto-implements high-confidence proposals

Two modes:
- **Review** (default): Analyze → synthesize → present table → wait for user decision
- **Auto-improve**: Analyze → synthesize → implement proposals above confidence threshold

## Components

### 1. Capture Infrastructure (hook changes)

**File:** `hooks/post_tool_use.py`

Extend the existing post_tool_use hook to emit two new event types in
`session-log.jsonl`:

#### Session start event

On the first tool call for a given `session_id`, emit:

```json
{
  "ts": "2026-03-27T10:00:00Z",
  "session_id": "abc123",
  "event": "session_start",
  "plugin_version": "0.2.21",
  "project": "/Users/jjackson/emdash-projects/canopy"
}
```

**How:** Track seen session IDs in a module-level `set()`. On each hook invocation,
check if the session ID is new. If so, read plugin version from
`~/.claude/plugins/installed_plugins.json` (parse JSON, find canopy entry, extract
version). Cache the version after first read.

#### Skill invoked event

When the tool name is `Skill`, emit:

```json
{
  "ts": "2026-03-27T10:05:00Z",
  "session_id": "abc123",
  "event": "skill_invoked",
  "skill": "canopy:walkthrough",
  "plugin_version": "0.2.21"
}
```

**How:** The hook receives `hook_data` from stdin with `tool_name` and `tool_input`.
For built-in tools, `tool_name` is the tool name string (e.g., `"Skill"`).
Check `tool_name == "Skill"`, then extract `tool_input.get("skill")` for the
skill name.

#### Design constraints

- **Stdlib only.** The hook runs with system python3. JSON parsing only, no PyYAML.
- **Graceful degradation.** If `installed_plugins.json` doesn't exist or can't be
  parsed, set `plugin_version` to `"unknown"`. Never crash the hook.
- **Schema evolution.** All fields are additive. Consumers treat every field as
  optional. Old entries without `plugin_version` mean "version unknown" — analysis
  continues without staleness detection for those sessions.
- **Session ID tracking.** The `set()` resets per-process. If the hook is invoked
  in a new process for the same session, it may emit a duplicate `session_start`.
  This is acceptable — consumers deduplicate by session_id.

### 2. Directory Rename

**`~/.claude/orchestrator/` → `~/.claude/canopy/`**

#### New shared path module

Create `src/orchestrator/paths.py`:

```python
from pathlib import Path

CANOPY_DIR = Path.home() / ".claude" / "canopy"
_LEGACY_DIR = Path.home() / ".claude" / "orchestrator"


def ensure_canopy_dir() -> Path:
    """Return CANOPY_DIR, migrating from legacy path if needed."""
    if _LEGACY_DIR.exists() and not CANOPY_DIR.exists():
        _LEGACY_DIR.rename(CANOPY_DIR)
    CANOPY_DIR.mkdir(parents=True, exist_ok=True)
    return CANOPY_DIR
```

#### Files to update

**Python files (5):**
- `src/orchestrator/cli.py` — 7 references to `Path.home() / ".claude" / "orchestrator"`
- `src/orchestrator/scheduler.py` — 2 references
- `src/orchestrator/campaigns.py` — 1 docstring reference
- `hooks/post_tool_use.py` — 2 references (LOG_FILE, REPO_MAP_FILE)
- `hooks/install.py` — 1 print message

All Python files import `CANOPY_DIR` from `paths.py` instead of hardcoding.
Exception: `hooks/post_tool_use.py` inlines the path (stdlib-only constraint, can't
import from src/).

**Markdown files (6) — doc references only:**
- `docs/superpowers/specs/2026-03-21-transcript-browser-design.md`
- `docs/superpowers/specs/2026-03-20-orchestrator-design.md`
- `docs/superpowers/plans/2026-03-23-phase-2b-intelligence.md`
- `docs/superpowers/plans/2026-03-23-canopy-plugin-merge.md`
- `docs/superpowers/plans/2026-03-20-improvement-loop.md`
- `plugins/canopy/skills/patterns/SKILL.md`

#### Migration behavior

- If `~/.claude/orchestrator/` exists and `~/.claude/canopy/` does not → rename
- If both exist → use `canopy/`, log warning
- If neither exists → create `canopy/`

### 3. Agent Definition

**File:** `plugins/canopy/agents/session-review.md`

#### Frontmatter

```yaml
---
name: session-review
description: Review recent sessions, detect patterns and stale skills, propose improvements with confidence scores
model: inherit
memory: [user]
---
```

#### Memory structure

Location: `~/.claude/agent-memory/session-review/`

```
MEMORY.md                — Index of memory files
reviewed-sessions.md     — Session IDs already reviewed (avoids re-reviewing)
priorities.md            — User's stated priorities and preferences
proposal-history.md      — What was proposed, accepted, rejected, and outcomes
```

- `reviewed-sessions.md`: Append-only list of `session_id | date | project` entries.
  Used to filter out already-reviewed sessions from the fetch step.
- `priorities.md`: Updated when the user expresses preferences ("I care more about
  MCP reliability than skill polish"). Influences ranking in synthesis.
- `proposal-history.md`: Tracks proposals surfaced by this agent and their
  disposition (accepted → implemented, rejected, deferred). Cross-referenced in
  future runs to avoid re-proposing rejected items.

#### Pipeline

**Step 1: Load context**

Read memory files from `~/.claude/agent-memory/session-review/`. Read existing
observations from `~/.claude/canopy/observations/` and proposals from
`~/.claude/canopy/proposals/` to know what's already been observed and attempted.

**Step 2: Fetch sessions**

```bash
uv run canopy sessions list --json-output --hours <H>
```

Or limit by count. Filter out session IDs found in `reviewed-sessions.md`.

**Step 3: Analyze individually**

For each unreviewed session:

```bash
uv run canopy analyze <transcript_path>
```

Collect per-session observations (friction, gaps, issues). Each analysis produces
structured observations with type, severity, description, and related servers.

**Step 4: Check version staleness**

For each session, search `~/.claude/canopy/session-log.jsonl` for entries matching
the session ID:

- Look for `session_start` events → extract `plugin_version`
- Look for `skill_invoked` events → extract skill name + version
- Compare against current plugin version (read from
  `~/.claude/plugins/installed_plugins.json`)
- If session version < current version, flag as stale. Note which skills were
  invoked on the old version.
- If no version metadata exists (historical session), note "version unknown" and
  skip staleness check for that session.

**Step 5: Cross-reference prior work**

For each observation from Step 3:

- Check if a matching observation exists in `~/.claude/canopy/observations/`
  (match by type + related_servers + lifecycle_stage)
- If matched, check if proposals exist for that observation:
  - `status: implemented` → Was the session before or after implementation?
    If before, the friction is expected (stale). If after, the fix didn't work.
  - `status: failed` → Note the failure reason. Confidence for a new attempt
    should be lower unless the approach is different.
  - `status: pending` → Already queued, don't duplicate.
- Check `proposal-history.md` from agent memory — was this previously surfaced
  and rejected by the user? If so, don't re-propose unless severity escalated.

**Step 6: Synthesize table**

Run a single `claude -p` synthesis pass across all findings from Steps 3-5.
The prompt includes:

- All per-session observations
- Version staleness flags
- Prior work cross-references
- User priorities from memory

Output: a ranked table.

| # | Finding | Sessions | Severity | Stale? | Proposed Fix | Confidence | Prior Attempts |
|---|---------|----------|----------|--------|-------------|------------|----------------|
| 1 | Example finding | 3,7,9 | high | Yes (v0.2.19) | Proposed fix description | 85% | Partial fix in v0.2.20 |

**Confidence scoring factors:**

- **High (80-95%):** Clear root cause identified. Similar fix succeeded before or
  fix is straightforward (config change, small code edit). Low complexity.
- **Medium (50-79%):** Root cause identified but fix is non-trivial (new feature,
  architectural change). Or no prior attempt data to calibrate against.
- **Low (20-49%):** Symptom observed but root cause unclear. Or a prior fix for
  the same issue failed. Or the fix requires changes outside our control.

Plus a "Recommended next steps" narrative section prioritizing by
severity × confidence, suggesting which skills/commands to use for implementation.

**Step 7: Record and present**

- Save all reviewed session IDs to `reviewed-sessions.md`
- **Review mode (default):** Present the table to the user via `AskUserQuestion`.
  Wait for their decision on what to act on. Record decisions to
  `proposal-history.md`.
- **Auto-improve mode:** Present the table for visibility, then automatically
  implement all proposals with confidence >= 70%. For each:
  - Create a branch in the target repo
  - Implement the fix
  - Run verification (lint + tests)
  - Create a PR
  - Record outcome to `proposal-history.md`
  - Flag anything below 70% as "needs manual review"

#### Auto-improve specifics

- Confidence threshold: 70% (hardcoded initially, could become configurable)
- Branch naming: `canopy/session-review/<observation-id-short>`
- Never commits to main — always branches + PRs
- If implementation fails (tests don't pass), mark as failed in proposal-history
  and move to next proposal
- At the end, present a summary: N implemented, M failed, K skipped (low confidence)

### 4. Command Definition

**File:** `plugins/canopy/commands/session-review.md`

```yaml
---
description: Review recent sessions, detect stale skills, propose improvements with confidence scores
argument-hint: [<count>|hours <N>|project <name>|auto-improve]
allowed-tools: [Read, Bash, Write, Edit, Glob, Grep, AskUserQuestion, Agent, Skill]
---
```

All invocations route to the `session-review` agent. No skill-only mode.

**Argument parsing (by the agent):**

- `<count>` (integer, default 10): Number of sessions to review
- `hours <N>`: Time window instead of count
- `project <name>`: Filter to sessions from a specific project
- `auto-improve`: Enable auto-implementation of high-confidence proposals
- Arguments can be combined: `15 auto-improve`, `hours 48 project canopy`

**Examples:**

- `/canopy:session-review` → review last 10 sessions, present table
- `/canopy:session-review 20` → review last 20
- `/canopy:session-review hours 48` → last 48 hours
- `/canopy:session-review auto-improve` → review + auto-implement >= 70% confidence
- `/canopy:session-review 15 auto-improve` → 15 sessions + auto-implement

## File inventory

### New files

| File | Purpose |
|------|---------|
| `plugins/canopy/agents/session-review.md` | Agent definition |
| `plugins/canopy/commands/session-review.md` | Command entry point |
| `src/orchestrator/paths.py` | Shared CANOPY_DIR constant + migration |

### Modified files

| File | Change |
|------|--------|
| `hooks/post_tool_use.py` | Add session_start + skill_invoked events, update path |
| `hooks/install.py` | Update log path in print message |
| `src/orchestrator/cli.py` | Import CANOPY_DIR from paths.py |
| `src/orchestrator/scheduler.py` | Import CANOPY_DIR from paths.py |
| `src/orchestrator/campaigns.py` | Update docstring path |
| `plugins/canopy/skills/patterns/SKILL.md` | Update path reference |
| `docs/superpowers/specs/2026-03-21-transcript-browser-design.md` | Update path refs |
| `docs/superpowers/specs/2026-03-20-orchestrator-design.md` | Update path refs |
| `docs/superpowers/plans/2026-03-23-phase-2b-intelligence.md` | Update path ref |
| `docs/superpowers/plans/2026-03-23-canopy-plugin-merge.md` | Update path ref |
| `docs/superpowers/plans/2026-03-20-improvement-loop.md` | Update path ref |
| `VERSION` | Bump patch version |
| `plugins/canopy/.claude-plugin/plugin.json` | Bump patch version |

### Version bump

`0.2.21` → `0.2.22`

## Non-goals

- **No new CLI commands.** The agent is invoked via the command/agent system, not `canopy` CLI.
- **No UI changes.** The transcript browser is not modified.
- **No changes to the analysis engine.** We use `canopy analyze` as-is. The synthesis
  pass is a separate `claude -p` call in the agent, not a new pipeline stage.
- **No retroactive version backfill.** Historical sessions without version metadata
  are handled gracefully, not patched.
