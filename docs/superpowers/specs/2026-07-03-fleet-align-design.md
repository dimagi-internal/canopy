# fleet-align — cross-agent improvement spread

> **Status: shipped — `canopy fleet-align` + the `fleet-align` skill live; the factory template it compares against is now the `canopy_agent_factory` package (#636). Historical record, not current-state.**

**Status:** implemented (analysis); apply is AI-dispatched from the skill
**Author:** Jonathan Jackson + Claude
**Related:** `docs/agent-operating-model.md` (§4a boundary, §1b reply-quality primitives, §6.5 spread/execute/measure), `src/orchestrator/agent_review.py` (Build 2, per-agent friction), `plugins/canopy/skills/alignment/SKILL.md` (2-repo directional precursor)

> **Revision (2026-07-03) — apply is AI-dispatched, not programmatic.** The sections below describe
> a Python `render_patch`/`--apply` that computes and writes the file edit. That was built and
> rejected: deterministic string/JSON surgery can't renumber every skill shape, substitute identity
> placeholders, judge applicability (e.g. don't add an email deny rail to an agent with no email
> adapter), or combine same-file findings. Corrected design, matching canopy's pipeline-stops-at-
> proposals architecture: **Python does the analysis only** (discover / diff / evidence / judge /
> rank) and emits a deterministic `change_brief` per distribute finding (target file + the
> template's exact reference text + an instruction). **The edit + PR + merge is dispatched to a
> Claude Code agent by the `fleet-align` skill** — from canopy, into the laggard's repo, as a
> minimal in-place splice (never a regeneration). `canopy fleet-align` is read-only; there is no
> `--apply` flag on the CLI. Where the text below says `render_patch`/`--apply`, read
> `change_brief` + skill-dispatched AI apply.

## Problem

The agent factory (`canopy create-agent`) stamps every agent from a shared set of
templates — a `turn` checklist, a `self-review` skill, a `gating.json` + gating
hook, `bin/` shims, CLAUDE.md conventions. After stamping, each agent **evolves
independently**: echo's `self-review` grows from 2 reply-quality rules to 5, ace
adds a domain skill that turns out to be generic, hal's gating hook drifts from
canopy's engine. Nothing carries a good idea from one agent to the others, or back
into the factory so future + existing agents inherit it.

Today that spread happens by hand and by luck — the operating model records the
canonical failure: "rules 3–5 were proven in echo and promoted here **after ACE
repeated all three by hand** in a single turn." We want to detect that convergence
mechanically and close the loop.

This is the **spread** verb of §6.5 (`spread / execute / measure`), fleet-wide.
`agent-review` already owns *measure* (one agent's friction); `fleet-align` owns
*spread across the fleet*, and — crucially — reaches *execute* (a real PR), not a
card on a feed.

### Why not just reuse `alignment`

`alignment` is directional prior art, not a base to build on. Its output model
underdelivers: a subagent free-reads two repos and posts prose cards to the
canopy-web `/insights` feed. That output is non-reproducible, imprecise, and
terminal — a card you must remember to read and then act on by hand. It has not
been used successfully.

`fleet-align` improves on it in three ways, all enabled by the fact that agents
are **factory-stamped and therefore structurally uniform** (the same files exist
in every agent):

1. **Structured, not free-form.** Compare a known taxonomy of shared artifacts
   file-by-file, normalized for identity — deterministic and reproducible.
2. **Template-anchored.** The factory template is ground truth, so "behind the
   template" and "ahead of the template" are computed, not guessed.
3. **Actionable output.** Each finding renders a concrete patch and opens a PR
   into the target repo — spread reaches *execute*, not a feed card.

## Goals / Non-goals

**Goals**
- Detect divergence in shared agent artifacts across the whole fleet.
- Classify each divergence as **DISTRIBUTE** (backport a better version into
  laggards) or **PROMOTE** (lift a generalized pattern into canopy) or
  **RECONCILE** (no clear winner — human decides).
- Produce a concrete, reviewable patch per finding; open it as a gated PR.
- Support a **dry-run** preview (full patch/PR plan, no writes) and a gated
  **apply** (opens PRs), as part of skill execution — the house pattern.
- Deterministic, offline-testable core; LLM used only for judgment + rationale.

**Non-goals**
- Not per-agent friction analysis — that's `agent-review`.
- Not a `/insights` feed producer (explicitly dropped; PRs are the artifact — an
  optional one-line digest is the most the feed gets).
- Not auto-merge. Nothing merges; PRs wait for human review.
- Not comparing identity (`config/agent.json`, `allowlist.txt`) — pure identity is
  never a finding.

## Approach (chosen)

**Template-anchored structural differ → gated patch PRs**, with two optional
enrichers:
- **friction ranker** (optional) — annotate a divergence with `agent-review`
  friction counts so painful ones sort first.
- **LLM judgment** (optional, `--no-llm` to skip) — decide best-of-fleet when the
  deterministic rule is ambiguous, and write the PR rationale. Never used to
  *find* divergences.

Rejected alternatives: friction-clustered spread (blind to good ideas that left no
friction trace; needs fresh turns per agent) and an LLM fleet-sweep (the
imprecise, non-reproducible thing that underdelivered as `alignment`).

## Artifact taxonomy

The comparable surfaces every factory agent has. This taxonomy is what makes the
diff precise.

| Class | File(s) | Compared on |
|---|---|---|
| `turn` | `skills/turn/SKILL.md` | step set, ordering, CLOSE-CHECKLIST items |
| `self-review` | `skills/self-review/SKILL.md` | which reply-quality rules (§1b 1–5) are present |
| `gating-rules` | `config/gating.json` | deny rails carried |
| `gating-hook` | `hooks/gating_guard.py` | should equal canopy's engine → any diff = stale/drift |
| `bin-shim` | `bin/<slug>-email`, other shims | thin shim vs. logic that drifted in |
| `claude-md` | `CLAUDE.md` | guardrail wording, worktree rules |
| `domain-skill` | `skills/*` with no template slot | novelty → PROMOTE candidate if generic |
| **identity** (excluded) | `config/agent.json`, `allowlist.txt` | never compared |

**Normalization.** Before diffing any pair, replace each agent's identity tokens
(name, slug, mailbox, `gog_client`) with placeholders on both sides, so only
substantive divergence surfaces — "echo" vs "ace" is not a finding.

## Finding model

```
AlignFinding:
  kind:        distribute | promote | reconcile
  artifact:    <taxonomy class>
  reference:   <slug> | "canopy-template" | "none — reconcile"
  laggards:    [<slug>, ...]           # who adopts the change
  reasoning:   <one paragraph, why this reference wins>
  evidence:    { <slug|template>: "<repo-relative path>[#section]", ... }  # a handle on EVERY side
  recency:     YYYY-MM-DD              # last-touched date of the winning artifact
  friction:    <int|null>             # optional agent-review friction count (ranker)
  patch:       <unified diff or rendered target file>   # the concrete change
  pr_plan:     { target_repo, branch, title, body }
```

**Kind semantics**
- **DISTRIBUTE** — a better version of a *shared* artifact exists in one agent or
  the current template; backport into `laggards`. Subsumes "agent is stale vs. a
  newer factory template" (reference = `canopy-template`).
- **PROMOTE** — an artifact evolved beyond the template in one or more agents and
  is generic; lift it into canopy (`agent_factory._TURN_SKILL` /
  `_SELF_REVIEW_SKILL`, or the relevant package module). **Strongest signal when
  ≥2 agents independently converged** on the same improvement (the §1b story).
- **RECONCILE** — shared artifact diverged with no clear winner; surfaced for a
  human decision, **never auto-patched**.

## Architecture

Follows canopy's `analyzer` / `agent_review` shape: deterministic core + optional
`claude -p` synthesis.

- **`src/orchestrator/fleet_align.py`** — **FRAMEWORK tier** (agent-agnostic
  substrate). Imports framework only (`agent_factory` for templates, `repo_paths`,
  `transcripts`/`agent_review` for optional friction); never product code. Must be
  registered in `src/orchestrator/TIERS.md` and pass `tests/test_plugin_boundary.py`.
  Responsibilities:
  - `discover_agents(bases)` — find repos containing `config/agent.json` across the
    known bases (`~/emdash/repositories`, `~/emdash-projects`, plus `--repo`
    overrides). Skip non-agent repos silently.
  - `load_template_baseline()` — read the factory templates from `agent_factory`
    (`_TURN_SKILL`, `_SELF_REVIEW_SKILL`, `config/gating.json` default, the hook)
    as the reference baseline.
  - `extract_artifacts(agent)` → normalized rep per taxonomy class.
  - `diff_fleet(agents, baseline)` → `AlignFinding[]` (deterministic classify:
    behind-template / ahead-of-template / peer-drift → kind).
  - `render_patch(finding)` → unified diff + `pr_plan`.
  - pure/offline; no network, no LLM in the core.
- **Optional LLM judgment** — `synthesize(findings, runner=...)`, injectable
  subprocess runner (mockable). `--no-llm` skips it. Decides best-of-fleet on
  ambiguous ties and writes `reasoning` + `pr_plan.body`.
- **CLI** `canopy fleet-align` in `cli.py`:
  - default: analyze + print findings table (read-only).
  - `--dry-run`: render every patch + PR plan and print exactly what *would* open;
    no writes.
  - `--apply`: open the PRs behind the skill's consolidated gate (see skill).
  - `--no-llm`, `--repo <dir>` (repeatable), `--json-output`.
- **Skill** `plugins/canopy/skills/fleet-align/SKILL.md` — orchestrates:
  1. run analysis (read-only),
  2. present the findings table (kind / artifact / reference / laggards / friction),
  3. **read-only until the gate.** Then present ONE consolidated gate: "open these
     N PRs?" — dry-run shows the branch/target/diff per finding; apply opens them
     (per-finding branch into the laggard repo for DISTRIBUTE, into canopy for
     PROMOTE). RECONCILE findings are reported only, never gated for auto-apply.
  4. summary: PRs opened (with URLs) + anything left as reconcile/skip.

## Execution model (dry-run vs apply)

Mirrors `test-audit` / `issue-triage`:

| Mode | Behavior |
|---|---|
| default (skill) | analyze → present table → consolidated gate → open PRs on approval |
| `--dry-run` | analyze → render exact patches + PR plan → **print only, no writes** |
| `--apply` | analyze → open PRs behind the consolidated gate |

`--dry-run` is the safe preview: it produces the identical patch set apply would,
so the user reviews real diffs before anything is written. No PR is ever opened
without passing the consolidated gate; nothing auto-merges.

## Data flow

```
discover agents (config/agent.json marker)
  → load factory template baseline
  → per artifact class: extract + normalize from every agent + template
  → diff → classify (behind / ahead / drift) → AlignFinding[]
  → [optional] annotate with agent-review friction counts (ranker)
  → [optional] LLM best-of + rationale
  → render patch + pr_plan per finding
  → present table
  → dry-run: print plan | apply: consolidated gate → open PRs
```

## Error handling / edge cases

- **Missing artifact** in an agent → a finding (e.g. no `self-review` step), not a
  crash.
- **Stale agent** (stamped from an old template) → surfaces as behind-template, a
  legitimate DISTRIBUTE-from-`canopy-template` finding.
- **False PROMOTE** (looks generic, is domain-specific) → caught by the LLM
  judgment gate + mandatory human approval before any PR opens.
- **Non-agent repo** (no `config/agent.json`) → skipped silently.
- **Worktree PR mechanics** — opening a PR into an agent repo checked out in an
  emdash worktree follows the same `gh pr merge --squash` (no `--delete-branch`)
  caveat the factory turn skill documents; the skill notes it but does not merge.
- **PROMOTE into canopy bumps VERSION** — a PROMOTE PR touches `plugins/canopy/`
  or the package, so the PR plan must run `canopy version bump` (the repo's #1
  discipline); the skill checklist enforces it.

## Testing

- Deterministic core: unit tests with **fixture agent dirs** — temp repos with a
  minimal `config/agent.json` + skills/hooks that diverge in known ways (echo-ahead
  on self-review, hal-drifted hook, ace-stale turn). Assert exact
  findings/kinds/references. No network, no LLM.
- Normalization: assert identity tokens are stripped so "echo" vs "ace" produces no
  finding but a real rule difference does.
- LLM path: mocked via injectable runner; assert `--no-llm` yields deterministic
  findings only.
- Boundary: `tests/test_plugin_boundary.py` entry keeping `fleet_align` framework-tiered.

## Open questions

- **PROMOTE convergence threshold** — is ≥2 independent agents the bar, or ≥N?
  Start at 2, tune.
- **friction ranker default** — on or off by default? (Leaning off; it needs fresh
  turns per agent and adds cost. Off by default, `--rank-by-friction` to enable.)
- **name** — `fleet-align` vs `agent-align`. Using `fleet-align`.
