---
name: fleet-align
description: >
  Cross-agent improvement spread — the fleet-level sibling of agent-review. Compares the
  factory-stamped agent fleet (echo, eva, hal, …) against the current canopy factory template
  and each other, and surfaces what to DISTRIBUTE (backport a better/newer version into laggards)
  or PROMOTE (lift a converged pattern into canopy). For every finding it searches the laggards'
  RECENT SESSIONS for evidence the gap actually cost something, and weighs that evidence in a
  judgment pass — a finding with real evidence outranks a speculative one. Then, behind a
  consolidated gate, it dispatches an AI to ship the change as a surgical PR. Invoke it with NO
  arguments — it auto-discovers the whole fleet. Use when asked to "align the agents", "spread
  improvements across agents", "fleet-align", or to run the fleet self-improvement loop. Read-only
  until the gate.
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])" 2>/dev/null)"
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
```

If output shows `UPGRADE_AVAILABLE <old> <new>`, mention the upgrade once and continue.

# Fleet-align — spread improvements across the agent fleet

`agent-review` measures ONE agent's friction and ships fixes into that agent's repo. `fleet-align`
is the other half: it looks ACROSS agents. Because every agent is factory-stamped from a shared
template, the same artifacts (`skills/turn`, `skills/self-review`, `config/gating.json`) exist in
each — so divergence is precise and computable, not vibes. Two things move:

- **DISTRIBUTE →** a better/newer version of a shared artifact exists (in the template, or a peer);
  backport it into the laggards. Subsumes "agent is stale vs. a newer template."
- **PROMOTE ↑** an artifact evolved beyond the template in ≥2 agents (they converged) → lift it
  back into canopy's factory template so everyone inherits it.
- **RECONCILE ?** divergence with no clear winner / a legacy lineage (e.g. echo, the ancestor) —
  surfaced for a human to harvest, never auto-patched.

**Evidence is the point.** A structural gap only matters if it costs something. For each finding
the tool searches the laggards' recent turns for the moment the change would have helped, and the
judgment pass ranks evidence-backed findings above speculative ones. Zero evidence is a real
result — it says "structurally real, but hasn't bitten yet; low priority."

## Step 1 — Analyze (read-only) — just run it, no arguments

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
uv run --project "$CANOPY_ROOT" canopy fleet-align
```

That's the whole command. It **auto-discovers every agent on the machine** (marker:
`skills/turn/SKILL.md`, so legacy agents like echo are included), diffs each shared artifact
against the factory template + peers, attaches recent-session evidence, runs the claude -p judgment
pass, and prints findings **ranked by evidence first**. Each carries: kind, artifact, reference,
laggards, the specific markers, a judge rationale, a recommended action, and any evidence excerpts.
You never pass which agents to look at — it finds them.

**Advanced overrides (optional, rarely needed):** `--no-llm` (skip the judgment pass — faster,
deterministic-only), `--hours N` (evidence window, default 14d), `--no-evidence`, `--repo <dir>`
(add a repo outside the default bases), `--model`.

## Step 2 — Triage

Present the findings as a table (kind · artifact · reference · → laggards · #evidence · action).
Decide implement / defer / skip per finding. Bias:
- **Evidence-backed DISTRIBUTE first** — a stale artifact that already cost a laggard a real miss.
- **PROMOTE** when ≥2 agents converged — that's the strongest signal the template is behind
  (the §1b story: "ACE re-did echo's fixes by hand" → it belonged in canopy).
- **RECONCILE / legacy** — never auto-apply; note what's worth harvesting from the ancestor.

## Step 3 — Execute: dispatch an AI to make the edit + PR (never programmatic splicing)

Read-only until here. Then present ONE consolidated gate: **"apply these N findings?"**

**The edit is done by an AI, not by string/JSON surgery in Python.** This matches canopy's own
architecture — the pipeline stops at proposals; a Claude Code agent implements (as in
`/canopy:improve` and `agent-review`). Brittle programmatic splicing can't renumber cleanly across
every skill's shape, substitute identity placeholders, or judge applicability. The AI can. Python's
job ended at the *brief*.

To get the machine-readable briefs, **the skill itself** runs
`uv run --project "$CANOPY_ROOT" canopy fleet-align --json-output`
under the hood (an internal step — the user never types flags). Each distribute finding then
carries a `change_brief` (target file + the template's exact reference text).

For each accepted DISTRIBUTE finding, **dispatch a Claude Code agent** (Task tool, general-purpose)
into the **laggard's own repo**, handing it the finding + its `change_brief`. Instruct it to:
> Make the SMALLEST surgical edit to `<change_brief.target_relpath>` that adopts the improvement.
> `add_reference` is the template's exact text for the step(s) this agent is missing — splice it
> into the agent's EXISTING file (renumber to continue its list, keep everything else, do NOT
> regenerate). `remove_hint` names a block to delete. Substitute the agent's real name/slug for any
> `{{AGENT_NAME}}`/`{{AGENT_SLUG}}`. If a step names a channel this agent doesn't have (e.g. an
> email deny rail but no email adapter), adapt or skip it and say so. Then, in the laggard's repo
> (an emdash worktree; `main` is checked out elsewhere): branch → commit → `gh pr create`.
> **In dry-run, stop after opening the PR (do NOT merge).** In apply mode, `gh pr merge <n> --squash`
> (NEVER `--delete-branch` in a worktree). Report the PR URL + exactly what you changed/skipped.

- **PROMOTE** → the PR goes into **canopy**, editing the factory template string in
  `src/orchestrator/agent_factory.py`; because that touches `plugins/canopy/`, the agent runs
  `canopy version bump` and follows the plugin-update flow. Existing agents then adopt it via the
  same distribute path — **never by re-scaffolding.**
- **RECONCILE / legacy** — never auto-applied; surface for a human to harvest.

One PR per finding (or a tight batch). This changes *code* only — it never sends on anyone's behalf.

## Step 4 — Measure (close the loop)

Re-run the Step 1 analyze command (`uv run --project "$CANOPY_ROOT" canopy fleet-align`, with the
same resolution lines; add `--no-llm` if you just want the fast deterministic check) and
confirm the targeted divergence is gone (the laggard no longer shows as stale). Report
before→after. A change that doesn't collapse the finding isn't done — this is what makes it a loop,
not a report.

## Notes

- **Never regenerate an agent's file.** Agents evolve their own artifacts; the edit is always a
  minimal in-place splice, and it is made by a dispatched AI with judgment — not a template re-stamp
  and not deterministic string surgery.
- **Gating is delicate.** `config/gating.json` carries agent-specific channel config. Drop the
  deprecated `approve` block; add a missing deny rail *only if the agent has that channel* (heed the
  `change_brief` applicability instruction).
- Legacy agents (no `config/agent.json`, e.g. echo) are never stale laggards — they're the ancestor.
  Harvest their good ideas via PROMOTE, don't "fix" them toward the template.
- `canopy fleet-align` is read-only analysis; it emits `change_brief`s for the apply agent. Backed
  by `src/orchestrator/fleet_align.py`; sibling to `agent-review`. Design:
  `docs/superpowers/specs/2026-07-03-fleet-align-design.md`.
