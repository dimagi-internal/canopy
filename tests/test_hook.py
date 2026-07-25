"""Tests for the canopy PostToolUse capture hook.

The canonical implementation is the PLUGIN-MANAGED copy at
plugins/canopy/hooks/post_tool_use.py (registered via hooks.json /
${CLAUDE_PLUGIN_ROOT}); hooks/post_tool_use.py at the repo root is a
legacy-compat shim that forwards to it.
"""

import importlib
import io
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

HOOK_PATH = (
    Path(__file__).parent.parent / "plugins" / "canopy" / "hooks" / "post_tool_use.py"
)


def _load_hook():
    """Import the hook module fresh each time (it lives outside the package)."""
    spec = importlib.util.spec_from_file_location("post_tool_use", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_main(hook_module, stdin_data: str, log_file: Path, env: dict | None = None):
    """Run hook_module.main() with mocked stdin and LOG_FILE.

    CANOPY_CAPTURE_LEGACY_SHIM=1 bypasses the defer-to-legacy-registration
    check so tests exercise capture regardless of this machine's
    ~/.claude/settings.json (individual tests may override it).
    """
    merged_env = {"CANOPY_CAPTURE_LEGACY_SHIM": "1", **(env or {})}
    with (
        mock.patch.object(hook_module, "LOG_FILE", log_file),
        mock.patch("sys.stdin", io.StringIO(stdin_data)),
        mock.patch.dict("os.environ", merged_env, clear=False),
    ):
        hook_module.main()


def _read_log(log_file: Path) -> list[dict]:
    if not log_file.exists():
        return []
    lines = [l.strip() for l in log_file.read_text().splitlines() if l.strip()]
    return [json.loads(l) for l in lines]


# ---------------------------------------------------------------------------
# Basic MCP call handling
# ---------------------------------------------------------------------------


class TestMainMcpToolCall:
    def test_writes_entry_for_valid_mcp_call(self, tmp_path):
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        plugins_file = tmp_path / "no-plugins.json"
        data = json.dumps({
            "tool_name": "mcp__my-server__my_tool",
            "session_id": "sess-1",
            "tool_input": {"param": "value"},
        })
        with mock.patch.object(hook, "_PLUGINS_FILE", plugins_file):
            _run_main(hook, data, log)
        entries = _read_log(log)
        # session_start + mcp entry = 2
        assert len(entries) == 2

    def test_entry_has_correct_server(self, tmp_path):
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        plugins_file = tmp_path / "no-plugins.json"
        data = json.dumps({
            "tool_name": "mcp__my-server__my_tool",
            "session_id": "sess-1",
            "tool_input": {},
        })
        with mock.patch.object(hook, "_PLUGINS_FILE", plugins_file):
            _run_main(hook, data, log)
        mcp_entry = [e for e in _read_log(log) if "server" in e][0]
        assert mcp_entry["server"] == "my-server"

    def test_entry_has_correct_tool(self, tmp_path):
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        plugins_file = tmp_path / "no-plugins.json"
        data = json.dumps({
            "tool_name": "mcp__my-server__my_tool",
            "session_id": "sess-1",
            "tool_input": {},
        })
        with mock.patch.object(hook, "_PLUGINS_FILE", plugins_file):
            _run_main(hook, data, log)
        mcp_entry = [e for e in _read_log(log) if "server" in e][0]
        assert mcp_entry["tool"] == "my_tool"

    def test_entry_has_session_id(self, tmp_path):
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        plugins_file = tmp_path / "no-plugins.json"
        data = json.dumps({
            "tool_name": "mcp__srv__tool",
            "session_id": "my-session-abc",
            "tool_input": {},
        })
        with mock.patch.object(hook, "_PLUGINS_FILE", plugins_file):
            _run_main(hook, data, log)
        mcp_entry = [e for e in _read_log(log) if "server" in e][0]
        assert mcp_entry["session_id"] == "my-session-abc"

    def test_entry_has_ts_field(self, tmp_path):
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        plugins_file = tmp_path / "no-plugins.json"
        data = json.dumps({"tool_name": "mcp__srv__tool", "tool_input": {}})
        with mock.patch.object(hook, "_PLUGINS_FILE", plugins_file):
            _run_main(hook, data, log)
        mcp_entry = [e for e in _read_log(log) if "server" in e][0]
        assert "ts" in mcp_entry
        assert isinstance(mcp_entry["ts"], str)

    def test_success_true_when_no_error(self, tmp_path):
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        plugins_file = tmp_path / "no-plugins.json"
        data = json.dumps({"tool_name": "mcp__srv__tool", "tool_input": {}})
        with mock.patch.object(hook, "_PLUGINS_FILE", plugins_file):
            _run_main(hook, data, log)
        mcp_entry = [e for e in _read_log(log) if "server" in e][0]
        assert mcp_entry["success"] is True

    def test_success_false_when_tool_error(self, tmp_path):
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        plugins_file = tmp_path / "no-plugins.json"
        data = json.dumps({
            "tool_name": "mcp__srv__tool",
            "tool_input": {},
            "tool_error": "something went wrong",
        })
        with mock.patch.object(hook, "_PLUGINS_FILE", plugins_file):
            _run_main(hook, data, log)
        mcp_entry = [e for e in _read_log(log) if "server" in e][0]
        assert mcp_entry["success"] is False

    def test_project_from_env(self, tmp_path):
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        plugins_file = tmp_path / "no-plugins.json"
        data = json.dumps({"tool_name": "mcp__srv__tool", "tool_input": {}})
        with mock.patch.object(hook, "_PLUGINS_FILE", plugins_file):
            _run_main(hook, data, log, env={"CLAUDE_PROJECT_DIR": "/my/project"})
        mcp_entry = [e for e in _read_log(log) if "server" in e][0]
        assert mcp_entry["project"] == "/my/project"

    def test_project_unknown_when_env_not_set(self, tmp_path):
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        plugins_file = tmp_path / "no-plugins.json"
        data = json.dumps({"tool_name": "mcp__srv__tool", "tool_input": {}})
        env_without_project = {k: v for k, v in mock.patch.dict("os.environ", {}).start().items()
                               if k != "CLAUDE_PROJECT_DIR"}
        with (
            mock.patch.object(hook, "LOG_FILE", log),
            mock.patch.object(hook, "_PLUGINS_FILE", plugins_file),
            mock.patch("sys.stdin", io.StringIO(data)),
            mock.patch.dict(
                "os.environ", {"CANOPY_CAPTURE_LEGACY_SHIM": "1"}, clear=True
            ),
        ):
            hook.main()
        mcp_entry = [e for e in _read_log(log) if "server" in e][0]
        assert mcp_entry["project"] == "unknown"


# ---------------------------------------------------------------------------
# Non-MCP tool call is ignored
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Tool name splitting: mcp__server__tool
# ---------------------------------------------------------------------------


class TestToolNameSplitting:
    def test_splits_server_and_tool_correctly(self, tmp_path):
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        plugins_file = tmp_path / "no-plugins.json"
        data = json.dumps({
            "tool_name": "mcp__commcare-hq__get_app_structure",
            "tool_input": {},
        })
        with mock.patch.object(hook, "_PLUGINS_FILE", plugins_file):
            _run_main(hook, data, log)
        mcp_entry = [e for e in _read_log(log) if "server" in e][0]
        assert mcp_entry["server"] == "commcare-hq"
        assert mcp_entry["tool"] == "get_app_structure"

    def test_malformed_mcp_only_two_parts_not_written(self, tmp_path):
        """mcp__server with no tool part should be ignored."""
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        plugins_file = tmp_path / "no-plugins.json"
        data = json.dumps({"tool_name": "mcp__server", "tool_input": {}})
        with mock.patch.object(hook, "_PLUGINS_FILE", plugins_file):
            _run_main(hook, data, log)
        mcp_entries = [e for e in _read_log(log) if "server" in e]
        assert len(mcp_entries) == 0

    def test_tool_name_with_extra_underscores_preserved(self, tmp_path):
        """mcp__server__tool__extra splits at first two __ only."""
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        plugins_file = tmp_path / "no-plugins.json"
        data = json.dumps({
            "tool_name": "mcp__my-server__my_tool__extra",
            "tool_input": {},
        })
        with mock.patch.object(hook, "_PLUGINS_FILE", plugins_file):
            _run_main(hook, data, log)
        mcp_entry = [e for e in _read_log(log) if "server" in e][0]
        assert mcp_entry["server"] == "my-server"
        assert mcp_entry["tool"] == "my_tool__extra"


# ---------------------------------------------------------------------------
# Malformed JSON input
# ---------------------------------------------------------------------------


class TestMalformedJsonInput:
    def test_plain_text_does_not_crash(self, tmp_path):
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        _run_main(hook, "this is not json", log)
        assert not log.exists()

    def test_empty_string_does_not_crash(self, tmp_path):
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        _run_main(hook, "", log)
        assert not log.exists()

    def test_partial_json_does_not_crash(self, tmp_path):
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        _run_main(hook, '{"tool_name": "mcp__s__t"', log)
        assert not log.exists()

    def test_null_json_no_log_written(self, tmp_path):
        """JSON 'null' parses successfully but main() raises AttributeError on .get().
        The outer __main__ guard catches it; calling main() directly surfaces the error.
        Either way, no JSONL entry should be written."""
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        try:
            _run_main(hook, "null", log)
        except AttributeError:
            pass  # expected — main() has no guard for non-dict JSON
        assert not log.exists()


# ---------------------------------------------------------------------------
# Long string truncation
# ---------------------------------------------------------------------------


class TestLongStringTruncation:
    def test_long_value_truncated_to_100_chars(self, tmp_path):
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        plugins_file = tmp_path / "no-plugins.json"
        long_str = "x" * 200
        data = json.dumps({
            "tool_name": "mcp__srv__tool",
            "tool_input": {"query": long_str},
        })
        with mock.patch.object(hook, "_PLUGINS_FILE", plugins_file):
            _run_main(hook, data, log)
        mcp_entry = [e for e in _read_log(log) if "server" in e][0]
        assert len(mcp_entry["input_summary"]["query"]) == 103  # 100 + "..."

    def test_long_value_ends_with_ellipsis(self, tmp_path):
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        plugins_file = tmp_path / "no-plugins.json"
        data = json.dumps({
            "tool_name": "mcp__srv__tool",
            "tool_input": {"query": "a" * 200},
        })
        with mock.patch.object(hook, "_PLUGINS_FILE", plugins_file):
            _run_main(hook, data, log)
        mcp_entry = [e for e in _read_log(log) if "server" in e][0]
        assert mcp_entry["input_summary"]["query"].endswith("...")

    def test_short_value_not_truncated(self, tmp_path):
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        plugins_file = tmp_path / "no-plugins.json"
        data = json.dumps({
            "tool_name": "mcp__srv__tool",
            "tool_input": {"query": "short"},
        })
        with mock.patch.object(hook, "_PLUGINS_FILE", plugins_file):
            _run_main(hook, data, log)
        mcp_entry = [e for e in _read_log(log) if "server" in e][0]
        assert mcp_entry["input_summary"]["query"] == "short"

    def test_exactly_100_chars_not_truncated(self, tmp_path):
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        plugins_file = tmp_path / "no-plugins.json"
        exact = "y" * 100
        data = json.dumps({
            "tool_name": "mcp__srv__tool",
            "tool_input": {"query": exact},
        })
        with mock.patch.object(hook, "_PLUGINS_FILE", plugins_file):
            _run_main(hook, data, log)
        mcp_entry = [e for e in _read_log(log) if "server" in e][0]
        assert mcp_entry["input_summary"]["query"] == exact

    def test_non_string_value_not_truncated(self, tmp_path):
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        plugins_file = tmp_path / "no-plugins.json"
        data = json.dumps({
            "tool_name": "mcp__srv__tool",
            "tool_input": {"count": 42, "flag": True},
        })
        with mock.patch.object(hook, "_PLUGINS_FILE", plugins_file):
            _run_main(hook, data, log)
        mcp_entry = [e for e in _read_log(log) if "server" in e][0]
        assert mcp_entry["input_summary"]["count"] == 42
        assert mcp_entry["input_summary"]["flag"] is True


# ---------------------------------------------------------------------------
# JSONL output correctness
# ---------------------------------------------------------------------------


class TestJsonlOutput:
    def test_output_is_valid_json(self, tmp_path):
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        plugins_file = tmp_path / "no-plugins.json"
        data = json.dumps({"tool_name": "mcp__srv__tool", "tool_input": {}})
        with mock.patch.object(hook, "_PLUGINS_FILE", plugins_file):
            _run_main(hook, data, log)
        line = log.read_text().strip().splitlines()[-1]
        parsed = json.loads(line)
        assert isinstance(parsed, dict)

    def test_multiple_calls_produce_multiple_lines(self, tmp_path):
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        plugins_file = tmp_path / "no-plugins.json"
        for i in range(3):
            data = json.dumps({
                "tool_name": f"mcp__srv__tool{i}",
                "session_id": f"sess-{i}",
                "tool_input": {},
            })
            with mock.patch.object(hook, "_PLUGINS_FILE", plugins_file):
                _run_main(hook, data, log)
        entries = _read_log(log)
        mcp_entries = [e for e in entries if "server" in e]
        assert len(mcp_entries) == 3

    def test_entry_keys_present(self, tmp_path):
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        plugins_file = tmp_path / "no-plugins.json"
        data = json.dumps({"tool_name": "mcp__srv__tool", "tool_input": {}})
        with mock.patch.object(hook, "_PLUGINS_FILE", plugins_file):
            _run_main(hook, data, log)
        mcp_entry = [e for e in _read_log(log) if "server" in e][0]
        for key in ("ts", "session_id", "project", "server", "tool", "input_summary", "success"):
            assert key in mcp_entry, f"Missing key: {key}"

    def test_log_file_parent_dirs_created(self, tmp_path):
        hook = _load_hook()
        log = tmp_path / "deep" / "nested" / "dir" / "session-log.jsonl"
        plugins_file = tmp_path / "no-plugins.json"
        data = json.dumps({"tool_name": "mcp__srv__tool", "tool_input": {}})
        with mock.patch.object(hook, "_PLUGINS_FILE", plugins_file):
            _run_main(hook, data, log)
        assert log.exists()


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


# ---------------------------------------------------------------------------
# Defer-to-legacy-registration guard
# ---------------------------------------------------------------------------


class TestLegacyDefer:
    def _settings_with_legacy(self, tmp_path, hook_script: Path | None):
        """Write a settings.json carrying a legacy PostToolUse registration."""
        cmd_path = hook_script if hook_script else tmp_path / "gone" / "post_tool_use.py"
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({
            "hooks": {
                "PostToolUse": [
                    {"hooks": [{"command": f"python3 {cmd_path}"}]}
                ]
            }
        }))
        return settings

    def test_defers_when_legacy_entry_points_at_existing_file(self, tmp_path):
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        legacy_script = tmp_path / "post_tool_use.py"
        legacy_script.write_text("# legacy")
        settings = self._settings_with_legacy(tmp_path, legacy_script)
        data = json.dumps({
            "tool_name": "mcp__srv__tool",
            "session_id": "s1",
            "tool_input": {},
        })
        fake_home = tmp_path
        (fake_home / ".claude").mkdir()
        (fake_home / ".claude" / "settings.json").write_text(settings.read_text())
        with (
            mock.patch.object(hook, "LOG_FILE", log),
            mock.patch("sys.stdin", io.StringIO(data)),
            mock.patch.dict("os.environ", {"CANOPY_CAPTURE_LEGACY_SHIM": ""}, clear=False),
            mock.patch.object(hook.Path, "home", staticmethod(lambda: fake_home)),
        ):
            hook.main()
        assert _read_log(log) == []

    def test_captures_when_legacy_entry_is_stale(self, tmp_path):
        """A legacy entry whose script file was deleted must NOT block capture."""
        hook = _load_hook()
        log = tmp_path / "session-log.jsonl"
        settings_content = self._settings_with_legacy(tmp_path, None).read_text()
        fake_home = tmp_path
        (fake_home / ".claude").mkdir()
        (fake_home / ".claude" / "settings.json").write_text(settings_content)
        plugins = tmp_path / "no-plugins.json"
        data = json.dumps({
            "tool_name": "mcp__srv__tool",
            "session_id": "s1",
            "tool_input": {},
        })
        with (
            mock.patch.object(hook, "LOG_FILE", log),
            mock.patch.object(hook, "_PLUGINS_FILE", plugins),
            mock.patch("sys.stdin", io.StringIO(data)),
            mock.patch.dict("os.environ", {"CANOPY_CAPTURE_LEGACY_SHIM": ""}, clear=False),
            mock.patch.object(hook.Path, "home", staticmethod(lambda: fake_home)),
        ):
            hook.main()
        entries = _read_log(log)
        assert any(e.get("tool") == "tool" for e in entries)
