"""Read rails around credential paths (gating-baseline `always`; runner isolation units §7).

Defence in depth INSIDE a unit: one agent's turn must not read another agent's env file,
another turn's /run/canopy/ env, the box's device key, another agent's gog home, the shared
gog dir or /etc/canopy-runner/. The turn's OWN env (its ~/.<slug>/.env on a laptop, its own
/run/canopy/<uid>/<turn>/) stays readable, and a person's own session is not railed.

Run: uv run pytest tests/test_gating_baseline_credential_reads.py
"""
import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parent.parent / "plugins" / "canopy"
OWN = "/run/canopy/1001/turn-A/env"


def _guard():
    loader = importlib.machinery.SourceFileLoader(
        "gating_guard_creds", str(PLUGIN / "agent-core" / "gating_guard.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    guard = importlib.util.module_from_spec(spec)
    loader.exec_module(guard)
    return guard


@pytest.fixture
def call(monkeypatch, tmp_path):
    """A turn of agent `hal`, whose runner gave it /run/canopy/1001/turn-A/env."""
    monkeypatch.setenv("CANOPY_PLUGIN_DIR", str(PLUGIN))
    monkeypatch.setenv("CANOPY_AGENT_ENV_FILE", OWN)
    guard = _guard()
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "gating.json").write_text(
        json.dumps({"slug": "hal", "channels": [], "deny": [], "approve": []}))
    return lambda tool, tool_input: guard.run(
        str(tmp_path), {"tool_name": tool, "tool_input": tool_input})


DENIED = [
    # another agent's env file
    ("Read", {"file_path": "/home/cu-dimagi/.eva/.env"}),
    ("Read", {"file_path": "/Users/jj/.eva/.env"}),
    ("Bash", {"command": "cat ~/.eva/.env"}),
    ("Bash", {"command": "grep CANOPY_WEB_PAT $HOME/.eva/.env | head -1"}),
    ("Read", {"file_path": "/home/u/.hal/../.eva/.env"}),
    # another turn's tmpfs env
    ("Read", {"file_path": "/run/canopy/1001/turn-B/env"}),
    ("Bash", {"command": "ls /run/canopy/1001/"}),
    ("Bash", {"command": "cat /run/canopy/1001/turn-A/../turn-B/env"}),
    ("Glob", {"path": "/run/canopy", "pattern": "**/env"}),
    # the device key, gog homes, the unit config
    ("Read", {"file_path": "/home/cu-dimagi/.canopy/device-key/sw.pem"}),
    ("Bash", {"command": "cat ~/.canopy/device-key/ecdh.pem"}),
    ("Read", {"file_path": "/home/cu-dimagi/.gog/eva/keyring/token"}),
    ("Bash", {"command": "ls ~/.gog"}),
    ("Glob", {"path": "/home/u/.gog", "pattern": "*"}),
    ("Read", {"file_path": "/home/ubuntu/.config/gogcli/config.json"}),
    ("Bash", {"command": "cat /etc/canopy-runner/dimagi.env"}),
    ("Bash", {"command": "xxd /etc/canopy-runner/dimagi.gog-secret"}),
    ("Grep", {"path": "/etc/canopy-runner", "pattern": "TOKEN"}),
]


@pytest.mark.parametrize("tool,tool_input", DENIED)
def test_other_credentials_are_denied(call, tool, tool_input):
    code, _, err = call(tool, tool_input)
    assert code == 2, (tool, tool_input)
    assert "not its own" in err


def test_another_turns_env_is_denied_when_the_turn_has_no_file(monkeypatch, tmp_path):
    monkeypatch.setenv("CANOPY_PLUGIN_DIR", str(PLUGIN))
    monkeypatch.delenv("CANOPY_AGENT_ENV_FILE", raising=False)
    guard = _guard()
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "gating.json").write_text(json.dumps({"slug": "hal", "channels": []}))
    code, _, _ = guard.run(str(tmp_path), {"tool_name": "Read",
                                           "tool_input": {"file_path": "/run/canopy/1/t/env"}})
    assert code == 2


ALLOWED = [
    ("Read", {"file_path": "/home/u/.hal/.env"}),            # its own legacy env
    ("Bash", {"command": "cat ~/.hal/.env.tpl"}),
    ("Read", {"file_path": "/home/u/.eva/.env.tpl"}),          # a template, no values
    ("Read", {"file_path": OWN}),                              # its own turn file
    ("Bash", {"command": f"canopy cred env --slug hal --out {OWN}"}),
    ("Read", {"file_path": "/home/u/.gog/hal/config.json"}),   # its own gog home
    ("Read", {"file_path": "/home/u/work/hal/.env"}),          # a repo's .env
    ("Bash", {"command": "ls /run/canopy-kms/"}),
    ("Bash", {"command": "grep -rn '/etc/canopy-runner' docs/"}),
    ("Edit", {"file_path": "/home/u/work/canopy/docs/architecture/session-identity.md"}),
]


@pytest.mark.parametrize("tool,tool_input", ALLOWED)
def test_own_credentials_and_ordinary_paths_pass(call, tool, tool_input):
    assert call(tool, tool_input)[0] == 0, (tool, tool_input)


def test_a_persons_own_session_is_not_railed(monkeypatch, tmp_path):
    monkeypatch.setenv("CANOPY_PLUGIN_DIR", str(PLUGIN))
    guard = _guard()
    plain = tmp_path / "plain"
    plain.mkdir()
    payload = {"tool_name": "Read", "tool_input": {"file_path": "/Users/jj/.eva/.env"},
               "cwd": str(plain)}
    assert guard.run_session(payload, env={"CLAUDE_PROJECT_DIR": str(plain)})[0] == 0


def test_the_session_hook_routes_file_reads_to_the_guard():
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())
    matchers = [h["matcher"] for h in hooks["hooks"]["PreToolUse"]
                if any("gating_guard.py" in x.get("command", "") for x in h["hooks"])]
    assert matchers and all(t in matchers[0].split("|") for t in ("Read", "Grep", "Glob"))
