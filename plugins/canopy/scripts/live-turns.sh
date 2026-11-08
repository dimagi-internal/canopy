#!/usr/bin/env bash
# Which OTHER live turns share my scope? — the duplicate/sibling check from agent-core/turn.md.
#
# WHY THIS IS A SCRIPT AND NOT A ONE-LINER IN THE PROCEDURE
# --------------------------------------------------------
# A one-liner such as
#
#     ps aux | grep '[c]laude --session-id' | grep -c -- '--thread <the-ref>'
#
# reads the scope out of the process's argv, and argv does not survive a resume.
# A session that is resumed (after an interrupt, a stall, a crash, or a context
# handoff) runs as `claude --resume <uuid>` with NO `--thread` and no `/<slug>:`
# in its command line. So that check returns **0**, for every live turn including
# the one running it.
#
# Zero reads as "nobody else is here." It is the all-clear, and it is produced by
# the check failing, silently, in exactly the situation the check exists for: a
# long or resumed turn is precisely the turn a recovery dispatch was sent to
# replace.
#
# So this script never reads scope from argv. A session's scope is in its
# transcript, which survives a resume because the resume replays it.
#
# Wrong ways to read scope from a transcript, each of which returns a confident
# wrong number rather than an error (which is why this belongs in code):
#   1. matching `^claude ` in `ps` output: misses absolute-path invocations;
#   2. `head -c 20000` of the transcript: a transcript opens with large
#      system-reminder blocks, so the scope line sits past the truncation and
#      every session reports "not scoped to this ref";
#   3. grepping the whole transcript for the bare ref: matches any sibling that
#      merely DISCUSSED the ref (checking for duplicates is itself a mention), so
#      it over-reports and the stand-down rules fire against innocent turns.
#
# SCOPE IS NOT ONLY SET BY `:turn`. A turn dispatched as /eva:chief-of-staff has
# neither `command-args>--thread <ref><` nor `command-name>/<slug>:turn<`: an
# inbox-draining entry point discovers a thread by scanning and never records it
# as an arg. The unattended dispatches a fleet runs on a schedule (chief-of-staff,
# morning briefings, weekly goals) are all non-`:turn` entry points, so:
#   (a) --slug matches ANY `/<slug>:` entry point;
#   (b) live sessions that mention the ref without being scoped to it are listed
#       separately from COUNT;
#   (c) live sessions that carry no scope line at all are warned about.
# (b) and (c) are deliberately NOT folded into COUNT: over-reporting fires the
# stand-down rules against innocent turns (wrong way 3 above). They are pointers
# to go READ, and the reading is what decides.
#
# THE CHECK MUST NOT CONTAMINATE ITSELF. agent-core/turn.md tells a turn to READ
# THE OTHER SESSIONS' TRANSCRIPTS, and doing so prints the read session's first
# user prompt (command-args and all) into the READER's transcript as tool output.
# A reader that matches the full command-args pattern anywhere in its file then
# sees a duplicate that does not exist, and the false positives compound as more
# of the fleet follows the procedure. The stand-down language in turn.md is
# deliberately emphatic ("two agent emails to one person is worse than no email"),
# so a compliant turn acting on a phantom COUNT drops real work. The lesson is
# about WHERE, not WHAT: tightening the PATTERN cannot fix it, because a quoted
# prompt reproduces the full pattern verbatim. Only bounding the REGION can, so
# only the FIRST `"type":"user"` record is matched, which is where a slash
# command's args are, and which still survives a resume (a resume replays the
# original prompt).
#
# `ps` CANNOT SEE A SESSION BETWEEN PROCESSES. A session being RESUMED has no
# process for a few seconds: the old one has exited and the new one has not been
# exec'd. Its transcript is on disk, correctly scoped, and invisible to `ps`.
# That gap is exactly the window a recovery dispatch fires in, because a stalled
# turn is what a recovery dispatch replaces. So a SECOND liveness source is used:
# transcripts modified in the last CANOPY_LIVE_TURNS_RECENT_MIN minutes (default
# 10) whose session has no live process. They are reported OUTSIDE COUNT: they
# are read-these pointers, not stand-down evidence, for the same reason as the
# mention block.
#
# The phantom-duplicate failure (contamination) and the invisible-session failure
# (resume gap) point in OPPOSITE directions: one over-reports, the other
# under-reports. A check exposed to both is wrong in both directions at once,
# which is why COUNT alone is never safe to act on and the blocks under it carry
# the real answer.
#
# THE INVARIANT, AND THE ONE TO TEST ANY CHANGE AGAINST: a check that cannot see
# something must SAY SO. Every count this prints is a lower bound, and it says
# that out loud rather than rendering "I could not look" as "nobody is there."
#
# USAGE
#   live-turns.sh --ref <gmail-thread-id|slack-ts>   # turns scoped to THAT ref
#   live-turns.sh --slug <agent-slug>                # any live turn of that agent
#   live-turns.sh --ref <ref> --slug <slug>          # both counts, labelled
#
# Prints one session id per line under each heading, plus a COUNT= line that
# says whether YOU are in it — and that is not a formality. A turn scoped to the
# ref (or, for --slug, any turn of that agent) is in its own count, so COUNT>1
# means a sibling. An UNSCOPED turn that picked the ref out of its inbox is NOT
# in the --ref count, so for it COUNT=1 already means one other session holds
# the thread. The line names which case you are in, and "OTHERS=" is the number
# to act on either way: OTHERS>=1 means go read that session's transcript and
# apply turn.md's stand-down rules before you write or send.
#
# THE LABEL ON EACH COUNT LINE IS THE ANSWER. A COUNT line ending "(includes you)"
# unconditionally would read as "just me" even when a live `--thread <ref>`
# sibling is the only session listed: turn.md tells an unscoped turn to run --ref
# on the thread it just picked, and that turn is never in the --ref count. So the
# caller is identified by CLAUDE_CODE_SESSION_ID (CANOPY_LIVE_TURNS_SELF overrides
# it, which is the test seam), it is dropped from the "mentions" block (its own
# earlier check mentions the ref, which would make every unscoped turn's
# --require-solo re-check refuse on itself), and when the caller CANNOT be
# identified the line says so instead of guessing.
#
# COUNT=1 is NOT by itself an all-clear. Read the three blocks below it as well:
# sessions that MENTION the ref without being scoped to it; sessions matching
# your scope that are active on disk but have NO live process (a resume in
# flight); and live sessions with no scope line at all. Any of the three can be
# a sibling working your item, and none of them can enter COUNT without
# re-creating the over-reporting of wrong way 3 above.
#
# Exit 0 always when it could look; exit 2 if it could not enumerate sessions at
# all, because "I could not check" must never render as "nothing found".
#
# --require-solo: exit 3 instead of 0 when ANY sibling signal was printed — an
# OTHERS above 0, a session other than you that mentions the ref, or a
# same-scope session with no live process. Use it to GATE the write it protects:
#
#     live-turns.sh --ref <ref> --slug <slug> --require-solo && <the write>
#
# Without it, "re-run the counts right before you write" is a printout, and a
# printout chained into the write does not stop it: the write can run before
# anyone reads the output. Live sessions with no scope line do NOT trip it: every
# machine has some, and a gate that always fires is a gate people route around.

