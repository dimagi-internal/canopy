"""The canopy-web MCP headers helper: a colleague's FULL-profile session sends the
asker's caller token, never the box's PAT (canopy-web#1332). Runs the real script under node."""
import json
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "plugins/canopy/scripts/canopy-web-mcp-headers.js"
PAT = "canopy_pat_BOX"
TURN = "3f2b8c1e-0000-4000-8000-000000000003"


def _run(home, cwd, env=None):
    e = {"PATH": os.environ["PATH"], "HOME": str(home), "CANOPY_WEB_PAT": PAT, **(env or {})}
    r = subprocess.run(["node", str(SCRIPT)], cwd=str(cwd), env=e, capture_output=True, text=True,
                       timeout=20)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout).get("Authorization", "")


def _token(home, kind, name, value="cct_asker"):
    d = home / ".canopy" / "scoped" / kind
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.token").write_text(value)


def test_a_cloud_scoped_turn_sends_its_token_not_the_pat(tmp_path):
    home = tmp_path / "home"
    _token(home, "turn", TURN)
    assert _run(home, tmp_path, {"CANOPY_SCOPED_TURN": TURN}) == "Bearer cct_asker"


def test_a_cloud_scoped_turn_without_its_file_never_sends_the_pat(tmp_path):
    home = tmp_path / "home"
    got = _run(home, tmp_path, {"CANOPY_SCOPED_TURN": TURN})
    assert got.startswith("Bearer cct_missing") and PAT not in got


def test_a_hostile_scoped_turn_id_never_sends_the_pat(tmp_path):
    got = _run(tmp_path / "home", tmp_path, {"CANOPY_SCOPED_TURN": "../../x"})
    assert PAT not in got


def test_a_laptop_scoped_session_sends_its_token_from_the_worktree(tmp_path):
    home = tmp_path / "home"
    _token(home, "task", "ace-chat-x1")
    wt = home / "emdash" / "worktrees" / "ace" / "emdash" / "ace-chat-x1-q7r2k"
    wt.mkdir(parents=True)
    assert _run(home, wt) == "Bearer cct_asker"


def test_a_laptop_session_with_no_scoped_file_is_unchanged(tmp_path):
    home = tmp_path / "home"
    wt = home / "emdash" / "worktrees" / "ace" / "emdash" / "ace-chat-x1-q7r2k"
    wt.mkdir(parents=True)
    assert _run(home, wt) == f"Bearer {PAT}"


def test_a_non_token_file_is_not_a_scoped_session_on_a_laptop(tmp_path):
    home = tmp_path / "home"
    _token(home, "task", "ace-chat-x1", "not-a-caller-token")
    wt = home / "emdash" / "worktrees" / "ace" / "emdash" / "ace-chat-x1-q7r2k"
    wt.mkdir(parents=True)
    assert _run(home, wt) == f"Bearer {PAT}"
