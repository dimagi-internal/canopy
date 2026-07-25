# Gating Baseline Centralization (chunk B) Implementation Plan

> **Status: shipped (PR #315, chunk B) — `agent-core/gating-baseline.json` live. Historical record, not current-state.**

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox syntax.

**Goal:** Fleet-baseline deny rails live once in canopy, keyed by channel mount; agent repos keep only mounts + agent-specific additions (add-only); a rail fix propagates by `/canopy:update`.

**Spec:** `docs/superpowers/specs/2026-07-13-agent-core-shared-skills-design.md` §4, with one **amended mechanism**: the spec's "thin shim calling the installed engine" cannot be a package import (hooks run under system python3; canopy lives in an isolated uv-tool venv) and a `canopy` subprocess per tool call costs ~0.5s on every Bash/Edit/Write. Instead the baseline ships as **data** in the versioned plugin (`plugins/canopy/agent-core/gating-baseline.json`) and the stdlib hook resolves the installed plugin path (same `installed_plugins.json` one-liner the skill stubs use) and merges baseline + local rails at call time. Same propagation property; no latency, no import problem. Fail-closed is preserved: config present but baseline unresolvable → deny gated calls with the exact remediation.

**Architecture:** `gating-baseline.json` maps channel → deny rails (messages carry `{slug}`). `config/gating.json` becomes `{slug, channels, deny:[extras], approve:[]}`. The factory-stamped `hooks/gating_guard.py` gains `_baseline_rails()`: resolve plugin dir (env override `CANOPY_PLUGIN_DIR` for tests) → load baseline → substitute `{slug}` → prepend to local deny. Legacy configs (no `channels` key) keep local-rails-only behavior — ACE's deliberately different plugin-level hook is untouched and never migrated.

## Global Constraints
- Same as chunk A plan (version bump via CLI only, PR flow, worktree rules, stdlib-only hooks).
- Add-only invariant: local config ADDS deny rails; baseline rails cannot be removed locally.
- Fail-closed only when `config/gating.json` EXISTS with `channels` but the baseline is unreadable; missing local config stays allow (hook copied outside an agent repo must not brick sessions).

### Task 1: `plugins/canopy/agent-core/gating-baseline.json` (+ tests)
Email channel rails = the two current template rails with `{{AGENT_SLUG}}`→`{slug}`. Test: file valid JSON, has `channels.email` with both patterns, no `{{` tokens.

### Task 2: New `_GATING_JSON` (slug/channels/empty-deny) + `_GATING_GUARD` baseline merge + fail-closed
TDD: update `test_gating_defaults_to_deny_rails_only` (new shape); hook e2e tests run with `CANOPY_PLUGIN_DIR` pointing at the repo's `plugins/canopy`; new tests: fail-closed (bad plugin dir → exit 2), legacy-config (no channels → local-only, allow unrailed).

### Task 3: Ship canopy PR (bump → PR → merge → deploy)

### Task 4: Migrate echo, eva, hal (3 parallel agents, worktree each)
Replace `hooks/gating_guard.py` with the new stamped guard (rendered) and rewrite `config/gating.json` to `{slug, channels:["email"], deny:[agent-specific extras only], approve:[]}` — extras = current rails minus the two baseline ones (eva keeps her 3 allowlist deny rails; echo/hal likely none). Verify by running the hook for real: raw `gog gmail send` denied, `--account` denied, `git status` allowed, fail-closed path with a bogus `CANOPY_PLUGIN_DIR`. PR + squash merge each.

### Task 5: Measure
`canopy fleet-align --no-llm` stays clean (template gating.json now has empty deny → no missing-rail findings; approve check unchanged). Live-fire the three agents' hooks post-merge.
