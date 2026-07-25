# Session Review Agent Implementation Plan

> **Status: shipped — session-review agent/command live in `plugins/canopy/`. Historical record, not current-state.**

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a session-review agent that batch-reviews recent sessions, detects stale skill versions, cross-references prior improvement attempts, and produces a ranked findings table with confidence scores.

**Architecture:** Three components: (1) capture infrastructure in the post_tool_use hook to record plugin versions and skill invocations, (2) directory rename from `~/.claude/orchestrator/` to `~/.claude/canopy/` via a shared paths module, (3) the session-review agent + command plugin files. The hook changes are stdlib-only. The paths module centralizes the directory constant and handles legacy migration. The agent is a markdown definition that orchestrates existing canopy CLI commands.

**Tech Stack:** Python 3.11+ (stdlib for hooks), PyYAML + Click (for src/), Claude Code plugin system (markdown agents/commands)

---

### Task 1: Create paths module with CANOPY_DIR constant and migration

**Files:**
- Create: `src/orchestrator/paths.py`
- Test: `tests/test_paths.py`

- [ ] **Step 1: Write failing tests for paths module**

```python
# tests/test_paths.py
"""Tests for orchestrator.paths module."""

from pathlib import Path

import pytest

from orchestrator.paths import CANOPY_DIR, _LEGACY_DIR, ensure_canopy_dir


class TestCanopyDirConstant:
    def test_canopy_dir_is_under_home(self):
        assert CANOPY_DIR.parts[-2:] == (".claude", "canopy")

    def test_canopy_dir_is_absolute(self):
        assert CANOPY_DIR.is_absolute()

    def test_legacy_dir_is_under_home(self):
        assert _LEGACY_DIR.parts[-2:] == (".claude", "orchestrator")


class TestEnsureCanopyDir:
    def test_creates_canopy_dir_when_nothing_exists(self, tmp_path, monkeypatch):
        canopy = tmp_path / ".claude" / "canopy"
        legacy = tmp_path / ".claude" / "orchestrator"
        monkeypatch.setattr("orchestrator.paths.CANOPY_DIR", canopy)
        monkeypatch.setattr("orchestrator.paths._LEGACY_DIR", legacy)
        result = ensure_canopy_dir()
        assert result == canopy
        assert canopy.is_dir()

    def test_migrates_legacy_to_canopy(self, tmp_path, monkeypatch):
        canopy = tmp_path / ".claude" / "canopy"
        legacy = tmp_path / ".claude" / "orchestrator"
        legacy.mkdir(parents=True)
        (legacy / "session-log.jsonl").write_text('{"test": true}\n')
        monkeypatch.setattr("orchestrator.paths.CANOPY_DIR", canopy)
        monkeypatch.setattr("orchestrator.paths._LEGACY_DIR", legacy)
        result = ensure_canopy_dir()
        assert result == canopy
        assert canopy.is_dir()
        assert not legacy.exists()
        assert (canopy / "session-log.jsonl").read_text() == '{"test": true}\n'

    def test_uses_canopy_when_both_exist(self, tmp_path, monkeypatch):
        canopy = tmp_path / ".claude" / "canopy"
        legacy = tmp_path / ".claude" / "orchestrator"
        canopy.mkdir(parents=True)
        legacy.mkdir(parents=True)
        (canopy / "marker.txt").write_text("canopy")
        (legacy / "marker.txt").write_text("legacy")
        monkeypatch.setattr("orchestrator.paths.CANOPY_DIR", canopy)
        monkeypatch.setattr("orchestrator.paths._LEGACY_DIR", legacy)
        result = ensure_canopy_dir()
        assert result == canopy
        assert (canopy / "marker.txt").read_text() == "canopy"
        # Legacy still exists (not deleted when both present)
        assert legacy.exists()

    def test_returns_existing_canopy_dir(self, tmp_path, monkeypatch):
        canopy = tmp_path / ".claude" / "canopy"
        legacy = tmp_path / ".claude" / "orchestrator"
        canopy.mkdir(parents=True)
        monkeypatch.setattr("orchestrator.paths.CANOPY_DIR", canopy)
        monkeypatch.setattr("orchestrator.paths._LEGACY_DIR", legacy)
        result = ensure_canopy_dir()
        assert result == canopy

    def test_creates_nested_parents(self, tmp_path, monkeypatch):
        canopy = tmp_path / "deep" / ".claude" / "canopy"
        legacy = tmp_path / "deep" / ".claude" / "orchestrator"
        monkeypatch.setattr("orchestrator.paths.CANOPY_DIR", canopy)
        monkeypatch.setattr("orchestrator.paths._LEGACY_DIR", legacy)
        ensure_canopy_dir()
        assert canopy.is_dir()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_paths.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.paths'`

