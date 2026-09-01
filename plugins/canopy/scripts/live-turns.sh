#!/usr/bin/env bash
# Which OTHER live turns share my scope? — the duplicate/sibling check from agent-core/turn.md.
#
# WHY THIS IS A SCRIPT AND NOT A ONE-LINER IN THE PROCEDURE
# --------------------------------------------------------
# turn.md used to print this, three times:
#
#     ps aux | grep '[c]laude --session-id' | grep -c -- '--thread <the-ref>'
#
# It reads the scope out of the process's argv, and argv does not survive a
# resume. A session that is resumed — after an interrupt, a stall, a crash, or a
# context handoff — runs as `claude --resume <uuid>` with NO `--thread` and no
# `/<slug>:` in its command line. So the check returns **0**, for every live turn
# including the one running it.
#
# Zero reads as "nobody else is here." It is the all-clear, and it is produced by
# the check failing, silently, in exactly the situation the check exists for: a
# long or resumed turn is precisely the turn a recovery dispatch was sent to
# replace. Measured 2026-09-01 — a hal turn re-ran the documented check mid-turn
# and got 0 while FOUR hal turns were live, two of them holding the very ref it
# was asking about.
#
# The fix is to stop reading scope from argv. A session's scope is in its
# transcript, which survives a resume because the resume replays it.
#
# Three wrong versions of that got written before this one, which is the tell
# that it belongs in code:
#   1. matching `^claude ` in `ps` output — misses absolute-path invocations;
#   2. `head -c 20000` of the transcript — a transcript opens with large
#      system-reminder blocks, so the scope line sits past the truncation and
#      every session reports "not scoped to this ref";
#   3. grepping the whole transcript for the bare ref — matches any sibling that
#      merely DISCUSSED the ref (checking for duplicates is itself a mention), so
#      it over-reports and the stand-down rules fire against innocent turns.
# Each returned a confident wrong number rather than an error.
#
# USAGE
#   live-turns.sh --ref <gmail-thread-id|slack-ts>   # turns scoped to THAT ref
#   live-turns.sh --slug <agent-slug>                # any live turn of that agent
#   live-turns.sh --ref <ref> --slug <slug>          # both counts, labelled
#
# Prints one session id per line under each heading, plus a COUNT= line that
# INCLUDES YOU. So COUNT=1 on --ref is the all-clear; COUNT>1 means go read the
# other session's transcript and apply turn.md's stand-down rules before you
# write or send.
#
# Exit 0 always when it could look; exit 2 if it could not enumerate sessions at
# all, because "I could not check" must never render as "nothing found".

set -uo pipefail

REF=""
SLUG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --ref)  REF="${2:-}"; shift 2 ;;
    --slug) SLUG="${2:-}"; shift 2 ;;
    -h|--help) sed -n '1,50p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac
done

if [ -z "$REF" ] && [ -z "$SLUG" ]; then
  echo "usage: live-turns.sh [--ref <thread-id>] [--slug <agent-slug>]" >&2
  exit 64
fi

PROJECTS="${CLAUDE_PROJECTS_DIR:-$HOME/.claude/projects}"
if [ ! -d "$PROJECTS" ]; then
  echo "cannot read $PROJECTS — NOT a clean result, do not treat as all-clear" >&2
  exit 2
fi

# Session ids of every live claude process, from EITHER flag. `--resume` is the
# one the old check missed entirely.
live_session_ids() {
  ps ax -o args= 2>/dev/null \
    | grep -i '[c]laude' \
    | grep -oE -- '--(session-id|resume) [0-9a-f-]{36}' \
    | awk '{print $2}' \
    | sort -u
}

transcript_for() {
  find "$PROJECTS" -name "$1.jsonl" -type f 2>/dev/null | head -1
}

IDS="$(live_session_ids)"
if [ -z "$IDS" ]; then
  echo "no live claude sessions found — NOT a clean result if you are running inside one," >&2
  echo "which means this check could not see itself and must not be trusted." >&2
  exit 2
fi

report() {
  local heading="$1" pattern="$2" count=0
  echo "$heading"
  for sid in $IDS; do
    f="$(transcript_for "$sid")"
    [ -n "$f" ] || continue
    if grep -qlF -- "$pattern" "$f" 2>/dev/null; then
      echo "  $sid"
      count=$((count + 1))
    fi
  done
  echo "COUNT=$count (includes you)"
  echo
}

# Match the session's OWN scope line, not any mention of the ref. The slash
# command's args are recorded verbatim in the first user message.
[ -n "$REF" ]  && report "turns scoped to ref $REF:" "command-args>--thread $REF<"
[ -n "$SLUG" ] && report "live $SLUG turns:"          "command-name>/$SLUG:turn<"

exit 0
