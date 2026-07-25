#!/usr/bin/env python3
"""PreToolUse guard: a non-worktree canopy checkout must stay on `main`.

canopy uses emdash worktrees — feature work happens in a worktree; the clone itself stays
on `main`. Branching IN the clone silently changes what the globally-installed `canopy` CLI
runs (it's deployed from main), which is exactly how a fresh session got
`No such command 'harvest'`.

This keys off `/worktrees/` in the git-dir, NOT a hardcoded path, so it protects EVERY
non-worktree clone (there is often more than one — e.g. `~/emdash-projects/canopy` and
`~/emdash/repositories/canopy`). Note the limit: it only sees `git` run as a Bash tool
call, so branch switches performed by emdash itself bypass it.

This blocks `git checkout`/`git switch` to a non-`main` branch and branch creation
(`-b`/`-c`/`git branch <name>`) — but ONLY when the project is the primary (non-worktree)
checkout. Worktrees may branch freely (that's their job). File restores (`git checkout -- f`,
`git checkout .`) and `git checkout main` are allowed.

Override: `CANOPY_ALLOW_PRIMARY_BRANCH=1`. Stdlib only (runs under system python3).
"""
import json
import os
import re
import subprocess
import sys

if os.environ.get("CANOPY_ALLOW_PRIMARY_BRANCH"):
    sys.exit(0)

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

ti = data.get("tool_input")
cmd = ti.get("command", "") if isinstance(ti, dict) else ""
if not cmd:
    sys.exit(0)

_SWITCH = re.compile(r"(?:^|[\n;&|()])\s*git\s+(?:checkout|switch)\b([^\n;&|]*)")
_BRANCH_CREATE = re.compile(
    r"(?:^|[\n;&|()])\s*git\s+branch\s+(?!-d\b|-D\b|--delete|--list|-a\b|-r\b|-v\b|--show|--merged|--no-merged)"
    r"([A-Za-z0-9._/-]+)"
)


def _offmain_switch(args: str) -> bool:
    toks = args.split()
    if any(t in ("-b", "-c", "--create", "-B") for t in toks):
        return True                      # creating a branch
    if "--" in toks or args.strip() == ".":
        return False                     # file restore, not a branch switch
    names = [t for t in toks if not t.startswith("-")]
    if not names:
        return False                     # bare `git checkout`
    return names[0] != "main"            # switching to a non-main branch


why = ""
m = _SWITCH.search(cmd)
if m and _offmain_switch(m.group(1)):
    why = "git checkout/switch to a non-main branch"
elif _BRANCH_CREATE.search(cmd):
    why = "git branch <name> (creating a branch)"
if not why:
    sys.exit(0)

# Only enforce in the PRIMARY (non-worktree) checkout. Worktrees branch freely.
proj = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
try:
    gd = subprocess.run(
        ["git", "-C", proj, "rev-parse", "--git-dir"],
        capture_output=True, text=True, timeout=5,
    ).stdout.strip()
except Exception:
    gd = ""
if "/worktrees/" in gd:                  # this IS a worktree — allow
    sys.exit(0)

sys.stderr.write(
    f"BLOCKED: {why} in the PRIMARY canopy checkout ({proj}).\n"
    "The primary checkout stays on `main` — do feature work in an emdash worktree.\n"
    "The global `canopy` CLI is deployed from main; branching here silently changes what it runs.\n"
    "Use a worktree, or override with CANOPY_ALLOW_PRIMARY_BRANCH=1.\n"
)
sys.exit(2)
