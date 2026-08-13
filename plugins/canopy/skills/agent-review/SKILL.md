---
name: agent-review
description: >
  Point canopy's self-improvement loop at an AGENT's own turns (Build 2 of the agent operating
  model). Reviews an agent's recent turn transcripts for operating-model friction — dropped
  checklist steps, tool failures/retries, gating gaps, auth friction, repeated manual work that
  should be a skill — and turns findings into fixes shipped as PRs into the agent's repo, then
  re-measures. Use when asked to "review an agent", "improve echo", "agent-review <slug>", or to
  run the fleet self-improvement loop. The active learning loop reef never had — own all three
  verbs: spread, execute, measure.
---

# Agent Review — the self-improvement loop, pointed at an agent

This is Build 2 of `docs/agent-operating-model.md`: canopy reviewing an agent's *turns* and
shipping improvements back into that agent's repo. reef could *see* friction but never *act* on
it — this skill owns all three verbs: **spread** (find the fix), **execute** (PR it into the
agent repo), **measure** (re-run and confirm the friction dropped).

## Step 1 — Run the review
```
canopy agent-review <slug-or-path> [--hours N] [--json-output]
```
This finds the agent's recent turn transcripts (by cwd, across repo + worktrees), extracts
deterministic friction signals (failures, gating blocks, auth friction, retry loops, checklist
gaps), then runs a claude -p synthesis that returns ranked **findings**, each with a
`friction_type`, `fix_kind` (skill_edit | hook_rule | claude_update | channel_fix | new_skill),
a `target` path in the agent repo, and a `recommendation`. Use `--no-llm` for signals only.

## Step 2 — Triage the findings
Present the findings as a ranked table (friction_type · title · fix_kind · target · confidence).
For each, decide implement / defer / skip. Bias:
- **`hook_rule`** for any "never do X" invariant — a rule in the agent's `config/gating.json`,
  NOT prose. (Prose fails under load; the gating hook forces it. §1a / §6.6.)
- **`new_skill` / `skill_edit`** when a multi-step manual pattern repeats — capture it so the
  agent stops re-deriving it (the Voyager/skill-library lesson, §6.2).
- **`channel_fix`** for auth/setup friction (e.g. echo's OAuth "API not enabled" loop, 1Password
  round-trip) — make setup self-heal / validate, don't just document it.

**`↻ fix_kind coerced from '<x>'` on a finding.** An invariant finding (never/always phrasing, or
one mined from a human `safety_override`) must ship structurally, so when the synthesis pass
routes one at `skill_edit`/`claude_update` anyway, `qualify_findings` corrects the label to
`hook_rule` (or `schema_validator` for a schema target) and marks it, rather than throwing the
finding away — the evidence is sound, only the routing was wrong. **Read the `recommendation`,
which is preserved verbatim, and confirm the `target` actually fits a hook rule** before you
implement it; the model was proposing a prose edit, so the target may want redirecting at
`config/gating.json`. The EVIDENCE gate is different — an unevidenced finding is still a hard
drop, and is never rescued this way.

## Step 3 — Execute (PR into the AGENT's repo)
For each accepted finding, make the change **in the agent's own repo** (not canopy — unless the
fix is shared infra, in which case it belongs in canopy per the §4a boundary). Ship via that
repo's flow: branch → commit → PR → merge (the agent repos use the same "no human review, merge
it yourself" convention). One PR per finding (or a tight batch). The runtime guardrail still
holds: this changes *code*, never sends on a human's behalf.

If the agent repo isn't checked out locally, say so and stop — don't guess.

## Step 4 — Measure (close the loop)
Re-run `canopy agent-review <slug> --no-llm` and confirm the targeted signals dropped (fewer
failures, the checklist gap now covered, the gating rule now present). Report before→after. A
fix that doesn't move the signal isn't done. This measurement is what separates this loop from
reef's passive observatory.

## Notes
- Findings are scoped to ONE agent at a time (the operating model's one-thing-at-a-time
  discipline). Run it per agent across the fleet.
- This composes with `canopy agent-publish` (Step 1 of a turn refreshes the workspace) — the
  agent's `/agents/<slug>` page is where these improvements become visible over time.
- Backed by `src/orchestrator/agent_review.py`.
