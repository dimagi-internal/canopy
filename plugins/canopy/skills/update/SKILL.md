---
name: update
description: Update the canopy plugin to the latest version from GitHub
---

# Update Canopy

**This is a rigid, scripted skill. Run the bash blocks EXACTLY as written. Do NOT
explore, ls, glob, read files, or improvise. The scripts below are the complete
procedure — there is nothing else to discover.**

## Step 1: Fast version check (ONE command)

Reads the remote VERSION via `git fetch` against the local marketplace clone —
uncached, unlike `raw.githubusercontent.com` (CDN-cached 1–5 min, which would
spuriously report `UP_TO_DATE` right after a push). Completes in ~1–3s
(dominated by the network fetch). The script prints exactly one line.

```bash
bash "$(sed -n '/"canopy@canopy"/,/\]/{ s/.*"installPath": *"\([^"]*\)".*/\1/p; }' "$HOME/.claude/plugins/installed_plugins.json" | head -1)/scripts/canopy-update-check.sh"
```

The comparison is on the **commit SHA**, not the version number — two commits on
`main` can carry the same version (parallel PRs both bump to N, and GitHub does
not re-run the loser's version check when the base moves). A SHA can't collide
that way.

**Read the single output line:**
- `UP_TO_DATE <version>` → Tell the user "Already up to date at **vX.Y.Z**." and **STOP. Do nothing else.**
- `UPGRADE_AVAILABLE <old> <new>` → Continue to Step 2.
- `ERROR <reason>` → Show the error to the user and **STOP**.

`<old>` and `<new>` are sometimes the SAME version — that is a real upgrade, not a
bug: the code differs even though the label was reused. Use `<new>` for Step 2.

## Step 2: Pull, install, and register (ONE command)

Run this single bash command. Replace `NEW_VERSION` with the remote version
from Step 1:

```bash
NEW_VERSION=<version from step 1> && \
cd ~/.claude/plugins/marketplaces/canopy && \
echo "ON BRANCH: $(git rev-parse --abbrev-ref HEAD)" && \
git checkout main 2>&1 && \
echo "PULLING: git pull origin main" && \
git pull --ff-only origin main 2>&1 && \
mkdir -p ~/.claude/plugins/cache/canopy/canopy/$NEW_VERSION && \
rsync -a --delete --exclude=node_modules --exclude=runtime \
  ~/.claude/plugins/marketplaces/canopy/plugins/canopy/ ~/.claude/plugins/cache/canopy/canopy/$NEW_VERSION/ && \
mkdir -p ~/.claude/plugins/cache/canopy/canopy/$NEW_VERSION/runtime && \
rsync -a --exclude=.venv --exclude=__pycache__ --exclude=node_modules \
  ~/.claude/plugins/marketplaces/canopy/src \
  ~/.claude/plugins/marketplaces/canopy/scripts \
  ~/.claude/plugins/marketplaces/canopy/evals \
  ~/.claude/plugins/marketplaces/canopy/pyproject.toml \
  ~/.claude/plugins/cache/canopy/canopy/$NEW_VERSION/runtime/ && \
echo "RUNTIME BUNDLE: synced" && \
( cd ~/.claude/plugins/cache/canopy/canopy/$NEW_VERSION && { command -v npm >/dev/null 2>&1 && npm install --no-audit --no-fund >/dev/null 2>&1 && echo "GWS DEPS: installed" || echo "GWS DEPS: skipped (npm missing or install failed — canopy-gws MCP needs: cd $PWD && npm install)"; } ) && \
cd ~/.claude/plugins/marketplaces/canopy && python3 -c "
import json, subprocess, os
from datetime import datetime, timezone

home = os.path.expanduser('~')
version = '$NEW_VERSION'
cache_path = f'{home}/.claude/plugins/cache/canopy/canopy/{version}'
sha = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')

path = f'{home}/.claude/plugins/installed_plugins.json'
with open(path) as f:
    data = json.load(f)

entries = data.get('plugins', {}).get('canopy@canopy', [{}])
entries[0]['version'] = version
entries[0]['installPath'] = cache_path
entries[0]['gitCommitSha'] = sha
entries[0]['lastUpdated'] = now

with open(path, 'w') as f:
    json.dump(data, f, indent=2)
    f.write('\n')

# Verify
with open(path) as f:
    check = json.load(f)
cv = check['plugins']['canopy@canopy'][0]['version']
with open(f'{home}/.claude/plugins/marketplaces/canopy/plugins/canopy/.claude-plugin/plugin.json') as f:
    mv = json.load(f)['version']

if cv == mv:
    print(f'VERIFIED: v{cv} installed and matches GitHub')
else:
    print(f'MISMATCH: installed v{cv} but GitHub has v{mv}')
"
```

Three details in there are load-bearing, all learned the hard way on 2026-07-28:

- **`git checkout main` first.** The clone gets parked on feature branches, and
  `git pull origin main` from a parked branch merges main INTO that branch instead
  of updating the channel. (It was found on `ddd/preflight-applies-scroll`.)
- **`--ff-only`.** If local main has diverged, fail loudly rather than quietly
  writing a merge commit into the update channel.
- **`--delete` on the plugin rsync.** The cache dir is keyed by version, so when a
  version number gets reused the dir already exists with the OTHER commit's files.
  Without `--delete`, a plain overlay leaves that code in place. `node_modules` and
  `runtime` are excluded because both are rebuilt by the steps right after this one.

**Read the output:**
- `VERIFIED` → continue to Step 3 (the plugin cache is updated; the CLI still needs deploying).
- `MISMATCH` → Tell the user the update failed and show the mismatch. **Do not run Step 3.**

## Step 3: Deploy the global CLI (ONE command) — convenience + fleet compat

Since the runtime bundle (Step 2), canopy **skills** no longer depend on the global `canopy`
command — they run the bundled runtime via `scripts/canopy-runtime.sh` + `uv run --project`,
version-locked to the plugin. The global CLI remains deployed for humans at a shell and for
fleet agents' shims (e.g. ACE's `bin/ace-email`), from the SAME marketplace clone the plugin
came from (never an editable dev-checkout, which silently drifts with whatever branch is
checked out — that bug stranded `canopy harvest` from a fresh session).

