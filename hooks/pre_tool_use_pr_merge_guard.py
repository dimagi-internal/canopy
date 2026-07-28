#!/usr/bin/env python3
"""Claude Code PreToolUse hook: block a `gh pr merge` that would reuse a version.

The sibling guard (``pre_tool_use_version_bump_guard.py``) re-checks the bump at
``git push``. That still leaves a window open, and canopy fell through it on
2026-07-28:

  1. Two PRs are opened from the same base. Each runs ``canopy version bump``,
     which picks ``max(local, origin/main) + 1`` — so BOTH pick v0.2.369.
  2. Each PR's ``check-version`` passes, because each was checked against main as
     it was when that PR's CI ran.
  3. #423 merges. GitHub does NOT re-run #429's check against the new base
     (branch-up-to-date is not required), so #429's green check is now stale.
  4. #429 merges forty seconds later. `main` now has two different commits both
     labelled v0.2.369.

The damage is downstream and silent: the plugin cache is keyed by version, so the
second merge's cache dir already existed holding the FIRST merge's code, and a
version-comparing update check reports UP_TO_DATE — whose documented response is
"STOP. Do nothing else." The fix simply never reaches the machine.
``canopy-update-check.sh`` is SHA-driven now so the *detection* survives this,
but the collision itself is still worth refusing: two commits sharing a version
makes every version-keyed thing (cache dirs, `claude plugin list`, rollback)
ambiguous.

So this hook re-runs the exact same check CI runs, at the last possible moment
before the merge, against a freshly fetched ``origin/main``. If another PR took
the number, the merge is denied and the agent is told to bump, push, and re-wait
for CI.

Scope discipline — it judges ONLY the branch it can reason about. The check reads
the local working tree's VERSION, so it is meaningful only when the PR being
merged is the checked-out branch's PR. Merging any other PR from this worktree
fails open, as does a `gh` lookup that errors, times out, or is logged out.

Set ``CANOPY_ALLOW_MERGE_NO_BUMP=1`` to override (allows the merge, warns on
stderr) — mirrors ``CANOPY_ALLOW_PUSH_NO_BUMP`` on the push guard.

Stdlib-only, loaded by file path, fail-open everywhere: a hook that wedges merges
is worse than the bug it prevents.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_GUARDED_TOOL = "Bash"

# `gh pr merge …`, tolerating env prefixes and separators. `gh pr create`,
# `gh pr checks`, and `git merge` must NOT match.
_GH_PR_MERGE_RE = re.compile(r"\bgh\s+pr\s+merge\b")
_PR_URL_RE = re.compile(r"/pull/(\d+)")


_SEPARATORS = {"&&", "||", ";", "|", "&"}


def _tokens(command: str) -> list[str] | None:
    """Shell-split the command, or None if it won't parse.

    Tokenising rather than regex-matching the raw string is what keeps a quoted
    mention — `echo 'gh pr merge'` — from reading as a merge: the quoted text
    arrives as ONE token, so the three-word sequence is never adjacent.
    """
    try:
        import shlex
        return shlex.split(command, comments=False, posix=True)
    except ValueError:
        return None


def _merge_index(tokens: list[str]) -> int:
    """Index of the `merge` token in a real `gh pr merge`, else -1."""
    for i in range(len(tokens) - 2):
        if tokens[i] == "gh" and tokens[i + 1] == "pr" and tokens[i + 2] == "merge":
            return i + 2
    return -1


def _is_pr_merge(command: str) -> bool:
    if not command or "merge" not in command:
        return False
    if "--help" in command:
        return False
    tokens = _tokens(command)
    if tokens is None:
        # Unparseable (unbalanced quotes) — fall back to the looser match rather
        # than skipping the check entirely.
        return bool(_GH_PR_MERGE_RE.search(command))
    return _merge_index(tokens) >= 0


def _merge_target(command: str) -> str:
    """The PR number / URL / branch given to `gh pr merge`, or "" for none.

    "" means the current branch's PR, which is the case this guard is surest of.
    """
    tokens = _tokens(command)
    if tokens is None:
        return ""
    idx = _merge_index(tokens)
    if idx < 0:
        return ""
    for token in tokens[idx + 1:]:
        if token in _SEPARATORS:
            break               # a later command's words are not our target
        if token.startswith("-"):
            continue
        return token
    return ""


def _repo_root() -> Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env and Path(env).is_dir():
        return Path(env)
    return Path.cwd()


def _git(repo_root: Path, *args: str, timeout: int = 10) -> str:
    try:
        proc = subprocess.run(["git", *args], cwd=repo_root, capture_output=True,
                              text=True, timeout=timeout, check=False)
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except (subprocess.TimeoutExpired, OSError):
        return ""


def _current_branch(repo_root: Path) -> str:
    return _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")


def _current_branch_pr(repo_root: Path):
    """The PR number for the checked-out branch, or None if it can't be known.

    One `gh` call, and only on the path that needs it (an explicit numeric/URL
    target). Every failure — no gh, logged out, offline, no PR — returns None,
    which the caller reads as "cannot reason about this" and allows.
    """
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", "--json", "number", "-q", ".number"],
            cwd=repo_root, capture_output=True, text=True, timeout=15, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    text = proc.stdout.strip()
    return int(text) if text.isdigit() else None


def _targets_current_branch(repo_root: Path, target: str) -> bool:
    """Is the PR being merged the one for the branch we're standing on?"""
    if not target:
        return True                                   # no arg => current branch
    if target == _current_branch(repo_root):
        return True
    number = target if target.isdigit() else ""
    if not number:
        url_match = _PR_URL_RE.search(target)
        number = url_match.group(1) if url_match else ""
    if not number:
        return False                                  # some other branch name
    current = _current_branch_pr(repo_root)
    return current is not None and current == int(number)