set -uo pipefail

REF=""
SLUG=""
REQUIRE_SOLO=0
NOT_SOLO=0
while [ $# -gt 0 ]; do
  case "$1" in
    --require-solo) REQUIRE_SOLO=1; shift ;;
    --ref)  REF="${2:-}"; shift 2 ;;
    --slug) SLUG="${2:-}"; shift 2 ;;
    # Whole header block, found by SHAPE not by line number: a hardcoded range
    # goes stale the moment the header grows and silently truncates --help.
    -h|--help) awk '/^#/{print;next}{exit}' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac
done

if [ -z "$REF" ] && [ -z "$SLUG" ]; then
  echo "usage: live-turns.sh [--ref <thread-id>] [--slug <agent-slug>] [--require-solo]" >&2
  exit 64
fi

PROJECTS="${CLAUDE_PROJECTS_DIR:-$HOME/.claude/projects}"

# Who is asking. Claude Code exports the running session's id to every Bash
# call; without it the COUNT line cannot say whether you are in it (see THE LABEL
# ON EACH COUNT LINE above), so the unknown case is labelled as unknown, never as "you".
SELF="${CANOPY_LIVE_TURNS_SELF-${CLAUDE_CODE_SESSION_ID:-}}"
if [ ! -d "$PROJECTS" ]; then
  echo "cannot read $PROJECTS — NOT a clean result, do not treat as all-clear" >&2
  exit 2
fi

