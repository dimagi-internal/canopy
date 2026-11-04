# Changing your own repo (the `agent_edit` rail)

**The rule.** Only an asker who holds `agent_edit` on you (the `editor` role and above: editors,
admins, the owner; canopy-web lists it in the envelope's `permissions`) may change YOU, including
your own repo. Everyone else may read and write GitHub freely, but not your repo. Issues,
comments, other repos and a PR from a fork are all fine.

canopy-web enforces `agent_edit` for changes made through canopy-web. It cannot see what a session
does with `git` and `gh` directly, so the fleet gating hook (`gating_guard.py --session`) carries
the session-side half.

## What is refused (when the turn's asker lacks `agent_edit`)

Only writes to YOUR OWN repo (the `origin` of your clone, or the repo URL in `config/agent.json` /
`.claude-plugin/plugin.json`):

- `git push` of any branch or tag to it (your clone's origin, its URL, or `git -C <dir> push`),
- `gh pr create|merge|edit|close|review|ready|reopen|lock|unlock|update-branch` on it,
- `gh release create|edit|delete|upload|delete-asset` and `gh repo edit|delete|rename|archive|unarchive|sync|deploy-key` on it,
- `gh api` with `-X POST|PUT|PATCH|DELETE`, or with `-f/-F/--field/--raw-field/--input` and no
  `-X`, against `repos/<you>/...` (except `issues`, a PR's review comments and `forks`).

## What stays open

- `gh issue create|comment|close ...` on your repo, and `gh api repos/<you>/issues/...`.
- A PR **from a fork**: `gh repo fork <you>`, push to the fork, then
  `gh pr create -R <you> --head <fork-owner>:<branch>`.
- Any write to ANY OTHER repo, and every read.
- Editing local files and committing in your clone: nothing leaves the machine until a push.
  (The Write/Edit tools are not railed on `skills/`, `persona.md`, `CLAUDE.md` or `config/`:
  agents legitimately write state under `config/`, and a path rail cannot tell a state file
  from an edit to yourself. The push is the line.)

When you are refused, the message says so in plain words. Do not look for another route to the
same write; tell the asker they need the editor role (or offer the issue / fork-PR path).

## When it applies

Only to a session that belongs to an agent AND is a canopy turn: a runner's `$CANOPY_TURN_ID`, a
local turn (`~/.canopy/local/<session>.json`), or the laptop's by-task record
(`~/.canopy/caller/by-task/<task>.json`, unless the owner has taken over the keyboard). A person
running Claude Code in your repo by hand has no turn and is untouched; so is canopy's own repo.

The envelope is read from `~/.canopy/caller/<turn_id>.json` only (the file's own `turn_id` must
match its name, no symlink out). For a turn that has one and wants an own-repo write, a missing,
unreadable or mismatched envelope fails CLOSED. An older canopy-web envelope with no
`permissions` list falls back to `relationship` (owner, admin, system allowed).

## Forging

The fleet baseline also refuses, for any agent session: assigning `CANOPY_TURN_ID`,
`CANOPY_CALLER` or `CANOPY_SCOPED_TURN` in a command; writing into `~/.canopy/caller/` or
`~/.canopy/local/` (shell, Edit, Write). Read the envelope with `canopy caller tier`.

## What it does not cover

A guard rail, not a boundary: it reads the command's own words. It does not see a command built
from variables (`git -C "$WT" push`), a script file or other program that pushes, a push through
another tool or library, `gh api graphql` mutations, `gh api repos/<you>/issues/<n>` used to
close a PR (a PR is an issue), `gh workflow|secret|variable`, a token used with `curl`, or a
direct edit of the repo on the runner's disk by another process. canopy-web (not this hook) is
the boundary for changes made through canopy-web.
