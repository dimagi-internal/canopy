#!/usr/bin/env bash
# canopy-update-check — fast version check via git fetch (uncached).
#
# Output: exactly one line, one of:
#   UP_TO_DATE <version>
#   UPGRADE_AVAILABLE <local> <remote>
#   ERROR <reason>
#
# What it does:
#   1. Read the installed canopy version AND commit SHA from
#      ~/.claude/plugins/installed_plugins.json using sed (avoiding python3
#      startup overhead).
#   2. Read origin/main's SHA and VERSION via `git fetch` against the local
#      marketplace clone.
#   3. Compare SHAs; print result; exit.
#
# Why SHA and not the version number:
#   The version label is not unique across commits. `canopy version bump` picks
#   max(local, origin/main)+1, so two PRs opened from the same base both claim
#   the same number, and CI's version check only compares each branch against
#   main as it was when that branch's CI ran. On 2026-07-28 both #423 and #429
#   merged as v0.2.369 forty seconds apart. The plugin cache is keyed by version,
#   so the second merge had a cache dir that already existed holding the FIRST
#   one's code — and a version-only check reported UP_TO_DATE, whose documented
#   response is "STOP. Do nothing else." The merged fix simply never reached the
#   machine. A SHA advances on every merge, so it cannot collide this way; this
#   is the same detection the fleet session-start updater has always used.
#
#   Consequence worth knowing: when a version number IS reused, this prints
#   UPGRADE_AVAILABLE with the same version twice (`… 0.2.369 0.2.369`). That is
#   correct, not a bug — the code differs even though the label doesn't.
#
# Why git fetch instead of curl raw.githubusercontent.com:
#   raw.githubusercontent.com is CDN-cached ~1–5 minutes. Right after a push it
#   can serve the PREVIOUS version — a still-valid X.Y.Z — so a regex check
#   passes and the tool reports UP_TO_DATE falsely. That window is the common
#   case for canopy, whose documented flow is "merge, then /canopy:update
#   immediately." `git fetch` hits GitHub's git smart-HTTP endpoint, which is
#   not CDN-cached and reflects refs essentially immediately. The ~1s extra
#   latency over a curl is imperceptible for an interactive command, and Step 2
#   of the update needs this same marketplace checkout anyway (it `git pull`s
#   it), so requiring it here just surfaces a broken checkout one step earlier.
set -u

MARKETPLACE="${CANOPY_MARKETPLACE:-$HOME/.claude/plugins/marketplaces/canopy}"
INSTALLED_PLUGINS="$HOME/.claude/plugins/installed_plugins.json"

# ─── 1. Installed version ────────────────────────────────────
if [ ! -f "$INSTALLED_PLUGINS" ]; then
  echo "ERROR registry_missing"
  exit 0
fi

# Pull "version" out of the canopy@canopy entry without parsing the whole JSON.
LOCAL="$(sed -n '/"canopy@canopy"/,/\]/{ s/.*"version": *"\([^"]*\)".*/\1/p; }' \
  "$INSTALLED_PLUGINS" 2>/dev/null | head -1)"

if [ -z "$LOCAL" ]; then
  echo "ERROR no_local_version"
  exit 0
fi

# The SHA that cache dir was actually built from. Absent on registry entries
# written before SHA tracking existed — handled below by falling back to the
# version compare rather than refusing to answer.
LOCAL_SHA="$(sed -n '/"canopy@canopy"/,/\]/{ s/.*"gitCommitSha": *"\([^"]*\)".*/\1/p; }' \
  "$INSTALLED_PLUGINS" 2>/dev/null | head -1)"

# ─── 2. Remote version (git fetch — uncached, no CDN) ────────
if [ ! -d "$MARKETPLACE/.git" ]; then
  echo "ERROR marketplace_missing"
  exit 0
fi

git -C "$MARKETPLACE" fetch --quiet origin main 2>/dev/null
# Always origin/main, never HEAD: the clone gets parked on feature branches, and
# what ships is main regardless of what is checked out.
REMOTE="$(git -C "$MARKETPLACE" show origin/main:VERSION 2>/dev/null | tr -d '[:space:]')"
REMOTE_SHA="$(git -C "$MARKETPLACE" rev-parse origin/main 2>/dev/null)"

if ! echo "$REMOTE" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "ERROR fetch_failed"
  exit 0
fi

# ─── 3. Compare ──────────────────────────────────────────────
# SHA when we have both sides (authoritative); version otherwise (legacy entry).
if [ -n "$LOCAL_SHA" ] && [ -n "$REMOTE_SHA" ]; then
  if [ "$LOCAL_SHA" = "$REMOTE_SHA" ]; then
    echo "UP_TO_DATE $LOCAL"
  else
    echo "UPGRADE_AVAILABLE $LOCAL $REMOTE"
  fi
elif [ "$LOCAL" = "$REMOTE" ]; then
  echo "UP_TO_DATE $LOCAL"
else
  echo "UPGRADE_AVAILABLE $LOCAL $REMOTE"
fi
