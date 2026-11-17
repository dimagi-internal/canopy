"""The MCP headers helper sends the chat's key (X-Canopy-Chat-Key) beside the
bearer, so canopy's page tools answer about THIS chat — the bearer alone is the
agent's login, which is in every chat the agent is in.

The runner leaves the key where the helper can find it without a secret-looking
variable, which Claude Code strips from the helper's environment: by chat id on a
cloud box (CANOPY_CHAT_SESSION), by emdash task (from the worktree path) on a laptop.
"""
import json
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "plugins/canopy/scripts/canopy-web-mcp-headers.js"
CHAT = "eb742bd8-0000-4000-8000-00000000000a"


def _run(home, cwd, env=None):
    e = {"PATH": os.environ["PATH"], "HOME": str(home), "CANOPY_WEB_PAT": "canopy_pat_ECHO", **(env or {})}
    r = subprocess.run(["node", str(SCRIPT)], cwd=str(cwd), env=e, capture_output=True, text=True,
                       timeout=20)
    assert r.returncode == 0, r.stderr
    # Provenance headers (X-Canopy-Client, -Parent-*) ride along beside the bearer;
    # they are pinned in test_provenance.py — here only auth + chat key matter.
    return {k: v for k, v in json.loads(r.stdout).items()
            if k in ("Authorization", "X-Canopy-Chat-Key")}


def _key(home, kind, name, value):
    d = home / ".canopy" / "chat" / kind
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.key").write_text(value)


def test_a_cloud_chat_turn_sends_its_chats_key(tmp_path):
    home = tmp_path / "home"
    _key(home, "chat", CHAT, "chk_cloud")
    h = _run(home, tmp_path, {"CANOPY_CHAT_SESSION": CHAT})
    assert h == {"Authorization": "Bearer canopy_pat_ECHO", "X-Canopy-Chat-Key": "chk_cloud"}


def test_a_laptop_chat_session_sends_its_chats_key_from_the_worktree(tmp_path):
    home = tmp_path / "home"
    wt = home / "emdash" / "worktrees" / "echo" / "emdash" / "echo-chat-x1-q7r2k"
    wt.mkdir(parents=True)
    _key(home, "task", "echo-chat-x1", "chk_laptop")
    assert _run(home, wt)["X-Canopy-Chat-Key"] == "chk_laptop"


def test_a_session_that_drives_no_chat_sends_no_key(tmp_path):
    home = tmp_path / "home"
    _key(home, "chat", CHAT, "chk_someone_elses")
    h = _run(home, tmp_path)
    assert h == {"Authorization": "Bearer canopy_pat_ECHO"}


def test_a_laptop_chat_session_sends_its_key_when_the_helper_runs_outside_the_worktree(tmp_path):
    """Claude Code runs the headers helper away from the session's worktree; emdash
    names the task in EMDASH_TASK_NAME. The key must be found from that, exactly as
    the provenance headers are — a cwd-only lookup sent no key from ANY laptop chat."""
    home = tmp_path / "home"
    elsewhere = tmp_path / "not-the-worktree"
    elsewhere.mkdir()
    _key(home, "task", "c-can-you-look-at-all-my-eva-a81a", "chk_laptop")
    h = _run(home, elsewhere, {"EMDASH_TASK_NAME": "c-can-you-look-at-all-my-eva-a81a"})
    assert h["X-Canopy-Chat-Key"] == "chk_laptop"


def test_a_task_name_with_no_key_file_sends_no_key(tmp_path):
    home = tmp_path / "home"
    _key(home, "task", "someone-elses-task", "chk_other")
    h = _run(home, tmp_path, {"EMDASH_TASK_NAME": "c-mine-1234"})
    assert "X-Canopy-Chat-Key" not in h
