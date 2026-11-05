"""A guard that cannot check a tool call blocks it.

Claude Code treats every hook exit other than 2 as "allow", so a guard that crashes, cannot
read its payload, or cannot read the agent's config/gating.json would otherwise switch the
agent's rails off silently. The launcher maps a guard's stray exit code to 2, and the gating
guard treats a missing or broken config like an unknown one (every channel mounted).

Run: uv run pytest tests/hooks/test_guard_fails_closed.py
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = ROOT / "plugins" / "canopy"
GUARD = PLUGIN_DIR / "agent-core" / "gating_guard.py"
LAUNCHER = PLUGIN_DIR / "hooks" / "python.sh"
SH = shutil.which("sh")

RAW_SEND = "gog gmail send --to a@b.c --subject s --body b"


def _load():
    loader = importlib.machinery.SourceFileLoader("gating_guard_fail_closed", str(GUARD))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def guard(monkeypatch, tmp_path):
    monkeypatch.setenv("CANOPY_PLUGIN_DIR", str(PLUGIN_DIR))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("CANOPY_AGENT", raising=False)
    monkeypatch.delenv("CANOPY_AGENT_SLUG", raising=False)
    return _load()


def _repo(tmp_path, gating_text):
    repo = tmp_path / "agents" / "zed"
    (repo / "config").mkdir(parents=True)
    (repo / "config" / "agent.json").write_text(json.dumps({"slug": "zed"}))
    if gating_text is not None:
        (repo / "config" / "gating.json").write_text(gating_text)
    return repo


def _bash(cmd, cwd):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(cwd)}


@pytest.mark.parametrize("gating_text", [None, "{not json", "[]"],
                         ids=["missing", "corrupt", "not-an-object"])
def test_unusable_gating_json_keeps_the_raw_send_rail(guard, tmp_path, gating_text):
    repo = _repo(tmp_path, gating_text)
    code, _, err = guard.run_session(_bash(RAW_SEND, repo), env={})
    assert code == 2, err


def test_unusable_gating_json_still_allows_ordinary_work(guard, tmp_path):
    repo = _repo(tmp_path, "{not json")
    code, _, _ = guard.run_session(_bash("git status", repo), env={})
    assert code == 0


def _main(stdin, argv=("--session",), env_extra=None):
    env = {**os.environ, "CANOPY_PLUGIN_DIR": str(PLUGIN_DIR), **(env_extra or {})}
    return subprocess.run([sys.executable, str(GUARD), *argv], input=stdin, env=env,
                          capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("stdin", ["not json", "[1, 2]"])
def test_unreadable_payload_blocks(stdin):
    r = _main(stdin)
    assert r.returncode == 2 and "fail closed" in r.stderr


def test_crash_while_evaluating_blocks(guard, monkeypatch, capsys):
    def boom(_payload, env=None):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(guard, "run_session", boom)
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps(_bash("ls", "/"))))
    with pytest.raises(SystemExit) as exc:
        guard.main(argv=["--session"])
    assert exc.value.code == 2
    assert "kaboom" in capsys.readouterr().err


# ── the launcher ──────────────────────────────────────────────────────────────────────

def _launch(tmp_path, body, *args):
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    exe = bin_ / "python3"
    exe.write_text(f"#!/bin/sh\n{body}\n")
    exe.chmod(0o755)
    shbin = tmp_path / "shbin"
    shbin.mkdir(exist_ok=True)
    if not (shbin / "sh").exists():
        os.symlink(SH, shbin / "sh")
    env = {"PATH": os.pathsep.join([str(bin_), str(shbin)])}
    return subprocess.run([SH, str(LAUNCHER), *args], env=env, input="{}",
                          capture_output=True, text=True, timeout=10)


@pytest.mark.skipif(SH is None, reason="needs a POSIX sh")
@pytest.mark.parametrize("rc", [1, 3, 49, 127])
def test_launcher_turns_a_guard_crash_into_a_block(tmp_path, rc):
    r = _launch(tmp_path, f"echo trace >&2; exit {rc}", "--guard", "gating_guard.py", "--session")
    assert r.returncode == 2
    assert f"exited {rc}" in r.stderr and "gating_guard.py" in r.stderr


@pytest.mark.skipif(SH is None, reason="needs a POSIX sh")
@pytest.mark.parametrize("rc", [0, 2])
def test_launcher_passes_allow_and_block_through(tmp_path, rc):
    r = _launch(tmp_path, f"read -r _; echo out; exit {rc}", "--guard", "g.py")
    assert r.returncode == rc and r.stdout.strip() == "out"


@pytest.mark.skipif(SH is None, reason="needs a POSIX sh")
def test_launcher_leaves_advisory_hooks_alone(tmp_path):
    r = _launch(tmp_path, "exit 1", "caller_context.py")
    assert r.returncode == 1


@pytest.mark.skipif(SH is None, reason="needs a POSIX sh")
def test_launcher_passes_stdin_to_the_guard(tmp_path):
    r = _launch(tmp_path, 'read -r line; printf %s "$line"', "--guard", "g.py")
    assert r.returncode == 0 and r.stdout == "{}"
