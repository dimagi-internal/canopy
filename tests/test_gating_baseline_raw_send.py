"""The fleet's raw-send rail (gating-baseline.json `channels.email`) sees every common shape.

The rail used to anchor on `gog` immediately followed by `gmail send|reply` at the start of a
statement, so a global flag before the subcommand (the `-a <mailbox>` form agents are taught to
write), a full path, an `env`/`sudo`/`then` prefix, a quoted name or `sh -c` walked past it.
Drives the real engine against the real shipped baseline.

This is a rail against honest mistakes, not a boundary: a script file or a variable holding the
command still gets through. The negative half matters as much — reads must stay free.

Run: uv run pytest tests/test_gating_baseline_raw_send.py
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import re
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "canopy"
GUARD = PLUGIN_DIR / "agent-core" / "gating_guard.py"
BASELINE = PLUGIN_DIR / "agent-core" / "gating-baseline.json"
G = "gog"   # kept out of literal command text so editing this file is not itself railed

BLOCKED = [
    f"{G} gmail send --to a@b.c --subject s --body b",
    f"cd x; {G} gmail reply --to a",
    f"{G} -a hal@dimagi-ai.com gmail send --to a",
    f'{G} -a hal@dimagi-ai.com --client "$(canopy email client --repo .)" gmail send --to a',
    f"{G} gmail -a x send --to a",
    f"true && {G} --json gmail reply x",
    f"/opt/homebrew/bin/{G} gmail send --to a",
    f"C:\\Tools\\{G}.exe gmail send --to a",
    f"env {G} gmail send --to a",
    f"env -i {G} gmail send",
    f"FOO=1 {G} gmail send",
    f"sudo {G} gmail send",
    f"if true; then {G} gmail send --to a; fi",
    f"for i in 1; do {G} gmail send; done",
    f'"{G}" gmail send --to a',
    f"bash -c '{G} gmail send --to a'",
    f"x=$({G} gmail send --to a)",
]

ALLOWED = [
    f"{G} gmail search 'is:unread'",
    f'{G} gmail search "subject:reply" -a x',
    f"{G} gmail search -q send",
    f'{G} gmail search "from:x newer_than:3d" -a m --client "$(canopy email client --repo .)" -p',
    f"{G} gmail thread read 123",
    f'git commit -m "never use {G} gmail send"',
    f"echo {G} gmail send",
    f"grep -rn '{G} gmail send' docs",
    "bin/hal-email send --to a --body-file b",
    f"{G} drive ls",
    "catalog gmail send",
]


def _rail():
    rules = json.loads(BASELINE.read_text())["channels"]["email"]
    return next(r for r in rules if "gmail" in r.get("pattern", ""))


def _load():
    loader = importlib.machinery.SourceFileLoader("gating_guard_raw_send", str(GUARD))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def agent(monkeypatch, tmp_path):
    monkeypatch.setenv("CANOPY_PLUGIN_DIR", str(PLUGIN_DIR))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for var in ("CLAUDE_PROJECT_DIR", "CANOPY_AGENT", "CANOPY_AGENT_SLUG"):
        monkeypatch.delenv(var, raising=False)
    repo = tmp_path / "zed"
    (repo / "config").mkdir(parents=True)
    (repo / "config" / "gating.json").write_text(json.dumps(
        {"slug": "zed", "channels": ["email"], "deny": [], "approve": []}))
    guard = _load()

    def decide(cmd):
        payload = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(repo)}
        return guard.run_session(payload, env={})[0]
    return decide


@pytest.mark.parametrize("cmd", BLOCKED)
def test_blocked(agent, cmd):
    assert agent(cmd) == 2, cmd


@pytest.mark.parametrize("cmd", ALLOWED)
def test_allowed(agent, cmd):
    assert agent(cmd) == 0, cmd


@pytest.mark.parametrize("subject", [
    f"{G} x\n" * 8000,                     # many statements, none a send
    f"{G} " + "tok " * 10000,              # one long statement
    f"{G} gmail " + "-a b " * 5000,        # long flag run, no subcommand
    f"{G}" + " " * 40000,
])
def test_the_rail_is_linear_on_big_inputs(subject):
    # A 40 KB heredoc must not stall every tool call: the first broad draft of this rail took
    # 10 s on 8,000 lines because its token separator matched newlines.
    rx = re.compile(_rail()["pattern"])
    started = time.monotonic()
    rx.search(subject)
    assert time.monotonic() - started < 0.5
