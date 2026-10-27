"""Tests for the canopy repo-map SessionStart hook.

plugins/canopy/hooks/repo_map_capture.py maps a session's project dir to its
GitHub owner/repo in ~/.claude/canopy/repo-map.json. The repo-root
hooks/post_tool_use.py is the retired capture hook, kept as a no-op for
machines whose settings.json still registers it.
"""

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = ROOT / "plugins" / "canopy" / "hooks" / "repo_map_capture.py"
LEGACY_PATH = ROOT / "hooks" / "post_tool_use.py"


def _load_hook():
    """Import the hook module fresh each time (it lives outside the package)."""
    spec = importlib.util.spec_from_file_location("repo_map_capture", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git_repo(path: Path, remote: str | None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if remote:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote], check=True)
    return path


def _run_main(hook, stdin_data: str, repo_map: Path, env: dict | None = None):
    with (
        mock.patch.object(hook, "REPO_MAP_FILE", repo_map),
        mock.patch("sys.stdin", io.StringIO(stdin_data)),
        mock.patch.dict("os.environ", env or {}, clear=False),
    ):
        hook.main()


def _read(repo_map: Path) -> dict:
    return json.loads(repo_map.read_text()) if repo_map.exists() else {}


class TestProjectKey:
    def test_matches_claude_transcript_dir_naming(self):
        assert _load_hook().project_key("/Users/jon/code/ace") == "-Users-jon-code-ace"


class TestCapture:
    def test_maps_cwd_from_hook_input(self, tmp_path):
        hook = _load_hook()
        repo = _git_repo(tmp_path / "proj", "git@github.com:jjackson/canopy.git")
        repo_map = tmp_path / "canopy" / "repo-map.json"
        _run_main(hook, json.dumps({"cwd": str(repo), "hook_event_name": "SessionStart"}), repo_map)
        assert _read(repo_map) == {hook.project_key(str(repo)): "jjackson/canopy"}

    def test_https_remote(self, tmp_path):
        hook = _load_hook()
        repo = _git_repo(tmp_path / "proj", "https://github.com/dimagi/ace-web.git")
        repo_map = tmp_path / "repo-map.json"
        _run_main(hook, json.dumps({"cwd": str(repo)}), repo_map)
        assert _read(repo_map)[hook.project_key(str(repo))] == "dimagi/ace-web"

    def test_falls_back_to_claude_project_dir(self, tmp_path):
        hook = _load_hook()
        repo = _git_repo(tmp_path / "proj", "https://github.com/jjackson/ace")
        repo_map = tmp_path / "repo-map.json"
        _run_main(hook, "{}", repo_map, env={"CLAUDE_PROJECT_DIR": str(repo)})
        assert _read(repo_map)[hook.project_key(str(repo))] == "jjackson/ace"

    def test_cwd_wins_over_claude_project_dir(self, tmp_path):
        hook = _load_hook()
        a = _git_repo(tmp_path / "a", "https://github.com/o/a")
        b = _git_repo(tmp_path / "b", "https://github.com/o/b")
        repo_map = tmp_path / "repo-map.json"
        _run_main(hook, json.dumps({"cwd": str(a)}), repo_map, env={"CLAUDE_PROJECT_DIR": str(b)})
        assert _read(repo_map) == {hook.project_key(str(a)): "o/a"}

    def test_keeps_existing_entries(self, tmp_path):
        hook = _load_hook()
        repo = _git_repo(tmp_path / "proj", "https://github.com/o/new")
        repo_map = tmp_path / "repo-map.json"
        repo_map.write_text(json.dumps({"-other": "o/other"}))
        _run_main(hook, json.dumps({"cwd": str(repo)}), repo_map)
        assert _read(repo_map) == {"-other": "o/other", hook.project_key(str(repo)): "o/new"}

    def test_already_mapped_skips_git(self, tmp_path):
        hook = _load_hook()
        repo_map = tmp_path / "repo-map.json"
        key = hook.project_key("/some/dir")
        repo_map.write_text(json.dumps({key: "o/kept"}))
        with mock.patch.object(hook.subprocess, "run") as run:
            _run_main(hook, json.dumps({"cwd": "/some/dir"}), repo_map)
        run.assert_not_called()
        assert _read(repo_map) == {key: "o/kept"}

    def test_corrupt_map_is_replaced(self, tmp_path):
        hook = _load_hook()
        repo = _git_repo(tmp_path / "proj", "https://github.com/o/r")
        repo_map = tmp_path / "repo-map.json"
        repo_map.write_text("{not json")
        _run_main(hook, json.dumps({"cwd": str(repo)}), repo_map)
        assert _read(repo_map) == {hook.project_key(str(repo)): "o/r"}


