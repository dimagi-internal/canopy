#!/usr/bin/env bash
# Resolve $CANOPY_PM_DIR for the current cwd.
#
# Inside a git repo: <repo-root>/.canopy/pm
# Outside a git repo: $HOME/.canopy/pm/<basename-of-cwd>
#
# Side effect: if running inside a git repo and the destination is empty AND
# the legacy ~/.canopy/pm/<derived-project>/ directory exists with state,
# copy files in and commit on the current branch (best-effort).
#
# The .migrated marker in the legacy dir is a one-way signal: while it exists the
# legacy state is never copied in again, even if <repo>/.canopy/pm was later
# deleted on purpose.
# Stdout: the resolved path.
# Stderr: human-readable migration notes, if any.

set -euo pipefail

if REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null); then
  CANOPY_PM_DIR="$REPO_ROOT/.canopy/pm"

  # Migration: only if destination is empty (no tracked or untracked files).
  if [ ! -d "$CANOPY_PM_DIR" ] || \
     [ -z "$(find "$CANOPY_PM_DIR" -type f -print -quit 2>/dev/null)" ]; then
    LEGACY_PROJECT=$(git config --get remote.origin.url 2>/dev/null \
      | sed 's|.*[/:]||;s|\.git$||' || true)
    if [ -z "${LEGACY_PROJECT:-}" ]; then
      LEGACY_PROJECT=$(basename "$REPO_ROOT")
    fi
    LEGACY_DIR="$HOME/.canopy/pm/$LEGACY_PROJECT"
    LEGACY_MARKER="$LEGACY_DIR/.migrated"

    if [ -d "$LEGACY_DIR" ] && [ ! -e "$LEGACY_MARKER" ] && \
       { [ -e "$LEGACY_DIR/context.md" ] || \
         [ -e "$LEGACY_DIR/learnings.md" ] || \
         [ -e "$LEGACY_DIR/autonomous.yaml" ]; }; then
      mkdir -p "$CANOPY_PM_DIR"
      [ -e "$LEGACY_DIR/autonomous.yaml" ] && cp "$LEGACY_DIR/autonomous.yaml" "$CANOPY_PM_DIR/"
      [ -e "$LEGACY_DIR/context.md" ]      && cp "$LEGACY_DIR/context.md" "$CANOPY_PM_DIR/"
      [ -e "$LEGACY_DIR/learnings.md" ]    && cp "$LEGACY_DIR/learnings.md" "$CANOPY_PM_DIR/"
      [ -d "$LEGACY_DIR/runs" ]            && cp -R "$LEGACY_DIR/runs" "$CANOPY_PM_DIR/"

      # Best-effort commit. If git commit fails (no identity, hook rejects,
      # etc.) we leave the files staged-or-unstaged for the user to handle
      # and continue. Migration is still considered "done".
      if git -C "$REPO_ROOT" add -- ".canopy/pm" >/dev/null 2>&1; then
        if git -C "$REPO_ROOT" commit -m \
          "chore(canopy-pm): migrate state from ~/.canopy/pm/$LEGACY_PROJECT/" \
          -- ".canopy/pm" >/dev/null 2>&1; then
          echo "resolve_pm_dir: committed migrated state to current branch" >&2
        else
          echo "resolve_pm_dir: copied files but commit failed; review with 'git status' and commit manually" >&2
        fi
      fi

      {
        printf 'migrated_to: %s\n' "$REPO_ROOT/.canopy/pm"
        printf 'timestamp: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
      } > "$LEGACY_MARKER"
      echo "resolve_pm_dir: migrated PM state from $LEGACY_DIR" >&2
    fi
  fi

  mkdir -p "$CANOPY_PM_DIR"
else
  CANOPY_PM_DIR="$HOME/.canopy/pm/$(basename "$(pwd)")"
  mkdir -p "$CANOPY_PM_DIR"
fi

echo "$CANOPY_PM_DIR"
