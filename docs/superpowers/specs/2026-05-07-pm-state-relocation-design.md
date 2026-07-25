# Move PM state from `~/.canopy/pm/` to `<repo>/.canopy/pm/`

> **Status: shipped — historical record, not current-state.**

**Date:** 2026-05-07
**Status:** Approved design — ready for implementation plan
**Origin:** Brainstormed with jjackson while figuring out how to share canopy state across two macOS user accounts on one machine.

## Summary

Relocate per-project state owned by `canopy:product-management` from a
per-machine home-dir path (`~/.canopy/pm/<project>/`) into the project repo
itself (`<repo>/.canopy/pm/`). State becomes normal source-controlled content,
committed to git, portable across machines and user accounts via the same
git remote.

This plugs PM state into the namespace PR #37 already established for
project-level canopy specialization (`<repo>/.canopy/lenses/`,
`<repo>/.canopy/run-artifacts.yaml`, etc.). The global "self-improvement
brain" (`~/.claude/canopy/observations/`, `proposals/`, `session-log.jsonl`)
stays exactly where it is — that data is intentionally cross-project on a
single machine and is out of scope for this work.

## Why

The user runs canopy under two macOS accounts on one mac and wants per-project
state to converge across both. Today, `~/.canopy/pm/<project>/` is per-account,
so each account learns separately and never sees the other's `learnings.md`,
`context.md`, or run history.

The natural way to share project-scoped state across machines and accounts is
git: commit it under the project's own repo, let `git pull` and `git push`
handle the sync. PR #37 already chose this exact pattern for lens descriptors.

## Scope

In scope:
- `~/.canopy/pm/<project>/{autonomous.yaml, context.md, learnings.md, runs/}`
  → moves into the repo at `<repo>/.canopy/pm/`.
- All code that resolves `CANOPY_PM_DIR` or `CANOPY_PM_PROJECT`.
- One-shot auto-migration from the old home-dir location.

Out of scope:
- `~/.claude/canopy/` (the global self-improvement brain — stays per-machine on purpose).
- `~/.claude/projects/<encoded-cwd>/memory/` (Claude Code harness-managed auto-memory; canopy does not control its location).
- Any other per-project state that might exist outside PM mode (none today).

## Design principles

1. **Treat `.canopy/pm/` as ordinary committed source.** Same model as
   `.canopy/lenses/` from PR #37 and any other tracked file. No shadow
   storage, no hybrid live/snapshot duality. Simplicity wins.
2. **Drop the origin-URL `<project>` derivation.** The current
   `CANOPY_PM_PROJECT` resolver derives a project name from the git origin
   URL (with multiple fallbacks) so per-machine state can be keyed across
   worktrees. That whole branch goes away once state lives at repo root —
   `git rev-parse --show-toplevel` is the only resolution rule we need.
3. **Auto-commit, don't push.** PM writes auto-commit their own
   `.canopy/pm/` updates with a `chore(canopy-pm): …` prefix. Pushing remains
   the user's explicit action (`/ship`, manual push, or the existing
   autonomous-mode PR flow).
4. **Auto-migrate, then get out of the way.** First time PM runs in a
   project after this lands, if `~/.canopy/pm/<derived-name>/` exists and
   `<repo>/.canopy/pm/` doesn't, copy files across, commit them on the
   current branch, mark the old dir `.migrated`. No prompts, no manual
   command.

## Section 1 — Storage layout

```
<repo>/.canopy/pm/
├── autonomous.yaml      # PM autonomous config
├── context.md           # accumulated project context
├── learnings.md         # accumulated learnings across runs
└── runs/                # per-cycle run logs (YYYY-MM-DD-<lens>.md)
```

All four are committed to git. No part of `.canopy/pm/` is gitignored.

The `.canopy/` namespace at repo root is shared with PR #37's existing usage
(`.canopy/lenses/`, `.canopy/README.md`, `.canopy/run-artifacts.yaml`). PM
adds a `pm/` subdir alongside; nothing else moves.

## Section 2 — Path resolution

Replace today's `CANOPY_PM_PROJECT`/`CANOPY_PM_DIR` derivation (origin URL
parse with several fallbacks) with a single rule:

```bash
# Inside a git repo (worktree or main checkout):
CANOPY_PM_DIR="$(git rev-parse --show-toplevel)/.canopy/pm"

# Outside a git repo (rare; PM rarely useful here):
CANOPY_PM_DIR="$HOME/.canopy/pm/$(basename "$(pwd)")"
```

