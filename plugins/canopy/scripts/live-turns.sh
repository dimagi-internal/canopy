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
# FOURTH FAILURE, measured live 2026-09-02 — SCOPE IS NOT ONLY SET BY `:turn`.
# Version 4 read scope from the transcript, which fixed the resume case, but it
# recognised exactly two shapes: `command-args>--thread <ref><` and
# `command-name>/<slug>:turn<`. A turn dispatched as **/eva:chief-of-staff**
# matches NEITHER — a chief-of-staff cycle drains the inbox as one of its
# pillars, so it discovers a thread by scanning and never records it as an arg,
# and its command name is not `:turn`. Both counts therefore came back all-clear
# (`--ref` 1, `--slug` 2) while that session was, at that moment, creating a
# calendar event on the asking turn's ref and starting to write the same Google
# Doc tab. The asking turn was one call from a last-write-wins clobber of a
# shared team doc, mid-write. This matters more than it sounds: the unattended
# dispatches a fleet actually runs on a schedule — chief-of-staff, morning
# briefings, weekly goals — are all non-`:turn` entry points, so `--slug`, whose
# whole job is the wider "a sibling of mine is live somewhere" count, was blind
# to the most common shape of sibling there is.
#
# Fixed here by (a) matching ANY `/<slug>:` entry point for --slug, (b) listing,
# separately from COUNT, live sessions that mention the ref without being scoped
# to it, and (c) warning when live sessions carry no scope line at all. (b) and
# (c) are deliberately NOT folded into COUNT: over-reporting fires the stand-down
# rules against innocent turns, which is wrong version 3 above. They are pointers
# to go READ, and the reading is what decides.
#
# THE INVARIANT ALL FOUR VIOLATE, AND THE ONE TO TEST ANY VERSION 6 AGAINST:
# a check that cannot see something must SAY SO. Every count this prints is a
# lower bound, and it now says that out loud rather than rendering "I could not
# look" as "nobody is there."
#
# USAGE
#   live-turns.sh --ref <gmail-thread-id|slack-ts>   # turns scoped to THAT ref
#   live-turns.sh --slug <agent-slug>                # any live turn of that agent
#   live-turns.sh --ref <ref> --slug <slug>          # both counts, labelled
#
# Prints one session id per line under each heading, plus a COUNT= line that
# INCLUDES YOU. COUNT>1 means go read the other session's transcript and apply
# turn.md's stand-down rules before you write or send.
#
# COUNT=1 is NOT by itself an all-clear. Read the two blocks below it as well —
# sessions that mention the ref without being scoped to it, and live sessions
# with no scope line at all. Either can be a sibling working your item.
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
    -h|--help) sed -n '1,79p' "$0"; exit 0 ;;   # whole header block
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

# Fixed-string match against a session's OWN scope line. Two variants: -F for a
# literal (the ref), -E for a pattern (any of an agent's entry points).
matches_scope() {
  local file="$1" mode="$2" pattern="$3"
  if [ "$mode" = "E" ]; then
    grep -qE -- "$pattern" "$file" 2>/dev/null
  else
    grep -qF -- "$pattern" "$file" 2>/dev/null
  fi
}

report() {
  local heading="$1" mode="$2" pattern="$3" count=0
  echo "$heading"
  for sid in $IDS; do
    f="$(transcript_for "$sid")"
    [ -n "$f" ] || continue
    if matches_scope "$f" "$mode" "$pattern"; then
      echo "  $sid"
      count=$((count + 1))
    fi
  done
  echo "COUNT=$count (includes you)"
  echo
}

# Every live session that carries NO recognizable slash-command scope line at all.
# COUNT cannot see these, so it must not be read as a complete answer.
unscoped_sessions() {
  for sid in $IDS; do
    f="$(transcript_for "$sid")"
    [ -n "$f" ] || continue
    grep -qE -- '<command-name>/[a-z0-9_-]+:' "$f" 2>/dev/null || echo "$sid"
  done
}

# Match the session's OWN scope line, not any mention of the ref. The slash
# command's args are recorded verbatim in the first user message.
if [ -n "$REF" ]; then
  report "turns scoped to ref $REF:" F "command-args>--thread $REF<"

  # A turn that was NOT dispatched with --thread can still be working this ref:
  # an inbox-draining entry point (chief-of-staff, a morning briefing, a plain
  # unscoped turn) discovers the ref by scanning and never records it as an arg.
  # Those sessions are invisible to the COUNT above. Surface them SEPARATELY —
  # a mention is not proof of ownership (a sibling's own duplicate check mentions
  # the ref too), so this list is a read-these pointer, never a stand-down count.
  hits=""
  for sid in $IDS; do
    f="$(transcript_for "$sid")"
    [ -n "$f" ] || continue
    grep -qF -- "command-args>--thread $REF<" "$f" 2>/dev/null && continue
    grep -qF -- "$REF" "$f" 2>/dev/null && hits="$hits $sid"
  done
  if [ -n "$hits" ]; then
    echo "sessions NOT scoped to this ref that nonetheless mention it —"
    echo "the COUNT above cannot see these; go read them before you write or send:"
    for sid in $hits; do echo "  $sid"; done
    echo "(a mention may only be that session's own duplicate check — read, then decide)"
    echo
  fi
fi

if [ -n "$SLUG" ]; then
  # ANY of the agent's entry points, not just `turn`. A scheduled or cron
  # dispatch routinely arrives as /<slug>:chief-of-staff, /<slug>:morning-briefings,
  # /<slug>:goals-weekly … and every one of them drains an inbox and writes to the
  # same Sheets, Docs and calendars a `turn` does. Matching only `:turn<` returned
  # a false all-clear for exactly that case.
  report "live $SLUG sessions (any entry point):" E "<command-name>/$SLUG:"
fi

UNSCOPED="$(unscoped_sessions)"
if [ -n "$UNSCOPED" ]; then
  echo "WARNING — every COUNT above is a LOWER BOUND."
  echo "These live sessions carry no slash-command scope line, so this check cannot"
  echo "tell what they are working on (resumed sessions, SDK/API runs, plain prompts):"
  for sid in $UNSCOPED; do echo "  $sid"; done
  echo
fi

exit 0
