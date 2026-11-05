"""The fleet updater builds a version off to the side, checks it, then swaps it in.

A commit that does not bump the version lands in the same cache dir running sessions execute
from. Mirroring into it in place deleted `runtime/` and `node_modules/` under them; a broken
guard went live with no check. These pin the staged build, the preflight, and the swap.

Run: uv run pytest tests/test_fleet_update_swap.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "canopy"
_spec = importlib.util.spec_from_file_location(
    "fleet_update_swap", PLUGIN / "hooks" / "fleet_session_start_update.py")
flu = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(flu)


@pytest.fixture
def plugins(tmp_path, monkeypatch):
    d = tmp_path / "plugins"
    d.mkdir()
    monkeypatch.setattr(flu, "PLUGINS_DIR", d)
    monkeypatch.setattr(flu, "install_node_deps", lambda name, cache: None)
    return d


def _plugin_src(path: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        p = path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return path


def test_same_version_update_swaps_and_keeps_deps(plugins, tmp_path):
    cache = plugins / "cache" / "eva" / "eva" / "0.1.0"
    _plugin_src(cache, {
        "skills/s.md": "old", "skills/gone.md": "stale",
        "node_modules/dep/index.js": "dep",
    })
    src = _plugin_src(tmp_path / "src", {
        "skills/s.md": "new", ".claude-plugin/plugin.json": json.dumps({"version": "0.1.0"}),
    })
    held = open(cache / "skills" / "s.md")          # a running session's open file

    assert flu.build_and_swap("eva", src, str(tmp_path), cache) == ""

    assert (cache / "skills" / "s.md").read_text() == "new"
    assert not (cache / "skills" / "gone.md").exists()
    assert (cache / "node_modules" / "dep" / "index.js").read_text() == "dep"
    assert held.read() == "old"                     # the old tree stays readable, untouched
    held.close()
    retired = [p for p in (plugins / flu.STAGING_DIR).iterdir() if p.name.startswith("retired-")]
    assert len(retired) == 1
    assert not [p for p in (plugins / flu.STAGING_DIR).iterdir() if p.name.startswith("build-")]


def test_new_version_is_built_whole(plugins, tmp_path):
    cache = plugins / "cache" / "eva" / "eva" / "0.2.0"
    src = _plugin_src(tmp_path / "src", {"skills/s.md": "v2"})
    assert flu.build_and_swap("eva", src, str(tmp_path), cache) == ""
    assert (cache / "skills" / "s.md").read_text() == "v2"


def test_runtime_venv_survives_a_same_version_update(plugins, tmp_path):
    cache = plugins / "cache" / "canopyish" / "canopyish" / "1.0.0"
    _plugin_src(cache, {"runtime/.venv/marker": "venv", "runtime/src/old.py": "old"})
    clone = _plugin_src(tmp_path / "clone", {"src/new.py": "new"})
    src = _plugin_src(clone / "plugin", {
        ".claude-plugin/runtime.json": json.dumps({"dest": "runtime", "paths": ["src"]}),
    })
    assert flu.build_and_swap("canopyish", src, str(clone), cache) == ""
    assert (cache / "runtime" / ".venv" / "marker").read_text() == "venv"
    assert (cache / "runtime" / "src" / "new.py").exists()
    assert not (cache / "runtime" / "src" / "old.py").exists()


def test_unparseable_hooks_json_never_goes_live(plugins, tmp_path):
    cache = plugins / "cache" / "eva" / "eva" / "0.1.0"
    _plugin_src(cache, {"hooks/hooks.json": "{}", "skills/s.md": "old"})
    src = _plugin_src(tmp_path / "src", {"hooks/hooks.json": "{broken", "skills/s.md": "new"})
    err = flu.build_and_swap("eva", src, str(tmp_path), cache)
    assert err.startswith("preflight") and "hooks.json" in err
    assert (cache / "skills" / "s.md").read_text() == "old"
    assert not [p for p in (plugins / flu.STAGING_DIR).iterdir() if p.name.startswith("build-")]


def test_failed_swap_puts_the_installed_version_back(plugins, tmp_path, monkeypatch):
    cache = plugins / "cache" / "eva" / "eva" / "0.1.0"
    _plugin_src(cache, {"skills/s.md": "old"})
    src = _plugin_src(tmp_path / "src", {"skills/s.md": "new"})
    real = os.replace
    calls = []

    def flaky(a, b):
        calls.append((a, b))
        if len(calls) == 2:                           # staged -> cache_dir
            raise PermissionError("in use")
        return real(a, b)

    monkeypatch.setattr(flu.os, "replace", flaky)
    err = flu.build_and_swap("eva", src, str(tmp_path), cache)
    assert err.startswith("swap:")
    assert (cache / "skills" / "s.md").read_text() == "old"


def test_retired_versions_are_dropped_after_a_day(plugins):
    root = plugins / flu.STAGING_DIR
    old, fresh = root / "retired-old", root / "retired-fresh"
    for d in (old, fresh):
        d.mkdir(parents=True)
    past = time.time() - flu.RETIRED_KEEP_SECONDS - 60
    os.utime(old, (past, past))
    flu._clean_retired()
    assert not old.exists() and fresh.exists()


# ── canopy's guard preflight, against the real plugin ────────────────────────────────

def _staged_canopy(tmp_path) -> Path:
    staged = tmp_path / "staged"
    shutil.copytree(PLUGIN, staged, ignore=shutil.ignore_patterns("node_modules", "__pycache__"))
    return staged


def test_preflight_passes_the_real_guards(plugins, tmp_path):
    assert flu.preflight("canopy", _staged_canopy(tmp_path)) == ""


@pytest.mark.parametrize("guard", ["agent-core/gating_guard.py", "hooks/credential_guard.py"])
def test_preflight_refuses_a_guard_that_crashes(plugins, tmp_path, guard):
    staged = _staged_canopy(tmp_path)
    (staged / guard).write_text("raise RuntimeError('boom')\n")
    err = flu.preflight("canopy", staged)
    assert err.startswith("preflight") and guard in err


def test_preflight_refuses_a_gating_guard_that_stops_blocking(plugins, tmp_path):
    staged = _staged_canopy(tmp_path)
    (staged / "agent-core" / "gating_guard.py").write_text("import sys; sys.exit(0)\n")
    err = flu.preflight("canopy", staged)
    assert "gating_guard.py" in err and "want 2" in err