When the cwd is inside an emdash/conductor worktree,
`git rev-parse --show-toplevel` returns the worktree's root — that is the
intended target. PM writes land in the worktree's branch and propagate to
main via the worktree's normal merge / PR flow.

`CANOPY_PM_PROJECT` (the derived project-name string) is removed entirely.
No code path needs it once paths are repo-relative.

## Section 3 — Write semantics

PM operations that update files in `.canopy/pm/` follow this pattern:

1. Write the file(s) as today.
2. Stage only the `.canopy/pm/...` paths: `git add -- .canopy/pm/...`.
3. Commit with a scoped message:
   `chore(canopy-pm): <human description of the update>`.
4. Do not push. Push is the user's job (or autonomous mode's existing PR
   flow).

Staging only `.canopy/pm/` paths means PM never accidentally commits
unrelated working-tree changes. If the index has other staged changes from
the user, those are left alone — the commit includes only the `.canopy/pm/`
delta. (Implementation note: use `git commit --only -- .canopy/pm/...` or
explicit `git add` of just those paths, not `git commit -a`.)

If `<repo>/.canopy/pm/` has uncommitted user edits at the moment PM goes to
write, PM still runs and creates the commit; the user's prior uncommitted
edits to those same files become part of the commit (this is desired —
users editing PM artifacts manually want their edits preserved alongside
PM's update). For files outside `.canopy/pm/`, nothing changes.

## Section 4 — Parallel-worktree behavior

Accepted tradeoff: in the rare case the user runs PM-state-touching
operations in two parallel worktrees of the same project (and on different
branches), the resulting `learnings.md`/`context.md`/`runs/` divergence is
handled as a normal git merge.

This is acceptable because:
- PM autonomous mode already enforces a clean-worktree precondition (PR #31)
  and ships through PRs, so concurrent autonomous runs in the same project
  are unusual.
- Interactive `/canopy:pm-scout` is infrequent enough (lens-rotated, manually
  triggered) that two-worktree races are rare.
- When they do happen, the conflicts are in markdown files and runs-dir
  filenames (date-stamped per lens), which usually merge cleanly or with
  minimal manual edit.

No locking, no shared `.git/canopy/` mirror, no main-checkout routing — the
extra machinery isn't worth the rare conflict cost.

## Section 5 — Migration

On every PM-skill entry that resolves `CANOPY_PM_DIR`, run a one-shot
migration check:

1. Compute `CANOPY_PM_DIR` per Section 2.
2. If `<repo>/.canopy/pm/` already exists AND has any file in it, skip.
3. Otherwise, attempt to derive the legacy `<project>` name using today's
   origin-URL logic and check `~/.canopy/pm/<project>/`. If it exists and
   contains `context.md` or `learnings.md` or `autonomous.yaml`:
   - `mkdir -p <repo>/.canopy/pm/`
   - Copy `autonomous.yaml`, `context.md`, `learnings.md`, `runs/` into the
     new location.
   - `git add -- .canopy/pm/` and commit:
     `chore(canopy-pm): migrate state from ~/.canopy/pm/<project>/`
   - Touch `~/.canopy/pm/<project>/.migrated` with the destination repo path
     and a timestamp.
4. If `<repo>/.canopy/pm/` doesn't exist and there's nothing to migrate,
   PM's existing bootstrap (Phase 0 in autonomous mode) creates the dir +
   files from project signals.

The `.migrated` marker prevents re-migration if the user later deletes
`<repo>/.canopy/pm/` for any reason; we don't want to silently restore old
state that may have intentionally been thrown away. The user can delete
`~/.canopy/pm/<project>/` whenever they like — nothing reads it after
migration.

This migration is idempotent: running PM again after a successful migration
hits the "directory exists with content" early-return on step 2.

Migration runs only inside a git repo. Non-git projects keep using
`~/.canopy/pm/<basename>/` as today (Section 2's fallback).

## Section 6 — Files that change

Code:
- `plugins/canopy/skills/product-management/SKILL.md` — replace path
  resolution snippet (origin-URL derivation → `git rev-parse --show-toplevel`),
  drop the "why this location, not `.claude/pm/`" rationale paragraph, add
  a brief "where state lives" section pointing at `<repo>/.canopy/pm/`.
- `plugins/canopy/skills/product-management/scripts/validate_autonomous_config.py`
  — update path references in error messages and any hard-coded paths.
- `plugins/canopy/skills/product-management/templates/autonomous/cycle.md` —
  same path-resolution swap, drop the worktree-ephemerality justification.
- `plugins/canopy/skills/product-management/templates/autonomous/config-schema.md`
  — update header and examples.
- `plugins/canopy/agents/pm-supervisor.md` — drop `basename`-of-toplevel
  (it never matched the SKILL.md derivation anyway), use the new resolver.
- `plugins/canopy/commands/pm-status.md`,
  `plugins/canopy/commands/pm-scout.md`,
  `plugins/canopy/commands/pm-autonomous.md` — same path-resolution swap.
- New helper: `plugins/canopy/skills/product-management/scripts/resolve_pm_dir.sh`
  (or inline bash snippet shared across the above markdown files) that
  encapsulates "resolve CANOPY_PM_DIR, run migration if needed, echo path".

Tests:
- New: `tests/test_pm_state_path_resolution.py` — verifies the resolver
  returns `<repo>/.canopy/pm` inside a repo and `~/.canopy/pm/<basename>`
  outside.
- New: `tests/test_pm_state_migration.py` — fixture: legacy
  `~/.canopy/pm/foo/` + fresh repo with no `.canopy/pm/`. Run resolver,
  assert files copied, assert commit created, assert `.migrated` marker
  written, assert second invocation is a no-op.
- Existing: any test that hard-codes `~/.canopy/pm/` paths (audit and
  update).

Docs:
- `.claude/CLAUDE.md` — note `.canopy/pm/` as the canonical PM-state
  location alongside the existing `.canopy/lenses/` reference.
- This spec file.

VERSION + plugin.json bump per CLAUDE.md's hard rule (any change under
`plugins/canopy/` requires a patch bump in both `VERSION` and
`plugins/canopy/.claude-plugin/plugin.json`).

## Section 7 — Edge cases

| Case | Behavior |
|---|---|
| PM runs outside a git repo | Falls back to `~/.canopy/pm/<basename-of-cwd>/`. No migration attempted. PM works as it does today. |
| `<repo>/.canopy/pm/` exists but is empty | Treated as "already initialized, nothing to migrate." PM bootstrap fills it on first use. |
| `<repo>/.canopy/pm/` exists with content | Skip migration. Use what's there. |
| `~/.canopy/pm/<project>/.migrated` marker present | Skip migration unconditionally — even if `<repo>/.canopy/pm/` was later deleted. The marker is a one-way signal that this user has already moved on from the legacy location for this project. |
| User deletes `<repo>/.canopy/pm/` intentionally | PM bootstrap recreates an empty `.canopy/pm/` on next run. Old `~/.canopy/pm/<project>/` stays put with its `.migrated` marker — not restored. |
| Repo has uncommitted user edits in `.canopy/pm/` when PM writes | PM commits its delta (which includes the user's unstaged edits to those files); Section 3 covers this. |
| Repo has no remote configured | Auto-commit still happens locally. Cross-machine portability requires a remote, but PM doesn't enforce one. |
| Two parallel worktrees both write in the same hour | Each commits to its own branch. Merging produces normal markdown conflicts. Section 4 covers. |
| Bare repo | PM doesn't run in bare repos. Not handled. |
| Empty migration source (legacy dir exists but is empty) | No-op; PM bootstrap fills `<repo>/.canopy/pm/` from project signals as if first-run. |

## Section 8 — What stays unchanged

- The global `~/.claude/canopy/` data layout (session log, observations,
  proposals, runs, repo-map, workbench-token).
- The PM autonomous flow's PR-based shipping; this design just changes
  *where* PM reads/writes state files, not how it ships work.
- Lens rotation, working-backwards email critique, all higher-level PM
  behavior.
- All existing `canopy` CLI surface and skill entry points.

## Acceptance criteria

- After this lands and the user runs PM in a project for the first time,
  `<repo>/.canopy/pm/` exists with the migrated files and a single
  `chore(canopy-pm): migrate state …` commit on the current branch.
- `~/.canopy/pm/<project>/.migrated` marker written; old files left in
  place.
- Second PM invocation in the same repo is a no-op for migration; reads
  and writes happen against `<repo>/.canopy/pm/`.
- Resolver test passes inside a worktree (returns the worktree's
  `.canopy/pm`).
- Resolver test passes outside a git repo (returns the home-dir fallback).
- On the second mac account, after `git pull`, `<repo>/.canopy/pm/`
  carries the same content with no extra setup.
