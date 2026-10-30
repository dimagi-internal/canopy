"""The fleet rail around an agent's canopy token (gating-baseline `always`).

An agent's canopy token is a secret: a raw curl write to /canopy-token, or the MCP tool whose
result is the token, would put it in a transcript. The NEGATIVE half: masked reads pass.

Run: uv run pytest tests/test_gating_baseline_vault_paths.py
"""
import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parent.parent / "plugins" / "canopy"

# Built at runtime so the literals never sit on a Bash command line the guard scans.
VAULT = "https://canopy-web.dimagi.com/api/" + "agents/lex/" + "vault"
TOKEN = "https://canopy-web.dimagi.com/api/" + "agents/lex/" + "canopy-token"
MCP = "mcp__plugin_canopy_canopy-web__"


@pytest.fixture
def call(monkeypatch, tmp_path):
    monkeypatch.setenv("CANOPY_PLUGIN_DIR", str(PLUGIN))
    loader = importlib.machinery.SourceFileLoader(
        "gating_guard_vault", str(PLUGIN / "agent-core" / "gating_guard.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    guard = importlib.util.module_from_spec(spec)
    loader.exec_module(guard)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "gating.json").write_text(
        json.dumps({"slug": "eva", "channels": [], "deny": [], "approve": []}))
    return lambda tool, tool_input: guard.run(
        str(tmp_path), {"tool_name": tool, "tool_input": tool_input})


@pytest.mark.parametrize("cmd", [
    "curl -s -X POST " + TOKEN + " -d '{}'",
    "curl " + TOKEN.replace("/api/", "/api/w/connect/") + " --json '{}'",
])
def test_raw_paths_are_blocked(call, cmd):
    code, _, err = call("Bash", {"command": cmd})
    assert code == 2, cmd
    assert "canopy-web" in err


def test_the_token_minting_mcp_tool_is_blocked(call):
    code, _, err = call(MCP + "mint_agent_canopy_token", {"slug": "lex"})
    assert code == 2 and "canopy token" in err


@pytest.mark.parametrize("cmd", [
    "curl -s " + TOKEN,                                   # a read
    "curl -s " + VAULT,                                   # not this rail's
    "grep -rn 'canopy-token' docs/",
])
def test_reads_and_searches_pass(call, cmd):
    assert call("Bash", {"command": cmd})[0] == 0, cmd
