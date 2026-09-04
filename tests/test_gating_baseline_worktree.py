"""The fleet scratch-worktree deny rail (agent-core/gating-baseline.json `always`).

WHY THIS FILE EXISTS (2026-09-04). Agents stand up throwaway worktrees constantly — deploying
a clean `origin/main`, grounding a finding in a sibling repo, checking CI gates somewhere else
— and the skills that taught it handed out a FIXED path. hal's `skills/turn` taught
`worktree add /tmp/canopy-main` in the procedure it repeats most, so every later invention
(`/tmp/canopy-win`, `/tmp/canopy-harvest`, `/tmp/cl-1427`) inherited the shape.

Two sessions of the same agent run concurrently as a matter of course. `canopy agent-review
hal` measured three failures from this in a single 6h window, across two sessions:

    fatal: '/tmp/canopy-win' is a missing but already registered worktree
    fatal: 'hal/windows-text-encoding' is already used by worktree at '/private/tmp/canopy-win'
    fatal: a branch named 'hal/windows-text-encoding' already exists

The same day that repo carried 15 abandoned `/tmp/canopy-*` worktrees, every one merged and
clean, one still holding the branch a later session then failed to create.

The rail is `always` (not a channel) because every agent does this — a per-channel opt-in
leaves it missing for exactly the agent that forgot. The point of the test is the NEGATIVE
half: a rail that also blocks `$(mktemp -d)` would block its own remedy.

Run: uv run pytest tests/test_gating_baseline_worktree.py
"""
import json
import re
from pathlib import Path

import pytest

BASELINE = Path(__file__).parent.parent / "plugins" / "canopy" / "agent-core" / "gating-baseline.json"
ALWAYS = json.loads(BASELINE.read_text())["always"]
RAILS = [r for r in ALWAYS if r.get("tool") == "Bash" and "worktree" in r.get("pattern", "")]

# Mirrors agent-core/gating_guard.py::_STATEMENT_SPLIT — these rails are per_statement.
_SPLIT = re.compile(r"[\n;]|&&|\|\||[|&]")


def blocks(cmd: str) -> bool:
    stmts = [s.strip() for s in _SPLIT.split(cmd) if s.strip()]
    return any(re.search(r["pattern"], s) for r in RAILS for s in stmts)


def test_the_rail_exists():
    assert RAILS, "no scratch-worktree rail in `always` — the whole file is vacuous without it"


# --------------------------------------------------------------------------------------
# BLOCKED — a fixed path someone chose, in every shape the corpus actually produced
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    # the line skills/turn taught, verbatim
    'git -C "$CANOPY_REPO" worktree add /tmp/canopy-main origin/main --detach',
    # the three 2026-09-04 failures' own paths
    "git worktree add /tmp/canopy-win -b hal/windows-text-encoding",
    "git worktree add /tmp/canopy-harvest origin/main --detach",
    "git -C ~/emdash/repositories/connect-labs worktree add /tmp/cl-1427 origin/main --detach",
    # flags before the path
    "git worktree add --detach /tmp/canopy-510 origin/main",
    # macOS resolves /tmp -> /private/tmp; the corpus contains both spellings
    "git worktree add /private/tmp/canopy-win origin/main",
    # not anchored at the start of a line, and inside a compound command
    "cd /tmp && git worktree add /tmp/scratch origin/main --detach",
    "git fetch -q origin; git worktree add /tmp/grounding origin/main --detach",
])
def test_fixed_tmp_path_is_blocked(cmd):
    assert blocks(cmd), cmd


def test_the_message_names_the_remedy():
    """Rails, not gates: a block that doesn't say what to do instead just stalls the turn."""
    for r in RAILS:
        assert "mktemp -d" in r["message"]
        assert "--detach" in r["message"]


# --------------------------------------------------------------------------------------
# ALLOWED — the remedy itself, and everything that is not scratch /tmp
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    # the remedy the message names — blocking this would be self-defeating
    'WT=$(mktemp -d); git -C "$REPO" worktree add "$WT" origin/main --detach',
    'git -C "$REPO" worktree add "$(mktemp -d)" origin/main --detach',
    "git worktree add ${WT} origin/main --detach",
    # mktemp's own output, spelled literally (Linux shape)
    "git worktree add /tmp/tmp.MeP0Xc3kNj origin/main --detach",
    # named workspace homes are a convention, not scratch — deliberately out of scope
    "git worktree add ~/emdash/worktrees/canopy/my-branch -b my-branch",
    "git worktree add /Users/x/emdash-projects/canopy-findings -b feat/x",
    # the cleanup verbs must never be blocked — they are how you recover
    "git worktree remove --force /tmp/canopy-win",
    "git worktree prune",
    "git worktree list",
    # /tmp elsewhere in an unrelated statement must not taint the worktree statement
    'cat /tmp/notes.md; git worktree add "$WT" origin/main --detach',
])
def test_unique_or_unrelated_paths_are_allowed(cmd):
    assert not blocks(cmd), cmd
