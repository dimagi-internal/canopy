"""Tests for the fleet session-start auto-updater hook.

Covers fleet discovery (who gets updated) and the git/SHA-driven update itself
(fetch → reset clone → mirror into the version-keyed cache → patch the registry),
end-to-end against real temp git repos.
"""
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

HOOK = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "canopy"
    / "hooks"
    / "fleet_session_start_update.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("fleet_update", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


flu = _load_module()


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def _rev(cwd, ref="HEAD"):
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout.strip()


def _branch(cwd):
    return subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    ).stdout.strip()


# ── discovery ───────────────────────────────────────────────────────────────


def _install_path_with_marker(tmp_path, name, *, agent=True):
    p = tmp_path / "cache" / name / name / "0.1.0"
    (p / "config").mkdir(parents=True)
    if agent:
        (p / "config" / "agent.json").write_text("{}")
    return p


def test_discover_detects_canopy_and_agents(tmp_path, monkeypatch):
    monkeypatch.delenv("CANOPY_FLEET_UPDATE_PLUGINS", raising=False)
    monkeypatch.delenv("CANOPY_FLEET_UPDATE_EXCLUDE", raising=False)
    eva_path = _install_path_with_marker(tmp_path, "eva", agent=True)
    nova_path = _install_path_with_marker(tmp_path, "nova", agent=False)
    canopy_path = tmp_path / "cache" / "canopy" / "canopy" / "0.2.0"
    canopy_path.mkdir(parents=True)

    registry = {
        "plugins": {
            "canopy@canopy": [{"installPath": str(canopy_path), "version": "0.2.0", "gitCommitSha": "aaa"}],
            "eva@eva": [{"installPath": str(eva_path), "version": "0.1.0", "gitCommitSha": "bbb"}],
            "nova@nova-marketplace": [{"installPath": str(nova_path), "version": "9.0", "gitCommitSha": "ccc"}],
        }
    }
    names = [t["name"] for t in flu.discover_fleet(registry, {})]
    assert "canopy" in names          # host is always in the fleet
    assert "eva" in names             # factory agent (config/agent.json)
    assert "nova" not in names        # third-party plugin — left alone


def test_discover_honors_only_and_exclude(tmp_path, monkeypatch):
    canopy_path = tmp_path / "c"
    canopy_path.mkdir()
    eva_path = _install_path_with_marker(tmp_path, "eva", agent=True)
    registry = {
        "plugins": {
            "canopy@canopy": [{"installPath": str(canopy_path), "version": "0.2.0", "gitCommitSha": "a"}],
            "eva@eva": [{"installPath": str(eva_path), "version": "0.1.0", "gitCommitSha": "b"}],
        }
    }
    monkeypatch.setenv("CANOPY_FLEET_UPDATE_EXCLUDE", "canopy")
    assert [t["name"] for t in flu.discover_fleet(registry, {})] == ["eva"]

    monkeypatch.delenv("CANOPY_FLEET_UPDATE_EXCLUDE")
    monkeypatch.setenv("CANOPY_FLEET_UPDATE_PLUGINS", "canopy")
    assert [t["name"] for t in flu.discover_fleet(registry, {})] == ["canopy"]


def test_discover_resolves_clone_from_marketplaces(tmp_path):
    canopy_path = tmp_path / "c"
    canopy_path.mkdir()
    registry = {"plugins": {"canopy@canopy": [{"installPath": str(canopy_path), "version": "0.2.0", "gitCommitSha": "a"}]}}
    marketplaces = {"canopy": {"installLocation": "/custom/clone/loc"}}
    (target,) = flu.discover_fleet(registry, marketplaces)
    assert target["clone"] == "/custom/clone/loc"


# ── end-to-end update ───────────────────────────────────────────────────────


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_update_one_pulls_and_syncs_when_behind(tmp_path, monkeypatch):
    # Point the module's cache + registry at the sandbox.
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    registry_path = plugins_dir / "installed_plugins.json"
    monkeypatch.setattr(flu, "PLUGINS_DIR", plugins_dir)
    monkeypatch.setattr(flu, "REGISTRY", registry_path)

    # A bare "origin" the marketplace clone tracks.
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-b", "main")

    # Author v0.1.0 in a work tree and push it.
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "remote", "add", "origin", str(origin))
    (work / ".claude-plugin").mkdir()
    (work / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "eva", "version": "0.1.0"}))
    (work / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": "eva", "plugins": [{"name": "eva", "source": "./"}]})
    )
    (work / "skills").mkdir()
    (work / "skills" / "s.md").write_text("v1")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "v0.1.0")
    _git(work, "push", "origin", "main")
    old_sha = _rev(work)

    # The marketplace clone, currently at v0.1.0.
    clone = plugins_dir / "marketplaces" / "eva"
    clone.parent.mkdir(parents=True)
    _git(plugins_dir / "marketplaces", "clone", str(origin), "eva")

    # Installed registry pinned to the old sha + a stale cache dir.
    old_cache = plugins_dir / "cache" / "eva" / "eva" / "0.1.0"
    old_cache.mkdir(parents=True)
    registry_path.write_text(
        json.dumps(
            {"plugins": {"eva@eva": [{"installPath": str(old_cache), "version": "0.1.0", "gitCommitSha": old_sha}]}}
        )
    )

    # Now origin advances: v0.1.1 with a changed skill.
    (work / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "eva", "version": "0.1.1"}))
    (work / "skills" / "s.md").write_text("v2-updated")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "v0.1.1")
    _git(work, "push", "origin", "main")
    new_sha = _rev(work)

    target = {
        "key": "eva@eva",
        "name": "eva",
        "marketplace": "eva",
        "install_path": str(old_cache),
        "version": "0.1.0",
        "sha": old_sha,
        "clone": str(clone),
    }
    result = flu.update_one(target)

    assert result["status"] == "updated", result
    assert result["new_ver"] == "0.1.1"

    # New version-keyed cache dir exists with the UPDATED content.
    new_cache = plugins_dir / "cache" / "eva" / "eva" / "0.1.1"
    assert (new_cache / "skills" / "s.md").read_text() == "v2-updated"
    assert not (new_cache / ".git").exists(), ".git must be excluded from the cache"

    # Registry now points at the new version + sha.
    reg = json.loads(registry_path.read_text())
    entry = reg["plugins"]["eva@eva"][0]
    assert entry["version"] == "0.1.1"
    assert entry["installPath"] == str(new_cache)
    assert entry["gitCommitSha"] == new_sha


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_update_one_noop_when_current(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    monkeypatch.setattr(flu, "PLUGINS_DIR", plugins_dir)

    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-b", "main")
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "remote", "add", "origin", str(origin))
    (work / ".claude-plugin").mkdir()
    (work / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "eva", "version": "0.1.0"}))
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "v0.1.0")
    _git(work, "push", "origin", "main")
    sha = _rev(work)

    clone = plugins_dir / "marketplaces" / "eva"
    clone.parent.mkdir(parents=True)
    _git(plugins_dir / "marketplaces", "clone", str(origin), "eva")

    target = {
        "key": "eva@eva", "name": "eva", "marketplace": "eva", "install_path": "",
        "version": "0.1.0", "sha": sha, "clone": str(clone),
    }
    assert flu.update_one(target)["status"] == "up-to-date"


