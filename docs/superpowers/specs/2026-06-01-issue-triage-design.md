# `canopy:issue-triage` — design

> **Status: shipped — `canopy:issue-triage` live. Historical record, not current-state.**

**Date:** 2026-06-01
**Status:** approved (design) → implementing

## Problem

ACE (and other autonomous loops) now file GitHub issues as they hit problems,
so the team can self-improve. That backlog grows fast and goes stale: many
issues get fixed in passing, some are duplicates, some were never actionable.
Nobody has time to read every open issue against the current code and decide
what to do with it.

We want to point canopy at a GitHub repo and have it scan **all open issues**,
evaluate each one against the **latest code**, and recommend per-issue:

- **implement** — still valid, actionable, not yet done
- **investigate** — can't decide without repro / more info / scope clarification
- **close** — already fixed/implemented in code, obsolete, or duplicate (no longer relevant)

Then, behind gates, act on those recommendations: close the obsolete ones with a
reasoned comment, label/comment the ambiguous ones, and open draft PRs for the
ones worth building.

This is the **inverse** of the existing `pm-scout` / `product-management` scout,
which explores the codebase for *new* work. Issue-triage triages *existing*
issues. They are complementary, not the same skill.

## Shape

A dedicated command + skill, mirroring how `pm-scout` (command) drives
`product-management` (skill).

- **`plugins/canopy/commands/issue-triage.md`** — thin dispatcher. Argument:
  `[owner/repo]` (optional). Because a command and a skill share the name
  `issue-triage`, the command MUST use **Pattern B**: resolve the install path
  from `installed_plugins.json`, read `skills/issue-triage/SKILL.md` from disk,
  and follow it — never round-trip through the Skill tool (which would silently
  re-serve the command body). This is enforced by
  `tests/test_command_skill_collisions.py`.
- **`plugins/canopy/skills/issue-triage/SKILL.md`** — the procedure.

**Target resolution:** no arg → the current repo's `origin`
(`gh repo view --json nameWithOwner`). An explicit `owner/repo` arg overrides.

**Name check:** `issue-triage` is not a Claude Code built-in slash command and
does not collide with any existing canopy entry, so no namespace prefix needed.

## Procedure (phases)

### Phase 0 — Pre-flight (one sequential check)
- `gh auth status` (fail fast with a clear message if unauthenticated).
- Resolve the target slug (arg or `gh repo view`).
- Decide where the code to evaluate lives (see Phase 2) and where the report
  gets written (see Reporting).

### Phase 1 — Gather issues
```
gh issue list --repo <slug> --state open --limit <N> \
  --json number,title,body,labels,createdAt,updatedAt,comments
```
- Default `N = 30`. If `gh` reports more open issues than the cap, **log the
  truncation explicitly** ("triaged 30 of 47 open issues") — never silently drop.
- A `--limit <N>` style override may be passed through the command arg.

### Phase 2 — Resolve the code to evaluate against
- **Target == the repo we're in** (slug matches local `origin`): search the
  working tree directly. Cheapest, and the common case.
- **Target is a different repo**: shallow-clone
  `git clone --depth=1 https://github.com/<slug>` into a temp dir, search there,
  remove it at the end.

### Phase 3 — Evaluate (fan-out, read-only)
One subagent per issue, with bounded concurrency. Each subagent receives the
issue (title, body, labels, comments) and access to the code, and returns a
**structured verdict**:

```
{ number, title, disposition, confidence, effort, evidence[], reasoning }
```

- `disposition` ∈ { implement, investigate, close }
- `confidence` ∈ { high, medium, low }
- `effort` ∈ { S, M, L } (only meaningful for `implement`)
- `evidence[]` — `file:line` citations that justify the verdict (required for
  `close`: point at the code that already resolves it)
- `reasoning` — 1–3 sentences

Rubric:
- **close** when the code already does what the issue asks, the issue describes
  behavior that no longer exists, or it duplicates another open issue.
- **implement** when the request is still valid and not yet satisfied by the code.
- **investigate** when the issue can't be adjudicated from the code alone
  (needs a repro, is under-specified, or spans an external system).

**No GitHub writes happen in Phases 0–3.** Evaluation is strictly read-only.

### Phase 4 — Report
Print a ranked table to chat (close-candidates first — those are the cheap
wins), and write a run log to:
- `<repo>/.canopy/issue-triage/runs/YYYY-MM-DD.md` when triaging the local repo
- `$HOME/.canopy/issue-triage/<owner>-<repo>/YYYY-MM-DD.md` as the fallback for
  remote targets (no local checkout to commit into)

The report records every issue, its verdict, confidence, evidence, and the
action taken (filled in after Phase 5).

### Phase 5 — Act (gated, grouped by disposition)
Group the verdicts and confirm **each group** via its own `AskUserQuestion`
with options **Approve all / Skip / Let me pick** (so outward-facing actions
are gated and individually overridable):

- **close group** → `gh issue close <n> --comment "<reasoned summary + evidence>"`,
  optionally adding a label (e.g. `triage:obsolete`).
- **investigate group** → post a comment stating what's needed to proceed and
  add a `needs-info` label; leave the issue open.
- **implement group** → for approved issues, hand off to the
  `product-management` implement/ship conventions (Phase 4/5): branch
  `<prefix>/<issue-slug>`, implement, run validation, open a **draft** PR whose
  body references the issue (`Refs #<n>`). One issue at a time. PRs are drafts —
  never auto-merged; the merge decision stays with the human / PM flow.

## Design choices (locked at brainstorm)

- **Per-group gating, not one blanket yes.** Closing issues and opening PRs are
  hard to reverse, so each group is confirmed separately and is cherry-pickable.
- **Draft PRs only.** The implement path opens drafts; autonomous merge is out
  of scope.
- **Read-only until the gate.** Gather/Evaluate never mutate GitHub.
- **Reuse, don't reimplement.** The implement path defers to
  `product-management`'s existing branch+PR machinery rather than duplicating it.
- **No silent truncation.** If the open-issue count exceeds the cap, say so.

## Out of scope (YAGNI)

- Triaging closed issues or PRs.
- Auto-merging PRs.
- Cross-repo / org-wide sweeps in a single run (one repo per invocation).
- A bespoke local state/resolver script — reuse the `.canopy/` namespace
  convention and a simple path fallback.

## Ship checklist

- New `commands/issue-triage.md` (Pattern B) + `skills/issue-triage/SKILL.md`.
- `canopy version bump` (VERSION + plugin.json).
- CLAUDE.md skill/command list updated.
- `uv run pytest tests/test_command_skill_collisions.py
  tests/test_builtin_command_collisions.py` green (collision + reserved-name).
- PR → auto-merge → `/canopy:update` → `/reload-plugins`.
