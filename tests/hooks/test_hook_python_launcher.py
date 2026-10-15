"""hooks/python.sh — every canopy hook runs through it, and a guard BLOCKS when
there is no working Python.

Origin (2026-10-15): on Windows `python3` is usually the Microsoft Store stub, which
exits 49. Claude Code treats any hook exit other than 2 as non-blocking, so the
gating, confinement and one-shot guards never ran on that box and a raw send went
through. These tests fake each interpreter on PATH, so they run on any POSIX host.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOKS_DIR = ROOT / "plugins" / "canopy" / "hooks"
LAUNCHER = HOOKS_DIR / "python.sh"
GUARDS = {"gating_guard.py", "profile_guard.py", "one_shot_turn_guard.py"}

SH = shutil.which("sh")
pytestmark = pytest.mark.skipif(SH is None, reason="needs a POSIX sh")


def _fake(dirpath: Path, name: str, body: str) -> None:
    dirpath.mkdir(parents=True, exist_ok=True)
    exe = dirpath / name
    exe.write_text(f"#!/bin/sh\n{body}\n")
    exe.chmod(0o755)


def _run(tmp_path: Path, path_dirs: list[Path], *args: str) -> subprocess.CompletedProcess:
    # Only our fake dirs + a dir holding `sh` itself: no real python can leak in.
    sh_dir = tmp_path / "shbin"
    sh_dir.mkdir(exist_ok=True)
    if not (sh_dir / "sh").exists():
        os.symlink(SH, sh_dir / "sh")
    env = {"PATH": os.pathsep.join(str(d) for d in [*path_dirs, sh_dir])}
    return subprocess.run([SH, str(LAUNCHER), *args], env=env,
                          capture_output=True, text=True, timeout=10)


def test_real_python3_runs_the_hook(tmp_path):
    bin_ = tmp_path / "bin"
    _fake(bin_, "python3", 'echo "python3 $*"')
    r = _run(tmp_path, [bin_], "--guard", "g.py", "--session")
    assert r.returncode == 0 and r.stdout.strip() == "python3 g.py --session"


def test_store_stub_is_skipped_for_py_launcher(tmp_path):
    stub = tmp_path / "AppData" / "Local" / "Microsoft" / "WindowsApps"
    _fake(stub, "python3", "echo 'Python was not found'; exit 49")
    _fake(stub, "python", "echo 'Python was not found'; exit 49")
    win = tmp_path / "Windows"
    _fake(win, "py", 'echo "py $*"')
    r = _run(tmp_path, [stub, win], "--guard", "g.py")
    assert r.returncode == 0 and r.stdout.strip() == "py -3 g.py"


def test_guard_blocks_when_no_python_exists(tmp_path):
    stub = tmp_path / "WindowsApps"
    _fake(stub, "python3", "exit 49")
    r = _run(tmp_path, [stub], "--guard", "gating_guard.py", "--session")
    assert r.returncode == 2  # the ONLY exit code Claude Code treats as blocking
    assert "gating_guard.py" in r.stderr and "blocking" in r.stderr


def test_advisory_hook_is_skipped_not_blocking(tmp_path):
    r = _run(tmp_path, [], "caller_context.py")
    assert r.returncode == 0 and "skipped" in r.stderr


def test_every_plugin_hook_goes_through_the_launcher():
    hooks = json.loads((HOOKS_DIR / "hooks.json").read_text())["hooks"]
    commands = [h["command"] for groups in hooks.values() for g in groups
                for h in g["hooks"] if h.get("type") == "command"]
    assert commands
    for cmd in commands:
        assert cmd.startswith('sh "${CLAUDE_PLUGIN_ROOT}/hooks/python.sh" '), cmd
        is_guard = any(g in cmd for g in GUARDS)
        assert ("--guard" in cmd) == is_guard, cmd