# Session ids of every live claude process, from EITHER flag (`--session-id`
# or `--resume`; a resumed session has only the latter).
#
# This is the FIRST of two liveness sources, and on its own it is not enough: a
# session mid-resume has no process at all (see the resume gap in the header). The second
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
# That is wrong way 3 from the header in a narrower disguise: matching the full
# command-args string instead of the bare ref tightens the PATTERN but leaves the
# REGION as the whole file, and a quoted prompt reproduces the full pattern too.
# Tightening the pattern can never fix this; only bounding the region can. A
# --thread-scoped turn would otherwise be told COUNT=2 and "you are a duplicate"
# by a sibling scoped to a DIFFERENT thread that merely read its transcript, and
# standing down on that reading drops real work (turn.md's stand-down language is
# deliberately emphatic).
#
# Taking the first `"type":"user"` record still survives a resume: a resume
# REPLAYS the original prompt, so the scope line is still in the first user
# message.
# Tolerant of JSON whitespace on purpose. Claude Code writes compact records
# (`{"type":"user"`), so a fixed-string match would work — but if that
# ever gained a space it would return NOTHING, matches_scope then returns false for every
# session, and COUNT collapses to 0. That is a false ALL-CLEAR produced by a
# formatting change, i.e. the check failing silently in the safe-looking
# direction, which is the failure shape this whole file guards against.
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
# the blind spot `ps` has — or one that stopped moments ago. Both are worth reading before you write; neither is a duplicate you can
# count, so these never enter COUNT.
RECENT_MIN="${CANOPY_LIVE_TURNS_RECENT_MIN:-10}"
RECENT_IDS=""
# `live_session_ids` ends in `sort -u`, so IDS is NEWLINE-separated. The
# membership test below is space-delimited, so it must be normalised first —
# unquoted here deliberately, to collapse the newlines to spaces. Without this
# the `case` never matches and EVERY live session leaks into RECENT_IDS, so the
# "NO live process" block lists the very sessions `ps` just found and tells the
# reader to go chase sessions already sitting in COUNT.
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
  local heading="$1" mode="$2" pattern="$3" count=0 self_in=0 others
  echo "$heading"
  for sid in $IDS; do
    f="$(transcript_for "$sid")"
    [ -n "$f" ] || continue
    if matches_scope "$f" "$mode" "$pattern"; then
      if [ -n "$SELF" ] && [ "$sid" = "$SELF" ]; then
        echo "  $sid   (you)"
        self_in=1
      else
        echo "  $sid"
      fi
      count=$((count + 1))
    fi
  done
  # The label is the answer, not decoration: see THE LABEL ON EACH COUNT LINE in the header.
  if [ -z "$SELF" ]; then
    others=$((count > 0 ? count - 1 : 0))
    echo "COUNT=$count OTHERS>=$others (could not identify this session — CLAUDE_CODE_SESSION_ID unset;"
    echo "  if you are NOT scoped to this, every session listed is another turn)"
  elif [ "$self_in" = 1 ]; then
    others=$((count - 1))
    echo "COUNT=$count OTHERS=$others (includes you)"
  else
    others=$count
    echo "COUNT=$count OTHERS=$others (you are NOT in this count — every session listed is another turn)"
  fi
  [ "$others" -gt 0 ] && NOT_SOLO=1

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
    NOT_SOLO=1
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

# The ref's scope pattern. The runner dispatches `--thread <ref> --caller <path>`,
# so the ref is NOT necessarily the last arg: requiring `--thread <ref><` (the
# ref immediately followed by `</command-args>`) made every runner-dispatched
# turn invisible to COUNT — a false all-clear in exactly the case this check
# exists for (a `/eva:turn --thread <ref> --caller <path>` session would be told
# `COUNT=0 (includes you)` about its OWN ref). So match
# `--thread <ref>` anywhere inside the command-args, ended by a space or `<`
# (the ref is escaped so a Slack-style ref's `.` stays literal, and a longer ref
# sharing this one as a prefix never matches).
REF_RE="$(printf '%s' "$REF" | sed 's/[][\\.*^$+?(){}|/]/\\&/g')"
REF_SCOPE="command-args>[^<]*--thread ${REF_RE}([[:space:]]|<)"

