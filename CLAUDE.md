# Canopy

The operating substrate for the Dimagi agent fleet, shipped as a Claude Code plugin plus a Python CLI
(`canopy`). It gives agents shared rails, turn procedures and identity; talks to canopy-web (the control
plane, a separate repo); and watches Claude Code sessions to build improvements to skills, hooks and itself.
It invokes gstack and superpowers skills headlessly for review, QA and implementation quality.

**How to write this file:** current state only; a rule plus its pointer in ≤ 2 lines; detail in
`docs/architecture/<topic>.md`; history goes in the PR description, not here. `tests/test_claude_md_budget.py`
checks size and history.

## Layout

- `src/orchestrator/` — the Python package behind the `canopy` CLI (`cli.py`), split into framework and product tiers.
- `plugins/canopy/` — the plugin: `skills/`, `commands/`, `agents/`, `hooks/`, `mcp/`, and `agent-core/` (fleet process docs and shared rail bodies every agent reads at runtime).
- `plugins/canopy-web/` — a slim plugin that is only canopy-web's HTTP MCP server with browser sign-in; versions on its own.
- `packages/canopy_agent_factory/` — the published, zero-dependency agent factory; edit templates in `canopy_agent_factory/_factory.py`.
- `scripts/` (`ddd/`, `narrative/`, `walkthrough/`) ships in the plugin runtime; `video-engine/` (Remotion) does not.
- `hooks/` — repo-local Claude Code guards (`.claude/settings.json`).

## Test

- Docs-related tests, hermetically (throwaway HOME, scrubbed env), about 4 s: `scripts/test-docs.sh`.
- `uv run --extra browser pytest` from the root. The browser extra is **required**: without it `scripts/walkthrough/record_video.py` calls `sys.exit` during collection and the session aborts. It installs only the `playwright` package, as CI does.
- `(cd packages/canopy_agent_factory && uv run --with pytest pytest -q)` — sits outside `testpaths`.
- `uv run canopy structure-drift --strict` — the structural self-audit CI runs.

## Ship

Always PR, and merge it yourself: the maintainer does not review canopy PRs, so the PR is CI and record, not a review gate.

```bash
git push -u origin <branch>
gh pr create --title "..." --body "...\n\nCloses #<issue>"
gh pr merge <n> --auto        # no strategy flag: the merge queue sets it
```

- `main` is protected with `enforce_admins`; `pytest`, `check-version`, `structure-drift` and `hook-compat` are required checks. Never `--admin` past a red one.
- **Anything under `plugins/canopy/` needs `uv run canopy version bump`** (and any CLI change you want deployed). Guarded on push and merge. → `docs/architecture/releasing.md`
- After merging: `/canopy:update`, then `/reload-plugins`, or this session keeps running the old plugin and CLI.
- If main moved and `VERSION` conflicts: rebase, re-bump, push; the entry re-queues.
- Keep PRs scoped and the body honest about what was and wasn't verified.
- Direct merge to main is the fallback only, for no `gh` or a conflict that needs hand-resolving.

## Git worktrees

You're usually in a worktree (`git rev-parse --git-dir` contains `/worktrees/`), where `git checkout main` fails.

- A machine can hold several clones of this repo, each parenting worktrees. `git worktree list` says which is yours.
- `main` is often checked out in a worktree, not the clone. Refresh it with `git -C <worktree-on-main> merge --ff-only origin/main`.
- A clone's local `main` can sit far behind `origin/main`, which silently bases new worktrees on old code. Before branching: `git fetch origin` and confirm `git rev-list --left-right --count origin/main...main` reads `0 0`.
- `hooks/pre_tool_use_main_branch_guard.py` keeps non-worktree checkouts on `main`, but only sees `git` run as a Bash tool call; emdash's own switches bypass it. Check, don't assume.
- Never a bare `git stash`: the stack is shared across worktrees and concurrent sessions. Use `git stash push -u -m "<tag>"`.

## Invariants

