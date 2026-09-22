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
