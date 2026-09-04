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
# FIFTH FAILURE, measured live 2026-09-04 — THE CHECK CONTAMINATED ITSELF.
# Version 5 matched `command-args>--thread <ref><` anywhere in the transcript.
# But turn.md instructs a turn to READ THE OTHER SESSIONS' TRANSCRIPTS, and
# doing so prints the read session's first user prompt — command-args and all —
# into the READER's transcript as tool output. The reader then matches the read
# session's scope pattern exactly, and every subsequent check sees a duplicate
# that does not exist. The false positives COMPOUND: the more faithfully the
# fleet follows the procedure, the more sessions carry each other's scope lines.
#
# A hal turn scoped to the `ALARM:` thread of one CloudWatch alarm was told
# COUNT=2 by a sibling scoped to an entirely different thread, whose only
# involvement was having read its transcript one minute earlier. turn.md's
# stand-down language is deliberately emphatic ("two agent emails to one person
# is worse than no email"), so a compliant turn acting on that number drops real
# work — the precise cost wrong version 3 was fixed to avoid.
#
# This is version 3 in a narrower disguise, and the lesson is about WHERE, not
# WHAT: version 5 tightened the PATTERN (bare ref -> the full command-args
# string) and left the REGION as the whole file. A quoted prompt reproduces the
# full pattern verbatim, so no amount of pattern-tightening can fix it. Only
# bounding the region can. Fixed by matching the FIRST `"type":"user"` record
# only, which is where a slash command's args actually are — and which still
# survives a resume, because a resume replays the original prompt.
#
# SIXTH FAILURE, measured live 2026-09-04 — `ps` CANNOT SEE A SESSION BETWEEN
# PROCESSES. Found independently, the same day, by a second hal turn.
#
# Enumeration was `ps` and nothing else. But a session being RESUMED has no
# process for a few seconds: the old one has exited and the new one has not been
# exec'd. Its transcript is on disk, correctly scoped, and it is completely
# invisible. Measured: `--slug hal` at 07:07 returned COUNT=1 while THREE hal
# turns were live, their transcripts written at 07:05:34, 07:05:47 and 07:06:03,
# every one carrying `<command-name>/hal:turn`. Their `--resume` processes only
# showed up in `ps` at 07:07-07:08. Cost ~40 minutes of duplicated work before
# the collision was found by tripping over the other session's scratch worktree.
#
# That gap is not a random few seconds — it is PRECISELY the window a recovery
# dispatch fires in, because a stalled turn is what a recovery dispatch replaces.
# So the one moment the check is blind is the one moment it is most needed.
#
# Fixed by adding a SECOND liveness source: transcripts modified in the last
# CANOPY_LIVE_TURNS_RECENT_MIN minutes (default 10) whose session has no live
# process. Deliberately reported OUTSIDE COUNT — they are read-these pointers,
# not stand-down evidence, for the same reason as the mention block.
#
# Note how five and six point in OPPOSITE directions: five over-reported
# (phantom duplicates), six under-reported (invisible real ones). A check with
# both is wrong in both directions at once, which is why COUNT alone was never
# a safe thing to act on and why the blocks under it carry the real answer.
#
# THE INVARIANT ALL SIX VIOLATE, AND THE ONE TO TEST ANY VERSION 8 AGAINST:
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
# COUNT=1 is NOT by itself an all-clear. Read the three blocks below it as well:
# sessions that MENTION the ref without being scoped to it; sessions matching
# your scope that are active on disk but have NO live process (a resume in
# flight); and live sessions with no scope line at all. Any of the three can be
# a sibling working your item, and none of them can enter COUNT without
# re-creating the over-reporting of wrong version 3.
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
    # Whole header block, found by SHAPE not by line number: a hardcoded range
    # goes stale the moment the header grows (it was '1,79p' while the header
    # ran to 103 lines, silently truncating --help mid-sentence).
    -h|--help) awk '/^#/{print;next}{exit}' "$0"; exit 0 ;;
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
#
# This is the FIRST of two liveness sources, and on its own it is not enough: a
# session mid-resume has no process at all (see SIXTH FAILURE above). The second
# source is RECENT_IDS below.
#
# CANOPY_LIVE_TURNS_SESSION_IDS overrides enumeration entirely — a test seam, so
# the matcher can be exercised against fixture transcripts without a live fleet.
live_session_ids() {
  if [ -n "${CANOPY_LIVE_TURNS_SESSION_IDS:-}" ]; then
    printf '%s\n' $CANOPY_LIVE_TURNS_SESSION_IDS | sed '/^$/d' | sort -u
    return
  fi
  ps ax -o args= 2>/dev/null \
    | grep -i '[c]laude' \
    | grep -oE -- '--(session-id|resume) [0-9a-f-]{36}' \
    | awk '{print $2}' \
    | sort -u
}

transcript_for() {
  find "$PROJECTS" -name "$1.jsonl" -type f 2>/dev/null | head -1
}