- [ ] **Step 3: Implement paths module**

```python
# src/orchestrator/paths.py
"""Shared path constants for canopy data directory.

Centralizes the data directory path and handles migration from the
legacy ~/.claude/orchestrator/ location.
"""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

CANOPY_DIR = Path.home() / ".claude" / "canopy"
_LEGACY_DIR = Path.home() / ".claude" / "orchestrator"


def ensure_canopy_dir() -> Path:
    """Return CANOPY_DIR, migrating from legacy path if needed."""
    if _LEGACY_DIR.exists() and not CANOPY_DIR.exists():
        logger.info("Migrating %s -> %s", _LEGACY_DIR, CANOPY_DIR)
        CANOPY_DIR.parent.mkdir(parents=True, exist_ok=True)
        _LEGACY_DIR.rename(CANOPY_DIR)
    elif _LEGACY_DIR.exists() and CANOPY_DIR.exists():
        logger.warning(
            "Both %s and %s exist. Using %s.",
            _LEGACY_DIR, CANOPY_DIR, CANOPY_DIR,
        )
    CANOPY_DIR.mkdir(parents=True, exist_ok=True)
    return CANOPY_DIR
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_paths.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/paths.py tests/test_paths.py
git commit -m "feat: add paths module with CANOPY_DIR and legacy migration"
```

---

### Task 2: Update cli.py to use CANOPY_DIR from paths module

**Files:**
- Modify: `src/orchestrator/cli.py:136,167,190,240,325,349,373`
- Test: `tests/test_cli.py` (existing tests should still pass)

- [ ] **Step 1: Write a failing test that cli imports from paths**

```python
# Add to tests/test_cli.py at the end of the file

class TestCliUsesCanopyDir:
    def test_sessions_list_uses_canopy_dir(self, tmp_path):
        """Verify sessions list reads from canopy dir, not orchestrator."""
        from orchestrator import paths
        canopy_dir = tmp_path / ".claude" / "canopy"
        canopy_dir.mkdir(parents=True)
        (canopy_dir / "repo-map.json").write_text("{}")

        with mock.patch.object(paths, "CANOPY_DIR", canopy_dir), \
             mock.patch("orchestrator.cli.find_registry", return_value=SAMPLE_REGISTRY), \
             mock.patch("orchestrator.scanner.scan_all_transcripts", return_value=[]):
            result = CliRunner().invoke(main, ["sessions", "list"])
        assert result.exit_code == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py::TestCliUsesCanopyDir -v`
Expected: FAIL (cli.py still uses hardcoded orchestrator path)

- [ ] **Step 3: Update cli.py to import and use CANOPY_DIR**

In `src/orchestrator/cli.py`, add the import at the top:

```python
from orchestrator.paths import CANOPY_DIR, ensure_canopy_dir
```

Then replace all 7 occurrences of `Path.home() / ".claude" / "orchestrator"` with the appropriate reference:

