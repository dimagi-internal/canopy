"""The fleet rails around an agent's vault key and canopy token (gating-baseline `always`).

WHY (2026-10-15). `canopy agent vault` is the one path that mints an agent's vault key and
its own canopy token and stores them without printing either. The raw paths — `op
service-account create`, a curl write to /vault or /canopy-token, and the MCP tools whose
argument or result is the secret — each put one in a transcript. These rails lived only in
Ada's config/gating.json; every agent needs them, so they are baseline now.

The NEGATIVE half: the command itself, Ada's shim to it, and masked reads must pass.

Run: uv run pytest tests/test_gating_baseline_vault_paths.py
"""
import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parent.parent / "plugins" / "canopy"

# Built at runtime so the literals never sit on a Bash command line the guard scans.
SA = "op service-" + "account create"
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
    SA + " canopy-web-lex --vault Agent-Lex:read_items --raw",
    "cd /x && " + SA + " x --vault V:read_items",
    "OP_ACCOUNT=dimagi " + SA + " x --vault V:read_items",
    "curl -X PUT " + VAULT + " --data @key.json",
    "curl -s -X POST " + TOKEN + " -d '{}'",
    "curl " + TOKEN.replace("/api/", "/api/w/connect/") + " --json '{}'",
])
def test_raw_paths_are_blocked_and_name_the_command(call, cmd):
    code, _, err = call("Bash", {"command": cmd})
    assert code == 2, cmd
    assert "canopy agent vault" in err


@pytest.mark.parametrize("tool", ["set_agent_vault", "set_shared_vault",
                                  "mint_agent_canopy_token"])
def test_secret_bearing_mcp_tools_are_blocked(call, tool):
    code, _, err = call(MCP + tool, {"slug": "lex"})
    assert code == 2 and "canopy agent vault" in err


@pytest.mark.parametrize("cmd", [
    "canopy agent vault --slug lex --vault Agent-Lex",
    "canopy agent vault --slug lex --check",
    "bin/ada-vault-provision --slug lex --dry-run",
    "curl -s " + VAULT,                                   # masked read
    "op service-account list",
    "grep -rn 'service-account create' docs/",
])
def test_the_command_reads_and_searches_pass(call, cmd):
    assert call("Bash", {"command": cmd})[0] == 0, cmd


def test_masked_mcp_reads_pass(call):
    assert call(MCP + "get_agent_vault", {"slug": "lex"})[0] == 0
