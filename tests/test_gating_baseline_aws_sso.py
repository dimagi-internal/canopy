"""The fleet `aws sso login` rail (agent-core/gating-baseline.json `always`).

WHY THIS FILE EXISTS (2026-10-09). An expired AWS SSO session used to be fixable only
by a human at that box's terminal, so agents learned to stop and ask. `canopy aws
login` now pushes a tap-to-approve alert to the runner owner's phone, saying who is
asking, on which runner, and why. The rail turns every raw `aws sso login` into that
command, for every agent, without each one having to have read about it.

The NEGATIVE half matters most: the remedy (`canopy aws login`) and a grep for the old
string must both pass, or the rail blocks its own fix and every search for it.

Run: uv run pytest tests/test_gating_baseline_aws_sso.py
"""
import json
import re
from pathlib import Path

import pytest

BASELINE = Path(__file__).parent.parent / "plugins" / "canopy" / "agent-core" / "gating-baseline.json"
ALWAYS = json.loads(BASELINE.read_text())["always"]
RAILS = [r for r in ALWAYS if r.get("tool") == "Bash" and "sso" in r.get("pattern", "")]

# Mirrors agent-core/gating_guard.py::_STATEMENT_SPLIT — this rail is per_statement.
_SPLIT = re.compile(r"[\n;]|&&|\|\||[|&]")


def blocks(cmd: str) -> bool:
    stmts = [s.strip() for s in _SPLIT.split(cmd) if s.strip()]
    return any(re.search(r["pattern"], s) for r in RAILS for s in stmts)


def test_the_rail_exists():
    assert len(RAILS) == 1, "exactly one aws-sso rail in `always`"


@pytest.mark.parametrize("cmd", [
    "aws sso login --profile labs",
    "aws sso login --profile labs --use-device-code --no-browser",
    "aws sso login",
    "AWS_PROFILE=labs aws sso login",
    "aws sts get-caller-identity --profile labs || aws sso login --profile labs",
    "cd ~/repo && aws sso login --profile labs",
    "if ! aws sts get-caller-identity; then aws sso login --profile labs; fi",
])
def test_raw_sso_login_is_blocked(cmd):
    assert blocks(cmd), cmd


@pytest.mark.parametrize("cmd", [
    # the remedy — blocking it would be self-defeating
    'canopy aws login --profile labs --reason "confirm the 5xx cause"',
    'uv run canopy aws login --profile labs --reason "x"',
    # searching for, or quoting, the old string
    'grep -rn "aws sso login" skills/',
    "git grep -n 'aws sso login' origin/main",
    'echo "run aws sso login if you must"',
    # first-time profile setup and reads are different verbs
    "aws configure sso --profile labs",
    "aws sts get-caller-identity --profile labs",
    "aws sso logout",
    # reading its usage
    "aws sso login --help",
])
def test_the_remedy_searches_and_other_verbs_pass(cmd):
    assert not blocks(cmd), cmd


def test_the_message_names_the_remedy_and_requires_a_reason():
    """Rails, not gates: a block that doesn't say what to do instead just stalls the turn."""
    (r,) = RAILS
    assert "canopy aws login" in r["message"]
    assert "--reason" in r["message"]
    assert "run_in_background" in r["message"]


def test_the_real_guard_blocks_it_for_an_agent_with_no_channels(monkeypatch, tmp_path):
    """Through gating_guard itself, not this file's mirror of its splitter: `always`
    must reach an agent that mounts no channel at all, and {slug} must be filled in."""
    import importlib.machinery
    import importlib.util

    plugin = Path(__file__).parent.parent / "plugins" / "canopy"
    monkeypatch.setenv("CANOPY_PLUGIN_DIR", str(plugin))
    loader = importlib.machinery.SourceFileLoader("gating_guard", str(plugin / "agent-core" / "gating_guard.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    guard = importlib.util.module_from_spec(spec)
    loader.exec_module(guard)

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "gating.json").write_text(
        json.dumps({"slug": "eva", "channels": [], "deny": [], "approve": []}))
    call = lambda cmd: guard.run(str(tmp_path), {"tool_name": "Bash", "tool_input": {"command": cmd}})

    code, _, err = call("aws sso login --profile labs")
    assert code == 2
    assert "eva is asking" in err and "canopy aws login" in err
    assert call('canopy aws login --profile labs --reason "x"')[0] == 0
