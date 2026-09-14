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
CLONE=~/.claude/plugins/marketplaces/canopy && \
PY=$(command -v python3 || command -v python) && \
cd "$CLONE" && \
echo "ON BRANCH: $(git rev-parse --abbrev-ref HEAD)" && \
git checkout main 2>&1 && \
echo "PULLING: git pull origin main" && \
git pull --ff-only origin main 2>&1 && \
"$PY" "$CLONE/plugins/canopy/hooks/fleet_session_start_update.py" --sync-cache \
  --clone "$CLONE" --version "$NEW_VERSION"
```

**No `rsync`.** This step used to shell out to it twice. rsync does not exist on Windows, and
both calls were inside this one `&&` chain, so Step 2 died at `rsync: command not found`
(exit 127) **after** the git pull and **before** the cache sync and the registry patch. That
leaves the worst available state, silently: the marketplace clone is on the new commit while
`installed_plugins.json` still points at the old cache dir, so the plugin is half-updated and
nothing says so, and unlike the session-start hook's version of this skew it cannot self-heal.
(Measured 2026-09-14 on a Windows operator's machine: cache stuck at 0.2.459, clone at 0.2.476.)
`--sync-cache` reuses `mirror_tree`, the pure-Python mirror the hook in that same file already
had for exactly this reason — one mirror in the codebase, covered by the hook's tests.

`PY=$(command -v python3 || command -v python)` because a Windows git-bash often has only
`python`; a hardcoded `python3` is the same class of assumption as rsync.

Four details are load-bearing, the first three learned the hard way on 2026-07-28:

- **`git checkout main` first.** The clone gets parked on feature branches, and
  `git pull origin main` from a parked branch merges main INTO that branch instead
  of updating the channel. (It was found on `ddd/preflight-applies-scroll`.)
- **`--ff-only`.** If local main has diverged, fail loudly rather than quietly
  writing a merge commit into the update channel.
- **Mirror-with-delete on the plugin dir.** The cache dir is keyed by version, so when a
  version number gets reused the dir already exists with the OTHER commit's files.
  Without the delete pass, a plain overlay leaves that code in place. `node_modules` and
  `runtime` are excluded (and therefore preserved) because both are rebuilt right after.
- **`--sync-cache` exits non-zero on ANY failed stage**, and the registry patch is the last
  thing it does. So a non-zero exit means the registry was NOT moved — the old version stays
  installed and working. Half-synced is the one state it will not leave you in.

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
for attempt in 1 2 3; do
  out=$(uv tool install --reinstall --force "$HOME/.claude/plugins/marketplaces/canopy" 2>&1); rc=$?
  echo "$out" | tail -3
  [ $rc -eq 0 ] && break
  case "$out" in
    *"Access is denied"*|*"os error -2147024891"*|*"being used by another process"*|*"Permission denied"*)
      echo "RETRYING (transient file lock, attempt $attempt/3)"; sleep 3 ;;
    *) break ;;
  esac
done
if [ $rc -eq 0 ] && canopy --help >/dev/null 2>&1; then
  echo "CLI DEPLOYED: $(canopy --version 2>/dev/null || echo ok)"
else
  echo "CLI DEPLOY FAILED (exit $rc) — run: uv tool install --reinstall --force ~/.claude/plugins/marketplaces/canopy"
fi
```

**Why this is a loop and not a one-liner** — two separate defects, both reported 2026-09-14:

- **`… | tail -3 && …` read `tail`'s exit status, not `uv`'s.** A pipeline's status is its LAST
  command's, and `tail` succeeds on anything. So a failed install fell through to the success
  branch and printed **CLI DEPLOYED after uninstalling ten packages and installing nothing** —
  the same wrong-thing-exited-0 shape as the `echo "exit=$?"` idiom. `rc` is captured from the
  command itself, before anything is piped.
- **On Windows the first attempt reliably fails and a plain retry succeeds** — Defender holds the
  files open (`Access is denied. (os error -2147024891)`), the same pattern as
  `claude plugin marketplace add`. The retry is narrow on purpose: only for messages that look
  like contention, so a genuine error still surfaces on the first pass instead of three times.

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