def _parked_clone_fixture(tmp_path, monkeypatch):
    """A marketplace clone sitting on a feature branch, with origin ahead.

    Not hypothetical: the canopy marketplace clone can be found on a feature
    branch. `git reset --hard origin/main` rewrites
    whichever branch is checked out, so the auto-updater silently moved that
    branch ref to main's tip — the commits survived only because they had been
    pushed."""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    registry_path = plugins_dir / "installed_plugins.json"
    monkeypatch.setattr(flu, "PLUGINS_DIR", plugins_dir)
    monkeypatch.setattr(flu, "REGISTRY", registry_path)

    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-b", "main")
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "remote", "add", "origin", str(origin))
    (work / ".claude-plugin").mkdir()
    (work / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "eva", "version": "0.1.0"}))
    (work / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": "eva", "plugins": [{"name": "eva", "source": "./"}]})
    )
    (work / "skills").mkdir()
    (work / "skills" / "s.md").write_text("v1")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "v0.1.0")
    _git(work, "push", "origin", "main")
    old_sha = _rev(work)

    clone = plugins_dir / "marketplaces" / "eva"
    clone.parent.mkdir(parents=True)
    _git(plugins_dir / "marketplaces", "clone", str(origin), "eva")

    old_cache = plugins_dir / "cache" / "eva" / "eva" / "0.1.0"
    old_cache.mkdir(parents=True)
    registry_path.write_text(json.dumps(
        {"plugins": {"eva@eva": [{"installPath": str(old_cache), "version": "0.1.0",
                                  "gitCommitSha": old_sha}]}}))

    # Someone parks the clone on a feature branch and commits there.
    _git(clone, "checkout", "-b", "parked")
    (clone / "wip.txt").write_text("work in progress")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "parked work")
    parked_sha = _rev(clone)

    # origin/main moves on.
    (work / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "eva", "version": "0.1.1"}))
    (work / "skills" / "s.md").write_text("v2-updated")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "v0.1.1")
    _git(work, "push", "origin", "main")

    target = {"key": "eva@eva", "name": "eva", "marketplace": "eva",
              "install_path": str(old_cache), "version": "0.1.0", "sha": old_sha,
              "clone": str(clone)}
    return plugins_dir, clone, parked_sha, target


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_update_switches_to_main_instead_of_rewriting_a_parked_branch(tmp_path, monkeypatch):
    plugins_dir, clone, parked_sha, target = _parked_clone_fixture(tmp_path, monkeypatch)

    result = flu.update_one(target)

    assert result["status"] == "updated", result
    assert (plugins_dir / "cache" / "eva" / "eva" / "0.1.1" / "skills" / "s.md"
            ).read_text() == "v2-updated"
    assert _rev(clone, "parked") == parked_sha, \
        "the parked branch must not be moved by an update"
    assert _branch(clone) == "main", "the update must leave the clone on main"


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_update_refuses_to_clobber_uncommitted_work_in_the_clone(tmp_path, monkeypatch):
    """A dirty parked clone is someone's workspace, wrong as that is. Report it
    and leave it alone rather than destroying the edit to install a plugin."""
    plugins_dir, clone, parked_sha, target = _parked_clone_fixture(tmp_path, monkeypatch)
    (clone / "skills" / "s.md").write_text("uncommitted local edit")

    result = flu.update_one(target)

    assert result["status"] == "error"
    assert "parked" in result["error"], result
    assert (clone / "skills" / "s.md").read_text() == "uncommitted local edit"
    assert _rev(clone, "parked") == parked_sha


def test_update_one_errors_without_clone(tmp_path):
    target = {
        "key": "x@x", "name": "x", "marketplace": "x", "install_path": "",
        "version": "0.1.0", "sha": "z", "clone": str(tmp_path / "nope"),
    }
    result = flu.update_one(target)
    assert result["status"] == "error"
    assert "clone" in result["error"]


# ── disable / opt-out ───────────────────────────────────────────────────────


def test_disabled_by_env(monkeypatch):
    monkeypatch.setenv("CANOPY_FLEET_AUTOUPDATE", "0")
    assert flu._disabled() is True
    monkeypatch.setenv("CANOPY_FLEET_AUTOUPDATE", "1")
    monkeypatch.setattr(flu, "DISABLE_FILE", Path("/nonexistent/disable"))
    assert flu._disabled() is False


def test_disabled_by_sentinel_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CANOPY_FLEET_AUTOUPDATE", "1")
    sentinel = tmp_path / "fleet-autoupdate-disabled"
    sentinel.write_text("")
    monkeypatch.setattr(flu, "DISABLE_FILE", sentinel)
    assert flu._disabled() is True


