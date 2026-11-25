"""The canopy-web MCP headers helper: a confined session sends its caller token,
never the owner's PAT. Runs the real script under node."""
import json
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "plugins/canopy/scripts/canopy-web-mcp-headers.js"
PAT = "canopy_pat_OWNER"


def _run(home, cwd, env=None):
    e = {"PATH": os.environ["PATH"], "HOME": str(home), **(env or {})}
    r = subprocess.run(["node", str(SCRIPT)], cwd=str(cwd), env=e, capture_output=True,
                       text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout).get("Authorization", "")


def _home(tmp_path):
    home = tmp_path / "home"
    (home / ".claude" / "canopy").mkdir(parents=True)
    (home / ".claude" / "canopy" / "workbench-token").write_text(PAT)
    (home / ".canopy" / "profiles").mkdir(parents=True)
    return home


def test_an_ordinary_session_sends_the_pat(tmp_path):
    home = _home(tmp_path)
    w = tmp_path / "ace-c89535f9" / "emdash-c-daily-6886-rn860"
    w.mkdir(parents=True)
    assert _run(home, w) == f"Bearer {PAT}"


def test_a_cx_worktree_sends_its_caller_token(tmp_path):
    home = _home(tmp_path)
    (home / ".canopy/profiles/cx-re-pay-4a4e.json").write_text(json.dumps({"mcp_token": "cct_abc"}))
    w = tmp_path / "ace-c89535f9" / "emdash-cx-re-pay-4a4e-rn860"
    (w / "sub").mkdir(parents=True)
    assert _run(home, w) == "Bearer cct_abc"
    assert _run(home, w / "sub") == "Bearer cct_abc"          # from a subdirectory too


def test_a_cx_worktree_without_a_profile_never_sends_the_pat(tmp_path):
    home = _home(tmp_path)
    w = tmp_path / "ace-c89535f9" / "emdash-cx-re-pay-4a4e-rn860"
    w.mkdir(parents=True)
    got = _run(home, w)
    assert got.startswith("Bearer cct_missing") and PAT not in got


def test_canopy_profile_env_wins(tmp_path):
    home = _home(tmp_path)
    prof = tmp_path / "p.json"
    prof.write_text(json.dumps({"mcp_token": "cct_cloud"}))
    assert _run(home, tmp_path, {"CANOPY_PROFILE": str(prof)}) == "Bearer cct_cloud"


def test_canopy_profile_env_without_a_token_never_sends_the_pat(tmp_path):
    home = _home(tmp_path)
    prof = tmp_path / "p.json"
    prof.write_text(json.dumps({"capability": {}}))
    assert PAT not in _run(home, tmp_path, {"CANOPY_PROFILE": str(prof)})
    assert PAT not in _run(home, tmp_path, {"CANOPY_PROFILE": str(tmp_path / "missing.json")})


# Claude Code runs the helper from the PLUGIN's directory, not the session's
# worktree, so the cwd never names the cx- task. emdash exports EMDASH_TASK_NAME
# into the session and it reaches the helper (not secret-looking). A cwd-only
# lookup left every laptop confined session with no token at all (a 401 and
# OAuth placeholders, no site tools), or, with no CANOPY_AGENT, the owner's PAT.

def test_a_cx_task_sends_its_caller_token_when_the_helper_runs_outside_the_worktree(tmp_path):
    home = _home(tmp_path)
    (home / ".canopy/profiles/cx-re-pay-4a4e.json").write_text(json.dumps({"mcp_token": "cct_abc"}))
    plugin_root = tmp_path / "plugin-root"
    plugin_root.mkdir()
    for extra in ({}, {"CANOPY_AGENT": "connect-helper"}):
        assert _run(home, plugin_root, {"EMDASH_TASK_NAME": "cx-re-pay-4a4e", **extra}) == "Bearer cct_abc"


def test_a_cx_task_without_a_profile_never_sends_the_pat_from_outside_the_worktree(tmp_path):
    home = _home(tmp_path)
    plugin_root = tmp_path / "plugin-root"
    plugin_root.mkdir()
    got = _run(home, plugin_root, {"EMDASH_TASK_NAME": "cx-re-pay-4a4e"})
    assert got.startswith("Bearer cct_missing") and PAT not in got


def test_a_non_cx_task_name_is_not_confined(tmp_path):
    home = _home(tmp_path)
    (home / ".canopy/profiles/cx-re-pay-4a4e.json").write_text(json.dumps({"mcp_token": "cct_abc"}))
    assert _run(home, tmp_path, {"EMDASH_TASK_NAME": "c-daily-6886"}) == f"Bearer {PAT}"
