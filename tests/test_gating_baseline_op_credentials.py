"""The fleet 1Password-credential-store rails (agent-core/gating-baseline.json `always`).

WHY THIS FILE EXISTS (2026-10-15). Debugging a desktop-app sign-in, Ada `cat`-ed
~/.config/op/config through a sed meant to redact long tokens. The account's Secret Key
(`accountKey`) is shorter than the filter's cutoff, so it printed in full into the
transcript and Jonathan had to rotate it. A redaction filter is the model choosing to be
careful; the rail removes the read.

The NEGATIVE half matters as much: `op` itself (whoami, account list, the provisioning
bins) and a repo grep for the path string must pass, or the rail blocks its own remedy.

Run: uv run pytest tests/test_gating_baseline_op_credentials.py
"""
import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parent.parent / "plugins" / "canopy"
BASELINE = PLUGIN / "agent-core" / "gating-baseline.json"
RAILS = [r for r in json.loads(BASELINE.read_text())["always"]
         if "2BUA8C4S2C" in r.get("pattern", "")]

# Built at runtime so the literals never sit on a Bash command line the guard scans.
CFG = "/.config/" + "op"
GROUP = "/Library/Group Containers/2BUA8C4S2C.com.1password"


@pytest.fixture
def call(monkeypatch, tmp_path):
    """Through gating_guard itself, for an agent that mounts no channel at all."""
    monkeypatch.setenv("CANOPY_PLUGIN_DIR", str(PLUGIN))
    loader = importlib.machinery.SourceFileLoader(
        "gating_guard_op", str(PLUGIN / "agent-core" / "gating_guard.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    guard = importlib.util.module_from_spec(spec)
    loader.exec_module(guard)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "gating.json").write_text(
        json.dumps({"slug": "eva", "channels": [], "deny": [], "approve": []}))
    return lambda tool, tool_input: guard.run(
        str(tmp_path), {"tool_name": tool, "tool_input": tool_input})


def test_both_rails_exist_and_name_the_remedy():
    assert len(RAILS) == 2, "one shell rail + one file-tool rail"
    for r in RAILS:
        assert "Secret Key".lower() in r["message"].lower()
        assert "OP_BIOMETRIC_UNLOCK_ENABLED" in r["message"]


@pytest.mark.parametrize("cmd", [
    "cat ~" + CFG + "/config",                                   # the 2026-10-15 leak
    "cat ~" + CFG + "/config | sed 's/x/y/' | head -20",
    'cat "$HOME' + CFG + '/config"',
    "cat ${HOME}" + CFG + "/config",
    "head /Users/haldimagi" + CFG + "/config",
    "ls -la ~" + CFG,
    "python3 -c 'print(1)' < ~" + CFG + "/config",
    "cp /home/ubuntu" + CFG + "/config /tmp/x",
    "cat ~/.op/config",
    "grep -o developers ~" + GROUP.replace(" ", "\\ ") + "/settings.json",
    'ls "/Users/haldimagi' + GROUP + '"',
])
def test_reading_the_store_from_a_shell_is_blocked(call, cmd):
    code, _, err = call("Bash", {"command": cmd})
    assert code == 2, cmd
    assert "eva" in err and "Secret Key".lower() in err.lower()


@pytest.mark.parametrize("tool,tool_input", [
    ("Read", {"file_path": "/Users/haldimagi" + CFG + "/config"}),
    ("Grep", {"pattern": "accountKey", "path": "/home/ubuntu" + CFG}),
    ("Read", {"file_path": "/Users/jj" + GROUP + "/Data/settings/settings.json"}),
])
def test_reading_the_store_with_file_tools_is_blocked(call, tool, tool_input):
    assert call(tool, tool_input)[0] == 2


@pytest.mark.parametrize("cmd", [
    "op whoami --account dimagi.1password.com",
    "op account list",
    "env -u OP_SERVICE_ACCOUNT_TOKEN OP_BIOMETRIC_UNLOCK_ENABLED=true op vault list",
    "bin/ada-vault-provision --slug lex --vault Agent-Lex",
    'grep -rn "' + CFG.lstrip("/") + '" docs/',
    "git grep -n 'config/op' origin/main",
    "cat config/gating.json",
])
def test_op_itself_and_repo_greps_pass(call, cmd):
    assert call("Bash", {"command": cmd})[0] == 0, cmd


def test_unrelated_file_reads_pass(call):
    assert call("Read", {"file_path": "/Users/haldimagi/repo/config/gating.json"})[0] == 0