# Match the session's OWN scope line, not any mention of the ref. The slash
# command's args are recorded verbatim in the first user message.
if [ -n "$REF" ]; then
  report "turns scoped to ref $REF:" E "$REF_SCOPE"

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
    matches_scope "$f" E "$REF_SCOPE" && continue
    # Not yourself: your own earlier run of this check put the ref in your
    # transcript, so listing you here tells you to go read yourself — and made
    # every unscoped turn's --require-solo re-check refuse on its own shadow.
    [ -n "$SELF" ] && [ "$sid" = "$SELF" ] && continue
    grep -qF -- "$REF" "$f" 2>/dev/null && hits="$hits $sid"
  done
  if [ -n "$hits" ]; then
    NOT_SOLO=1
    echo "sessions NOT scoped to this ref that nonetheless mention it —"
    echo "the COUNT above cannot see these; go read them before you write or send:"
    for sid in $hits; do echo "  $sid"; done
    echo "(a mention may only be that session's own duplicate check — read, then decide)"
    echo
  fi

  # A FINISHED TURN IS NOT NO TURN.
  #
  # Both liveness sources above are real-time: `ps`, and transcripts touched in
  # the last RECENT_MIN minutes. A turn that ran yesterday and ENDED is invisible
  # to both, so COUNT=0 comes back true and useless — and "ended, still owed an
  # answer" is the state a parked item spends almost all of its life in.
  #
  # The loop: in manual mode a turn triages the thread, asks the human something,
  # and ends. turn.md then correctly tells it to leave the thread UNREAD, because
  # unread is the only record that a draft or a question is live. The poller fires
  # on unread. So the next poll dispatches a fresh turn, which runs this check, is
  # told it is alone, and re-derives the identical triage from scratch — every
  # poll, unbounded, until a human answers.
  #
  # Without this block, a thread whose earlier turn ended "Session paused —
  # waiting on you" gets COUNT=0 here and the next turn redoes all of the
  # earlier turn's triage, finding it only by chance.
  #
  # Printed OUTSIDE and UNDER the count, like the no-process block, and for the
  # same reason: a completed turn is NEVER a duplicate to stand down on. It is a
  # transcript to READ, so you resume from its conclusion instead of rebuilding
  # it. Standing down on one would be strictly wrong — nobody is holding the item.
  COMPLETED_DAYS="${CANOPY_LIVE_TURNS_COMPLETED_DAYS:-14}"
  completed_hits=""
  if [ "$COMPLETED_DAYS" -gt 0 ] 2>/dev/null; then
    while IFS= read -r f; do
      [ -n "$f" ] || continue
      sid="$(basename "$f" .jsonl)"
      case " $IDS_FLAT " in *" $sid "*) continue ;; esac     # live — already in COUNT
      case " $RECENT_IDS " in *" $sid "*) continue ;; esac   # mid-resume — listed above
      # Cheap prefilter FIRST. Without it this greps every transcript in the
      # window end to end (~9s over 920 files here, because a non-match reads the
      # whole file); bounding the read to the head makes it ~4s. The scope line
      # lives in the FIRST user message, which sits at line <=20 in every
      # transcript sampled, so 200 is ~10x margin. If Claude Code pushed it
      # deeper this would silently find nothing (the fail-toward-silence shape),
      # so widen the bound, never tighten it.
      # NOT `head … | grep -q`: this script runs under `set -o pipefail`, and
      # `grep -q` exits the moment it matches, which SIGPIPEs `head` (141). With
      # pipefail the pipeline then reports FAILURE **on a match**, so the guard
      # skips exactly the sessions it is meant to find. Keep it pipe-free.
      hdr="$(head -n 200 "$f" 2>/dev/null)"
      case "$hdr" in *"$REF"*) ;; *) continue ;; esac
      matches_scope "$f" E "$REF_SCOPE" \
        && completed_hits="$completed_hits $sid"
    done <<EOF
$(find "$PROJECTS" -name '*.jsonl' -type f -mtime "-$COMPLETED_DAYS" 2>/dev/null)
EOF
  fi
  if [ -n "$completed_hits" ]; then
    echo "COMPLETED turns scoped to this ref (not live, last ${COMPLETED_DAYS}d) —"
    echo "a turn ALREADY disposed of this ref. Read it before you re-derive its work:"
    for sid in $completed_hits; do
      f="$(transcript_for "$sid")"
      echo "  $sid   ${f:-<transcript not found>}"
    done
    echo "These are NOT duplicates to stand down on — nobody is holding the item."
    echo "If one ended parked on a human answer, the item is AWAITING A DECISION,"
    echo "not untriaged: resume from its conclusion, do not start the triage again."
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

if [ "$REQUIRE_SOLO" = 1 ] && [ "$NOT_SOLO" = 1 ]; then
  echo "NOT SOLO — --require-solo refused: read the sessions listed above before" >&2
  echo "you write or send, then decide. Exiting 3 so a chained write does not run." >&2
  exit 3
fi

exit 0