def _load_version_bump(repo_root: Path):
    mod_path = repo_root / "src" / "orchestrator" / "version_bump.py"
    if not mod_path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("canopy_version_bump_merge_guard", mod_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _block(reason: str) -> None:
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }
    sys.stdout.write(json.dumps(payload))
    sys.stdout.flush()
    sys.exit(0)


def _allow_silently() -> None:
    sys.stdout.write(json.dumps({"continue": True}))
    sys.stdout.flush()
    sys.exit(0)


def _allow_with_warning(message: str) -> None:
    sys.stderr.write(f"[canopy pr-merge guard] WARNING: {message}\n")
    _allow_silently()


def _build_block_message(info: dict) -> str:
    local_v = info.get("local_version")
    main_v = info.get("main_version")
    return (
        "BLOCKED by canopy pr-merge guard — merging this would put a SECOND "
        "commit on main carrying a version that is already there.\n\n"
        f"{(info.get('reason') or '').strip()}\n\n"
        f"branch VERSION: {local_v}   origin/main VERSION (just fetched): {main_v}\n\n"
        "This PR's green `check-version` is stale: it ran against main before "
        "another PR merged and took your number. GitHub does not re-run it when "
        "the base moves, so nothing else will catch this.\n\n"
        "Why it matters: the plugin cache is keyed by version. Two commits "
        "sharing one version means the second one's cache dir already exists "
        "holding the first one's code — how a merged fix silently fails to reach "
        "any machine.\n\n"
        "Fix:\n"
        "  uv run canopy version bump\n"
        "  git add -A && git commit -m 'chore: bump version'\n"
        "  git push\n"
        "  gh pr checks <n>     # wait for the re-run to go green\n"
        "  gh pr merge <n> --merge\n\n"
        "Override (rarely correct): set CANOPY_ALLOW_MERGE_NO_BUMP=1."
    )


def evaluate(hook_data: dict) -> tuple[str, object]:
    """Return ``(action, detail)``: action in {allow, block, override}."""
    if hook_data.get("tool_name", "") != _GUARDED_TOOL:
        return "allow", None

    tool_input = hook_data.get("tool_input", {}) or {}
    if not isinstance(tool_input, dict):
        return "allow", None

    command = tool_input.get("command", "") or ""
    if not _is_pr_merge(command):
        return "allow", None

    repo_root = _repo_root()
    if not _targets_current_branch(repo_root, _merge_target(command)):
        return "allow", None

    module = _load_version_bump(repo_root)
    if module is None:
        return "allow", None

    try:
        info = module.verify_bump_when_plugin_changed(repo_root)
    except Exception:
        return "allow", None

    if info.get("ok") or info.get("skipped"):
        return "allow", None

    if os.environ.get("CANOPY_ALLOW_MERGE_NO_BUMP") == "1":
        return "override", info
    return "block", info


def main() -> None:
    try:
        raw = sys.stdin.read()
        hook_data = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, EOFError):
        _allow_silently()
        return

    action, info = evaluate(hook_data)

    if action == "allow":
        _allow_silently()
    elif action == "override":
        reason = info.get("reason", "version collision") if isinstance(info, dict) else ""
        _allow_with_warning(
            f"CANOPY_ALLOW_MERGE_NO_BUMP=1 — allowing merge despite: {reason}"
        )
    else:
        _block(_build_block_message(info if isinstance(info, dict) else {}))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        try:
            _allow_silently()
        except Exception:
            pass
