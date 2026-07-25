#!/usr/bin/env bash
# Resolve $CANOPY_DDD_DIR for the current cwd.
#
# Inside a git repo: <repo-root>/.canopy/ddd
# Outside a git repo: $HOME/.canopy/ddd/<basename-of-cwd>
#
# With --runs: print the RUNS root instead. Run artifacts are deliberately kept
# OUT of the project repo (they accumulate multi-MB generated files and were
# silently bloating checkouts); context.md / learnings.md stay repo-local.
#   Inside a git repo: $HOME/.canopy/ddd/runs/<repo-name>
#   Outside a git repo: <ddd-dir>/runs
#   $CANOPY_DDD_RUNS_DIR overrides both.
#
# Stdout: the resolved path (created if it doesn't exist).
# Stderr: human-readable notes, if any.
#
# Keep in sync with the other resolver: scripts/ddd/runstate._resolve_ddd_dir()

set -euo pipefail

if REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null); then
  CANOPY_DDD_DIR="$REPO_ROOT/.canopy/ddd"
else
  CANOPY_DDD_DIR="$HOME/.canopy/ddd/$(basename "$(pwd)")"
fi

if [ "${1:-}" = "--runs" ]; then
  if [ -n "${CANOPY_DDD_RUNS_DIR:-}" ]; then
    RUNS_DIR="$CANOPY_DDD_RUNS_DIR"
  elif [ -n "${REPO_ROOT:-}" ]; then
    RUNS_DIR="$HOME/.canopy/ddd/runs/$(basename "$REPO_ROOT")"
  else
    RUNS_DIR="$CANOPY_DDD_DIR/runs"
  fi
  mkdir -p "$RUNS_DIR"
  echo "$RUNS_DIR"
  exit 0
fi

mkdir -p "$CANOPY_DDD_DIR"
echo "$CANOPY_DDD_DIR"
