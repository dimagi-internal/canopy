#!/bin/sh
# Run a canopy hook under a Python 3 that actually works — and, for a guard, BLOCK
# when there is none.
#
#   sh python.sh [--guard] <script.py> [args...]
#
# A hook launched as bare `python3` is unsafe on Windows, where that name is usually
# the Microsoft Store stub (…\WindowsApps\python3.exe), which prints "Python was not
# found" and exits 49. Claude Code treats any hook exit other than 2 as NON-blocking,
# so on such a box the gating guard, the confinement guard and the one-shot guard
# would simply never run — every fleet rail off, silently.
#
# Order: a real `python3`, then `python`, then the Windows `py -3` launcher. A path
# under WindowsApps is the Store stub and is skipped. On macOS/Linux the first
# candidate wins, so this costs one `exec`.
#
# --guard: the guard must answer 0 (allow) or 2 (block). Anything else (a traceback's
# exit 1, an interpreter that cannot start, a stub's 49) would be read by Claude Code
# as "allow", so it becomes exit 2 with the reason. No working interpreter → exit 2.
# Without --guard the hook is advisory: warn and exit 0 rather than block every prompt.

guard=0
if [ "$1" = "--guard" ]; then
  guard=1
  shift
fi

# A guard runs as a child so its exit code can be checked; an advisory hook is exec'd.
run_hook() {
  if [ "$guard" = 0 ]; then
    exec "$@"
  fi
  "$@"
  rc=$?
  case "$rc" in
    0|2) exit "$rc" ;;
  esac
  echo "canopy: the guard $script exited $rc instead of answering allow (0) or block (2), so it did not check this tool call — blocking it rather than running unguarded. Run /canopy:canopy-doctor or /canopy:update; if it persists, report the guard's error above." >&2
  exit 2
}

script="$1"
for cand in python3 python; do
  path=$(command -v "$cand" 2>/dev/null) || continue
  case "$path" in
    *WindowsApps*) continue ;;
  esac
  run_hook "$path" "$@"
done

if command -v py >/dev/null 2>&1; then
  run_hook py -3 "$@"
fi

if [ "$guard" = 1 ]; then
  echo "canopy: no working Python 3 on PATH (python3, python, py -3), so the guard $1 cannot run — blocking this tool call rather than running unguarded. Install Python 3 (python.org, or 'winget install Python.Python.3.12' on Windows) and restart the session." >&2
  exit 2
fi
echo "canopy: no working Python 3 on PATH — hook $1 skipped" >&2
exit 0
