#!/bin/sh
# Run a canopy hook under a Python 3 that actually works — and, for a guard, BLOCK
# when there is none.
#
#   sh python.sh [--guard] <script.py> [args...]
#
# Every hook used to be launched as bare `python3`. On Windows that name is usually
# the Microsoft Store stub (…\WindowsApps\python3.exe), which prints "Python was not
# found" and exits 49. Claude Code treats any hook exit other than 2 as NON-blocking,
# so on such a box the gating guard, the confinement guard and the one-shot guard
# simply never ran — every fleet rail was off, silently (2026-10-15, the first real
# Windows agent: a raw send was allowed until the hook was hand-edited to `py -3`).
#
# Order: a real `python3`, then `python`, then the Windows `py -3` launcher. A path
# under WindowsApps is the Store stub and is skipped. On macOS/Linux the first
# candidate wins, so this is one `exec` on top of what ran before.
#
# --guard: no working interpreter → exit 2 (block the tool call, show the reason).
# Without it the hook is advisory: warn and exit 0 rather than block every prompt.

guard=0
if [ "$1" = "--guard" ]; then
  guard=1
  shift
fi

for cand in python3 python; do
  path=$(command -v "$cand" 2>/dev/null) || continue
  case "$path" in
    *WindowsApps*) continue ;;
  esac
  exec "$path" "$@"
done

if command -v py >/dev/null 2>&1; then
  exec py -3 "$@"
fi

if [ "$guard" = 1 ]; then
  echo "canopy: no working Python 3 on PATH (python3, python, py -3), so the guard $1 cannot run — blocking this tool call rather than running unguarded. Install Python 3 (python.org, or 'winget install Python.Python.3.12' on Windows) and restart the session." >&2
  exit 2
fi
echo "canopy: no working Python 3 on PATH — hook $1 skipped" >&2
exit 0
