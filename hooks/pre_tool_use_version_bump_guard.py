#!/usr/bin/env python3
"""Claude Code PreToolUse hook: block `git push` of an un-bumped plugin change.

The #1 source of red CI on canopy is the "Version sync check" → `verify-bump`
step: a branch touches ``plugins/canopy/`` but VERSION did not advance past
``origin/main``. Two flavours, both common when 100% of commits are AI-generated
across parallel emdash worktrees:

  A. No bump at all — the agent forgot to run ``canopy version bump``.
  B. Stale/collided bump — the agent bumped to N, but another worktree already
     claimed N and merged it to main first, so ``local`` no longer exceeds main.

A git ``pre-push`` hook (``scripts/hooks/pre-push``) already runs this exact
check, but it is opt-in (``git config core.hooksPath scripts/hooks``) and in
emdash worktrees ``core.hooksPath`` points elsewhere, so it never fires. This
Claude Code hook needs no opt-in — it is checked into ``.claude/settings.json``
and intercepts the agent at the exact tool (``Bash``) and moment (``git push``)
that every AI commit uses. The deny message becomes agent-visible feedback, so
the agent runs ``canopy version bump`` and retries — turning a red-CI round-trip
into a local self-correction.

Set ``CANOPY_ALLOW_PUSH_NO_BUMP=1`` to override (allows the push, warns on
stderr). Mirrors the ``CANOPY_ALLOW_CACHE_PATCH`` escape hatch on the sibling
plugin-cache guard.

Stdlib-only — runs under system python3. The version-bump logic in
``src/orchestrator/version_bump.py`` is itself stdlib-only and is loaded by file
path (no package import, no uv, no network beyond the git fetch it already
does), so this hook is cheap and fail-open.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path

# We only police the Bash tool — that's where `git push` happens.
_GUARDED_TOOL = "Bash"

# A real push: `git push ...`, allowing for `git -C <dir> push`, env prefixes,
# and leading separators. We deliberately do NOT match `git push --help` or a
# dry run (handled below) — false positives are the real cost.
_GIT_PUSH_RE = re.compile(r"\bgit\b(?:\s+-[^\s]+|\s+-C\s+\S+)*\s+push\b")


def _is_git_push(command: str) -> bool:
    if not command or "push" not in command:
        return False
    if not _GIT_PUSH_RE.search(command):
        return False
    # Skip non-mutating push invocations.
    if "--dry-run" in command or "--help" in command:
        return False
    return True


def _repo_root() -> Path:
    """Best-effort project root. CLAUDE_PROJECT_DIR is set by Claude Code."""
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env and Path(env).is_dir():
        return Path(env)
    return Path.cwd()


_GIT_C_RE = re.compile(r"\bgit\s+-C\s+(\"[^\"]+\"|'[^']+'|\S+)")
_LEADING_CD_RE = re.compile(r"^\s*cd\s+(\"[^\"]+\"|'[^']+'|\S+)\s*(?:&&|;)")


def _pushed_repo(command: str, hook_data: dict) -> Path:
    """The repo the push is FROM — not the session's project. A session opened in
    a canopy worktree also pushes other repos (`git -C ~/x push`, `cd ~/x && git
    push`); judging those by the canopy worktree's branch blocked a canopy-web
    push for a canopy bump it could never contain (canopy-web#1337 follow-up).
    Order: `git -C <dir>` → a leading `cd <dir>` → the hook's `cwd` →
    :func:`_repo_root`. Resolved to the git toplevel; on any failure, the old
    behaviour (:func:`_repo_root`)."""
    m = _GIT_C_RE.search(command) or _LEADING_CD_RE.search(command)
    if m:
        start = m.group(1).strip("\"'")
    else:
        start = str(hook_data.get("cwd") or _repo_root())
    start = os.path.expandvars(os.path.expanduser(start))
    try:
        import subprocess

        proc = subprocess.run(["git", "-C", start, "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, timeout=10, check=False)
        top = proc.stdout.strip()
        if proc.returncode == 0 and top and Path(top).is_dir():
            return Path(top)
    except Exception:
        pass
    return _repo_root()


def _load_version_bump(repo_root: Path):
    """Import version_bump.py by file path — avoids importing the orchestrator
    package (which would need src/ on sys.path and is heavier than needed)."""
    mod_path = repo_root / "src" / "orchestrator" / "version_bump.py"
    if not mod_path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("canopy_version_bump_guard", mod_path)
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
    sys.stderr.write(f"[canopy version-bump guard] WARNING: {message}\n")
    _allow_silently()


def _build_block_message(info: dict) -> str:
    files = info.get("plugin_files_changed") or []
    shown = "\n".join(f"  - {f}" for f in files[:10])
    if len(files) > 10:
        shown += f"\n  …and {len(files) - 10} more"
    local_v = info.get("local_version")
    main_v = info.get("main_version")
    return (
        "BLOCKED by canopy version-bump guard — this push would fail the "
        "`Version sync check` CI gate.\n\n"
        f"{info.get('reason', '').strip()}\n\n"
        f"branch VERSION: {local_v}   origin/main VERSION: {main_v}\n\n"
        f"plugins/canopy/ files changed on this branch:\n{shown}\n\n"
        "Fix (handles both 'forgot to bump' and 'another worktree took your "
        "number'):\n"
        "  uv run canopy version bump\n"
        "  git add VERSION plugins/canopy/.claude-plugin/plugin.json "
        ".claude-plugin/marketplace.json\n"
        "  git commit -m 'chore: bump version'   # or --amend --no-edit\n"
        "  # then re-run your push\n\n"
        "`canopy version bump` fetches origin first, so it will pick a number "
        "above whatever main is at right now. Override (rarely correct): "
        "set CANOPY_ALLOW_PUSH_NO_BUMP=1."
    )


def evaluate(hook_data: dict) -> tuple[str, object]:
    """Return ``(action, detail)``: action in {allow, block, override}.

    detail is the info dict from verify-bump on block/override, else None.
    """
    if hook_data.get("tool_name", "") != _GUARDED_TOOL:
        return "allow", None

    tool_input = hook_data.get("tool_input", {}) or {}
    if not isinstance(tool_input, dict):
        return "allow", None

    command = tool_input.get("command", "") or ""
    if not _is_git_push(command):
        return "allow", None

    repo_root = _pushed_repo(command, hook_data)
    module = _load_version_bump(repo_root)
    if module is None:
        # Not a canopy checkout (no version_bump.py), or the checker is missing —
        # nothing of canopy's to protect; fail open rather than wedge the push.
        return "allow", None

    try:
        info = module.verify_bump_when_plugin_changed(repo_root)
    except Exception:
        return "allow", None

    # ok or skipped => nothing to block. The check skips itself when it can't
    # reach origin/main, and passes when no plugin files changed or VERSION
    # already advanced.
    if info.get("ok") or info.get("skipped"):
        return "allow", None

    if os.environ.get("CANOPY_ALLOW_PUSH_NO_BUMP") == "1":
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
        reason = info.get("reason", "plugin change without bump") if isinstance(info, dict) else ""
        _allow_with_warning(
            f"CANOPY_ALLOW_PUSH_NO_BUMP=1 — allowing push despite: {reason}"
        )
    else:  # block
        _block(_build_block_message(info if isinstance(info, dict) else {}))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # Last-ditch: never wedge Claude Code on hook failure.
        try:
            _allow_silently()
        except Exception:
            pass
