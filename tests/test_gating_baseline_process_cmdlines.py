"""The fleet rail on listing other processes' full command lines (gating-baseline `always`).

WHY. This tool's shell is started with its environment exported INLINE on its
command line, an agent's service-account key among it. A `pgrep -fl wire.sh`
(checking whether a background job was still running) prints that key into the transcript,
and the key then has to be rotated. PIDs and bare command names are safe; arguments are not.

Run: uv run pytest tests/test_gating_baseline_process_cmdlines.py
"""
import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parent.parent / "plugins" / "canopy"


@pytest.fixture
def call(monkeypatch, tmp_path):
    monkeypatch.setenv("CANOPY_PLUGIN_DIR", str(PLUGIN))
    loader = importlib.machinery.SourceFileLoader(
        "gating_guard_proc", str(PLUGIN / "agent-core" / "gating_guard.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    guard = importlib.util.module_from_spec(spec)
    loader.exec_module(guard)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "gating.json").write_text(
        json.dumps({"slug": "eva", "channels": [], "deny": [], "approve": []}))
    return lambda cmd: guard.run(str(tmp_path), {"tool_name": "Bash", "tool_input": {"command": cmd}})


@pytest.mark.parametrize("cmd", [
    "pgrep -fl wire.sh",                      # leaks the exported env
    "pgrep -af canopy",
    "pgrep -l claude",
    "ps aux",
    "ps aux | grep wire",
    "ps -ef",
    "ps -eF",
    "ps ax",
    "ps -f -p 123",
    "ps -o pid,args -p 123",
    "ps -p 123 -o command",
    "cat /proc/123/cmdline",
    "tr '\\0' ' ' < /proc/self/environ",
    "sleep 1; ps aux",
])
def test_listing_full_command_lines_is_blocked(call, cmd):
    code, _, err = call(cmd)
    assert code == 2, cmd
    assert "pgrep -f <pattern>" in err


@pytest.mark.parametrize("cmd", [
    "pgrep -f wire.sh",                       # PIDs only
    "pgrep -x claude",
    "ps -o pid,etime,comm -p 123",
    "ps -p 123",
    "kill 123",
    "grep -rn 'ps aux' docs/",
    "echo 'do not run ps aux'",
    "lsof -p 123",
])
def test_pids_and_command_names_pass(call, cmd):
    assert call(cmd)[0] == 0, cmd


def test_no_fleet_procedure_tells_an_agent_to_run_what_this_rail_blocks(call):
    """A doc printing a railed command spends a turn on the block, and it takes the
    rest of that command down with it. A liveness check in turn.md could do this."""
    offenders = []
    for doc in sorted((PLUGIN / "agent-core").glob("*.md")):
        in_fence = False
        for number, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
            elif in_fence and "other processes" in call(line)[2]:
                offenders.append(f"{doc.name}:{number}: {line.strip()}")
    assert not offenders, "\n".join(offenders)