# ── mirror_tree: the rsync replacement (rsync does not exist on Windows) ─────────────────

class TestMirrorTree:
    """The fleet self-updater shelled out to rsync, so on Windows it failed on EVERY session —
    'rsync not found', eight consecutive failures, no successes, into a log nobody opens. The
    agent stayed frozen on whatever plugin version it was installed with."""

    def test_copies_files_and_directories(self, tmp_path):
        mod = flu
        src, dest = tmp_path / "src", tmp_path / "dest"
        (src / "sub").mkdir(parents=True)
        (src / "a.txt").write_text("a", encoding="utf-8")
        (src / "sub" / "b.txt").write_text("b", encoding="utf-8")
        assert mod.mirror_tree(src, dest, exclude=set()) == ""
        assert (dest / "a.txt").read_text(encoding="utf-8") == "a"
        assert (dest / "sub" / "b.txt").read_text(encoding="utf-8") == "b"

    def test_excludes_match_path_components(self, tmp_path):
        mod = flu
        src, dest = tmp_path / "src", tmp_path / "dest"
        (src / "node_modules" / "pkg").mkdir(parents=True)
        (src / "node_modules" / "pkg" / "x.js").write_text("x", encoding="utf-8")
        (src / "keep.txt").write_text("k", encoding="utf-8")
        assert mod.mirror_tree(src, dest, exclude={"node_modules"}) == ""
        assert (dest / "keep.txt").is_file()
        assert not (dest / "node_modules").exists()

    def test_delete_removes_stale_entries(self, tmp_path):
        """--delete semantics: the cache dir is version-keyed, so a file dropped upstream
        must not survive in the installed copy."""
        mod = flu
        src, dest = tmp_path / "src", tmp_path / "dest"
        src.mkdir()
        dest.mkdir()
        (src / "current.txt").write_text("new", encoding="utf-8")
        (dest / "current.txt").write_text("old", encoding="utf-8")
        (dest / "removed-upstream.txt").write_text("stale", encoding="utf-8")
        (dest / "stale-dir").mkdir()
        assert mod.mirror_tree(src, dest, exclude=set(), delete=True) == ""
        assert (dest / "current.txt").read_text(encoding="utf-8") == "new"
        assert not (dest / "removed-upstream.txt").exists()
        assert not (dest / "stale-dir").exists()

    def test_delete_preserves_venv_and_uv_lock(self, tmp_path):
        """uv materializes .venv/uv.lock in the cache; a same-version resync must not thrash
        them (the behaviour the original rsync call was careful to keep)."""
        mod = flu
        src, dest = tmp_path / "src", tmp_path / "dest"
        src.mkdir()
        (dest / ".venv").mkdir(parents=True)
        (dest / ".venv" / "marker").write_text("keep", encoding="utf-8")
        (dest / "uv.lock").write_text("lock", encoding="utf-8")
        assert mod.mirror_tree(src, dest, exclude=set(), delete=True) == ""
        assert (dest / ".venv" / "marker").read_text(encoding="utf-8") == "keep"
        assert (dest / "uv.lock").is_file()

    def test_returns_error_string_never_raises(self, tmp_path):
        mod = flu
        err = mod.mirror_tree(tmp_path / "does-not-exist", tmp_path / "dest", exclude=set())
        assert err and "copy" in err

    @pytest.mark.skipif(not hasattr(Path, "symlink_to"), reason="needs symlinks")
    def test_a_claude_code_worktree_with_a_dangling_link_does_not_freeze_the_plugin(self, tmp_path):
        """On a runner an agent's directory marketplace IS its agent clone, and a subagent's
        ephemeral worktree there (`.claude/worktrees/agent-…`, gitignored) can hold a
        `node_modules` link to a dep store that has been pruned. Following it raises ENOENT and
        aborts every update, which only fleet-update.log shows. A Claude Code worktree is a whole
        repo checkout, never plugin content; a dangling link is never worth aborting the copy for."""
        mod = flu
        src, dest = tmp_path / "src", tmp_path / "dest"
        wt = src / ".claude" / "worktrees" / "agent-a0252edd"
        wt.mkdir(parents=True)
        (wt / "SKILL.md").write_text("a stale checkout", encoding="utf-8")
        (wt / "node_modules").symlink_to(tmp_path / "pruned-store" / "node_modules")
        (src / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
        (src / "skills").mkdir()
        (src / "skills" / "rule.md").write_text("current", encoding="utf-8")
        (src / "skills" / "gone").symlink_to(tmp_path / "nowhere")
        (src / "top-gone").symlink_to(tmp_path / "nowhere-either")
        assert mod.mirror_tree(src, dest, exclude={".git"}) == ""
        assert (dest / "skills" / "rule.md").read_text(encoding="utf-8") == "current"
        assert (dest / ".claude" / "settings.json").is_file()
        assert not (dest / ".claude" / "worktrees").exists()
        assert not (dest / "skills" / "gone").exists() and not (dest / "top-gone").exists()


# ── --sync-cache: the /canopy:update Step 2 cache sync ─────────────────


def _clone_with_plugin(tmp_path, version="9.9.9", runtime=True):
    """A marketplace clone shaped like canopy's: plugins/canopy/ + repo-root runtime paths."""
    clone = tmp_path / "clone"
    plugin = clone / "plugins" / "canopy" / ".claude-plugin"
    plugin.mkdir(parents=True)
    (plugin / "plugin.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    (clone / ".claude-plugin").mkdir(exist_ok=True)
    (clone / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"plugins": [{"name": "canopy", "source": "./plugins/canopy"}]}), encoding="utf-8")
    (clone / "plugins" / "canopy" / "skills").mkdir()
    (clone / "plugins" / "canopy" / "skills" / "a.md").write_text("skill", encoding="utf-8")
    if runtime:
        (plugin / "runtime.json").write_text(
            json.dumps({"dest": "runtime", "paths": ["src", "pyproject.toml"]}), encoding="utf-8")
        (clone / "src").mkdir()
        (clone / "src" / "mod.py").write_text("x = 1", encoding="utf-8")
        (clone / "pyproject.toml").write_text("[project]", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(clone), check=True)
    _git(clone, "add", "-A")
    _git(clone, "commit", "-qm", "init")
    return clone


