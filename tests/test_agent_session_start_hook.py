"""agent_session_start: an agent's laptop/emdash session keeps its env current from the Canopy vault.

Pins: inert outside agent sessions; borrowed credentials are staged for a person's own session
but never in a confined caller turn or a runner turn; `cred sync` runs unless the turn has its
own env file; every failure is fail-open and says why.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "plugins/canopy/hooks/agent_session_start.py"


def _fake_canopy(tmp_path, sync="unchanged", sync_rc=0):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "canopy-args"
    script = bindir / "canopy"
    script.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\n"
                      f"[ \"$2\" = stage-delegated ] && exit 0\n"
                      f"[ \"$1\" = cred ] && {{ echo \"[canopy cred] $4: {sync}\" >&2; exit {sync_rc}; }}\n"
                      "exit 0\n")
    script.chmod(0o755)
    return str(bindir), log


def _run(tmp_path, *, slug="eva", env_file=True, sync="unchanged", sync_rc=0, turn_id=None):
    bindir, log = _fake_canopy(tmp_path, sync, sync_rc)
    env = {k: v for k, v in os.environ.items()
           if k not in ("CANOPY_AGENT", "CLAUDE_ENV_FILE", "CANOPY_TURN_ID")}
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env["HOME"] = str(home)
    env["PATH"] = bindir + os.pathsep + env["PATH"]
    if slug:
        env["CANOPY_AGENT"] = slug
    if turn_id:
        env["CANOPY_TURN_ID"] = turn_id
    ef = tmp_path / "session.env"
    if env_file:
        env["CLAUDE_ENV_FILE"] = str(ef)
    r = subprocess.run([sys.executable, str(HOOK)], capture_output=True, text=True, env=env, timeout=30)
    return r, ef, log


def test_stages_borrowed_creds_then_syncs(tmp_path):
    r, _, log = _run(tmp_path)
    assert r.returncode == 0
    assert log.read_text().splitlines() == ["agent stage-delegated --slug eva",
                                            "cred sync --agent eva"]
    assert r.stdout == ""                          # an unchanged vault says nothing


def test_inert_outside_an_agent_session(tmp_path):
    r, ef, log = _run(tmp_path, slug="")
    assert r.returncode == 0 and r.stdout == "" and not ef.exists() and not log.exists()


def test_a_vault_change_is_reported_in_one_line(tmp_path):
    r, _, _ = _run(tmp_path, sync="re-injected from canopy-web")
    assert r.returncode == 0 and "credentials changed on canopy-web" in r.stdout


def test_a_failed_sync_is_fail_open_and_says_why(tmp_path):
    r, _, _ = _run(tmp_path, sync="canopy-web unreachable", sync_rc=1)
    assert r.returncode == 0
    assert "may be stale" in r.stdout and "canopy-web unreachable" in r.stdout


def test_a_callers_confined_turn_never_stages_borrowed_creds(tmp_path, monkeypatch):
    monkeypatch.setenv("CANOPY_PROFILE", "caller")
    _, _, log = _run(tmp_path)
    logged = log.read_text() if log.exists() else ""
    assert "stage-delegated" not in logged and "cred sync" not in logged


def test_a_runner_turn_never_stages_borrowed_creds_itself(tmp_path):
    # The cloud runner already staged them with ITS credentials; the hook would ask
    # /credentials/resolve with the agent's own PAT, which 403s (lex, 2026-10-16).
    r, _, log = _run(tmp_path, turn_id="6f1c0000-0000-0000-0000-000000000001")
    assert r.returncode == 0
    assert log.read_text().splitlines() == ["cred sync --agent eva"]


def test_registered_on_session_start():
    hooks = json.loads((ROOT / "plugins/canopy/hooks/hooks.json").read_text())["hooks"]["SessionStart"]
    entry = next(h for m in hooks for h in m["hooks"] if "agent_session_start.py" in h["command"])
    assert not entry.get("async"), "must finish before the first Bash command"
