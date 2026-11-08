# Releasing and updating canopy

Canopy is one versioned artifact: the plugin and the CLI ship together from `main`. There is no
`CHANGELOG.md`: release history lives in `git log` and the GitHub PRs.

## Never patch the installed plugin

There are three copies of canopy on a machine; only the first is editable:

| Location | What it is | May I edit it? |
|----------|------------|----------------|
| `plugins/canopy/` in **this repo** | Source of truth | Yes: all changes start here |
| `~/.claude/plugins/cache/canopy/canopy/<version>/` | The **installed** plugin Claude Code runs | Never hand-edit |
| `~/.claude/plugins/marketplaces/canopy/` | The update **channel** `/canopy:update` pulls from | Never develop here, and never leave it on a branch |

**The channel clone stays on `main`.** Both updaters act on whatever ref is checked out: the skill's
pull would merge main into a parked branch, and the fleet hook's `git reset --hard origin/main` would
rewrite that branch's ref. The skill checks out `main` first; the hook switches to `main` and refuses
when that would strand uncommitted work. If an update looks wrong, check first:
`git -C ~/.claude/plugins/marketplaces/canopy rev-parse --abbrev-ref HEAD`.

**Never copy, rsync or write files into `~/.claude/plugins/cache/`, or edit
`~/.claude/plugins/installed_plugins.json` by hand.** That bypasses the plugin system, creates version
mismatches and makes bugs hard to diagnose. Use `/canopy:update`. Enforced by
`hooks/pre_tool_use_plugin_cache_guard.py`.

## Bump VERSION for anything you want deployed

Any change under `plugins/canopy/` (skills, commands, agents, the plugin.json `description`, anything)
must bump the version; a change to the CLI/package in `src/orchestrator/` you want deployed bumps it
too.

The updaters compare commit SHAs, so an unbumped change does reach installs, but under a version label
it shares with older code. The plugin cache is keyed by version, so the sync overwrites that dir in
place (under any session running from it) and the label can no longer say which code a machine has.

The layers that enforce it:

1. **`uv run canopy version bump`** is the only correct way to advance the version
   (`src/orchestrator/version_bump.py::bump`): `max(local, origin/main) + patch+1`, written to every
   version file (`VERSION`, `plugin.json`, `marketplace.json`, `pyproject.toml`). Don't hand-edit a subset.
2. **`canopy version verify`** confirms they agree (`version_bump.py::verify`); CI fails on a mismatch.
3. **The Claude Code push guard** (below) stops the push before a PR exists.
4. **CI `check-version`** (`.github/workflows/version-check.yml`) runs `canopy version verify-bump` on
   every PR. It is a required status check, so a missing or bad bump blocks the merge. Never `--admin`
   past a red check.

## Update workflow

1. Change `plugins/canopy/` or `src/orchestrator/`.
2. `uv run canopy version bump`.
3. Commit, push, PR, `gh pr merge <n> --auto` (no strategy flag: the merge queue sets it). The
   maintainer does not review; merge it yourself. If main moved and `VERSION` conflicts, rebase, re-bump
   and push; the entry re-queues.
4. **Immediately after merging, run `/canopy:update` in the current session.** It pulls from GitHub,
   creates a new cache dir, updates `installed_plugins.json`, and rebuilds the `canopy` CLI from the
   marketplace clone. The CLI is non-editable: never an editable install of a checkout, which couples
   `canopy` to your dev branch; for CLI dev use `uv run` from a worktree. Without this step the session
   runs a stale plugin and a stale CLI.
5. `/reload-plugins` to activate the new version in the current session. It only reloads skills from the
   existing cache; it does not detect version changes or re-install, which is why `/canopy:update` runs
   first.

New sessions pick up the new version on startup with no manual step.

`.github/workflows/release.yml` publishes a versioned artifact for each merge that bumps `VERSION`;
"released" means merged to `main` with the version bumped.

## Guards

### Claude Code PreToolUse guards (auto-active; the primary defense)

Canopy commits come through Claude Code, so the reliable guard is a `PreToolUse` hook checked into
`.claude/settings.json`: no `git config` step, and it works whatever emdash sets `core.hooksPath` to.
Each guard's docstring names its override variable.

- `hooks/pre_tool_use_version_bump_guard.py` intercepts `git push` and runs the same
  `verify_bump_when_plugin_changed` check CI runs; it denies a branch that touched `plugins/canopy/`
  without advancing VERSION past `origin/main` (forgot to bump, or a parallel worktree claimed your patch
  number).
