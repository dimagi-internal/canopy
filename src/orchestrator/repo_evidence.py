"""Generic git-repo evidence helpers — FRAMEWORK tier (agent-agnostic, stdlib + git only).

Extracted from verify_findings (PRODUCT) so FRAMEWORK modules can build the same
"is this already in origin/main?" evidence without importing a product module. Two
consumers today: verify_findings (proposals) and agent_review's source-verification
gate (findings). See src/orchestrator/TIERS.md.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

# Backtick-quoted identifiers in a piece of text — file paths, function names, env
# vars, config keys. The things worth grepping the current tree for.
SYMBOL_RX = re.compile(r"`([^`]{2,80})`")


def git_log_recent(repo: Path, since: str = "14 days ago") -> str:
    """Recent origin/main commits (short hash + date + subject), '' on any failure."""
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo), "log", "origin/main",
             f"--since={since}", "--pretty=format:%h %ad %s",
             "--date=short"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
        ).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return ""


# The ref these helpers answer ABOUT. Every consumer asks the same question — "is this
# already in the agent repo's current origin/main?" — so the evidence must come from
# origin/main, NOT from whatever happens to be checked out.
#
# This was silently wrong. `git grep <pat>` and `open(repo/"CHANGELOG.md")` both read the
# WORKING TREE, while the callers fetch origin/main first and their prompts announce the
# results as origin/main. Agent repos are normally parked on a feature branch — work
# happens in worktrees, so the main checkout is left wherever it last was. Measured
# 2026-08-19: `hal` was parked on `hal/cursor-version-note`, and grepping for
# `hal:agent-turn-review` returned NOTHING from the working tree while origin/main had it
# in three files, including the finding's own target. So the source-verification gate was
# told "no hits", concluded the fix was absent, and kept an already-shipped finding —
# which is precisely the waste the gate exists to prevent. chrome-sales was parked too;
# this is the steady state, not an outlier.
#
# Falling back to the working tree when origin/main is unreadable is deliberate (a fresh
# clone with no remote, a repo mid-rebase), but the fallback is LABELLED in the output —
# an unlabelled fallback would reintroduce the exact confusion this fixes.
_REF = "origin/main"


def _ref_or_none(repo: Path, ref: str = _REF) -> str | None:
    """`ref` if it resolves in `repo`, else None (caller falls back to the work tree)."""
    try:
        subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            stderr=subprocess.DEVNULL, text=True, timeout=10,
        )
        return ref
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None


def changelog_head(repo: Path, lines: int = 200) -> str:
    """First `lines` of the repo's CHANGELOG.md **on origin/main**, '' if absent.

    Reads the ref, not the working tree — see the _REF note above.
    """
    ref = _ref_or_none(repo)
    text = ""
    if ref:
        try:
            text = subprocess.check_output(
                ["git", "-C", str(repo), "show", f"{ref}:CHANGELOG.md"],
                stderr=subprocess.DEVNULL, text=True, timeout=15,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            text = ""
    if not text:
        cl = repo / "CHANGELOG.md"
        if not cl.exists():
            return ""
        try:
            with open(cl, encoding="utf-8") as f:
                text = "".join(f.readline() for _ in range(lines))
        except OSError:
            return ""
    return "\n".join(text.splitlines()[:lines]).rstrip()


def _path_head(repo: Path, path: str, ref: str | None, lines: int = 40) -> str | None:
    """First `lines` of `path` on `ref` (or the work tree), or None if it isn't a file.

    A symbol that names a FILE needs its CONTENT shown, not a content-grep for its own
    name. `git grep 'skills/agent-turn-review/SKILL.md'` searches for that string INSIDE
    files — it never looks at the file itself — so a target whose fix lives in its own
    body reports "(no hits)" and reads as unfixed. That is the second half of the
    2026-08-19 miss: the target existed AND carried the fix, and the gate saw neither.
    """
    try:
        if ref:
            out = subprocess.check_output(
                ["git", "-C", str(repo), "show", f"{ref}:{path}"],
                stderr=subprocess.DEVNULL, text=True, timeout=10,
            )
        else:
            fp = repo / path
            if not fp.is_file():
                return None
            out = fp.read_text(encoding="utf-8", errors="replace")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None
    return "\n".join(out.splitlines()[:lines]).rstrip()


def grep_repo(repo: Path, symbols: list[str]) -> str:
    """Evidence for each symbol **from origin/main** (see the _REF note above).

    Two shapes, because two questions are being asked:
      * any symbol -> `git grep` on the ref, up to 5 hits, or '(no hits)'
      * a symbol that IS a file on the ref -> ALSO the head of that file, since a fix
        frequently lives in the target's own body where a name-grep cannot see it
    '' if no symbols given.
    """
    if not symbols:
        return ""
    ref = _ref_or_none(repo)
    scope = ref or "WORKING TREE (origin/main unavailable — evidence may not reflect main)"
    parts: list[str] = [f"(searched: {scope})"]
    for sym in symbols:
        # `-F` (fixed string): these symbols are paths and identifiers, not regexes — a
        # `.` or `(` in one would otherwise silently widen or break the search.
        # `-e sym` keeps a symbol starting with `-` from being read as an option.
        cmd = ["git", "-C", str(repo), "grep", "-n", "-F", "-e", sym]
        if ref:
            cmd.append(ref)
        cmd += ["--", ":!.git", ":!node_modules"]
        try:
            out = subprocess.check_output(
                cmd, stderr=subprocess.DEVNULL, text=True, timeout=10,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            out = ""
        out = out.strip()
        block = [f"=== `{sym}` ==="]
        block.append("\n".join(out.splitlines()[:5]) if out else "(no hits)")
        if "/" in sym or sym.endswith((".py", ".md", ".json", ".sh", ".yaml", ".yml")):
            head = _path_head(repo, sym, ref)
            if head is not None:
                block.append(f"--- FILE EXISTS on {ref or 'work tree'}; first lines ---\n{head}")
        parts.append("\n".join(block))
    return "\n\n".join(parts)