**`--reinstall` is required, not optional.** The Python package version is pinned (`0.1.0`); it does
NOT bump with the plugin VERSION. So `uv tool install --force` alone keys on the unchanged version
and serves a **cached build** — silently shipping stale CLI code (this stranded `harvest --full`).
`--reinstall` forces a rebuild from the freshly-pulled source.

```bash
uv tool install --reinstall --force "$HOME/.claude/plugins/marketplaces/canopy" 2>&1 | tail -3 && \
  canopy --help >/dev/null 2>&1 && echo "CLI DEPLOYED: $(canopy --version 2>/dev/null || echo ok)" \
  || echo "CLI DEPLOY FAILED — run: uv tool install --reinstall --force ~/.claude/plugins/marketplaces/canopy"
```

- `CLI DEPLOYED` → Tell the user: "Updated canopy to **vX.Y.Z** (plugin + CLI, verified). Run `/reload-plugins` to activate the plugin."
- `CLI DEPLOY FAILED` → show the error; the plugin updated but the CLI didn't (commands like `canopy harvest` may be stale).

## Rules

- The CLI is **non-editable**, installed from the marketplace clone — NEVER an editable install of
  `~/emdash-projects/canopy` (that couples `canopy` to your dev branch). For CLI dev, `uv run` from a worktree.

- **Run EXACTLY the bash blocks above.** No exploring, no ls, no reading files, no globbing.
- Always pull from `~/.claude/plugins/marketplaces/canopy` — NEVER from `~/emdash-projects/canopy`
- If Step 1 says UP_TO_DATE, STOP immediately. Do not run Step 2 **or Step 3** (CLI already current).
- Step 3 (CLI deploy) runs on every real update — the plugin and CLI ship together.
- Always tell the user to run `/reload-plugins` after a successful update.