class TestNothingToMap:
    def test_no_origin_writes_nothing(self, tmp_path):
        hook = _load_hook()
        repo = _git_repo(tmp_path / "proj", None)
        repo_map = tmp_path / "repo-map.json"
        _run_main(hook, json.dumps({"cwd": str(repo)}), repo_map)
        assert not repo_map.exists()

    def test_non_github_remote_writes_nothing(self, tmp_path):
        hook = _load_hook()
        repo = _git_repo(tmp_path / "proj", "https://gitlab.com/o/r.git")
        repo_map = tmp_path / "repo-map.json"
        _run_main(hook, json.dumps({"cwd": str(repo)}), repo_map)
        assert not repo_map.exists()

    def test_not_a_git_dir_writes_nothing(self, tmp_path):
        hook = _load_hook()
        repo_map = tmp_path / "repo-map.json"
        _run_main(hook, json.dumps({"cwd": str(tmp_path)}), repo_map,
                  env={"GIT_CEILING_DIRECTORIES": str(tmp_path.parent)})
        assert not repo_map.exists()

    def test_no_project_dir_writes_nothing(self, tmp_path, monkeypatch):
        hook = _load_hook()
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        repo_map = tmp_path / "repo-map.json"
        _run_main(hook, "{}", repo_map)
        assert not repo_map.exists()

    def test_git_timeout_is_swallowed(self, tmp_path):
        hook = _load_hook()
        repo_map = tmp_path / "repo-map.json"
        with mock.patch.object(hook.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("git", 2)):
            _run_main(hook, json.dumps({"cwd": str(tmp_path)}), repo_map)
        assert not repo_map.exists()


class TestFailOpen:
    """Run as Claude Code runs it: a subprocess that must exit 0 on every input."""

    def _run(self, stdin: str, home: Path, extra_env: dict | None = None):
        env = {**os.environ, "HOME": str(home), **(extra_env or {})}
        env.pop("CLAUDE_PROJECT_DIR", None)
        return subprocess.run([sys.executable, str(HOOK_PATH)], input=stdin, env=env,
                              capture_output=True, text=True, timeout=20)

    def test_malformed_json_exits_zero(self, tmp_path):
        r = self._run("not json", tmp_path)
        assert r.returncode == 0 and r.stdout == "" and r.stderr == ""

    def test_empty_stdin_exits_zero(self, tmp_path):
        assert self._run("", tmp_path).returncode == 0

    def test_non_object_json_exits_zero(self, tmp_path):
        assert self._run("null", tmp_path).returncode == 0

    def test_writes_under_home(self, tmp_path):
        repo = _git_repo(tmp_path / "proj", "https://github.com/o/r")
        r = self._run(json.dumps({"cwd": str(repo)}), tmp_path / "home")
        assert r.returncode == 0
        written = tmp_path / "home" / ".claude" / "canopy" / "repo-map.json"
        assert json.loads(written.read_text()) == {
            "-" + str(repo).lstrip("/").replace("/", "-"): "o/r"
        }


class TestRegistration:
    def test_registered_on_session_start(self):
        hooks = json.loads((HOOK_PATH.parent / "hooks.json").read_text())["hooks"]
        cmds = [h["command"] for m in hooks["SessionStart"] for h in m["hooks"]]
        assert any("repo_map_capture.py" in c for c in cmds)

    def test_no_post_tool_use_capture(self):
        """The retired capture hook stays retired. PostToolUse itself is used again, by
        credential_guard (a tool failing on credentials aborts the turn)."""
        hooks = json.loads((HOOK_PATH.parent / "hooks.json").read_text())["hooks"]
        cmds = [h["command"] for event in ("PostToolUse", "PostToolUseFailure")
                for m in hooks.get(event, []) for h in m["hooks"]]
        assert not any("post_tool_use" in c for c in cmds)
        assert all("credential_guard.py" in c for c in cmds)


class TestRetiredLegacyHook:
    def test_is_a_no_op_that_exits_zero(self, tmp_path):
        env = {**os.environ, "HOME": str(tmp_path)}
        r = subprocess.run(
            [sys.executable, str(LEGACY_PATH)],
            input=json.dumps({"tool_name": "mcp__x__y", "session_id": "s"}),
            env=env, capture_output=True, text=True, timeout=20,
        )
        assert r.returncode == 0 and r.stdout == ""
        assert not (tmp_path / ".claude").exists()
