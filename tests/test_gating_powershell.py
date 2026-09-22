"""Every shell rail applies to PowerShell too (agent-core/gating_guard.py SHELL_TOOLS).

WHY THIS FILE EXISTS (2026-09-22). On Windows, Claude Code offers a PowerShell tool beside
Bash, and every rail in the fleet applied to Bash alone. Shayoni Mazumdar found it on fizzy:
`gog gmail send`, a Salesforce `curl -X POST` and `python sf-refresh.py` were blocked from Bash
and allowed from PowerShell. It was three layers deep, and fixing any one alone does nothing:

  1. the factory matcher never routed PowerShell to the hook (tested in test_factory /
     test_agent_doctor),
  2. `subject_for` sent PowerShell down the JSON-of-input path, so the command arrived as
     `{"command": "gog gmail send …"}` and every rail anchored on `(?:^|[\\n;&|(])` missed on
     the leading quote — she fixed the matcher alone first and her tests still failed,
  3. every rail says `"tool": "Bash"`, which `matches` compared exactly.

Her suite ran 11 cases across both shells, harmless commands included; the shape is kept here
(before: 6/11, all five failures PowerShell). The negative half matters as much as the positive:
a shell rail that starts blocking `Get-ChildItem` is a worse bug than the one fixed.

This drives the real engine (`run`, against the real shipped baseline) rather than a mirror.
"""
import importlib.util
import json
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parent.parent / "plugins" / "canopy"
AGENT_CORE = PLUGIN / "agent-core"

_spec = importlib.util.spec_from_file_location("gating_guard", AGENT_CORE / "gating_guard.py")
engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(engine)

# An agent-local curl rail of the kind fizzy carries — the thing PowerShell's cmdlets bypassed.
LOCAL_SF_RAIL = {
    "tool": "Bash",
    "pattern": r"(?:^|[\n;&|(])\s*curl(?:\.exe)?\b[^\n]*-X\s*(?:POST|PATCH|DELETE)\b[^\n]*salesforce",
    "message": "BLOCKED: Salesforce writes go through bin/sf-write.",
}
LOCAL_SCRIPT_RAIL = {
    "tool": "Bash",
    "pattern": r"\bsf-refresh\.py\b",
    "message": "BLOCKED: run sf-refresh through the wrapper.",
}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("CANOPY_PLUGIN_DIR", str(PLUGIN))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "gating.json").write_text(json.dumps({
        "slug": "fizzy", "channels": ["email", "gws"],
        "deny": [LOCAL_SF_RAIL, LOCAL_SCRIPT_RAIL], "approve": []}))
    return tmp_path


def code(repo, tool, command):
    rc, _out, _err = engine.run(str(repo), {"tool_name": tool, "tool_input": {"command": command}})
    return rc


BLOCKED_IN_BOTH = [
    "gog gmail send --to a@b.c --subject x --body y",
    "cd C:\\Projects\\fizzy; gog gmail reply --to a@b.c",
    "curl -X POST https://dimagi.my.salesforce.com/services/data/v60.0/sobjects/Lead -d '{}'",
    "python sf-refresh.py --all",
    "canopy email send --account someone@else.com --to a@b.c",
    "gog sheets create 'roster'",
]

ALLOWED_IN_BOTH = [
    "git status",
    "gog gmail search 'in:inbox is:unread'",
    "curl https://dimagi.my.salesforce.com/services/data/v60.0/sobjects/Lead",
    "canopy email send --to a@b.c --body-file body.md",
    "gog sheets create 'roster' --parent 1AbC",
]


@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
@pytest.mark.parametrize("cmd", BLOCKED_IN_BOTH)
def test_shell_rails_block_from_every_shell(repo, tool, cmd):
    assert code(repo, tool, cmd) == 2


@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
@pytest.mark.parametrize("cmd", ALLOWED_IN_BOTH)
def test_reads_and_sanctioned_paths_stay_free_in_every_shell(repo, tool, cmd):
    assert code(repo, tool, cmd) == 0


def test_powershell_subject_is_the_command_line_not_json():
    """Layer 2: the JSON path put a `"` in front of the command and every anchor missed."""
    assert engine.subject_for("PowerShell", {"command": "gog gmail send"}) == "gog gmail send"


# --------------------------------------------------------------------------------------
# PowerShell's own HTTP-write spellings — the curl rails cannot see these
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "Invoke-RestMethod -Uri https://x.my.salesforce.com/services/data/v60.0/sobjects/Lead -Method Post -Body $b",
    "Invoke-WebRequest https://api.example.com/items -Method PATCH -Body '{}'",
    "irm https://api.example.com/items/3 -Method Delete",
    "iwr -Uri https://api.example.com -Method 'Put' -Body $b",
    "$r = Invoke-RestMethod -Uri $u -Me POST -Body $j",
    "Invoke-RestMethod -Uri $u -Method:Post -Body $j",
    "Invoke-RestMethod -Uri $u -CustomMethod PURGE",
    "Get-Item x; irm $u -Method post",
])
def test_powershell_http_write_cmdlets_are_blocked(repo, cmd):
    assert code(repo, "PowerShell", cmd) == 2


@pytest.mark.parametrize("cmd", [
    "Invoke-RestMethod -Uri https://api.example.com/items",
    "irm https://api.example.com/items -Method Get",
    "Invoke-WebRequest https://example.com -Method Head",
    "Get-ChildItem -Recurse | Select-Object Name",
    "Write-Output 'Invoke-RestMethod is how you POST'",
    "git log --oneline -5; Get-Content README.md",
])
def test_powershell_reads_are_not_blocked(repo, cmd):
    assert code(repo, "PowerShell", cmd) == 0


def test_the_cmdlet_rail_is_powershell_only(repo):
    """Bash has no Invoke-RestMethod; a Bash command that merely names it is not a write."""
    assert code(repo, "Bash", "echo 'irm $u -Method Post'") == 0


# --------------------------------------------------------------------------------------
# bash_only — rails about the SHELL, not the command
# --------------------------------------------------------------------------------------

def test_zsh_equals_rail_stays_bash_only(repo):
    """EQUALS expansion is zsh behaviour. In PowerShell `echo =====` just prints."""
    assert code(repo, "Bash", "echo =====; git status") == 2
    assert code(repo, "PowerShell", "echo =====; git status") == 0


def test_bash_only_opts_a_rule_out_of_the_shell_family():
    rule = {"tool": "Bash", "bash_only": True, "pattern": "x"}
    assert engine.tool_applies(rule, "Bash")
    assert not engine.tool_applies(rule, "PowerShell")
    assert engine.tool_applies({"tool": "Bash"}, "PowerShell")
    assert not engine.tool_applies({"tool": "PowerShell"}, "Bash")
    assert not engine.tool_applies({"tool": "Bash"}, "Edit")


def test_every_bash_rail_in_the_baseline_reaches_powershell_unless_marked():
    """Guards the default: a new shell rail covers PowerShell without anyone remembering to."""
    base = json.loads((AGENT_CORE / "gating-baseline.json").read_text())
    rails = list(base["always"]) + [r for rs in base["channels"].values() for r in rs]
    for r in rails:
        if r.get("tool") == "Bash":
            assert engine.tool_applies(r, "PowerShell") is not bool(r.get("bash_only"))
