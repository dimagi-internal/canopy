#!/usr/bin/env python3
"""Claude Code PostToolUse hook for orchestrator session capture.

Reads hook data from stdin (JSON), detects MCP tool calls,
and appends to ~/.claude/canopy/session-log.jsonl.

Exit 0 always — hook failures should never block Claude Code.
"""

from __future__ import annotations  # PEP 604 unions work on Python 3.9 (Xcode CLT default)

import json
import sys
import os
from datetime import datetime, timezone
from pathlib import Path

LOG_FILE = Path.home() / ".claude" / "canopy" / "session-log.jsonl"
REPO_MAP_FILE = Path.home() / ".claude" / "canopy" / "repo-map.json"
HOOK_ERROR_LOG = Path.home() / ".claude" / "canopy" / "hook-errors.log"
_PLUGINS_FILE = Path.home() / ".claude" / "plugins" / "installed_plugins.json"

POST_TIMEOUT_SECONDS = 15

# Literal duplicate of orchestrator.canopy_web.DEFAULT_API — unavoidable: this hook
# runs under the system python3 with a bare sys.path and cannot import orchestrator.
CANOPY_WEB_API = os.environ.get(
    "CANOPY_WEB_API_URL",
    "https://labs.connect.dimagi.com/canopy",
)
WORKBENCH_TOKEN_FILE = Path.home() / ".claude" / "canopy" / "workbench-token"

TRACKED_SKILLS = {
    "canopy:doc-regen",
    "canopy:doc-regeneration",
    "canopy:improve",
    "canopy:patterns",
    "canopy:brief",
    "canopy:session-review",
    "canopy:walkthrough",
    "canopy:walkthrough-eval",
    "canopy:portfolio-review",
    "canopy:activity-summary",
    "code-review:code-review",
    "superpowers:requesting-code-review",
    "dev-utils:resolve-ci-failures",
    "dev-utils:resolve-pr-comments",
}

_seen_sessions: set[str] = set()
_cached_version: str | None = None


def _get_plugin_version() -> str:
    """Return the installed canopy plugin version, cached after first read."""
    global _cached_version
    if _cached_version is not None:
        return _cached_version
    try:
        with open(_PLUGINS_FILE) as f:
            data = json.load(f)
        plugins = data.get("plugins", {})
        for key, value in plugins.items():
            if "canopy" in key:
                entries = value if isinstance(value, list) else [value]
                if entries:
                    version = entries[0].get("version", "unknown")
                    _cached_version = version
                    return _cached_version
    except Exception:
        pass
    _cached_version = "unknown"
    return _cached_version


def maybe_capture_repo(project_dir: str):
    """Capture git remote -> repo mapping if not already known.

    Uses JSON (stdlib) instead of YAML to avoid dependency issues —
    the hook must work with any system Python.
    """
    import subprocess
    import re

    project_key = "-" + project_dir.lstrip("/").replace("/", "-")

    # Check if already mapped
    repo_map = {}
    if REPO_MAP_FILE.exists():
        try:
            with open(REPO_MAP_FILE) as f:
                repo_map = json.load(f)
        except Exception:
            repo_map = {}

    if project_key in repo_map:
        return

    # Try to get git remote
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

    # Extract owner/repo
    match = re.search(r"github\.com[:/]([^/]+/[^/\s]+?)(?:\.git)?$", url)
    if not match:
        return

    repo_map[project_key] = match.group(1)
    REPO_MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(REPO_MAP_FILE, "w") as f:
        json.dump(repo_map, f, indent=2)

try:
    from orchestrator.capture import append_log_entry
except ImportError:
    # Fallback: package not installed; write inline so the hook never breaks.
    def append_log_entry(log_file: Path, entry: dict) -> None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")


