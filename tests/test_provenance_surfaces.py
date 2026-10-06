"""Provenance beyond `canopy_web.call` (#768): the canopy-web MCP headersHelper, the
post_tool_use hook's direct POST, and the uploaders that build their own requests.

#768 put the headers on the CLI's shared client. These are the other doors to
canopy-web a session uses — the MCP server (remote, but canopy owns its
`headersHelper`) is the one an agent calls most, so without it a dispatch through
`enqueue_turn` still arrived anonymous.
"""
import importlib.util
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "plugins/canopy/scripts/canopy-web-mcp-headers.js"
TURN = "55555555-0000-4000-8000-000000000005"
SESS = "66666666-0000-4000-8000-000000000006"
TURN_ENV = "11111111-0000-4000-8000-000000000001"
CLAUDE = "77777777-0000-4000-8000-000000000007"
TASK = "dispatch-x1"


def _helper(home, cwd, env):
    e = {"PATH": os.environ["PATH"], "HOME": str(home), **env}
    r = subprocess.run(["node", str(HELPER)], cwd=str(cwd), env=e, capture_output=True,
                       text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_the_mcp_helper_sends_the_laptop_parent_beside_the_bearer(tmp_path):
    home = tmp_path / "home"
    d = home / ".canopy" / "caller" / "by-task"
    d.mkdir(parents=True)
    (d / f"{TASK}.json").write_text(json.dumps({"turn_id": TURN, "session_id": SESS}))
    wt = home / "emdash" / "worktrees" / "hal-0ceb29c5" / f"emdash-{TASK}-q7r2k"
    wt.mkdir(parents=True)
    h = _helper(home, wt, {"CANOPY_WEB_PAT": "pat", "CLAUDE_CODE_SESSION_ID": CLAUDE})
    assert h["Authorization"] == "Bearer pat"
    assert h["X-Canopy-Client"].startswith("canopy-mcp/")
    assert h["X-Canopy-Parent-Turn"] == TURN
    assert h["X-Canopy-Parent-Session"] == SESS
    # the task only located the record; nothing runner-specific is sent
    assert "X-Canopy-Parent-Task" not in h and "X-Canopy-Parent-Host" not in h
    assert h["X-Canopy-Claude-Session"] == CLAUDE


def test_the_mcp_helper_prefers_the_cloud_runners_env(tmp_path):
    h = _helper(tmp_path, tmp_path, {"CANOPY_WEB_PAT": "pat", "CANOPY_TURN_ID": TURN_ENV,
                                     "CANOPY_SESSION_ID": SESS})
    assert h["X-Canopy-Parent-Turn"] == TURN_ENV and h["X-Canopy-Parent-Session"] == SESS


def test_the_mcp_helper_drops_garbage(tmp_path):
    h = _helper(tmp_path, tmp_path, {"CANOPY_WEB_PAT": "pat", "CANOPY_TURN_ID": "x\ny"})
    assert "X-Canopy-Parent-Turn" not in h


def test_the_mcp_helper_sends_nothing_without_a_token(tmp_path):
    # No bearer → {} exactly, so the 401 still leads to the browser sign-in.
    assert _helper(tmp_path, tmp_path, {"CANOPY_TURN_ID": TURN_ENV}) == {}


def test_every_direct_uploader_attaches_provenance():
    # These build their own urllib requests instead of going through canopy_web.call.
    for rel in ("scripts/ddd/upload.py", "scripts/ddd/review.py",
                "scripts/share-session/upload.py", "scripts/walkthrough-share/upload.py"):
        src = (ROOT / rel).read_text()
        assert "provenance_headers()" in src, rel