- `hooks/pre_tool_use_pr_merge_guard.py` intercepts `gh pr merge` and re-runs the bump check against a
  **freshly fetched** `origin/main`. CI checks your branch against main as it was when CI ran, so two PRs
  from the same base can both bump to N; the second merge's cache dir then already exists holding the
  first one's code, and the merged fix reaches nobody. It judges only the checked-out branch's PR and
  fails open on anything it cannot determine.
- `hooks/pre_tool_use_plugin_cache_guard.py` blocks local-patching of the plugin cache.
- `hooks/pre_tool_use_main_branch_guard.py` keeps any non-worktree checkout on `main` (it keys off
  `/worktrees/` in the git-dir): it blocks switching to a non-main branch and creating one there. Feature
  work goes in a worktree. It only sees `git` run as a Bash tool call; branch switches made by emdash
  itself bypass it.

### Git hooks (opt-in; for humans)

`scripts/hooks/` holds `pre-commit` (refuses a half-bump) and `pre-push` (refuses direct pushes to `main`,
runs `canopy version verify-bump`), inert until `git config core.hooksPath scripts/hooks`. In emdash
worktrees they don't fire, so the guards above are what covers the AI flow. `--no-verify` bypasses
either, and is almost always the wrong call: they catch earlier what the required `check-version`
enforces server-side anyway.

## Fleet auto-update

Fleet agents run from a **versioned plugin cache**, not their repo, so a merge to an agent's `main` is
invisible to the running harness until the cache moves. Two pieces, both shipped by canopy, keep it moving.

**A. Session-start auto-updater** (`plugins/canopy/hooks/fleet_session_start_update.py`; its module
docstring lists the opt-out and tuning variables). Registered `async` in `plugins/canopy/hooks/hooks.json`,
so on every session it discovers the fleet (canopy plus any plugin shipping `config/agent.json`) and for
each one pulls the marketplace clone, builds the new version in `~/.claude/plugins/canopy-update-staging/`
(`build_and_swap`), runs `preflight`, renames it over the version-keyed cache dir, and patches
`installed_plugins.json`. It also reinstalls the `canopy` CLI when it is behind the marketplace clone.

- **Never in place.** Running sessions execute from the version dir, and a commit without a bump lands in the
  same one, so the swap is two renames; the old tree is kept under `retired-*` for a day. A failed build,
  preflight or swap (Windows refuses to rename a dir in use) leaves the installed version untouched and the
  registry unpatched; the next session retries.
- **Channel:** a repo that publishes a `fleet-stable` tag is followed at that tag; without one, at `main`.
  `.github/workflows/fleet-stable.yml` moves the tag to the newest main commit that passed every required
  check. There is no soak or canary stage: a bad change is fixed forward. A machine follows `main` with
  `CANOPY_FLEET_CHANNEL=main` or `main` in `~/.claude/canopy/fleet-channel`. A tag that is not on main is
  ignored, and a machine already ahead of the channel is never moved back, so a rollback is a revert on
  main promoted by hand (run the workflow with `sha`). Repo variable `FLEET_HOLD=1` freezes the tag.
  `/canopy:update` installs `main`.
- **Preflight:** the staged `hooks/hooks.json`, `plugin.json`, `config/gating.json` and gating baseline must
  parse, and for canopy each PreToolUse guard must run under this machine's python, allowing a plain
  command, with the gating guard blocking a raw send. Guards fail closed, so one that cannot start would
  block every tool call. Detection is **SHA-driven** (installed `gitCommitSha` vs the channel ref), so it
  self-heals when a version bump was forgotten. The version bump is for label honesty, not for delivery.
- **High blast radius, so built to be inert:** `async` (never blocks startup), a non-blocking lock,
  timeout-bounded subprocesses, all errors swallowed, one line per run to
  `~/.claude/canopy/fleet-update.log`, nothing on stdout.

**B. Bump-on-every-merge (per-agent repo; canopy owns the template).** An `auto-version-bump.yml` workflow in
each agent repo bumps the PATCH version in `plugin.json` + `marketplace.json` on every merge to `main`, which
keeps the label and `claude plugin list` honest. The agent factory (`canopy create-agent`) stamps it into new
agents (`packages/canopy_agent_factory/canopy_agent_factory/_factory.py`, `_AUTO_BUMP_WORKFLOW`). GITHUB_TOKEN
pushes don't retrigger workflows and the bump commit carries `[skip bump]`, so it doesn't loop.