def _registry(tmp_path, install_path="/old/path", version="0.0.1"):
    reg = tmp_path / "installed_plugins.json"
    reg.write_text(json.dumps({"plugins": {"canopy@canopy": [
        {"version": version, "installPath": install_path, "gitCommitSha": "dead", "lastUpdated": "x"}]}}),
        encoding="utf-8")
    return reg


def test_sync_cache_mirrors_runtime_and_patches_registry(tmp_path, monkeypatch):
    """The whole of Step 2, with no rsync anywhere — the Windows failure it replaces."""
    clone = _clone_with_plugin(tmp_path)
    reg = _registry(tmp_path)
    monkeypatch.setattr(flu, "PLUGINS_DIR", tmp_path / "plugins")
    monkeypatch.setattr(flu, "REGISTRY", reg)
    monkeypatch.setattr(flu.shutil, "which", lambda _n: None)   # no npm in the test env

    assert flu.sync_cache(str(clone), "9.9.9") == 0

    cache = tmp_path / "plugins" / "cache" / "canopy" / "canopy" / "9.9.9"
    assert (cache / "skills" / "a.md").read_text(encoding="utf-8") == "skill"
    assert (cache / "runtime" / "src" / "mod.py").exists()      # repo-root runtime bundle
    assert (cache / "runtime" / "pyproject.toml").exists()

    entry = json.loads(reg.read_text(encoding="utf-8"))["plugins"]["canopy@canopy"][0]
    assert entry["version"] == "9.9.9"
    assert entry["installPath"] == str(cache)
    assert entry["gitCommitSha"] == _rev(clone)


def test_sync_cache_delete_pass_clears_a_reused_version_dir(tmp_path, monkeypatch):
    """Version numbers get reused; the OTHER commit's files must not survive in the cache."""
    clone = _clone_with_plugin(tmp_path)
    monkeypatch.setattr(flu, "PLUGINS_DIR", tmp_path / "plugins")
    monkeypatch.setattr(flu, "REGISTRY", _registry(tmp_path))
    monkeypatch.setattr(flu.shutil, "which", lambda _n: None)

    cache = tmp_path / "plugins" / "cache" / "canopy" / "canopy" / "9.9.9"
    cache.mkdir(parents=True)
    (cache / "stale.md").write_text("from the other commit", encoding="utf-8")
    (cache / "node_modules").mkdir()                            # excluded => preserved
    (cache / "node_modules" / "dep.js").write_text("dep", encoding="utf-8")

    assert flu.sync_cache(str(clone), "9.9.9") == 0
    assert not (cache / "stale.md").exists()
    assert (cache / "node_modules" / "dep.js").exists()


def test_sync_cache_leaves_the_registry_alone_when_a_stage_fails(tmp_path, monkeypatch):
    """Non-zero exit must mean the OLD version is still the installed one.

    This is the property the rsync chain lacked: it died between the pull and the registry
    patch, so the clone moved and the registry didn't, with nothing said about it.
    """
    clone = _clone_with_plugin(tmp_path)
    reg = _registry(tmp_path, install_path="/old/path", version="0.0.1")
    monkeypatch.setattr(flu, "PLUGINS_DIR", tmp_path / "plugins")
    monkeypatch.setattr(flu, "REGISTRY", reg)
    monkeypatch.setattr(flu.shutil, "which", lambda _n: None)
    monkeypatch.setattr(flu, "sync_runtime", lambda *a, **k: "disk on fire")

    assert flu.sync_cache(str(clone), "9.9.9") == 1
    entry = json.loads(reg.read_text(encoding="utf-8"))["plugins"]["canopy@canopy"][0]
    assert entry["version"] == "0.0.1" and entry["installPath"] == "/old/path"


def test_sync_cache_fails_loudly_on_a_missing_plugin_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(flu, "PLUGINS_DIR", tmp_path / "plugins")
    monkeypatch.setattr(flu, "REGISTRY", _registry(tmp_path))
    assert flu.sync_cache(str(tmp_path / "nope"), "9.9.9") == 1


def test_retry_sync_retries_only_transient_lock_errors(monkeypatch):
    """Windows Defender's `Access is denied` clears on a retry; a real error must not loop."""
    monkeypatch.setattr(flu.time, "sleep", lambda _s: None)

    calls = []
    def flaky():
        calls.append(1)
        return "copy a -> b: [WinError 5] Access is denied." if len(calls) < 3 else ""
    assert flu._retry_sync("plugin", flaky) == ""
    assert len(calls) == 3

    hard = []
    def broken():
        hard.append(1)
        return "runtime path missing in clone: src"
    assert flu._retry_sync("runtime", broken) == "runtime path missing in clone: src"
    assert len(hard) == 1                                        # no retry on a real error


def test_no_rsync_left_in_the_update_skill():
    """The fourth call site. rsync does not exist on Windows and this one cannot self-heal."""
    skill = HOOK.parent.parent / "skills" / "update" / "SKILL.md"
    body = skill.read_text(encoding="utf-8")
    code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith(("- ", "**", "(")))
    assert "rsync -a" not in code, "update/SKILL.md is shelling out to rsync again"


# ── node deps: a plugin's own installer wins ────────────────────────────────


def _cache_with_pkg(tmp_path, name="ace"):
    cache = tmp_path / "cache" / name / name / "1.0.0"
    (cache / "bin").mkdir(parents=True)
    (cache / "package.json").write_text("{}")
    return cache


