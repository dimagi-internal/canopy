# Plugin runtime bundle — how skills reach the Python runtime

The installed plugin cache is **self-contained**: the updaters (`/canopy:update` Step 2 and the fleet
session-start hook) sync the repo-root runtime into `<installPath>/runtime/` alongside the plugin. The
paths synced are declared in `plugins/canopy/.claude-plugin/runtime.json`; **every path `pyproject.toml`
needs at resolve time must be listed there**, including `[tool.uv.sources]` path deps, because
`uv run --project` dies on a missing one. `tests/test_plugin_runtime_resolution.py` enforces that
coverage. The runtime a skill executes is therefore version-locked to the installed plugin by
construction (same sync, same SHA).

**The ONE idiom.** Every bash block in plugin markdown that touches the runtime starts with this
two-liner and goes through the shared resolver:

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
```

then either `uv run --project "$CANOPY_ROOT" canopy <subcommand> …` (CLI; cwd is preserved, so
cwd-sensitive commands like `test-audit collect .` still work) or
`(cd "$CANOPY_ROOT" && uv run python -m scripts.ddd.<module> …)` (module runs). `uv` materializes `.venv`
inside the runtime dir on first use.

The resolver's ladder is in the header of `plugins/canopy/scripts/canopy-runtime.sh`. Two properties
matter: dev checkouts come **last**, so an edited checkout never silently shadows the installed version
(for dev, set `CANOPY_RUNTIME_ROOT` or `uv run` from your worktree); and a candidate missing any
declared path dep is skipped, so a mis-bundled cache falls through to the marketplace clone.

**Rules (CI-enforced by `tests/test_plugin_runtime_resolution.py`):**
- Plugin markdown must NOT reference `~/emdash-projects/canopy`, `~/emdash/repositories/canopy`, or
  `~/.claude/plugins/marketplaces/canopy` directly, and must not use bare `uv run canopy`. The allowed
  exceptions (update/setup/doctor/video-engine) are enumerated in the test's `ALLOWED` with reasons.
- The global `canopy` CLI on PATH is a convenience (humans at a shell, fleet shims like ACE's
  `bin/ace-email`), **never a skill dependency**.
- `video-engine/` is the one dev-checkout exception: Remotion's `node_modules` is too heavy to bundle per
  plugin version, so local render resolves a dev checkout where `/canopy:setup` ran `npm ci`, with a loud
  error otherwise.
- Canopy's hooks are **plugin-managed** (`plugins/canopy/hooks/hooks.json` → `${CLAUDE_PLUGIN_ROOT}/hooks/…`).
  Repo-root `hooks/post_tool_use.py` is a no-op that stays only so a `settings.json` that still registers
  it does not error on every tool call; `/canopy:setup` removes that entry.

## Cache path resolution

Plugin caches live at `~/.claude/plugins/cache/<plugin>/<plugin>/<version>/` (double-nested: the inner
dir name matches the outer plugin name). The version segment changes on every upgrade, so any path built
by concatenating a known version is fragile.

**Always derive the install path from `installed_plugins.json` instead of hardcoding a version.** The
canonical one-liner is the first line of the idiom above (`…['plugins']['canopy@canopy'][0]['installPath']`).
For the same shape per skill, use `canopy skills list --json-output` (each entry carries
`installed_version` and `cache_path`).