- Line 136 (`sessions_list`): `state_dir = ensure_canopy_dir()`
- Line 167 (`sessions_status`): `log_file = CANOPY_DIR / "session-log.jsonl"` (don't need ensure here, just reading)
- Line 190 (`improve`): `state_dir = ensure_canopy_dir()`
- Line 240 (`analyze_cmd`): `state_dir = ensure_canopy_dir()`
- Line 325 (`serve`): `state_dir = ensure_canopy_dir()`
- Line 349 (`brief`): `state_dir = ensure_canopy_dir()`
- Line 373 (`patterns_cmd`): `state_dir = ensure_canopy_dir()`

- [ ] **Step 4: Run all CLI tests to verify nothing broke**

Run: `uv run pytest tests/test_cli.py tests/test_cli_brief.py tests/test_cli_improve.py tests/test_cli_patterns.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/cli.py tests/test_cli.py
git commit -m "refactor: update cli.py to use CANOPY_DIR from paths module"
```

---

### Task 3: Update scheduler.py to use CANOPY_DIR

**Files:**
- Modify: `src/orchestrator/scheduler.py:55,76`

- [ ] **Step 1: Write a failing test**

```python
# Add to a new file or append to existing tests/test_scheduler.py
# tests/test_scheduler.py

from pathlib import Path
from unittest import mock

from orchestrator.scheduler import generate_plist, install_schedule


class TestSchedulerUsesCanopyDir:
    def test_generate_plist_default_log_dir_uses_canopy(self):
        content = generate_plist(Path("/my/project"))
        assert ".claude/canopy/logs" in content
        assert ".claude/orchestrator" not in content

    def test_install_schedule_creates_canopy_log_dir(self, tmp_path, monkeypatch):
        import orchestrator.paths as paths_mod
        canopy_dir = tmp_path / ".claude" / "canopy"
        monkeypatch.setattr(paths_mod, "CANOPY_DIR", canopy_dir)
        monkeypatch.setattr("orchestrator.scheduler.CANOPY_DIR", canopy_dir)
        # Mock home to use tmp_path for LaunchAgents
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        plist_path = install_schedule(Path("/my/project"))
        assert plist_path.exists()
        content = plist_path.read_text()
        assert "canopy/logs" in content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_scheduler.py::TestSchedulerUsesCanopyDir -v`
Expected: FAIL

- [ ] **Step 3: Update scheduler.py**

Add import at top of `src/orchestrator/scheduler.py`:

```python
from orchestrator.paths import CANOPY_DIR
```

Replace line 55:
```python
# Old: log_dir = Path.home() / ".claude" / "orchestrator" / "logs"
log_dir = CANOPY_DIR / "logs"
```

Replace line 76:
```python
# Old: log_dir = Path.home() / ".claude" / "orchestrator" / "logs"
log_dir = CANOPY_DIR / "logs"
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_scheduler.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/scheduler.py tests/test_scheduler.py
git commit -m "refactor: update scheduler.py to use CANOPY_DIR"
```

---

### Task 4: Update campaigns.py docstring and hook files

**Files:**
- Modify: `src/orchestrator/campaigns.py:8` (docstring only)
- Modify: `hooks/post_tool_use.py:5,16,17` (path constants)
- Modify: `hooks/install.py:41` (print message)

- [ ] **Step 1: Update campaigns.py docstring**

In `src/orchestrator/campaigns.py`, change line 8:
```python
# Old: ~/.claude/orchestrator/campaigns/
# New: ~/.claude/canopy/campaigns/
```

- [ ] **Step 2: Update hooks/post_tool_use.py path constants**

Change line 5 (docstring):
```python
# Old: and appends to ~/.claude/orchestrator/session-log.jsonl.
# New: and appends to ~/.claude/canopy/session-log.jsonl.
```

Change lines 16-17:
```python
# Old:
LOG_FILE = Path.home() / ".claude" / "orchestrator" / "session-log.jsonl"
REPO_MAP_FILE = Path.home() / ".claude" / "orchestrator" / "repo-map.json"
# New:
LOG_FILE = Path.home() / ".claude" / "canopy" / "session-log.jsonl"
REPO_MAP_FILE = Path.home() / ".claude" / "canopy" / "repo-map.json"
```

- [ ] **Step 3: Update hooks/install.py print message**

Change line 41:
```python
# Old: print(f"Hook installed. Logging to {Path.home() / '.claude' / 'orchestrator' / 'session-log.jsonl'}")
# New: print(f"Hook installed. Logging to {Path.home() / '.claude' / 'canopy' / 'session-log.jsonl'}")
```

- [ ] **Step 4: Run hook tests to verify nothing broke**

Run: `uv run pytest tests/test_hook.py -v`
Expected: All tests PASS (tests mock LOG_FILE so they don't depend on the actual path)

- [ ] **Step 5: Run full test suite**

Run: `uv run pytest -x -q`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/campaigns.py hooks/post_tool_use.py hooks/install.py
git commit -m "refactor: update remaining files to use canopy path"
```

---

### Task 5: Update markdown documentation references

**Files:**
- Modify: `plugins/canopy/skills/patterns/SKILL.md:37`
- Modify: `docs/superpowers/specs/2026-03-21-transcript-browser-design.md`
- Modify: `docs/superpowers/specs/2026-03-20-orchestrator-design.md`
- Modify: `docs/superpowers/plans/2026-03-23-phase-2b-intelligence.md`
- Modify: `docs/superpowers/plans/2026-03-23-canopy-plugin-merge.md`
- Modify: `docs/superpowers/plans/2026-03-20-improvement-loop.md`

- [ ] **Step 1: Update patterns skill**

In `plugins/canopy/skills/patterns/SKILL.md`, line 37:
```markdown
# Old: This reads from `~/.claude/orchestrator/observations/`
# New: This reads from `~/.claude/canopy/observations/`
```

- [ ] **Step 2: Update transcript browser design spec**

In `docs/superpowers/specs/2026-03-21-transcript-browser-design.md`, replace all occurrences of `~/.claude/orchestrator/` with `~/.claude/canopy/`. This includes references on lines 68, 93, 147, 150, 155, 157, 179.

- [ ] **Step 3: Update orchestrator design spec**

In `docs/superpowers/specs/2026-03-20-orchestrator-design.md`, replace all occurrences of `~/.claude/orchestrator/` with `~/.claude/canopy/`. This includes references on lines 56, 72, 289.

- [ ] **Step 4: Update remaining plan docs**

In each of these files, replace `~/.claude/orchestrator/` with `~/.claude/canopy/`:
- `docs/superpowers/plans/2026-03-23-phase-2b-intelligence.md` (line 137)
- `docs/superpowers/plans/2026-03-23-canopy-plugin-merge.md` (line 643)
- `docs/superpowers/plans/2026-03-20-improvement-loop.md` (line 2550)

- [ ] **Step 5: Commit**

```bash
git add plugins/canopy/skills/patterns/SKILL.md docs/superpowers/specs/ docs/superpowers/plans/
git commit -m "docs: update all references from orchestrator to canopy path"
```

---

### Task 6: Add version capture to post_tool_use hook

**Files:**
- Modify: `hooks/post_tool_use.py`
- Modify: `tests/test_hook.py`

- [ ] **Step 1: Write failing tests for session_start event**

Add these test classes to `tests/test_hook.py`:

```python
# ---------------------------------------------------------------------------
# Session start event
# ---------------------------------------------------------------------------


class TestSessionStartEvent:
    def _make_plugins_json(self, tmp_path, version="0.2.22"):
        """Create a fake installed_plugins.json."""
        plugins_file = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
        plugins_file.parent.mkdir(parents=True, exist_ok=True)
        plugins_file.write_text(json.dumps({
            "plugins": {
                "canopy@canopy": [{"version": version, "installPath": "/fake"}]
            }
        }))
        return plugins_file

    def test_emits_session_start_on_first_call(self, tmp_path):
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        plugins = self._make_plugins_json(tmp_path)
        data = json.dumps({
            "tool_name": "mcp__srv__tool",
            "session_id": "sess-new",
            "tool_input": {},
        })
        with mock.patch.object(hook, "_PLUGINS_FILE", plugins):
            _run_main(hook, data, log, env={"CLAUDE_PROJECT_DIR": "/my/proj"})
        entries = _read_log(log)
        # Should have session_start + mcp entry
        assert len(entries) == 2
        start = entries[0]
        assert start["event"] == "session_start"
        assert start["session_id"] == "sess-new"
        assert start["plugin_version"] == "0.2.22"

    def test_no_duplicate_session_start(self, tmp_path):
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        plugins = self._make_plugins_json(tmp_path)
        for i in range(3):
            data = json.dumps({
                "tool_name": "mcp__srv__tool",
                "session_id": "sess-dup",
                "tool_input": {},
            })
            with mock.patch.object(hook, "_PLUGINS_FILE", plugins):
                _run_main(hook, data, log, env={"CLAUDE_PROJECT_DIR": "/proj"})
        entries = _read_log(log)
        start_events = [e for e in entries if e.get("event") == "session_start"]
        assert len(start_events) == 1

    def test_session_start_version_unknown_when_no_plugins_file(self, tmp_path):
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        fake_plugins = tmp_path / "nonexistent" / "plugins.json"
        data = json.dumps({
            "tool_name": "mcp__srv__tool",
            "session_id": "sess-noplugin",
            "tool_input": {},
        })
        with mock.patch.object(hook, "_PLUGINS_FILE", fake_plugins):
            _run_main(hook, data, log, env={"CLAUDE_PROJECT_DIR": "/proj"})
        entries = _read_log(log)
        start = [e for e in entries if e.get("event") == "session_start"][0]
        assert start["plugin_version"] == "unknown"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_hook.py::TestSessionStartEvent -v`
Expected: FAIL

- [ ] **Step 3: Write failing tests for skill_invoked event**

```python
# ---------------------------------------------------------------------------
# Skill invoked event
# ---------------------------------------------------------------------------


class TestSkillInvokedEvent:
    def _make_plugins_json(self, tmp_path, version="0.2.22"):
        plugins_file = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
        plugins_file.parent.mkdir(parents=True, exist_ok=True)
        plugins_file.write_text(json.dumps({
            "plugins": {
                "canopy@canopy": [{"version": version, "installPath": "/fake"}]
            }
        }))
        return plugins_file

    def test_emits_skill_invoked_for_skill_tool(self, tmp_path):
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        plugins = self._make_plugins_json(tmp_path)
        data = json.dumps({
            "tool_name": "Skill",
            "session_id": "sess-skill",
            "tool_input": {"skill": "canopy:walkthrough", "args": "improve foo"},
        })
        with mock.patch.object(hook, "_PLUGINS_FILE", plugins):
            _run_main(hook, data, log, env={"CLAUDE_PROJECT_DIR": "/proj"})
        entries = _read_log(log)
        skill_events = [e for e in entries if e.get("event") == "skill_invoked"]
        assert len(skill_events) == 1
        assert skill_events[0]["skill"] == "canopy:walkthrough"
        assert skill_events[0]["plugin_version"] == "0.2.22"

    def test_no_skill_event_for_non_skill_tools(self, tmp_path):
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        plugins = self._make_plugins_json(tmp_path)
        data = json.dumps({
            "tool_name": "Read",
            "session_id": "sess-read",
            "tool_input": {"file_path": "/foo"},
        })
        with mock.patch.object(hook, "_PLUGINS_FILE", plugins):
            _run_main(hook, data, log, env={"CLAUDE_PROJECT_DIR": "/proj"})
        entries = _read_log(log)
        skill_events = [e for e in entries if e.get("event") == "skill_invoked"]
        assert len(skill_events) == 0

    def test_skill_event_has_session_id(self, tmp_path):
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        plugins = self._make_plugins_json(tmp_path)
        data = json.dumps({
            "tool_name": "Skill",
            "session_id": "sess-123",
            "tool_input": {"skill": "canopy:improve"},
        })
        with mock.patch.object(hook, "_PLUGINS_FILE", plugins):
            _run_main(hook, data, log, env={"CLAUDE_PROJECT_DIR": "/proj"})
        entries = _read_log(log)
        skill_event = [e for e in entries if e.get("event") == "skill_invoked"][0]
        assert skill_event["session_id"] == "sess-123"
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `uv run pytest tests/test_hook.py::TestSkillInvokedEvent -v`
Expected: FAIL

- [ ] **Step 5: Implement version capture in post_tool_use.py**

Update `hooks/post_tool_use.py`. The full updated file:

```python
#!/usr/bin/env python3
"""Claude Code PostToolUse hook for canopy session capture.

Reads hook data from stdin (JSON), detects MCP tool calls,
and appends to ~/.claude/canopy/session-log.jsonl.

Also records:
- session_start events with plugin version on first tool call per session
- skill_invoked events when the Skill tool is called

Exit 0 always — hook failures should never block Claude Code.
"""

import json
import sys
import os
from datetime import datetime, timezone
from pathlib import Path

LOG_FILE = Path.home() / ".claude" / "canopy" / "session-log.jsonl"
REPO_MAP_FILE = Path.home() / ".claude" / "canopy" / "repo-map.json"
_PLUGINS_FILE = Path.home() / ".claude" / "plugins" / "installed_plugins.json"

# Track seen sessions to emit session_start only once per process
_seen_sessions: set[str] = set()
_cached_version: str | None = None


def _get_plugin_version() -> str:
    """Read canopy plugin version from installed_plugins.json. Cached."""
    global _cached_version
    if _cached_version is not None:
        return _cached_version
    try:
        with open(_PLUGINS_FILE) as f:
            data = json.load(f)
        plugins = data.get("plugins", {})
        for key, entries in plugins.items():
            if "canopy" in key and isinstance(entries, list) and entries:
                _cached_version = entries[0].get("version", "unknown")
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


def main():
    try:
        hook_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, EOFError):
        return

    # Always capture repo mapping — runs on every tool call, not just MCP
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if project_dir:
        maybe_capture_repo(project_dir)

    tool_name = hook_data.get("tool_name", "")
    session_id = hook_data.get("session_id", "unknown")

    # Emit session_start on first tool call per session
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

    # Emit skill_invoked for Skill tool calls
    if tool_name == "Skill":
        tool_input = hook_data.get("tool_input", {})
        skill_name = tool_input.get("skill", "unknown") if isinstance(tool_input, dict) else "unknown"
        skill_entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "event": "skill_invoked",
            "skill": skill_name,
            "plugin_version": _get_plugin_version(),
            "project": os.environ.get("CLAUDE_PROJECT_DIR", "unknown"),
        }
        append_log_entry(LOG_FILE, skill_entry)
        return

    # Original MCP tool logging
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
        "session_id": session_id,
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
```

- [ ] **Step 6: Run all hook tests**

Run: `uv run pytest tests/test_hook.py -v`
Expected: All tests PASS (existing + new)

Note: The existing `TestNonMcpToolCallIgnored` tests check that non-MCP tools produce no log entry. These tests will need updating since non-MCP tools now produce `session_start` events. Update those tests to account for the session_start entry:

In `TestNonMcpToolCallIgnored`, the tests check `assert not log.exists()`. After our changes, a `session_start` entry WILL be written for any tool call (including non-MCP). Update these tests:

```python
class TestNonMcpToolCallIgnored:
    def test_non_mcp_tool_no_mcp_entry_written(self, tmp_path):
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        plugins_file = tmp_path / "no-plugins.json"
        data = json.dumps({"tool_name": "Bash", "session_id": "s1", "tool_input": {"command": "ls"}})
        with mock.patch.object(hook, "_PLUGINS_FILE", plugins_file):
            _run_main(hook, data, log)
        entries = _read_log(log)
        mcp_entries = [e for e in entries if "server" in e]
        assert len(mcp_entries) == 0

    def test_read_tool_no_mcp_entry_written(self, tmp_path):
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        plugins_file = tmp_path / "no-plugins.json"
        data = json.dumps({"tool_name": "Read", "session_id": "s1", "tool_input": {}})
        with mock.patch.object(hook, "_PLUGINS_FILE", plugins_file):
            _run_main(hook, data, log)
        entries = _read_log(log)
        mcp_entries = [e for e in entries if "server" in e]
        assert len(mcp_entries) == 0

    def test_empty_tool_name_only_session_start(self, tmp_path):
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        plugins_file = tmp_path / "no-plugins.json"
        data = json.dumps({"tool_name": "", "session_id": "s1", "tool_input": {}})
        with mock.patch.object(hook, "_PLUGINS_FILE", plugins_file):
            _run_main(hook, data, log)
        entries = _read_log(log)
        assert all(e.get("event") == "session_start" for e in entries)

    def test_missing_tool_name_only_session_start(self, tmp_path):
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        plugins_file = tmp_path / "no-plugins.json"
        data = json.dumps({"tool_input": {}, "session_id": "s1"})
        with mock.patch.object(hook, "_PLUGINS_FILE", plugins_file):
            _run_main(hook, data, log)
        entries = _read_log(log)
        assert all(e.get("event") == "session_start" for e in entries)
```

- [ ] **Step 7: Commit**

```bash
git add hooks/post_tool_use.py tests/test_hook.py
git commit -m "feat: add version capture and skill tracking to post_tool_use hook"
```

---

### Task 7: Create session-review command

**Files:**
- Create: `plugins/canopy/commands/session-review.md`

- [ ] **Step 1: Create the command file**

```markdown
---
description: Review recent sessions, detect stale skills, propose improvements with confidence scores. Use when asked to "review sessions", "session review", "what should I improve", or "analyze recent work".
argument-hint: [<count>|hours <N>|project <name>|auto-improve]
allowed-tools: [Read, Bash, Write, Edit, Glob, Grep, AskUserQuestion, Agent, Skill]
---

# Session Review

Batch-review recent sessions, detect stale skill versions, cross-reference prior
improvement attempts, and produce a ranked findings table with confidence scores.

## Arguments

- `<count>` (integer, default 10) — Number of sessions to review
- `hours <N>` — Time window instead of count
- `project <name>` — Filter to sessions from a specific project
- `auto-improve` — Automatically implement proposals with >= 70% confidence
- Arguments combine: `15 auto-improve`, `hours 48 project canopy`

## Examples

- `/canopy:session-review` — review last 10 sessions, present table
- `/canopy:session-review 20` — review last 20
- `/canopy:session-review hours 48` — last 48 hours
- `/canopy:session-review auto-improve` — review + auto-implement high-confidence proposals
- `/canopy:session-review 15 auto-improve` — 15 sessions + auto-implement

## Routing

All invocations route to the **session-review agent**. There is no skill-only mode —
session review always requires orchestration.

Read the agent definition and follow it:

```bash
python3 -c "import json; d=json.load(open('$HOME/.claude/plugins/installed_plugins.json')); print(d['plugins']['canopy@canopy'][0]['installPath'] + '/agents/session-review.md')"
```

Read that file with the Read tool and follow it. The agent handles:
- **Review mode** (default): Fetch → analyze → synthesize → present table → wait for user
- **Auto-improve mode**: Fetch → analyze → synthesize → implement >= 70% confidence proposals
```

- [ ] **Step 2: Commit**

```bash
git add plugins/canopy/commands/session-review.md
git commit -m "feat: add session-review command"
```

---

### Task 8: Create session-review agent

**Files:**
- Create: `plugins/canopy/agents/session-review.md`

- [ ] **Step 1: Create the agent file**

```markdown
---
name: session-review
description: >
  Review recent sessions, detect patterns and stale skills, propose improvements
  with confidence scores. Batch-analyzes sessions, cross-references prior work,
  and produces a ranked findings table. Auto-improve mode implements high-confidence
  proposals automatically.
model: inherit
memory: user
---

# Session Review Agent

You are a session review agent. Your job is to batch-review recent Claude Code
sessions, detect friction and stale skill versions, cross-reference prior
improvement attempts, and produce a ranked synthesis table with confidence scores.

## Your Memory

Your persistent memory at `~/.claude/agent-memory/session-review/` stores
cross-session knowledge:

- **Reviewed sessions** (`reviewed-sessions.md`): Session IDs already reviewed —
  avoids re-reviewing the same sessions across runs.
- **Priorities** (`priorities.md`): User's stated priorities and preferences
  (e.g., "I care more about MCP reliability than skill polish").
- **Proposal history** (`proposal-history.md`): What was proposed, accepted,
  rejected, and outcomes. Prevents re-proposing rejected items.

Read your MEMORY.md first. If it's empty, that's fine — you'll build it up.

## Arguments

Parse arguments from the command invocation:

- Number (e.g., `10`, `20`): session count (default: 10)
- `hours <N>`: time window instead of count
- `project <name>`: filter to a specific project
- `auto-improve`: enable auto-implementation of high-confidence proposals

## Pipeline

### Step 1: Load Context

1. Read memory files from `~/.claude/agent-memory/session-review/`
2. Read existing observations: `ls ~/.claude/canopy/observations/` — scan YAML files
   to understand what's already been observed
3. Read existing proposals: `ls ~/.claude/canopy/proposals/` — scan YAML files
   to know what's been attempted and their status (pending/implemented/failed)

### Step 2: Fetch Sessions

Run from the canopy repo working directory:

```bash
cd ~/emdash-projects/canopy && uv run canopy sessions list --json-output --hours <H>
```

Where `<H>` is calculated from the arguments:
- If count given: use `--hours 168` (1 week) and take the first N from the result
- If `hours <N>` given: use that directly

Parse the JSON output. Filter out any session IDs found in `reviewed-sessions.md`.
If no unreviewed sessions remain, tell the user and stop.

### Step 3: Analyze Individually

For each unreviewed session, run:

```bash
cd ~/emdash-projects/canopy && uv run canopy analyze <transcript_path> --propose
```

Collect the output. Each analysis produces observations (friction, gaps, issues)
with type, severity, description, and related servers, plus proposals.

Display progress: "Analyzing session N of M: <first message excerpt>..."

If analyzing many sessions, consider running up to 3 in parallel using the Agent
tool to dispatch analysis subagents.

### Step 4: Check Version Staleness

For each analyzed session, search `~/.claude/canopy/session-log.jsonl` for
entries matching that session's ID:

```bash
grep '"session_id": "<id>"' ~/.claude/canopy/session-log.jsonl | head -20
```

Look for:
- `session_start` events → extract `plugin_version`
- `skill_invoked` events → extract skill name + version
- Compare against current plugin version:

```bash
python3 -c "import json; d=json.load(open('$HOME/.claude/plugins/installed_plugins.json')); print(d['plugins']['canopy@canopy'][0].get('version', 'unknown'))"
```

**Staleness rules:**
- If session's `plugin_version` < current version → flag as **stale**
- If no version metadata → note "version unknown", skip staleness check
- Record which specific skills were invoked on the stale version

### Step 5: Cross-Reference Prior Work

For each observation from Step 3, check:

1. **Existing observations:** Does a matching observation already exist in
   `~/.claude/canopy/observations/`? Match by type + related_servers.
   If matched, note the frequency and when it was first seen.

2. **Existing proposals:** For matched observations, check proposals in
   `~/.claude/canopy/proposals/`:
   - `status: implemented` → Was the session before or after implementation?
     If before: friction expected (stale session). If after: fix didn't work.
   - `status: failed` → Note the failure reason. Lower confidence for retry.
   - `status: pending` → Already queued, don't duplicate.

3. **Agent memory:** Check `proposal-history.md` — was this previously surfaced
   and rejected by the user? Don't re-propose unless severity escalated.

### Step 6: Synthesize Table

Combine all findings into a ranked table. For each finding, determine a
confidence score based on:

**High (80-95%):**
- Clear root cause identified
- Similar fix succeeded before
- Fix is straightforward (config change, small code edit)
- Low complexity

**Medium (50-79%):**
- Root cause identified but fix is non-trivial
- No prior attempt data to calibrate against
- Requires changes in multiple files

**Low (20-49%):**
- Symptom observed but root cause unclear
- Prior fix for the same issue failed
- Requires changes outside our control

Present this table:

```
## Session Review Findings

| # | Finding | Sessions | Severity | Stale? | Proposed Fix | Confidence | Prior Attempts |
|---|---------|----------|----------|--------|-------------|------------|----------------|
| 1 | ... | 3,7,9 | high | Yes (v0.2.19) | ... | 85% | Partial fix v0.2.20 |
| 2 | ... | 2,5 | medium | No | ... | 60% | None |

## Recommended Next Steps

1. Start with #1 — high confidence, already partially fixed...
2. ...
```

If user priorities exist in memory, weight the ranking accordingly.

### Step 7: Record and Present

1. Save all reviewed session IDs to `reviewed-sessions.md` with date and project
2. **Review mode (default):**
   - Present the table using AskUserQuestion
   - Ask: "Which findings should I act on? (Enter numbers, 'all', or 'none')"
   - Record the user's decisions to `proposal-history.md`
   - If the user picks specific findings, suggest the right skill/command:
     - Code fixes → suggest `/canopy:select-session` or manual implementation
     - Skill improvements → suggest the specific skill file to edit
     - Infrastructure → suggest the module to modify

3. **Auto-improve mode:**
   - Present the table for visibility
   - Automatically implement all proposals with confidence >= 70%
   - For each implementation:
     - Create a branch: `canopy/session-review/<short-description>`
     - Implement the fix in the target repo
     - Run verification: lint + tests
     - Create a PR
     - Record outcome to `proposal-history.md`
   - Flag anything below 70% as "needs manual review"
   - Present summary: N implemented, M failed, K skipped (low confidence)
   - Never commit directly to main — always branches + PRs

## Rules

- Always read your agent memory before starting
- The canopy CLI does the actual analysis — you orchestrate and synthesize
- Never fabricate observations — only report what `canopy analyze` finds
- Treat all version metadata fields as optional — gracefully degrade
- Don't re-propose items the user previously rejected (check proposal-history.md)
- Save learnings to memory after every completed review cycle
- When presenting the table, include enough context for the user to make decisions
  without reading the raw analysis output
```

- [ ] **Step 2: Commit**

```bash
git add plugins/canopy/agents/session-review.md
git commit -m "feat: add session-review agent definition"
```

---

### Task 9: Update CLAUDE.md and bump version

**Files:**
- Modify: `.claude/CLAUDE.md`
- Modify: `VERSION`
- Modify: `plugins/canopy/.claude-plugin/plugin.json`

- [ ] **Step 1: Bump VERSION**

Change `VERSION` from `0.2.21` to `0.2.22`.

- [ ] **Step 2: Bump plugin.json version**

In `plugins/canopy/.claude-plugin/plugin.json`, change `"version": "0.2.21"` to `"version": "0.2.22"`.

- [ ] **Step 3: Update CLAUDE.md**

Add the session-review agent to the Key Modules / Plugin section. Under the existing agents line, add:

```markdown
- `plugins/canopy/agents/` — autonomous agents (pm-supervisor, session-review, walkthrough, website-builder)
```

Add paths module under Core pipeline section:

```markdown
- `src/orchestrator/paths.py` — shared CANOPY_DIR constant and legacy migration
```

Update any references to `~/.claude/orchestrator/` in CLAUDE.md to `~/.claude/canopy/`.

- [ ] **Step 4: Run full test suite**

Run: `uv run pytest -x -q`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add VERSION plugins/canopy/.claude-plugin/plugin.json .claude/CLAUDE.md
git commit -m "chore: bump version to 0.2.22, update CLAUDE.md for session-review agent"
```

---

### Task 10: Final verification

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest -v`
Expected: All tests PASS

- [ ] **Step 2: Verify no remaining orchestrator references in code**

```bash
grep -r "orchestrator" src/ hooks/ plugins/ --include="*.py" --include="*.md" -l
```

Expected: Only references should be in:
- `src/orchestrator/` directory names (package name, unchanged)
- Historical doc files (specs/plans)
- The `paths.py` `_LEGACY_DIR` constant
- `hooks/install.py` `"orchestrator" in hook.get("command", "")` detection string

No hardcoded `~/.claude/orchestrator/` paths should remain in active code.

- [ ] **Step 3: Verify plugin files are syntactically valid**

```bash
python3 -c "
import yaml
yaml.safe_load(open('plugins/canopy/agents/session-review.md').read().split('---')[1])
print('Agent frontmatter: OK')
"
```

- [ ] **Step 4: Commit any fixes if needed**

If any issues found in verification, fix and commit.