def test_install_node_deps_defers_to_the_plugins_own_installer(tmp_path, monkeypatch):
    cache = _cache_with_pkg(tmp_path)
    script = cache / "bin" / "ace-install-deps"
    script.write_text("#!/usr/bin/env bash\n")
    script.chmod(0o755)
    calls = []
    monkeypatch.setattr(flu, "run", lambda cmd, timeout, cwd=None: calls.append(cmd) or (0, "", ""))
    monkeypatch.setattr(flu.shutil, "which", lambda n: f"/usr/bin/{n}")
    flu.install_node_deps("ace", cache)
    assert calls == [["bash", str(script), str(cache)]]


def test_install_node_deps_falls_back_to_npm_when_the_installer_fails(tmp_path, monkeypatch):
    cache = _cache_with_pkg(tmp_path)
    script = cache / "bin" / "ace-install-deps"
    script.write_text("#!/usr/bin/env bash\n")
    script.chmod(0o755)
    calls = []
    monkeypatch.setattr(flu, "run", lambda cmd, timeout, cwd=None: calls.append(cmd) or (1 if cmd[0] == "bash" else 0, "", ""))
    monkeypatch.setattr(flu.shutil, "which", lambda n: f"/usr/bin/{n}")
    flu.install_node_deps("ace", cache)
    assert calls[-1] == ["npm", "install", "--no-audit", "--no-fund"]


def test_install_node_deps_without_an_installer_is_the_plain_npm_install(tmp_path, monkeypatch):
    cache = _cache_with_pkg(tmp_path, name="eva")
    calls = []
    monkeypatch.setattr(flu, "run", lambda cmd, timeout, cwd=None: calls.append(cmd) or (0, "", ""))
    monkeypatch.setattr(flu.shutil, "which", lambda n: f"/usr/bin/{n}")
    flu.install_node_deps("eva", cache)
    assert calls == [["npm", "install", "--no-audit", "--no-fund"]]


# ── the canopy CLI follows the plugin ───────────────────────────────────────