def _record_hook_error(reason: str, context: dict) -> None:
    """Append a one-line failure record to ~/.claude/canopy/hook-errors.log.

    The hook must never block Claude Code, so this itself is best-effort. But
    silent drops in `_post_action_to_workbench` made gaps invisible — this
    sidecar gives `/canopy:doctor` something to read when actions seem missing.
    """
    try:
        HOOK_ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            **context,
        }
        with open(HOOK_ERROR_LOG, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass


def _post_action_to_workbench(skill_name: str, session_id: str, project_dir: str):
    """POST a skill action to canopy-web's project actions API.

    Failures are recorded to HOOK_ERROR_LOG with a category tag rather than
    swallowed silently — see _record_hook_error.
    """
    import urllib.request

    if not WORKBENCH_TOKEN_FILE.exists():
        _record_hook_error("token_file_missing", {"skill": skill_name})
        return
    try:
        token = WORKBENCH_TOKEN_FILE.read_text().strip()
    except Exception as exc:
        _record_hook_error("token_read_failed", {"skill": skill_name, "error": str(exc)})
        return
    if not token:
        _record_hook_error("token_empty", {"skill": skill_name})
        return

    repo_map = {}
    if REPO_MAP_FILE.exists():
        try:
            with open(REPO_MAP_FILE) as f:
                repo_map = json.load(f)
        except Exception as exc:
            _record_hook_error("repo_map_read_failed", {"skill": skill_name, "error": str(exc)})
            return

    project_key = "-" + project_dir.lstrip("/").replace("/", "-")
    github_repo = repo_map.get(project_key, "")
    if not github_repo or "/" not in github_repo:
        _record_hook_error(
            "repo_unmapped",
            {"skill": skill_name, "project_dir": project_dir, "project_key": project_key},
        )
        return

    slug = github_repo.split("/")[-1]
    now = datetime.now(timezone.utc).isoformat()
    payload = json.dumps({
        "skill_name": skill_name,
        "session_id": session_id,
        "status": "completed",
        "started_at": now,
        "completed_at": now,
    }).encode()

    try:
        req = urllib.request.Request(
            f"{CANOPY_WEB_API}/api/projects/{slug}/actions/",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=POST_TIMEOUT_SECONDS)
    except urllib.error.HTTPError as exc:
        _record_hook_error(
            "http_error",
            {"skill": skill_name, "slug": slug, "status": exc.code, "reason": exc.reason},
        )
    except urllib.error.URLError as exc:
        _record_hook_error(
            "network_error",
            {"skill": skill_name, "slug": slug, "error": str(exc.reason)},
        )
    except Exception as exc:
        _record_hook_error(
            "unexpected_error",
            {"skill": skill_name, "slug": slug, "error": str(exc)},
        )


def _legacy_capture_registered() -> bool:
    """True if an older setup registered this hook in ~/.claude/settings.json.

    This plugin-managed copy defers to a WORKING legacy registration (command
    still points at an existing file) so the two never double-log a tool call.
    `/canopy:setup` removes the legacy entry, which flips capture to this copy.
    """
    try:
        with open(Path.home() / ".claude" / "settings.json") as f:
            settings = json.load(f)
        for matcher in settings.get("hooks", {}).get("PostToolUse", []):
            for hook in matcher.get("hooks", []):
                cmd = hook.get("command", "")
                if "post_tool_use.py" not in cmd:
                    continue
                for token in cmd.split():
                    if token.endswith("post_tool_use.py") and os.path.exists(token):
                        return True
    except Exception:
        return False
    return False


def main():
    # When invoked THROUGH the legacy repo-root shim, we ARE the legacy
    # registration — skip the defer check or nobody captures.
    if os.environ.get("CANOPY_CAPTURE_LEGACY_SHIM") != "1" and _legacy_capture_registered():
        return

    try:
        hook_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, EOFError):
        return

    # Always capture repo mapping — runs on every tool call, not just MCP
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if project_dir:
        maybe_capture_repo(project_dir)

    # Emit session_start on first call for this session
    session_id = hook_data.get("session_id", "unknown")
    if session_id not in _seen_sessions:
        _seen_sessions.add(session_id)
        start_entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "event": "session_start",
            "plugin_version": _get_plugin_version(),
            "project": os.environ.get("CLAUDE_PROJECT_DIR", "unknown"),
        }
        append_log_entry(LOG_FILE, start_entry)

    tool_name = hook_data.get("tool_name", "")

    # Emit skill_invoked for Skill tool calls and return
    if tool_name == "Skill":
        tool_input = hook_data.get("tool_input", {})
        skill_name = tool_input.get("skill", "") if isinstance(tool_input, dict) else ""
        skill_entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "event": "skill_invoked",
            "skill": skill_name,
            "plugin_version": _get_plugin_version(),
            "project": os.environ.get("CLAUDE_PROJECT_DIR", "unknown"),
        }
        append_log_entry(LOG_FILE, skill_entry)
        if skill_name in TRACKED_SKILLS and project_dir:
            _post_action_to_workbench(skill_name, session_id, project_dir)
        return

    if not tool_name.startswith("mcp__"):
        return

    parts = tool_name.split("__", 2)
    if len(parts) < 3:
        return

    server_name = parts[1]
    mcp_tool = parts[2]

    tool_input = hook_data.get("tool_input", {})
    input_summary = {}
    if isinstance(tool_input, dict):
        for k, v in tool_input.items():
            if isinstance(v, str) and len(v) > 100:
                input_summary[k] = v[:100] + "..."
            else:
                input_summary[k] = v

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": hook_data.get("session_id", "unknown"),
        "project": os.environ.get("CLAUDE_PROJECT_DIR", "unknown"),
        "server": server_name,
        "tool": mcp_tool,
        "input_summary": input_summary,
        "success": not hook_data.get("tool_error"),
    }

    append_log_entry(LOG_FILE, entry)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
