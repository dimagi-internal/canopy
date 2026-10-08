"""Tests for hooks/pre_tool_use_version_bump_guard.py.

The guard must:
  - allow anything that isn't a real `git push`,
  - allow `git push` when verify-bump passes or skips,
  - block `git push` when a plugins/canopy/ change has no VERSION bump,
  - honour CANOPY_ALLOW_PUSH_NO_BUMP=1 as an override,
  - fail open (never wedge the push) when the checker can't be loaded or raises.

evaluate() is exercised with a stubbed `_load_version_bump` so these stay fast
and deterministic — the real git-diff behavior is covered by
tests/test_version_bump_check.py.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

HOOK_PATH = Path(__file__).resolve().parents[2] / "hooks" / "pre_tool_use_version_bump_guard.py"


def _load_hook():
    spec = importlib.util.spec_from_file_location(
        "pre_tool_use_version_bump_guard", HOOK_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def hook():
    return _load_hook()


def _push_payload(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


class _FakeVB:
    """Stand-in for the version_bump module with a canned verify result."""

    def __init__(self, result=None, raises=False):
        self._result = result
        self._raises = raises

    def verify_bump_when_plugin_changed(self, repo_root):
        if self._raises:
            raise RuntimeError("boom")
        return self._result


def _stub_loader(monkeypatch, hook, fake):
    monkeypatch.setattr(hook, "_load_version_bump", lambda repo_root: fake)


# ---------------------------------------------------------------------------
# _is_git_push regex
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command,expected",
    [
        ("git push", True),
        ("git push -u origin b", True),
        ("git -C /x push", True),
        ("cd /repo && git push origin HEAD", True),
        ("git push --dry-run", False),
        ("git push --help", False),
        ("git status", False),
        ("gitpush", False),
        ("git pushup", False),
        ("", False),
    ],
)
def test_is_git_push(hook, command, expected):
    assert hook._is_git_push(command) is expected


# ---------------------------------------------------------------------------
# evaluate(): routing
# ---------------------------------------------------------------------------


def test_non_bash_tool_allowed(hook):
    action, _ = hook.evaluate({"tool_name": "Edit", "tool_input": {"file_path": "/x"}})
    assert action == "allow"


def test_non_push_command_allowed(hook):
    action, _ = hook.evaluate(_push_payload("git status"))
    assert action == "allow"


def test_dry_run_push_allowed(hook):
    action, _ = hook.evaluate(_push_payload("git push --dry-run origin x"))
    assert action == "allow"


def test_push_allowed_when_verify_ok(hook, monkeypatch):
    _stub_loader(monkeypatch, hook, _FakeVB({"ok": True, "skipped": False}))
    action, _ = hook.evaluate(_push_payload("git push origin b"))
    assert action == "allow"


def test_push_allowed_when_verify_skipped(hook, monkeypatch):
    _stub_loader(monkeypatch, hook, _FakeVB({"ok": False, "skipped": True}))
    action, _ = hook.evaluate(_push_payload("git push origin b"))
    assert action == "allow"


def test_push_blocked_when_unbumped(hook, monkeypatch):
    monkeypatch.delenv("CANOPY_ALLOW_PUSH_NO_BUMP", raising=False)
    info = {
        "ok": False,
        "skipped": False,
        "reason": "1 plugins/canopy/ file(s) changed but VERSION did not advance",
        "plugin_files_changed": ["plugins/canopy/skills/foo.md"],
        "local_version": "0.2.10",
        "main_version": "0.2.10",
    }
    _stub_loader(monkeypatch, hook, _FakeVB(info))
    action, detail = hook.evaluate(_push_payload("git push -u origin feat/x"))
    assert action == "block"
    assert detail is info


def test_override_env_allows_unbumped(hook, monkeypatch):
    monkeypatch.setenv("CANOPY_ALLOW_PUSH_NO_BUMP", "1")
    info = {"ok": False, "skipped": False, "reason": "nope", "plugin_files_changed": []}
    _stub_loader(monkeypatch, hook, _FakeVB(info))
    action, detail = hook.evaluate(_push_payload("git push origin feat/x"))
    assert action == "override"
    assert detail is info


def test_fail_open_when_verify_raises(hook, monkeypatch):
    monkeypatch.delenv("CANOPY_ALLOW_PUSH_NO_BUMP", raising=False)
    _stub_loader(monkeypatch, hook, _FakeVB(raises=True))
    action, _ = hook.evaluate(_push_payload("git push origin b"))
    assert action == "allow"


def test_fail_open_when_module_missing(hook, monkeypatch):
    monkeypatch.setattr(hook, "_load_version_bump", lambda repo_root: None)
    action, _ = hook.evaluate(_push_payload("git push origin b"))
    assert action == "allow"


def test_block_message_contains_fix(hook):
    info = {
        "ok": False,
        "reason": "VERSION did not advance",
        "plugin_files_changed": ["plugins/canopy/skills/foo.md"],
        "local_version": "0.2.10",
        "main_version": "0.2.10",
    }
    msg = hook._build_block_message(info)
    assert "canopy version bump" in msg
    assert "plugins/canopy/skills/foo.md" in msg
    assert "0.2.10" in msg


# --- the pushed repo, not the session's project --------------------------------

def _git_repo(path: Path) -> Path:
    import subprocess

    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def test_pushed_repo_follows_git_dash_c_and_leading_cd(hook, tmp_path, monkeypatch):
    other = _git_repo(tmp_path / "canopy-web")
    session = _git_repo(tmp_path / "canopy")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(session))
    resolved = other.resolve()
    assert hook._pushed_repo(f"git -C {other} push -u origin b", {}).resolve() == resolved
    assert hook._pushed_repo(f'cd "{other}" && git push', {}).resolve() == resolved
    assert hook._pushed_repo("git push", {"cwd": str(other)}).resolve() == resolved
    assert hook._pushed_repo("git push", {}).resolve() == session.resolve()


def test_push_of_a_non_canopy_repo_is_never_judged_by_the_session_branch(
    hook, tmp_path, monkeypatch
):
    """A canopy session pushing canopy-web was blocked for the CANOPY worktree's
    un-bumped plugin change. The real loader finds no version_bump.py in the
    pushed repo → allow."""
    other = _git_repo(tmp_path / "canopy-web")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(Path(__file__).resolve().parents[2]))
    action, _ = hook.evaluate(_push_payload(f"git -C {other} push -u origin fix/x"))
    assert action == "allow"
