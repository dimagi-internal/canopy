---
name: canopy-doctor
description: Diagnose canopy plugin health — workbench token, repo-map, session log, hook registration, and skill connectivity. Renamed from `doctor` to avoid colliding with Claude Code's native `/doctor` slash command.
---

# Canopy Doctor

Check that the canopy plugin is correctly configured. The substantive checks
now live in the `canopy doctor` CLI subcommand — this skill runs it and
interprets the result, plus runs the two checks the CLI deliberately leaves
out (system-python hook compatibility and auth preflight, which are
environment-specific and/or network-dependent).

## 1. Core health (CLI)

Run the structured health check:

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
uv run --project "$CANOPY_ROOT" canopy doctor
```

This runs read-only checks for hook registration, session log, repo map,
workbench token (presence, non-empty, mode 600), and the installed plugin
version. It exits 0 when everything passes and non-zero if any check fails,
so it is safe to gate on in scripts/CI.

For machine-readable output (one object with `ok` and a `checks[]` array of
`{name, ok, detail}`):

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
uv run --project "$CANOPY_ROOT" canopy doctor --json-output
```

Report the per-check `name` + `detail`. If `ok` is false, surface the failing
checks' `detail` strings — each one carries its own fix hint (e.g. "run
/canopy:setup", "chmod 600 …").

## 2. Hook Python compatibility (not in CLI)

The hook runs under whatever `python3` is on the user's PATH — often the
Xcode CLT default `python3.9` on macOS, not the project venv. A 3.10+-only
annotation in hook code (`str | None` PEP 604 union syntax, etc.) compiles
fine in CI under the project venv but silently no-ops on real user machines.
This is intentionally outside `canopy doctor` because it depends on the
caller's system python, which the CLI (running under the venv) cannot see.

```bash
SYS_PY=$(command -v python3)
PY_VER=$(python3 --version 2>&1 | awk '{print $2}')
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
HOOK_DIR=""
[ -d "$CANOPY_ROOT/hooks" ] && HOOK_DIR="$CANOPY_ROOT/hooks"

if [ -z "$HOOK_DIR" ]; then
  echo "WARN: could not locate hooks dir — skipping compat check"
else
  FAILED=""
  for f in "$HOOK_DIR"/*.py; do
    [ -f "$f" ] || continue
    if ! "$SYS_PY" -m py_compile "$f" 2>/dev/null; then
      FAILED="$FAILED $(basename "$f")"
    fi
  done
  if [ -z "$FAILED" ]; then
    echo "OK: hooks compile under $SYS_PY ($PY_VER)"
  else
    echo "FAIL: hooks fail to compile under $SYS_PY ($PY_VER):$FAILED"
    echo "  Fix: rewrite the incompatible syntax to support the system python."
  fi
fi
```

## 3. Workbench API connectivity + auth (not in CLI)

`canopy doctor` is intentionally offline and fast. To verify the token works
against the live API and that GitHub / 1Password / AWS SSO credentials are
fresh, run the auth preflight (the same checks are available standalone via
`canopy:auth-preflight`):

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
bash "$CANOPY_ROOT/scripts/canopy-auth-preflight.sh" || true
```

Treat auth-preflight results as informational — they do not change overall
canopy plugin health, but they're worth reporting so the user can fix them
before a deploy.

## Report

After running the above, present a summary. If `canopy doctor` exited
non-zero or any supplementary check FAILed, highlight the fix command. If
everything passed, say "All checks passed — canopy is healthy."