# A session's OWN scope lives in its FIRST user message and nowhere else.
#
# This must not be a whole-file grep, and the reason is circular in a way that
# is easy to miss: turn.md tells a turn to READ THE OTHER SESSIONS' TRANSCRIPTS,
# and the natural way to do that prints the other session's first user prompt —
# `<command-args>--thread <their-ref></command-args>` and all — into the reading
# session's own transcript as tool output. From then on, the reader matches the
# READ session's scope pattern, exactly and verbatim. So the act of performing
# the duplicate check is what makes a session look like a duplicate, and the
# false positives COMPOUND as more of the fleet follows the procedure.
#
# That is wrong version 3 from the header returning in a narrower disguise:
# version 5 tightened the PATTERN (bare ref -> the full command-args string) but
# left the REGION as the whole file, and a quoted prompt reproduces the full
# pattern too. Tightening the pattern can never fix this; only bounding the
# region can. (Measured 2026-09-04, hal: a --thread-scoped turn was told COUNT=2
# and "you are a duplicate" by a sibling that was scoped to a DIFFERENT thread
# and had merely read its transcript. Standing down on that reading would have
# dropped real work — turn.md's stand-down language is deliberately emphatic.)
#
# Taking the first `"type":"user"` record still survives a resume, which is the
# invariant version 4 exists to protect: a resume REPLAYS the original prompt,
# so the scope line is still in the first user message.
# Tolerant of JSON whitespace on purpose. Claude Code writes compact records
# (`{"type":"user"`), so a fixed-string match works today — but if that ever
# gains a space this returns NOTHING, matches_scope then returns false for every
# session, and COUNT collapses to 0. That is a false ALL-CLEAR produced by a
# formatting change, i.e. the check failing silently in the safe-looking
# direction, which is the one shape this file has been burned by six times.
first_user_message() {
  grep -m1 -E '"type"[[:space:]]*:[[:space:]]*"user"' "$1" 2>/dev/null
}

IDS="$(live_session_ids)"
if [ -z "$IDS" ]; then
  echo "no live claude sessions found — NOT a clean result if you are running inside one," >&2
  echo "which means this check could not see itself and must not be trusted." >&2
  exit 2
fi

# SECOND liveness source: transcripts touched in the last few minutes whose
# session has NO live process. That is either a session being resumed right now —
# the blind spot that cost 40 minutes on 2026-09-04 — or one that stopped moments
# ago. Both are worth reading before you write; neither is a duplicate you can
# count, so these never enter COUNT.
RECENT_MIN="${CANOPY_LIVE_TURNS_RECENT_MIN:-10}"
RECENT_IDS=""
# `live_session_ids` ends in `sort -u`, so IDS is NEWLINE-separated. The
# membership test below is space-delimited, so it must be normalised first —
# unquoted here deliberately, to collapse the newlines to spaces. Without this
# the `case` never matches and EVERY live session leaks into RECENT_IDS, so the
# "NO live process" block lists the very sessions `ps` just found and tells the
# reader to go chase sessions already sitting in COUNT. (Measured 2026-09-04:
# all four live hal turns appeared simultaneously in both lists.)
IDS_FLAT="$(echo $IDS)"
if [ "$RECENT_MIN" -gt 0 ] 2>/dev/null; then
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    sid="$(basename "$f" .jsonl)"
    case " $IDS_FLAT " in
      *" $sid "*) continue ;;            # already seen by `ps`
    esac
    RECENT_IDS="$RECENT_IDS $sid"
  done <<EOF
$(find "$PROJECTS" -name '*.jsonl' -type f -mmin "-$RECENT_MIN" 2>/dev/null)
EOF
fi

# Fixed-string match against a session's OWN scope line. Two variants: -F for a
# literal (the ref), -E for a pattern (any of an agent's entry points).
#
# Scoped to the FIRST USER MESSAGE, never the whole file — see first_user_message
# above for why a whole-file grep makes every reader of a transcript look like a
# duplicate of the session it read.
matches_scope() {
  local file="$1" mode="$2" pattern="$3" first
  first="$(first_user_message "$file")"
  [ -n "$first" ] || return 1
  if [ "$mode" = "E" ]; then
    printf '%s' "$first" | grep -qE -- "$pattern" 2>/dev/null
  else
    printf '%s' "$first" | grep -qF -- "$pattern" 2>/dev/null
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

  # Same scope, second liveness source. Deliberately printed UNDER the count and
  # outside it: a session with no process is either mid-resume (an owner you must
  # stand down on) or just-finished (innocent), and this check cannot tell which.
  # Only reading its transcript can.
  local recent_hits=""
  for sid in $RECENT_IDS; do
    f="$(transcript_for "$sid")"
    [ -n "$f" ] || continue
    matches_scope "$f" "$mode" "$pattern" && recent_hits="$recent_hits $sid"
  done
  if [ -n "$recent_hits" ]; then
    echo "  -- plus, matching this same scope, active in the last ${RECENT_MIN}m with NO live process:"
    for sid in $recent_hits; do echo "     $sid"; done
    echo "     A session being RESUMED has no process for a few seconds — that is the"
    echo "     window a recovery dispatch fires in, so these are the likeliest owners"
    echo "     of your item. Read them before you write or send; do not add to COUNT."
  fi
  echo
}

# Every live session that carries NO recognizable slash-command scope line at all.
# COUNT cannot see these, so it must not be read as a complete answer.
unscoped_sessions() {
  for sid in $IDS; do
    f="$(transcript_for "$sid")"
    [ -n "$f" ] || continue
    matches_scope "$f" E '<command-name>/[a-z0-9_-]+:' || echo "$sid"
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
    # Genuinely scoped to this ref (by its OWN first message) — already counted
    # above. Note this MUST use matches_scope, not a whole-file grep: a
    # whole-file grep skips every session that merely quoted the ref while
    # reading a transcript, so those sessions would be neither counted nor
    # listed here, and would vanish from the output entirely.
    matches_scope "$f" F "command-args>--thread $REF<" && continue
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
