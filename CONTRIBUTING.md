# Contributing to Canopy

This is the human-readable guide to making a change without breaking the release
mechanism. It is a **distillation of `CLAUDE.md`** — that file is authoritative
and exhaustive (and is loaded into every agent session). When the two disagree,
`CLAUDE.md` wins, and this file should be re-synced to match.

Before your first PR, read this top to bottom. It's short, and every section here
exists because skipping it has actually bitten someone.

## Before you contribute, use it

You can't contribute well to a self-improving system you've never watched improve
something. Install it (`/canopy:setup`), confirm `canopy doctor` is green, and live
with the core loop for a few days:

```
canopy improve --dry-run     # analyze → propose, no implementation
canopy proposals list        # what it wants to build
canopy patterns              # recurring friction it's seeing
```

See the [README](README.md) for the full user-facing tour.

## The three copies — never patch the cache

There are **three copies of canopy** on a machine; only one is editable:

| Copy | Editable? |
|------|-----------|
| `plugins/canopy/` in this repo | ✅ **all changes start here** |
| `~/.claude/plugins/cache/.../canopy/` (what Claude Code actually runs) | ❌ never hand-edit |
| `~/.claude/plugins/marketplaces/canopy/` (the update channel) | ❌ never edit |

**Never copy, rsync, or write files into `~/.claude/plugins/cache/`, and never
hand-edit `installed_plugins.json`.** That's "local patching" — it bypasses the
plugin system, creates version mismatches, and makes bugs miserable to diagnose.
If you feel the urge, stop and run `/canopy:update`.

## ⛔ The #1 mistake: forgetting to bump the version

If you change **anything** under `plugins/canopy/` (skills, commands, agents, even
the `plugin.json` description), you **must** bump the version:

```
uv run canopy version bump
```

This advances all three version files together — `VERSION`,
`plugins/canopy/.claude-plugin/plugin.json`, and both fields in
`.claude-plugin/marketplace.json`. **Do not edit them by hand** — partial bumps are
exactly how those files drift. Always bump the **patch** (last) number.

**Why it matters:** the version bump is the *only* signal that tells installed
sessions there's new work to pick up. Skip it and `/canopy:update` reports
`UP_TO_DATE` forever, every existing session keeps running the old cached copy, and
**your PR effectively didn't ship** even though it merged. CI flags this, but on
this private repo the merge button isn't gated on CI — so discipline is the real
defense. A `PreToolUse` guard hook also blocks the push if you forgot (override
with `CANOPY_ALLOW_PUSH_NO_BUMP=1`, almost always wrong).

Changes **outside** `plugins/canopy/` (this file, the README, the orchestrator
under `src/`) do **not** need a version bump.

## The ship flow — always PR, then self-merge

The maintainer does **not** review canopy PRs. Opening one and waiting for review
just strands the work — you open *and merge* your own.

```bash
# from your worktree branch, work committed (+ version bumped if plugins/ changed):
git push -u origin <branch>
gh pr create --title "..." --body "...\n\nCloses #<issue>"
gh pr merge <n> --merge          # don't wait for review
```

The PR is a record-keeping + CI step, not a review gate. The **real** gate (since
no human reviews) is you verifying before you merge:

- `uv run pytest` passes (or only known-unrelated failures remain)
- for `plugins/canopy/` changes, `canopy version verify` is green

Then **immediately run `/canopy:update`** in your session so you're not running
stale cached code, followed by `/reload-plugins` to activate it. This step is
mandatory — skipping it is how you end up debugging code that isn't what's on disk.

### Worktree note

This repo uses emdash-managed worktrees. You **cannot** `git checkout main` from a
worktree. To merge by hand (the fallback when `gh` is unavailable or a conflict
needs resolving):

```bash
cd ~/emdash-projects/canopy && git merge <branch-name> && git push
```

## Skill-authoring foot-guns

These have each reddened CI or silently broken a skill. Full detail lives in
`CLAUDE.md` § Skill Authoring.

- **No `$1`/`$2` in skill/command markdown.** Claude Code's slash-command argument
  expansion strips them before the shell sees them, causing silent empty-string
  failures. Move complex bash into `bin/`/`scripts/` and invoke it.
- **Don't collide with built-in command names** (`/doctor`, `/model`, `/compact`,
  etc.). A plugin entry with a reserved name silently routes to the built-in — and
  can truncate the whole skill-description block from the system prompt.
- **Command/skill name collisions need Pattern B.** If both `commands/<name>.md`
  and `skills/<name>/SKILL.md` exist, the Skill tool silently resolves to the
  command file. Colliding commands must read their SKILL.md from disk explicitly.
- **Mind the description budget.** Skill `description` frontmatter is surfaced in
  the system prompt; too many/too long and Claude Code drops skills wholesale. Use
  `canopy skills budget` and `canopy skills dropped` to diagnose.
- **Plugin hooks are stdlib-only.** `plugins/canopy/hooks/*.py` run under system
  `python3` with no PyYAML — keep them to the standard library.

`tests/test_command_skill_collisions.py` and
`tests/test_builtin_command_collisions.py` enforce the collision rules — adding a
bad entry fails CI.

## Tests

```
uv run pytest        # from the project root
```

A handful of browser-dependent tests error on collection unless the optional
extras are installed: `pip install -e '.[browser]'`.