- **Framework must never import product; product imports framework freely.** The only product→framework coupling lives in the `cli`, `pipeline` and `server` hubs. It's a direction, not a wall: don't split `orchestrator/` into subpackages. Enforced by `tests/test_plugin_boundary.py`; tiers in `src/orchestrator/TIERS.md`.
- **Every canopy-web call resolves transport and auth through `src/orchestrator/canopy_web.py`.** Never the operator's token in an agent's session: it raises `AgentIdentityError`. → `docs/architecture/session-identity.md`
- **Never patch the installed plugin or `installed_plugins.json`; the channel clone stays on `main`.** Guarded by `hooks/pre_tool_use_plugin_cache_guard.py`. → `docs/architecture/releasing.md`
- **Plugin hooks use stdlib only**: they run under whatever python3 `hooks/python.sh` finds, often without PyYAML. Every hook fires in every session on the fleet, so each is fail-open, except the PreToolUse guards, which fail closed (→ `docs/architecture/modules.md` § hooks.json).
- **Plugin markdown reaches Python through the runtime resolver**, never a dev-checkout or marketplace path, never bare `uv run canopy`, never the global CLI. Enforced by `tests/test_plugin_runtime_resolution.py`. → `docs/architecture/plugin-runtime.md`
- **Never hardcode a plugin cache path**: derive it from `installed_plugins.json` or `canopy skills list --json-output`. → `docs/architecture/plugin-runtime.md`
- **Skill authoring**: no `$1`/`$2` in skill bash blocks; a command colliding with a skill follows Pattern B; never reuse a built-in slash-command name; descriptions ≤ 1024 chars. Enforced by `tests/test_command_skill_collisions.py`, `tests/test_builtin_command_collisions.py` and `canopy structure-drift`. → `docs/architecture/skill-authoring.md`
- **`src/orchestrator/inbox_rules.py` is vendored verbatim by canopy-web's runner**, and `tests/test_inbox_rules.py` pins its hash: edit it, re-vendor, update both pins.
- **The Canopy vault is the only credential source.** `src/orchestrator/onepassword.py` is unreferenced on purpose; don't wire it back in.
- **A rail or process fix in `plugins/canopy/agent-core/` reaches the fleet by plugin update, with no per-agent PR.** Agents register no gating hook of their own: canopy's `gating_guard.py --session` merges the baseline and the session's agent's rails. → `docs/agent-operating-model.md`
- **DDD pause gates post to the canopy-web review surface**, never `AskUserQuestion`. → `plugins/canopy/agents/ddd.md`
- **Never extract YAML with awk/sed/grep**: folded scalars and quoted keys truncate silently. Use `yaml.safe_load` or a CLI that does.

## Tool hygiene

- Read with the same absolute path you'll Edit with; worktree-relative paths can resolve to a different file.
- A failing parallel Bash call can cancel its siblings and discard their output. If you need every result, run them in sequence.
- Don't dump binaries with `strings` or `cat`; pipe through `head -c 4000`, or use a real parser.

## Per-project state

- `<repo>/.canopy/` is committed to the project: `pm/` (resolved by `plugins/canopy/skills/product-management/scripts/resolve_pm_dir.sh`; outside a git repo, `$HOME/.canopy/pm/<basename-of-cwd>/`), `lenses/`, `run-artifacts.yaml`, `README.md`.
- `~/.claude/canopy/` holds the cross-project brain on a single machine (`observations/`, `proposals/`, `repo-map.json`). Canopy keeps no session log; Claude Code's transcripts under `~/.claude/projects/` are the record.

## Where to read before touching X

| Touching | Read |
|---|---|
| A `canopy` command | `docs/architecture/cli-reference.md` (grep it), and `canopy <cmd> --help` |
| Any module | `docs/architecture/modules.md` (grep it), `src/orchestrator/TIERS.md` |
| Releases, version bumps, the auto-updater, guards | `docs/architecture/releasing.md` |
| How skills reach Python, cache paths | `docs/architecture/plugin-runtime.md` |
| A skill, command or agent file | `docs/architecture/skill-authoring.md` |
| `analyze` / `improve` / `agent-review`, prompt loading | `docs/architecture/session-analysis.md` |
| Identity, `canopy cred`, the vault, local use | `docs/architecture/session-identity.md` |
| Email, Docs, gog | `docs/architecture/shared-gog-gdrive.md` |
| `/api/agents` client, provenance headers | `docs/architecture/agent-client-rest-contract.md` |
| Agent setup stages | `docs/architecture/agent-setup-lifecycle.md` |
| Agent threads | `docs/architecture/agent-threads.md` |
| The agent model and factory | `docs/agent-operating-model.md` |
| Knowledge bases | `docs/knowledge-bases.md` |
| Vocabulary | `docs/GLOSSARY.md` |
| How docs stay true | `docs/documentation.md` |
| Fleet process, as agents read it | `plugins/canopy/agent-core/` (`using-canopy.md` routes the rest) |