def _cli_fixture(tmp_path, monkeypatch, *, plugin="0.2.900", cli="0.2.800", receipt_dir=None):
    clone = tmp_path / "clone"
    (clone / ".claude-plugin").mkdir(parents=True)
    (clone / ".claude-plugin" / "marketplace.json").write_text(json.dumps(
        {"plugins": [{"name": "canopy", "source": "./plugins/canopy"}]}))
    (clone / "plugins/canopy/.claude-plugin").mkdir(parents=True)
    (clone / "plugins/canopy/.claude-plugin/plugin.json").write_text(json.dumps({"version": plugin}))
    tooldir = tmp_path / "tools"
    (tooldir / "canopy").mkdir(parents=True)
    (tooldir / "canopy" / "uv-receipt.toml").write_text(
        f'[tool]\nrequirements = [{{ name = "canopy", directory = "{receipt_dir or "/elsewhere"}" }}]\n')
    calls = []

    def fake_run(cmd, timeout, cwd=None):
        calls.append(cmd)
        if cmd[1:] == ["--version"]:
            return (0, f"canopy {cli}", "") if cli else (1, "", "boom")
        if cmd[1:] == ["tool", "dir"]:
            return 0, str(tooldir), ""
        return 0, "", ""

    monkeypatch.setattr(flu, "run", fake_run)
    monkeypatch.setattr(flu, "_which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(flu, "off_release_branch", lambda clone, head="": "")
    monkeypatch.delenv("CANOPY_CLI_REFRESH", raising=False)
    return str(clone), calls


def test_refresh_cli_reinstalls_a_cli_behind_the_plugin(tmp_path, monkeypatch):
    clone, calls = _cli_fixture(tmp_path, monkeypatch)
    assert flu.refresh_cli(clone) == "refreshed 0.2.800→0.2.900"
    assert calls[-1] == ["/bin/uv", "tool", "install", "--reinstall", "--force", clone]


def test_refresh_cli_never_reinstalls_from_a_clone_whose_head_has_not_landed(tmp_path, monkeypatch):
    clone, calls = _cli_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(flu, "off_release_branch", lambda clone, head="": "HEAD abc is not on origin/main")
    assert flu.refresh_cli(clone).startswith("not reinstalling: HEAD abc is not on origin/main")
    assert not any(c[:2] == ["/bin/uv", "tool"] and c[2] in ("install", "upgrade") for c in calls)


def test_refresh_cli_upgrades_in_place_when_installed_from_the_clone(tmp_path, monkeypatch):
    """`upgrade --reinstall` keeps what was installed beside canopy (`--with canopy-kb`)."""
    clone, calls = _cli_fixture(tmp_path, monkeypatch, receipt_dir=str(tmp_path / "clone"))
    assert flu.refresh_cli(clone).startswith("refreshed")
    assert calls[-1] == ["/bin/uv", "tool", "upgrade", "--reinstall", "canopy"]


@pytest.mark.parametrize("cli", ["0.2.900", "0.2.950"])
def test_refresh_cli_leaves_a_current_or_ahead_cli_alone(tmp_path, monkeypatch, cli):
    clone, calls = _cli_fixture(tmp_path, monkeypatch, cli=cli)
    assert flu.refresh_cli(clone) == "current"
    assert not any("tool" in c for c in calls)


def test_refresh_cli_reinstalls_a_cli_that_cannot_say_its_version(tmp_path, monkeypatch):
    clone, calls = _cli_fixture(tmp_path, monkeypatch, cli="")
    assert flu.refresh_cli(clone) == "refreshed ?→0.2.900"


def test_refresh_cli_never_installs_a_missing_cli(tmp_path, monkeypatch):
    clone, calls = _cli_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(flu, "_which", lambda name: None if name == "canopy" else f"/bin/{name}")
    assert flu.refresh_cli(clone) == "no canopy CLI" and calls == []


def test_refresh_cli_can_be_turned_off(tmp_path, monkeypatch):
    clone, calls = _cli_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("CANOPY_CLI_REFRESH", "0")
    assert flu.refresh_cli(clone) == "off" and calls == []


def test_refresh_cli_reports_a_failed_reinstall(tmp_path, monkeypatch):
    clone, calls = _cli_fixture(tmp_path, monkeypatch)
    inner = flu.run
    monkeypatch.setattr(flu, "run", lambda cmd, timeout, cwd=None:
                        (1, "", "Access is denied") if "install" in cmd else inner(cmd, timeout, cwd))
    assert flu.refresh_cli(clone) == "reinstall failed: Access is denied"


def test_main_refreshes_the_cli_after_updating_the_plugins(tmp_path, monkeypatch):
    seen = []
    target = {"name": "canopy", "clone": "/c", "version": "1", "sha": "", "key": "canopy@canopy",
              "marketplace": "canopy", "install_path": "/i"}
    monkeypatch.setattr(flu, "CANOPY_DIR", tmp_path)
    monkeypatch.setattr(flu, "LOCK_FILE", tmp_path / "lock")
    monkeypatch.setattr(flu, "LOG_FILE", tmp_path / "log")
    monkeypatch.setattr(flu, "STAMP_FILE", tmp_path / "stamp")
    monkeypatch.setattr(flu, "_disabled", lambda: False)
    monkeypatch.setattr(flu, "load_json", lambda p: {})
    monkeypatch.setattr(flu, "discover_fleet", lambda r, m: [target])
    monkeypatch.setattr(flu, "update_one", lambda t: seen.append("plugin") or
                        {"name": "canopy", "status": "up-to-date"})
    monkeypatch.setattr(flu, "refresh_cli", lambda clone: seen.append(("cli", clone)) or
                        "refreshed 0.2.1→0.2.2")
    assert flu.main() == 0
    assert seen == ["plugin", ("cli", "/c")]
    assert "canopy-cli refreshed 0.2.1→0.2.2" in (tmp_path / "log").read_text()


# ── `--refresh`: one plugin, to its clone's HEAD ────────────────────────────


def _refresh_box(tmp_path, monkeypatch, *, name="ace", mkt="ace"):
    """A clone one commit ahead of what is installed from it, with a node_modules of its own."""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    monkeypatch.setattr(flu, "PLUGINS_DIR", plugins_dir)
    monkeypatch.setattr(flu, "REGISTRY", plugins_dir / "installed_plugins.json")
    monkeypatch.setattr(flu, "MARKETPLACES", plugins_dir / "known_marketplaces.json")
    monkeypatch.setattr(flu, "CANOPY_DIR", tmp_path / "canopy")
    monkeypatch.setattr(flu, "LOG_FILE", tmp_path / "canopy" / "fleet-update.log")
    monkeypatch.setattr(flu, "install_node_deps", lambda *a: None)
    monkeypatch.setattr(flu, "preflight", lambda *a: "")

    clone = tmp_path / "agents" / name
    clone.mkdir(parents=True)
    _git(clone, "init", "-b", "main")
    (clone / ".claude-plugin").mkdir()
    (clone / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": mkt, "plugins": [{"name": name, "source": "./"}]}))
    (clone / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": name, "version": "1.0.0"}))
    (clone / "skills").mkdir()
    (clone / "skills" / "s.md").write_text("old")
    (clone / ".gitignore").write_text("node_modules/\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "old")
    old_sha = _rev(clone)
    old_cache = plugins_dir / "cache" / mkt / name / "1.0.0"
    (old_cache / "skills").mkdir(parents=True)
    (old_cache / "skills" / "s.md").write_text("old")

    (clone / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": name, "version": "1.0.1"}))
    (clone / "skills" / "s.md").write_text("new")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "new")
    # What the runner's claim-time pull leaves: HEAD has landed on origin/main.
    _git(clone, "update-ref", "refs/remotes/origin/main", "HEAD")
    (clone / "node_modules" / "dep").mkdir(parents=True)
    (clone / "node_modules" / "dep" / "index.js").write_text("x")

    flu.REGISTRY.write_text(json.dumps({"plugins": {f"{name}@{mkt}": [
        {"installPath": str(old_cache), "version": "1.0.0", "gitCommitSha": old_sha}]}}))
    flu.MARKETPLACES.write_text(json.dumps({mkt: {"installLocation": str(clone)}}))
    return clone, old_cache, old_sha


def _entry(key="ace@ace"):
    return json.loads(flu.REGISTRY.read_text())["plugins"][key][0]


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_refresh_installs_the_clones_head_and_links_its_node_modules(tmp_path, monkeypatch):
    clone, _old, _ = _refresh_box(tmp_path, monkeypatch)

    result = flu.refresh_plugin("ace", str(clone))

    assert result["status"] == "updated", result
    new_cache = flu.PLUGINS_DIR / "cache" / "ace" / "ace" / "1.0.1"
    assert (new_cache / "skills" / "s.md").read_text() == "new"
    assert not (new_cache / ".git").exists()
    linked = new_cache / "node_modules" / "dep" / "index.js"
    assert linked.stat().st_ino == (clone / "node_modules" / "dep" / "index.js").stat().st_ino, \
        "node_modules is hard-linked from the clone, never copied"
    assert _entry() == {**_entry(), "version": "1.0.1", "installPath": str(new_cache),
                        "gitCommitSha": _rev(clone)}


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_refresh_is_a_no_op_when_the_clones_head_is_installed(tmp_path, monkeypatch):
    clone, old_cache, _ = _refresh_box(tmp_path, monkeypatch)
    flu.REGISTRY.write_text(json.dumps({"plugins": {"ace@ace": [
        {"installPath": str(old_cache), "version": "1.0.0", "gitCommitSha": _rev(clone)}]}}))
    monkeypatch.setattr(flu, "build_and_swap", lambda *a: pytest.fail("nothing to build"))

    assert flu.refresh_plugin("ace", str(clone))["status"] == "up-to-date"


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_refresh_never_moves_a_plugin_installed_ahead_of_its_clone_backwards(tmp_path, monkeypatch):
    clone, old_cache, old_sha = _refresh_box(tmp_path, monkeypatch)
    ahead = _rev(clone)
    _git(clone, "reset", "--hard", old_sha)
    flu.REGISTRY.write_text(json.dumps({"plugins": {"ace@ace": [
        {"installPath": str(old_cache), "version": "1.0.1", "gitCommitSha": ahead}]}}))
    monkeypatch.setattr(flu, "build_and_swap", lambda *a: pytest.fail("never move backwards"))

    assert flu.refresh_plugin("ace", str(clone))["status"] == "up-to-date"


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_refresh_refuses_a_clone_whose_head_has_not_landed_on_main(tmp_path, monkeypatch):
    """A clone left on a PR branch must never put that branch's hooks into the plugin cache."""
    clone, _old, old_sha = _refresh_box(tmp_path, monkeypatch)
    _git(clone, "checkout", "-q", "-b", "pr-branch")
    (clone / "skills" / "s.md").write_text("unreviewed")
    (clone / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "ace", "version": "1.0.2"}))
    _git(clone, "commit", "-qam", "pr")
    monkeypatch.setattr(flu, "build_and_swap", lambda *a: pytest.fail("built an unlanded commit"))

    result = flu.refresh_plugin("ace", str(clone))

    assert result["status"] == "error" and "not on origin/main" in result["error"], result
    assert _entry()["gitCommitSha"] == old_sha


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_refresh_installs_a_dependency_plugin_under_its_own_marketplace(tmp_path, monkeypatch):
    clone, _old, _ = _refresh_box(tmp_path, monkeypatch, name="nova", mkt="nova-marketplace")

    result = flu.refresh_plugin("nova", str(clone))

    assert result["status"] == "updated", result
    assert _entry("nova@nova-marketplace")["installPath"] == str(
        flu.PLUGINS_DIR / "cache" / "nova-marketplace" / "nova" / "1.0.1")


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_refresh_installs_a_plugin_whose_plugin_json_has_no_version(tmp_path, monkeypatch):
    """chrome-sales keeps its version in marketplace.json; label it by commit, as Claude Code does."""
    clone, _old, _ = _refresh_box(tmp_path, monkeypatch, name="chrome-sales", mkt="chrome-sales")
    (clone / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "chrome-sales"}))
    _git(clone, "commit", "-qam", "no version")
    _git(clone, "update-ref", "refs/remotes/origin/main", "HEAD")
    head = _rev(clone)

    result = flu.refresh_plugin("chrome-sales", str(clone))

    assert result["status"] == "updated", result
    new_cache = flu.PLUGINS_DIR / "cache" / "chrome-sales" / "chrome-sales" / head[:12]
    assert (new_cache / "skills" / "s.md").read_text() == "new"
    assert _entry("chrome-sales@chrome-sales")["version"] == head[:12]
    assert _entry("chrome-sales@chrome-sales")["gitCommitSha"] == head


