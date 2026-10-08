"""agent_op_env: an agent's emdash/laptop session gets its OWN 1Password key, like a cloud turn.

Without it `op` falls back to the desktop app, which locks and then hangs an unattended turn
(2026-10-07). Pins: the key lands in CLAUDE_ENV_FILE and nowhere else (never stdout); the hook
is inert outside agent sessions and when a key is already present; every failure is fail-open.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "plugins/canopy/hooks/agent_op_env.py"
KEY = "ops_SECRET_key_value"


def _fake_canopy(tmp_path, out=KEY, rc=0):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "canopy-args"
    script = bindir / "canopy"
    script.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\n"
                      f"[ \"$2\" = stage-delegated ] && exit 0\nprintf '%s\\n' '{out}'\n"
                      f"[ {rc} -ne 0 ] && echo 'no live runner you pair' >&2\nexit {rc}\n")
    script.chmod(0o755)
    return str(bindir), log


def _run(tmp_path, *, slug="eva", have_key=False, out=KEY, rc=0, env_file=True):
    bindir, log = _fake_canopy(tmp_path, out, rc)
    env = {k: v for k, v in os.environ.items() if k not in ("CANOPY_AGENT", "OP_SERVICE_ACCOUNT_TOKEN", "CLAUDE_ENV_FILE")}
    env["PATH"] = bindir + os.pathsep + env["PATH"]
    if slug:
        env["CANOPY_AGENT"] = slug
    if have_key:
        env["OP_SERVICE_ACCOUNT_TOKEN"] = "already"
    ef = tmp_path / "session.env"
    if env_file:
        env["CLAUDE_ENV_FILE"] = str(ef)
    r = subprocess.run([sys.executable, str(HOOK)], capture_output=True, text=True, env=env, timeout=30)
    return r, ef, log


def test_exports_the_agents_own_key_into_the_session_env(tmp_path):
    r, ef, log = _run(tmp_path)
    assert r.returncode == 0
    assert ef.read_text() == f"export OP_SERVICE_ACCOUNT_TOKEN={KEY}\n"
    assert log.read_text().splitlines() == ["agent stage-delegated --slug eva",
                                            "agent op-token --slug eva"]
    assert KEY not in r.stdout + r.stderr          # never into the transcript


def test_inert_outside_an_agent_session(tmp_path):
    r, ef, log = _run(tmp_path, slug="")
    assert r.returncode == 0 and r.stdout == "" and not ef.exists() and not log.exists()


def test_a_key_already_present_is_kept_but_borrowed_creds_are_still_staged(tmp_path):
    # A cloud turn has its key; it still gets what it borrows staged (canopy-web#1291).
    r, ef, log = _run(tmp_path, have_key=True)
    assert r.returncode == 0 and not ef.exists()
    assert log.read_text().splitlines() == ["agent stage-delegated --slug eva"]


def test_a_callers_confined_turn_never_stages_borrowed_creds(tmp_path, monkeypatch):
    monkeypatch.setenv("CANOPY_PROFILE", "caller")
    r, _, log = _run(tmp_path)
    assert "stage-delegated" not in (log.read_text() if log.exists() else "")


def test_failure_is_fail_open_and_says_so(tmp_path):
    r, ef, _ = _run(tmp_path, out="", rc=1)
    assert r.returncode == 0 and not ef.exists()
    assert "no live runner you pair" in r.stdout and "1Password app" in r.stdout


def test_registered_on_session_start():
    hooks = json.loads((ROOT / "plugins/canopy/hooks/hooks.json").read_text())["hooks"]["SessionStart"]
    cmds = [h["command"] for m in hooks for h in m["hooks"]]
    assert any("agent_op_env.py" in c for c in cmds)
    entry = next(h for m in hooks for h in m["hooks"] if "agent_op_env.py" in h["command"])
    assert not entry.get("async"), "must finish before the first Bash command reads CLAUDE_ENV_FILE"
