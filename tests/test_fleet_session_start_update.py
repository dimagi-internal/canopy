"""Tests for the fleet session-start auto-updater hook (GitHub #357).

Covers fleet discovery (who gets updated) and the git/SHA-driven update itself
(fetch → reset clone → rsync into the version-keyed cache → patch the registry),
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


@pytest.mark.skipif(not shutil.which("git") or not shutil.which("rsync"), reason="needs git+rsync")
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

    Not hypothetical: on 2026-07-28 the canopy marketplace clone was found on
    `ddd/preflight-applies-scroll`. `git reset --hard origin/main` rewrites
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


@pytest.mark.skipif(not shutil.which("git") or not shutil.which("rsync"), reason="needs git+rsync")
def test_update_switches_to_main_instead_of_rewriting_a_parked_branch(tmp_path, monkeypatch):
    plugins_dir, clone, parked_sha, target = _parked_clone_fixture(tmp_path, monkeypatch)

    result = flu.update_one(target)

    assert result["status"] == "updated", result
    assert (plugins_dir / "cache" / "eva" / "eva" / "0.1.1" / "skills" / "s.md"
            ).read_text() == "v2-updated"
    assert _rev(clone, "parked") == parked_sha, \
        "the parked branch must not be moved by an update"
    assert _branch(clone) == "main", "the update must leave the clone on main"


@pytest.mark.skipif(not shutil.which("git") or not shutil.which("rsync"), reason="needs git+rsync")
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