def test_install_version_prefers_plugin_json(tmp_path):
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": "2.0.0"}))
    assert flu._install_version(tmp_path, "0317005abcdef0123456789") == "2.0.0"
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "x"}))
    assert flu._install_version(tmp_path, "0317005abcdef0123456789") == "0317005abcde"
    assert flu._install_version(tmp_path, "") == ""


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_refresh_waits_for_another_update_of_the_same_plugin_then_says_so(tmp_path, monkeypatch):
    pytest.importorskip("fcntl")
    clone, _old, old_sha = _refresh_box(tmp_path, monkeypatch)

    with flu.plugin_lock("ace") as held:
        assert held.held
        result = flu.refresh_plugin("ace", str(clone), wait=0.3)

    assert result["status"] == "error" and "held the lock" in result["error"]
    assert _entry()["gitCommitSha"] == old_sha


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_refresh_cli_logs_and_exits_nonzero_on_error(tmp_path, monkeypatch, capsys):
    clone, _old, _ = _refresh_box(tmp_path, monkeypatch)
    assert flu._refresh_from_argv(["--refresh", "ace", "--clone", str(clone)]) == 0
    assert "ace 1.0.0→1.0.1" in capsys.readouterr().out
    assert "ace 1.0.0→1.0.1" in flu.LOG_FILE.read_text()

    assert flu._refresh_from_argv(["--refresh", "nope", "--clone", str(clone)]) == 1
    assert "not installed from this clone" in capsys.readouterr().out


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_refresh_never_installs_a_clone_the_plugin_was_not_installed_from(tmp_path, monkeypatch):
    clone, _old, old_sha = _refresh_box(tmp_path, monkeypatch)
    other = tmp_path / "elsewhere" / "ace"
    shutil.copytree(clone, other)

    result = flu.refresh_plugin("ace", str(other))

    assert result["status"] == "error" and "not installed from this clone" in result["error"]
    assert _entry()["gitCommitSha"] == old_sha


@pytest.mark.parametrize("manifest", [
    {"dest": "/tmp/elsewhere", "paths": ["src"]},
    {"dest": "../../outside", "paths": ["src"]},
    {"dest": "runtime", "paths": ["../../.ssh"]},
    {"dest": "runtime", "paths": ["/etc/passwd"]},
    {"dest": "~/x", "paths": ["src"]},
])
def test_sync_runtime_refuses_a_manifest_path_that_leaves_its_root(tmp_path, manifest):
    clone = tmp_path / "clone"
    (clone / "src").mkdir(parents=True)
    cache = tmp_path / "cache"
    cache.mkdir()
    err = flu.sync_runtime(str(clone), manifest, cache)
    assert "must be" in err
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cache", "clone"]
    assert list(cache.iterdir()) == []


# ── stored agents: `canopy agent sync`, never `git fetch` ──────────────────────────────────


def _stored_rev(n: int, skill: str) -> dict:
    plugin = json.dumps({"name": "helper", "version": f"0.{n}.0"}).encode()
    market = json.dumps({"name": "helper", "plugins": [{"name": "helper", "source": "./"}]}).encode()
    return {".claude-plugin/plugin.json": (plugin, False),
            ".claude-plugin/marketplace.json": (market, False),
            "config/agent.json": (b"{}", False),
            "skills/s/SKILL.md": (skill.encode(), False)}


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_update_one_syncs_a_stored_agent_instead_of_fetching(tmp_path, monkeypatch):
    from orchestrator import agent_materialize as am
    from tests.git_http_fake import GitHttpFake, http_backend

    if not http_backend():
        pytest.skip("needs git http-backend")
    srv = GitHttpFake(tmp_path / "srv", token="T").start()
    try:
        _stored_update_flow(tmp_path, monkeypatch, am, srv)
    finally:
        srv.stop()


