# Agent-core shared skills — design

> **Status: shipped (PRs #312, #315) — `plugins/canopy/agent-core/` live. Historical record, not current-state.**

**Date:** 2026-07-13
**Status:** approved (brainstormed with Jonathan; sections approved in-session)
**Supersedes:** the DISTRIBUTE half of routine fleet-align usage (the skill itself remains for
promote/reconcile/doctor duties — see §6)

## 1. Problem

The agent factory stamps full copies of shared process skills (`turn`, `task-tracker`,
`self-review`, `agent-turn-review`) and a full gating config into every agent repo. Copies drift:
the 2026-07-13 fleet-align run found hal missing the `task-tracker` skill entirely and missing an
email deny rail every sibling has, echo missing two turn steps, and the factory template itself
lagging echo's evolved task-tracker. Keeping copies in sync requires fleet-align's AI-judged
backport machinery — one PR per laggard per finding.

Motivations, all confirmed:

1. **Safety rails must not drift.** A missing deny rail is a live security gap by construction.
2. **Sync overhead is heavy.** Template improvement → fleet-align → N surgical backport PRs.
3. **Process consistency.** One canonical way agents run turns and track tasks; divergence in
   HOW agents work is a bug (divergence in WHAT they do — persona, domain skills — is fine).
4. **DRY as principle.** Near-identical text in N repos is bad architecture.

## 2. Decision

**Shared process skills get one canonical home in canopy; agent repos keep thin stubs that read
it at runtime.** Improvements land as canopy PRs and distribute via `/canopy:update`. Baseline
safety rails move into canopy's gating engine, keyed by channel mounts.

This extends the operating model's §4a boundary ("logic, adapters, and cross-agent skills are
common; identity, rules, secrets, and domain skills are the agent's") to the process-skill text
itself, and completes §4a's stated target for gating ("copied now, thinned to canopy-backed").

Precedent already shipped: `agent-turn-review` (v0.2.27x) is a canopy **plugin skill** whose
factory stamp is a thin per-agent stub that invokes `canopy:agent-turn-review` and adds agent
specifics. This design generalizes that motion — with one twist for skills that should NOT be
plugin skills (see §3.1).

## 3. Architecture

### 3.1 Canonical home: `plugins/canopy/agent-core/*.md`

Plain markdown documents inside the plugin — deliberately **not** under `plugins/canopy/skills/`:
anything there is parsed as a plugin skill and its description costs system-prompt budget in
**every** session on the machine (the documented "N skills dropped" aggregate-cap problem).
`agent-core/` ships in the same versioned plugin cache but is invisible to the skill loader.

Initial docs:

- `agent-core/turn.md` and `agent-core/task-tracker.md` — seeded verbatim from the v0.2.271
  factory templates (which just absorbed echo's task-tracker and hal's turn-close promotions,
  PR #311).
- ~~`agent-core/self-review.md`~~ — **dropped during planning.** Reading the fleet's copies showed
  hal's and eva's `self-review` are verbatim identical and the discipline is exactly what the
  fleet-wide `canopy:agent-turn-review` plugin skill already covers (fidelity / grounded
  commitments / presentation). Minting a core doc would create a third copy of the same
  discipline. Instead, migration replaces each agent's `skills/self-review` with a supersession
  stub pointing at `agent-turn-review`, moving any genuinely agent-unique review notes into that
  agent's `agent-turn-review` specifics section.

`agent-turn-review` stays a plugin skill (it is already central; it is also legitimately invoked
outside agent repos). Its stamped stub is already thin — unchanged by this design.

### 3.2 Agent-repo stubs

Each agent's `skills/<name>/SKILL.md` becomes ~10 lines:

```markdown
---
name: turn
description: {{AGENT_NAME}}'s turn orchestrator — canonical core lives in canopy (agent-core/turn.md)
---

1. Resolve the installed canopy plugin path from `installed_plugins.json`
   (the canonical one-liner — never a hardcoded version), and run
   `scripts/canopy-update-check.sh` from it; if it prints `UPGRADE_AVAILABLE`,
   say so and update before following a stale core.
2. Read `<installPath>/agent-core/turn.md` and follow it exactly.
3. **Agent-local notes** (the ONLY hand-edited section):
   - <persona flavor, channel mounts, quirks, pointers to agent-local skills>
```

Properties:

- **Project-scoped:** the skill loads only in that agent's sessions — zero global budget cost.
- **Versioned core:** the text an agent follows is exactly what the installed canopy ships;
  distribution = `/canopy:update`, same as every other canopy artifact.
- **Staleness-guarded:** step 1 reuses `canopy-update-check.sh` (same guard pattern as the email
  engine-staleness guard, PR #309).
- **Local notes are additive persona flavor only.** Process changes belong in the core (§3.4).

### 3.3 Factory stamps stubs

`agent_factory.py`'s `_TURN_SKILL` / `_TASK_TRACKER_SKILL` / self-review template shrink to stub
stampers (identity placeholders substituted as today). The document bodies move to `agent-core/`.
New agents are born on the shared core. `fleet_align.py` derives its skill taxonomy from the
factory stamp table, so it picks up the stub shapes automatically.

### 3.4 Evolution loop (inverted)

An agent that discovers a process improvement mid-turn opens a **canopy PR** against
`agent-core/*.md` (+ `canopy version bump`), self-merges per canopy policy, and the fleet
inherits on next `/canopy:update`. Agent-specific discoveries go in that agent's stub
local-notes. Hal already performs this motion today (canopy PRs #305, #309, #310 shipped from
inside hal turns), so the loop is proven, not aspirational.

## 4. Gating rails

- The canopy package ships a **fleet-baseline rail set keyed by channel mount** — e.g. any agent
  with an email mount gets the raw `gog gmail send/reply` deny and the
  `canopy email send --account` deny by construction.
- `config/gating.json` shrinks to **channel mounts + agent-specific additions**. The engine
  merges baseline + local at check time; local config can **add** rails but never remove
  baseline ones.
- The per-agent PreToolUse hook becomes a thin shim calling the installed engine; a rail fix in
  canopy propagates by version bump. Hal's missing-rail class of bug becomes structurally
  impossible.
- The shim **fails closed** (denies writes with a clear message) if canopy isn't importable.

## 5. Migration

1. **Canopy PR — chunk A (framework):** create `agent-core/{turn,task-tracker}.md`
   from the v0.2.271 templates; shrink the factory templates to stub stampers; version bump.
   Tests: factory stamps stubs; stub resolution one-liner works from a stamped repo; template
   docs exist and are non-empty; fleet-align taxonomy still derives.
2. **Per-agent PRs (echo, eva, hal):** replace each full skill with the stub, moving that
   agent's unique content into its local-notes (hal's architect steps and repo-awareness stay
   hal's; echo's legacy sheet machinery is dropped per PR #311's judgment or parked in echo's
   notes). Also: stamp the thin `agent-turn-review` stub where missing (echo, hal), and replace
   `skills/self-review` with the supersession stub (§3.1). This **subsumes the parked distribute
   findings**: echo's two missing turn steps and hal's missing task-tracker arrive via the core
   automatically.
3. **Canopy PR — chunk B (gating):** baseline rail set + engine entry point + shim template;
   then per-agent PRs swap the copied hook for the shim + mounts-based config. Hal's missing
   deny rail is fixed here (or, if chunk B lags, patched directly in hal's gating.json as a
   stopgap — the one distribute worth doing by hand).
4. **Measure:** re-run `canopy fleet-align --no-llm`; the stale/missing findings collapse.
   `canopy agent doctor` gains a "core resolvable + canopy current" check.

Chunk A (core extraction + stubs + factory + 3 agent migrations) is independent and the bigger
win; chunk B (gating centralization) rides the same pattern after.

## 6. Fleet-align's new role

Shrinks from AI-judged backport machinery to mostly a **doctor check**: stubs present and
pointing at the core; no shadow copies regrown; local-notes contain only agent-local content.
PROMOTE/RECONCILE remain for true divergent lineages (a future innovation that starts life in an
agent's local notes gets harvested into the core the same way echo's task-tracker just was).
DISTRIBUTE largely retires — distribution is now `/canopy:update`.

## 7. Testing

- **Canopy (pytest):** factory stamps stubs with substituted identity; `agent-core/` docs exist,
  non-empty, no stray `{{PLACEHOLDER}}`s; gating merge (baseline + local, add-only); shim
  fail-closed path; fleet-align stub-aware taxonomy.
- **Per-agent verification:** run the stub's resolution step for real in each migrated repo;
  `canopy agent doctor` green.
- **Fleet measure:** before/after `canopy fleet-align --no-llm` finding counts.

## 8. Out of scope

- Migrating ACE (a different, non-factory lineage) or the OpenClaw ancestors.
- Sinking task-tracker *mechanics* deeper into the `canopy agent` CLI (good follow-up, separate).
- Per-operator attribution and the broader §4b board work.
