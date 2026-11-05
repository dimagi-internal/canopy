# Plugin runtime bundle — how skills reach the Python runtime

The installed plugin cache is **self-contained**: the updaters (`/canopy:update`
Step 2 and the fleet session-start hook) sync the repo-root runtime — `src/`,
`scripts/`, `evals/`, `pyproject.toml`, declared in
`plugins/canopy/.claude-plugin/runtime.json` — into `<installPath>/runtime/`
alongside the plugin. The runtime a skill executes is therefore version-locked
to the installed plugin **by construction** (same sync, same SHA), not by a
process step that can be forgotten.

**The ONE idiom.** Every bash block in plugin markdown that touches the runtime
starts with the canonical two-liner and goes through the shared resolver:

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
```

then either `uv run --project "$CANOPY_ROOT" canopy <subcommand> …` (CLI; cwd
is preserved, so cwd-sensitive commands like `test-audit collect .` still work)
or `(cd "$CANOPY_ROOT" && uv run python -m scripts.ddd.<module> …)` (module
runs). `uv` materializes `.venv` inside the runtime dir on first use.

Resolver ladder (`plugins/canopy/scripts/canopy-runtime.sh`): `$CANOPY_RUNTIME_ROOT`
env override → `<plugin>/runtime` (production) → the script's containing repo
(when run from a checkout's copy) → marketplace clone (bootstrap/fresh-install
gap, self-heals next session start) → dev checkouts **last**, deliberately — an
edited checkout must never silently shadow the installed version.

**Rules (CI-enforced by `tests/test_plugin_runtime_resolution.py`):**
- Plugin markdown must NOT reference `~/emdash-projects/canopy`,
  `~/emdash/repositories/canopy`, or `~/.claude/plugins/marketplaces/canopy`
  directly, and must not use bare `uv run canopy`. Allowlisted exceptions
  (update/setup/doctor/video-engine) are enumerated in the test with reasons.
- The global `canopy` CLI on PATH is a convenience (humans at a shell, fleet
  shims like ACE's `bin/ace-email`) — **never a skill dependency**.
- `video-engine/` is the one dev-checkout exception: Remotion's `node_modules`
  is too heavy to bundle per plugin version, so local render resolves a dev
  checkout where `/canopy:setup` ran `npm ci`, with a loud error otherwise.
- Canopy's hooks are **plugin-managed** (`plugins/canopy/hooks/hooks.json` →
  `${CLAUDE_PLUGIN_ROOT}/hooks/…`). Repo-root `hooks/post_tool_use.py` is the
  retired capture hook, a no-op kept for machines whose settings.json still
  registers it; `/canopy:setup` removes that legacy entry.

## Cache Path Resolution

Plugin caches live at `~/.claude/plugins/cache/<plugin>/<plugin>/<version>/`
(double-nested by design — the inner dir name matches the outer plugin name).
The version segment changes every time the plugin is upgraded, so any path
constructed by string-concatenating a known version is fragile.

**Always derive the install path from `installed_plugins.json` instead of
hardcoding a version.** Canonical one-liner:

```bash
python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])"
```

For the same shape per skill, use `canopy skills list --json-output` —
each entry now carries `installed_version` and `cache_path` fields. No more
string-concatenated versions.
