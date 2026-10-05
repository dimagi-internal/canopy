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

**Coverage — stamped AND agent-unique.** The template diff above only sees the factory-stamped
artifacts (`turn`, `agent-turn-review`, `task-tracker`, `shipping`, `manager-sync`,
`answer-caller` + `config/gating.json`), and only their numbered steps/headings. It is blind to
what an agent grew on its own — which is exactly where fleet-generic tooling hides (eva's Google
Docs QA checker, markdown linter and share-gate hook lived in eva's repo for weeks). So a second,
deterministic **promotion-candidates** pass lists every NON-stamped `skills/<name>/`, `bin/<file>`
and `hooks/<file>` in each agent and flags a PROMOTE candidate (summary: *"agent-unique artifact
looks generic"*) when:
- **(a) shared name** — a non-trivial copy of the same-named artifact exists in ≥2 agents (names
  compared after stripping the agent's slug prefix + extension: `eva-preflight` ≡
  `echo_preflight.py`); or
- **(b) persona-free shared-channel mechanism** — a bin/hook file or a skill that ships code,
  where ≤6% of non-blank lines name the agent (slug / persona name / mailbox — `--account`/env
  default lines don't count), AND it is a gdoc / gmail / drive / calendar helper (named for the
  channel, or ≥8 mentions).

Both rules ignore stubs under 1.5KB, and a second clone of the same repo (same git origin, e.g.
`ace` + `ace-2`) counts as ONE agent. When canopy already ships a same-named file
(`plugins/canopy/agent-core/<name>`, `plugins/canopy/skills/<name>/`) the note says so — the
agent copy is then likely a stale fork to delete. These are **candidates**: the judgment pass
promotes the generic mechanisms and drops domain/persona work. Thresholds live as
`PROMOTE_*` constants in `fleet_align.py`.

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

**"No agent repos found" means the SEARCH SPACE is wrong, not that the fleet is empty.** The two
default bases (`~/emdash/repositories`, `~/emdash-projects`) are one operator's layout; a machine
that keeps its repos anywhere else — say `C:\Projects` — discovers nothing, and this skill's own
"just run it, no arguments" is what makes that read as a real verdict. The error now lists every
directory it searched. Fix it once, for the machine, rather than per invocation:

```bash
export CANOPY_AGENT_BASES="/c/Projects"        # os.pathsep-separated; dirs that CONTAIN agent repos
```

(Reported 2026-09-14: combined with a Windows path-separator bug in the template lookup — since
fixed — fleet-align had never once returned a true result on that machine, and reported a
confidently-reasoned fabrication instead of an error. `analyze()` now raises `BaselineUnusable`
rather than judging against a baseline that resolved to nothing.)

## Step 2 — Triage

Present the findings as a table (kind · artifact · reference · → laggards · #evidence · action).
Decide implement / defer / skip per finding. Bias:
- **Evidence-backed DISTRIBUTE first** — a stale artifact that already cost a laggard a real miss.
- **PROMOTE** when ≥2 agents converged — that's the strongest signal the template is behind
  (the §1b story: "ACE re-did echo's fixes by hand" → it belonged in canopy).
- **PROMOTE candidates (agent-unique)** — lift the mechanism into canopy (a CLI under
  `src/orchestrator/`, a fleet hook under `plugins/canopy/agent-core/`), parameterize any
  per-agent setting, then thin the agent copy to a stub (or delete it). If canopy already ships
  it, the action is just the delete. Drop candidates that are really the agent's own domain work.
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

- **PROMOTE (agent-unique candidate)** → one PR into **canopy** adding the generic mechanism,
  then one PR per owning agent removing/thinning its copy to call canopy's.
- **PROMOTE** → the PR goes into **canopy**, editing the factory template string in
  `packages/canopy_agent_factory/canopy_agent_factory/_factory.py` (the factory is its own
  published package since #636). Bump that package's own `[project] version` in
  `packages/canopy_agent_factory/pyproject.toml` (it publishes to PyPI on its own clock, for
  canopy-web) AND run `canopy version bump` (canopy's runtime bundle carries the package, so
  installs only pick the change up on a canopy release), then follow the plugin-update flow. Existing agents then adopt it via the
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
