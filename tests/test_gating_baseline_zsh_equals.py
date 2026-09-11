"""The fleet zsh-EQUALS deny rail (agent-core/gating-baseline.json `always`).

WHY THIS FILE EXISTS (2026-09-11). Agents' Bash tool runs under zsh, and zsh's EQUALS option
(on by default) replaces any word that starts with an unquoted `=` with the path of the
command named by the rest of the word (`=ls` -> `/bin/ls`). A separator like `echo =====`
therefore asks zsh for a command called `====`, which does not exist:

    (eval):1: ==== not found

That is not a failed echo. It aborts the whole command line: the statements before it have
printed, the ones after it never run, and the call returns `Exit code 1`. Over the 7 days to
2026-09-11 (27k Bash calls) there were 41 of these tool results in 15 sessions of ace, eva,
ada and hal. It also misleads diagnosis: `canopy agent-review hal` blamed "head/tail pipes
report exit 1" for the same failure, when the pipes were innocent and `echo ===` was the
statement that died.

The rail is `always` (not a channel) because every agent shares the shell. As with the
worktree rail, the point of the test is the NEGATIVE half: `==` is everywhere in legitimate
commands (`[[ ]]` tests, awk, python one-liners, assignments), and none of those is what
this rail is for. The rail matches only a bare `=` word as echo/printf's FIRST non-flag
argument. That was every failure in the corpus, and each attempt to reach further
(`printf '%s\\n' ===`) fired on legitimate commands instead. The corpus false positives are
pinned in the ALLOWED list below.

This drives the real engine (`gating_guard.subject_for` + `matches`) rather than a mirror of
its statement splitter, so the test cannot pass against a splitter the hook doesn't use.

Run: uv run pytest tests/test_gating_baseline_zsh_equals.py
"""
import importlib.util
import json
from pathlib import Path

import pytest

AGENT_CORE = Path(__file__).parent.parent / "plugins" / "canopy" / "agent-core"
ALWAYS = json.loads((AGENT_CORE / "gating-baseline.json").read_text())["always"]
RAILS = [r for r in ALWAYS if r.get("tool") == "Bash" and "EQUALS" in r.get("message", "")]

_spec = importlib.util.spec_from_file_location("gating_guard", AGENT_CORE / "gating_guard.py")
engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(engine)


def blocks(cmd: str) -> bool:
    subject = engine.subject_for("Bash", {"command": cmd})
    return any(engine.matches(r, "Bash", subject) for r in RAILS)


def test_the_rail_exists():
    assert len(RAILS) == 1, "no zsh-EQUALS rail in `always` — the whole file is vacuous without it"
    rail = RAILS[0]
    assert rail.get("per_statement") is True
    # `always` means everyone: a requires_path would silently exempt some agents.
    assert "requires_path" not in rail


# --------------------------------------------------------------------------------------
# BLOCKED — a bare `=` word in echo/printf, in the shapes the 2026-09-11 corpus produced
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    # the three verbatim finding examples (hal, ace, eva); tails after the separator filled in
    "echo ===; sed -n 1,15p tests/test_dependency_health.py",
    "git show b1d88935 --stat | head -40; echo ======; git show b1d88935 | head -120",
    "sed -n '150,260p' skills/agent-turn-review/SKILL.md; echo =========; sed -n '1,40p' README.md",
    # the other token shapes counted: bare runs of every length, and =-prefixed words
    "echo ==",
    "echo ====================",
    "grep -n href index.html; echo ==LINKS; grep -n src index.html",
    # separators other than `;`
    "git status && echo ==== && git log -3",
    "cat a.txt\necho =====\ncat b.txt",
    # inside a loop body (one of the 41) and a conditional
    'for f in *.md; do echo =====; head -3 "$f"; done',
    "if test -f x; then echo ===; fi",
    # flags before the separator, and printf
    "echo -n ======",
    "echo -e ====",
    "printf ====",
])
def test_bare_equals_word_is_blocked(cmd):
    assert blocks(cmd), cmd


def test_the_message_names_the_remedy_and_the_cause():
    """Rails, not gates: a block that doesn't say what to do instead just stalls the turn."""
    for r in RAILS:
        assert "echo '====='" in r["message"]
        assert "EQUALS" in r["message"]


# --------------------------------------------------------------------------------------
# ALLOWED — the remedy, and every legitimate `=` / `==` that is not a bare echo argument
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    # the finding's MUST-NOT list, verbatim
    "[[ $a == b ]]",
    "awk '$1 == 2'",
    'python3 -c "x == y"',
    "A=b",
    "echo '==='",
    # the remedy the message names, and its other spellings
    "echo '====='",
    'echo "====="',
    "echo \\=====",
    "echo -----",
    "printf '%s\\n' '==='",
    # `=` mid-word or lone — zsh leaves these alone
    "echo a=b",
    "echo --format=short",
    "echo x = y",
    "echo =",
    "printf '=%.0s' 1 2 3",
    "git log --format='=== %h ==='",
    # `==` in other statements of a line that also echoes
    "[[ $x == y ]] && echo same",
    "grep -c '==' file.py; echo done",
    '[ "$a" = "$b" ] || echo differ',
    # echo only as a substring of another word
    "myecho ===",
    "echoes ===",
    # legitimate commands a broader pattern fired on in the 2026-09-11 corpus
    "echo '=== ACE'\\''s claims about solicitations being PUBLIC ==='",
    "echo \"cards=$(grep -c \"nScope === 'programme'\" $F) scroll=$(grep -c 'scroll' $F)\"",
    'canopy agent dispatch --slug ada --title "canopy: rail for zsh EQUALS expansion (echo ===)"',
])
def test_legitimate_equals_is_allowed(cmd):
    assert not blocks(cmd), cmd
