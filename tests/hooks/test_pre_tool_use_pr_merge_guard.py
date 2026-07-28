"""Tests for hooks/pre_tool_use_pr_merge_guard.py.

The sibling push guard re-checks the version bump at `git push`. That leaves a
window: CI checks the branch against main *as it was when CI ran*, and GitHub
does not re-run it when the base moves. On 2026-07-28 two PRs opened from the
same base both bumped to v0.2.369; #423 merged, then #429 merged forty seconds
later — both claiming the same version. The plugin cache is keyed by version, so
the second merge landed in a dir that already existed with the first one's code,
and `/canopy:update` reported UP_TO_DATE.

This guard closes that window by re-running the same check at `gh pr merge`,
against a freshly fetched origin/main.

It must:
  - allow anything that isn't a real `gh pr merge`,
  - block a merge whose VERSION no longer advances past main,
  - only judge the branch it can actually reason about (the PR being merged must
    be the checked-out branch's PR — otherwise fail open),
  - honour CANOPY_ALLOW_MERGE_NO_BUMP=1,
  - fail open whenever anything is unknown or raises.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

HOOK_PATH = (Path(__file__).resolve().parents[2] / "hooks"
             / "pre_tool_use_pr_merge_guard.py")


def _load_hook():
    spec = importlib.util.spec_from_file_location("pre_tool_use_pr_merge_guard", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def hook():
    return _load_hook()


def _payload(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


COLLIDED = {
    "ok": False,
    "skipped": False,
    "reason": "VERSION (0.2.369) did not advance beyond `origin/main` (0.2.369).",
    "plugin_files_changed": ["plugins/canopy/.claude-plugin/plugin.json"],
    "local_version": "0.2.369",
    "main_version": "0.2.369",
}
CLEAN = {"ok": True, "skipped": False, "reason": "VERSION advanced 0.2.368 → 0.2.369."}


class _FakeVB:
    def __init__(self, result=None, raises=False):
        self._result, self._raises = result, raises

    def verify_bump_when_plugin_changed(self, repo_root):
        if self._raises:
            raise RuntimeError("boom")
        return self._result


def _stub(monkeypatch, hook, result=None, *, raises=False, branch="feat-x", pr_number=429):
    monkeypatch.setattr(hook, "_load_version_bump",
                        lambda repo_root: _FakeVB(result, raises))
    monkeypatch.setattr(hook, "_current_branch", lambda repo_root: branch)
    monkeypatch.setattr(hook, "_current_branch_pr", lambda repo_root: pr_number)


# ── what counts as a merge ──────────────────────────────────────────────────

@pytest.mark.parametrize("command,expected", [
    ("gh pr merge 429 --merge", True),
    ("gh pr merge --merge", True),
    ("gh pr merge --squash --delete-branch", True),
    ("gh pr create --title x", False),
    ("gh pr checks 429", False),
    ("gh pr view 429 --json state", False),
    ("git merge main", False),
    ("echo 'gh pr merge' >> notes.md", False),
    ("gh pr checks 429 && gh pr merge 429 --merge", True),
])
def test_recognises_a_real_merge(hook, command, expected):
    assert hook._is_pr_merge(command) is expected


def test_non_bash_tool_allowed(hook):
    assert hook.evaluate({"tool_name": "Read", "tool_input": {}})[0] == "allow"


# ── the collision ───────────────────────────────────────────────────────────

def test_blocks_a_merge_whose_version_no_longer_advances(hook, monkeypatch):
    """The exact 2026-07-28 shape: another PR took the number after CI passed."""
    _stub(monkeypatch, hook, COLLIDED)
    action, info = hook.evaluate(_payload("gh pr merge 429 --merge"))
    assert action == "block"
    assert info["main_version"] == "0.2.369"


def test_allows_a_merge_that_still_advances(hook, monkeypatch):
    _stub(monkeypatch, hook, CLEAN)
    assert hook.evaluate(_payload("gh pr merge 429 --merge"))[0] == "allow"


def test_block_message_names_the_recovery(hook):
    msg = hook._build_block_message(COLLIDED)
    assert "canopy version bump" in msg
    assert "0.2.369" in msg
    assert "gh pr checks" in msg, "must say to re-wait for CI before re-merging"


def test_no_pr_argument_means_the_current_branch(hook, monkeypatch):
    """`gh pr merge` with no target merges the checked-out branch's PR, so the
    local working tree is exactly what should be judged — no lookup needed."""
    _stub(monkeypatch, hook, COLLIDED, pr_number=None)
    assert hook.evaluate(_payload("gh pr merge --merge"))[0] == "block"


def test_a_pr_that_is_not_this_branch_is_left_alone(hook, monkeypatch):
    """Merging someone else's PR from this worktree says nothing about the local
    VERSION, so judging it would be a false positive."""
    _stub(monkeypatch, hook, COLLIDED, pr_number=429)
    assert hook.evaluate(_payload("gh pr merge 999 --merge"))[0] == "allow"


def test_branch_name_argument_matching_the_checkout_is_judged(hook, monkeypatch):
    _stub(monkeypatch, hook, COLLIDED, branch="feat-x")
    assert hook.evaluate(_payload("gh pr merge feat-x --merge"))[0] == "block"


def test_a_pr_url_for_this_branch_is_judged(hook, monkeypatch):
    _stub(monkeypatch, hook, COLLIDED, pr_number=429)
    cmd = "gh pr merge https://github.com/dimagi-internal/canopy/pull/429 --merge"
    assert hook.evaluate(_payload(cmd))[0] == "block"


# ── escape hatch + fail-open ────────────────────────────────────────────────

def test_override_env_allows_the_merge(hook, monkeypatch):
    monkeypatch.setenv("CANOPY_ALLOW_MERGE_NO_BUMP", "1")
    _stub(monkeypatch, hook, COLLIDED)
    assert hook.evaluate(_payload("gh pr merge 429 --merge"))[0] == "override"


def test_fails_open_when_the_checker_raises(hook, monkeypatch):
    _stub(monkeypatch, hook, None, raises=True)
    assert hook.evaluate(_payload("gh pr merge 429 --merge"))[0] == "allow"


def test_fails_open_when_the_checker_is_missing(hook, monkeypatch):
    monkeypatch.setattr(hook, "_load_version_bump", lambda repo_root: None)
    assert hook.evaluate(_payload("gh pr merge 429 --merge"))[0] == "allow"


def test_fails_open_when_the_pr_lookup_fails(hook, monkeypatch):
    """`gh` can be slow, logged out, or offline. None of that should wedge a merge."""
    _stub(monkeypatch, hook, COLLIDED, pr_number=None)
    assert hook.evaluate(_payload("gh pr merge 429 --merge"))[0] == "allow"


def test_a_skipped_check_is_not_a_block(hook, monkeypatch):
    _stub(monkeypatch, hook, {"ok": True, "skipped": True, "reason": "base unreachable"})
    assert hook.evaluate(_payload("gh pr merge --merge"))[0] == "allow"
