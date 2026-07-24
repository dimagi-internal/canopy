#!/bin/bash
# canopy-runtime.sh — print the canopy Python-runtime root on stdout.
#
# THE single resolver every canopy skill uses to find the runtime (the
# directory holding pyproject.toml + src/orchestrator + scripts/ddd etc.).
# Skills then run either:
#   uv run --project "$CANOPY_ROOT" canopy <subcommand> ...
#   (cd "$CANOPY_ROOT" && uv run python -m scripts.ddd.<module> ...)
#
# Resolution ladder (first hit wins):
#   0. $CANOPY_RUNTIME_ROOT           — explicit dev override
#   1. <plugin-root>/runtime          — the bundled copy the updaters sync into
#                                       the version-keyed cache. This is the
#                                       normal production path: the runtime is
#                                       version-locked to the installed plugin
#                                       BY CONSTRUCTION (same rsync, same SHA).
#   2. ~/.claude/plugins/marketplaces/canopy — the full repo clone that is the
#                                       update channel; covers a fresh native
#                                       install whose cache predates the
#                                       runtime bundle (the session-start
#                                       auto-updater repairs that on next start).
#   3. dev checkouts                  — last, deliberately: an edited checkout
#                                       must NOT silently shadow the installed
#                                       version. For dev, set CANOPY_RUNTIME_ROOT
#                                       or `uv run` from your worktree directly.
#
# Prints one absolute path and exits 0, or prints an error to stderr and
# exits 1. No other output — callers do CANOPY_ROOT="$(bash <this script>)".
#
# --git: return the first candidate that is ALSO a git checkout (the bundled
# cache runtime is an rsync, no .git). For the rare consumer that needs canopy's
# git history (e.g. session-review cross-referencing recent canopy commits) —
# resolves to the marketplace clone or a dev checkout, never the cache bundle.
set -u

NEED_GIT=0
[ "${1:-}" = "--git" ] && NEED_GIT=1

_ok() {
  [ -f "$1/pyproject.toml" ] && [ -d "$1/src/orchestrator" ] && [ -d "$1/scripts/ddd" ] || return 1
  [ "$NEED_GIT" = "1" ] && [ ! -e "$1/.git" ] && return 1
  return 0
}

if [ -n "${CANOPY_RUNTIME_ROOT:-}" ]; then
  if _ok "$CANOPY_RUNTIME_ROOT"; then
    echo "$CANOPY_RUNTIME_ROOT"
    exit 0
  fi
  echo "ERROR: CANOPY_RUNTIME_ROOT=$CANOPY_RUNTIME_ROOT is set but is not a canopy runtime root" >&2
  exit 1
fi

# $0 lives at <plugin-root>/scripts/canopy-runtime.sh — works identically from
# the cache, the marketplace clone, or a checkout's plugins/canopy/.
PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# If this script's copy lives inside a repo checkout (<repo>/plugins/canopy/),
# the containing repo IS the matching runtime — running a checkout's copy
# explicitly means "use that checkout". From the cache this resolves to
# cache/<name>/<name>/ (no pyproject.toml) and is skipped.
CONTAINING_REPO="$(cd "$PLUGIN_ROOT/../.." 2>/dev/null && pwd)"

for CAND in \
  "$PLUGIN_ROOT/runtime" \
  "${CONTAINING_REPO:-/nonexistent}" \
  "$HOME/.claude/plugins/marketplaces/canopy" \
  "$HOME/emdash-projects/canopy" \
  "$HOME/emdash/repositories/canopy"; do
  if _ok "$CAND"; then
    echo "$CAND"
    exit 0
  fi
done

echo "ERROR: canopy runtime not found (no <plugin>/runtime bundle, marketplace clone, or dev checkout). Run /canopy:update to sync it." >&2
exit 1
