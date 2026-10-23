#!/usr/bin/env python3
"""Claude Code SessionStart hook: record which GitHub repo this project dir is.

Adds `<project key> -> owner/repo` (from `git remote get-url origin`) to
~/.claude/canopy/repo-map.json, once per project dir. The project key is the dir
mangled the way Claude Code names its transcript dirs under ~/.claude/projects
(`/a/b` -> `-a-b`); readers are orchestrator.repo_map and everything that
resolves a transcript dir back to a repo (scanner, shareout, session_sources,
server).

The project dir is the hook input's `cwd`, else $CLAUDE_PROJECT_DIR.

Stdlib only (runs under whatever python the launcher finds), fast (one git call,
2s timeout, skipped when the key is already mapped), and fail-open: exits 0 on
every path, so it can never block a session.
"""

from __future__ import annotations  # PEP 604 unions work on Python 3.9 (Xcode CLT default)

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_MAP_FILE = Path.home() / ".claude" / "canopy" / "repo-map.json"


def project_key(project_dir: str) -> str:
    return "-" + project_dir.lstrip("/").replace("/", "-")


def maybe_capture_repo(project_dir: str) -> None:
    """Map project_dir to its origin's owner/repo, unless it is already mapped."""
    key = project_key(project_dir)

    repo_map = {}
    if REPO_MAP_FILE.exists():
        try:
            with open(REPO_MAP_FILE, encoding="utf-8") as f:
                repo_map = json.load(f)
        except Exception:
            repo_map = {}
    if not isinstance(repo_map, dict):
        repo_map = {}

    if key in repo_map:
        return

    try:
        result = subprocess.run(
            ["git", "-C", project_dir, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode != 0:
            return
        url = result.stdout.strip()
    except Exception:
        return

    match = re.search(r"github\.com[:/]([^/]+/[^/\s]+?)(?:\.git)?$", url)
    if not match:
        return

    repo_map[key] = match.group(1)
    REPO_MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(REPO_MAP_FILE, "w", encoding="utf-8") as f:
        json.dump(repo_map, f, indent=2)


def _project_dir(hook_input: object) -> str:
    cwd = hook_input.get("cwd") if isinstance(hook_input, dict) else None
    if isinstance(cwd, str) and cwd:
        return cwd
    return os.environ.get("CLAUDE_PROJECT_DIR", "")


def main() -> None:
    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, EOFError, ValueError):
        hook_input = {}
    project_dir = _project_dir(hook_input)
    if project_dir:
        maybe_capture_repo(project_dir)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