def _stored_update_flow(tmp_path, monkeypatch, am, srv):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    registry_path = plugins_dir / "installed_plugins.json"
    monkeypatch.setattr(flu, "PLUGINS_DIR", plugins_dir)
    monkeypatch.setattr(flu, "REGISTRY", registry_path)
    monkeypatch.setenv("CANOPY_WEB_API_URL", srv.base)
    srv.publish("connect", "helper", _stored_rev(1, "v1"), 1)

    def sync(dest):
        return am.materialize("helper", Path(dest), base_url=srv.base, token="T",
                              git_url=srv.url("connect", "helper"))

    clone = plugins_dir / "marketplaces" / "helper"
    old = sync(clone)
    old_cache = plugins_dir / "cache" / "helper" / "helper" / "0.1.0"
    old_cache.mkdir(parents=True)
    registry_path.write_text(json.dumps({"plugins": {"helper@helper": [
        {"installPath": str(old_cache), "version": "0.1.0", "gitCommitSha": old["commit"]}]}}))

    calls = []
    real_run = flu.run

    def fake_run(cmd, timeout, cwd=None):
        calls.append(cmd)
        if cmd[0] == "CANOPY":
            assert cmd[1:5] == ["agent", "sync", "--slug", "helper"] and cmd[5:] == ["--dest", str(clone)]
            return 0, json.dumps(sync(cmd[6])), ""
        return real_run(cmd, timeout, cwd)

    monkeypatch.setattr(flu, "run", fake_run)
    monkeypatch.setattr(flu, "_which", lambda name: "CANOPY" if name == "canopy" else None)
    target = {"key": "helper@helper", "name": "helper", "marketplace": "helper",
              "install_path": str(old_cache), "version": "0.1.0", "sha": old["commit"],
              "clone": str(clone)}

    assert flu.is_stored_clone(str(clone))
    assert flu.update_one(target)["status"] == "up-to-date"        # nothing published since

    srv.publish("connect", "helper", _stored_rev(2, "v2"), 2)
    result = flu.update_one(target)
    assert result["status"] == "updated", result
    assert result["new_ver"] == "0.2.0"
    new_cache = plugins_dir / "cache" / "helper" / "helper" / "0.2.0"
    assert (new_cache / "skills" / "s" / "SKILL.md").read_text() == "v2"
    # The updater itself never fetches a canopy origin (it has no token to send).
    assert not any(c[:2] == ["git", "fetch"] for c in calls)
    entry = json.loads(registry_path.read_text())["plugins"]["helper@helper"][0]
    assert entry["gitCommitSha"] == _rev(clone) and entry["installPath"] == str(new_cache)

    # And `--refresh` (what a runner calls after its own sync) accepts the synced HEAD.
    assert flu.off_release_branch(str(clone)) == ""


def test_is_stored_clone_recognises_canopy_git_origins(tmp_path, monkeypatch):
    monkeypatch.setenv("CANOPY_WEB_API_URL", "https://canopy.example")
    cases = {"https://canopy.example/git/connect/helper.git": True,
             "canopy://connect/helper": True,
             "https://github.com/o/helper.git": False,
             # Another canopy-web (staging): by shape, and only with the slug the sync records.
             "https://staging.example/git/connect/helper.git": False}
    for i, (url, want) in enumerate(cases.items()):
        c = tmp_path / str(i)
        c.mkdir()
        _git(c, "init", "-b", "main")
        _git(c, "remote", "add", "origin", url)
        assert flu.is_stored_clone(str(c)) is want, url
    _git(tmp_path / "3", "config", "canopy.slug", "helper")
    assert flu.is_stored_clone(str(tmp_path / "3"))


def test_update_one_exported_agent_takes_the_ordinary_path(tmp_path, monkeypatch):
    """The sync repointed origin at GitHub (the agent was exported): update_one goes on to
    the normal fetch path rather than reporting the stored result."""
    clone = tmp_path / "c"
    clone.mkdir()
    _git(clone, "init", "-b", "main")
    _git(clone, "remote", "add", "origin", "canopy://connect/helper")
    real_run = flu.run
    fetched = []

    def fake_run(cmd, timeout, cwd=None):
        if cmd[0] == "CANOPY":
            _git(clone, "remote", "set-url", "origin", "https://github.com/o/helper.git")
            return 0, "{}", ""
        if cmd[:2] == ["git", "fetch"]:
            fetched.append(cmd)
            return 1, "", "no network in tests"
        return real_run(cmd, timeout, cwd)

    monkeypatch.setattr(flu, "run", fake_run)
    monkeypatch.setattr(flu, "_which", lambda name: "CANOPY" if name == "canopy" else None)
    r = flu.update_one({"key": "helper@helper", "name": "helper", "marketplace": "helper",
                        "install_path": "", "version": "0.1.0", "sha": "", "clone": str(clone)})
    assert fetched and r["status"] == "error" and r["error"].startswith("fetch")


def test_update_one_stored_agent_without_a_cli_says_so(tmp_path, monkeypatch):
    clone = tmp_path / "c"
    clone.mkdir()
    _git(clone, "init", "-b", "main")
    _git(clone, "remote", "add", "origin", "canopy://connect/helper")
    monkeypatch.setattr(flu, "_which", lambda name: None)
    r = flu.update_one({"key": "helper@helper", "name": "helper", "marketplace": "helper",
                        "install_path": "", "version": "0.1.0", "sha": "", "clone": str(clone)})
    assert r["status"] == "error" and "no canopy CLI" in r["error"]
