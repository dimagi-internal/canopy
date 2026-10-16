"""The fleet rail on direct Slack history reads (gating-baseline `always`, canopy-web#1533).

WHY (Jonathan, 2026-10-16): an agent may go back through a Slack channel only by asking
for permission — "you are never allowed to do this without permission". canopy-web's
channel ingest is the permissioned path (an in-channel approval; canopy reads, the agent
gets a transcript), so every OTHER way to read history is a rail, not a remembered rule.

The NEGATIVE half matters as much: reading canopy-web's own source for the method name,
posting to Slack, canopy-web's own Slack MCP tools, and a human's own session stay free.

Run: uv run pytest tests/test_gating_baseline_slack_history.py
"""
import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parent.parent / "plugins" / "canopy"

# Built at runtime so the literals never sit on a Bash command line the guard scans.
API = "https://slack.com/" + "api/"
HIST, REPL = "conversations." + "history", "conversations." + "replies"


def _guard():
    loader = importlib.machinery.SourceFileLoader(
        "gating_guard_slack", str(PLUGIN / "agent-core" / "gating_guard.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    guard = importlib.util.module_from_spec(spec)
    loader.exec_module(guard)
    return guard


@pytest.fixture
def call(monkeypatch, tmp_path):
    monkeypatch.setenv("CANOPY_PLUGIN_DIR", str(PLUGIN))
    guard = _guard()
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "gating.json").write_text(
        json.dumps({"slug": "hal", "channels": [], "deny": [], "approve": []}))
    return lambda tool, tool_input: guard.run(
        str(tmp_path), {"tool_name": tool, "tool_input": tool_input})


@pytest.mark.parametrize("cmd", [
    f'curl -s -H "Authorization: Bearer $SLACK_BOT_TOKEN" "{API}{HIST}?channel=C123"',
    f"curl -s {API}{REPL} -d channel=C1 -d ts=1.2 -H 'Authorization: Bearer xoxb-1-2'",
    f"cd /x && curl {API}channels.history?channel=C1",
    f"curl {API}search.messages -d query=foo -H 'Authorization: Bearer xoxp-9'",
    "python3 - <<'EOF'\nfrom slack_sdk import WebClient\nc = WebClient(token=t)\n"
    "c.conversations_" + "history(channel='C1')\nEOF",
    "node -e \"const {WebClient}=require('@slack/web-api'); new WebClient(t).conversations." + "history({channel:'C1'})\"",
])
def test_a_direct_history_read_is_blocked_and_names_the_path(call, cmd):
    code, _, err = call("Bash", {"command": cmd})
    assert code == 2, cmd
    assert "canopy slack ingest request" in err and "slack-channel-ingest" in err


@pytest.mark.parametrize("cmd", [
    "grep -n " + HIST + " apps/slack/window.py",                     # reading canopy-web's source
    "rg '" + REPL + "' apps/slack/",
    f"curl -s {API}chat.postMessage -H 'Authorization: Bearer xoxb-1' -d channel=C1 -d text=hi",
    f"curl -s {API}users.info -d user=U1 -H 'Authorization: Bearer xoxb-1'",
    "canopy slack ingest request --agent hal --channel C1 --purpose 'digest'",
    "canopy slack ingest transcript 3f1c --out x.md",
])
def test_everything_else_passes(call, cmd):
    code, _, _ = call("Bash", {"command": cmd})
    assert code == 0, cmd


@pytest.mark.parametrize("tool", [
    "mcp__claude_ai_Slack__slack_read_channel",
    "mcp__claude_ai_Slack__slack_read_thread",
    "mcp__claude_ai_Slack__slack_search_public_and_private",
    "mcp__slack__conversations_history",
    "mcp__plugin_x_slack__get_channel_history",
])
def test_a_slack_connector_history_tool_is_blocked(call, tool):
    code, _, err = call(tool, {"channel_id": "C1"})
    assert code == 2, tool
    assert "canopy slack ingest" in err


@pytest.mark.parametrize("tool", [
    "mcp__plugin_canopy_canopy-web__share_session_to_slack",
    "mcp__plugin_canopy_canopy-web__start_new_slack_thread",
    "mcp__plugin_canopy_canopy-web__get_channel_ingest_transcript",
    "mcp__plugin_canopy_canopy-web__create_channel_ingest",
    "mcp__claude_ai_Slack__slack_send_message",
])
def test_canopy_webs_own_slack_tools_and_sends_pass(call, tool):
    code, _, _ = call(tool, {"channel": "C1"})
    assert code == 0, tool


def test_the_rails_are_agent_only():
    """A human's own session is not bound by the agent consent rule."""
    rails = [r for r in json.loads((PLUGIN / "agent-core" / "gating-baseline.json").read_text())["always"]
             if "slack-channel-ingest" in r.get("message", "")]
    assert len(rails) == 3 and all(r.get("agent_only") for r in rails)


def test_a_humans_own_session_is_not_railed(monkeypatch):
    monkeypatch.setenv("CANOPY_PLUGIN_DIR", str(PLUGIN))
    guard = _guard()
    mine = lambda rails: [r for r in rails if "slack-channel-ingest" in r.get("message", "")]  # noqa: E731
    assert len(mine(guard.baseline_rails({}, "hal", agent=True))) == 3
    assert mine(guard.baseline_rails({}, "", agent=False)) == []
