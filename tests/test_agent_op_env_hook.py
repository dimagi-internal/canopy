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


def _fake_canopy(tmp_path, out=KEY, rc=0, sync="unchanged", sync_rc=0):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "canopy-args"
    script = bindir / "canopy"
    script.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\n"
                      f"[ \"$2\" = stage-delegated ] && exit 0\n"
                      f"[ \"$1\" = cred ] && {{ echo \"key=$OP_SERVICE_ACCOUNT_TOKEN\" >> {log}; "
                      f"echo \"[canopy cred] $4: {sync}\" >&2; exit {sync_rc}; }}\nprintf '%s\\n' '{out}'\n"
                      f"[ {rc} -ne 0 ] && echo 'no live runner you pair' >&2\nexit {rc}\n")
    script.chmod(0o755)
    return str(bindir), log


def _run(tmp_path, *, slug="eva", have_key=False, out=KEY, rc=0, env_file=True,
         sync="unchanged", sync_rc=0, binding=None):
    bindir, log = _fake_canopy(tmp_path, out, rc, sync, sync_rc)
    env = {k: v for k, v in os.environ.items()
           if k not in ("CANOPY_AGENT", "OP_SERVICE_ACCOUNT_TOKEN", "CLAUDE_ENV_FILE",
                        "AGENT_VAULT", "AGENT_SLUG")}
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env["HOME"] = str(home)   # never the real ~/.<slug>/canopy-instance.json
    if binding is not None:
        (home / f".{slug}").mkdir(exist_ok=True)
        (home / f".{slug}" / "canopy-instance.json").write_text(json.dumps(binding))
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
                                            "agent op-token --slug eva",
                                            "cred sync --agent eva", f"key={KEY}"]
    assert KEY not in r.stdout + r.stderr          # never into the transcript
    assert r.stdout == ""                          # an unchanged vault says nothing


def test_inert_outside_an_agent_session(tmp_path):
    r, ef, log = _run(tmp_path, slug="")
    assert r.returncode == 0 and r.stdout == "" and not ef.exists() and not log.exists()


def test_a_key_already_present_is_kept_but_borrowed_creds_are_still_staged(tmp_path):
    # A cloud turn has its key; it still gets what it borrows staged (canopy-web#1291).
    r, ef, log = _run(tmp_path, have_key=True)
    assert r.returncode == 0 and not ef.exists()
    assert log.read_text().splitlines() == ["agent stage-delegated --slug eva",
                                            "cred sync --agent eva", "key=already"]


def test_a_vault_change_is_reported_in_one_line(tmp_path):
    r, _, _ = _run(tmp_path, sync="re-injected")
    assert r.returncode == 0 and "vault changed" in r.stdout and KEY not in r.stdout


def test_a_failed_sync_is_fail_open_and_says_why(tmp_path):
    r, ef, _ = _run(tmp_path, sync="op vault get Agent-Eva: this key signs in to ACCOUNT-ELSEWHERE",
                    sync_rc=1)
    assert r.returncode == 0 and ef.exists()
    assert "may be stale" in r.stdout and "ACCOUNT-ELSEWHERE" in r.stdout


def test_a_callers_confined_turn_never_stages_borrowed_creds(tmp_path, monkeypatch):
    monkeypatch.setenv("CANOPY_PROFILE", "caller")
    r, _, log = _run(tmp_path)
    logged = log.read_text() if log.exists() else ""
    assert "stage-delegated" not in logged and "cred sync" not in logged


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


def test_exports_the_bound_instance_vault_and_never_guesses_one(tmp_path):
    """2026-10-14: the vault belongs to the instance `canopy agent env` bound this install to;
    the hook exports it so a hand-run `op inject` of an op://${AGENT_VAULT}/… template works."""
    r, ef, _ = _run(tmp_path, have_key=True,
                    binding={"plugin": "eva", "agent": "eva-2", "vault": "Eva Vault"})
    assert r.returncode == 0
    assert ef.read_text() == "export AGENT_VAULT='Eva Vault'\nexport AGENT_SLUG=eva-2\n"

    r, ef2, _ = _run(tmp_path / "nobind" if (tmp_path / "nobind").mkdir() is None else tmp_path,
                     have_key=True)
    assert "AGENT_VAULT" not in (ef2.read_text() if ef2.exists() else "")


def _plugin_binding(home, dirname, binding):
    d = home / ".claude" / "plugins" / "data" / dirname
    d.mkdir(parents=True, exist_ok=True)
    (d / "canopy-instance.json").write_text(json.dumps(binding))


def _installed(home, *keys):
    p = home / ".claude" / "plugins" / "installed_plugins.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"version": 2, "plugins": {k: [{}] for k in keys}}))


ACE = {"plugin": "ace", "agent": "ace", "vault": "Agent-ACE"}


def test_ada_style_binding_in_the_home_dot_dir(tmp_path):
    r, ef, _ = _run(tmp_path, slug="ada", have_key=True,
                    binding={"plugin": "ada", "agent": "ada", "vault": "Agent-Ada"})
    assert r.returncode == 0 and r.stdout == ""
    assert ef.read_text() == "export AGENT_VAULT=Agent-Ada\nexport AGENT_SLUG=ada\n"


def test_ace_style_binding_in_the_plugin_data_dir(tmp_path):
    """ACE's setup writes its env + binding to $CLAUDE_PLUGIN_DATA (ace-<marketplace>)."""
    home = tmp_path / "home"
    _plugin_binding(home, "ace-ace", ACE)
    # another plugin whose name merely starts with "ace-" is not ACE's binding
    _plugin_binding(home, "ace-tools-x", {"plugin": "ace-tools", "agent": "t", "vault": "T"})
    r, ef, _ = _run(tmp_path, slug="ace", have_key=True)
    assert r.returncode == 0 and r.stdout == ""
    assert ef.read_text() == "export AGENT_VAULT=Agent-ACE\nexport AGENT_SLUG=ace\n"


def test_several_plugin_data_bindings_prefer_the_installed_plugin(tmp_path):
    home = tmp_path / "home"
    _plugin_binding(home, "ace-ace", {**ACE, "vault": "Stale"})
    _plugin_binding(home, "ace-canopy-agents", ACE)
    _installed(home, "ace@canopy-agents", "canopy@canopy")
    r, ef, _ = _run(tmp_path, slug="ace", have_key=True)
    assert r.returncode == 0 and r.stdout == ""
    assert ef.read_text() == "export AGENT_VAULT=Agent-ACE\nexport AGENT_SLUG=ace\n"


def test_ambiguous_bindings_export_nothing_and_say_so_once(tmp_path):
    home = tmp_path / "home"
    _plugin_binding(home, "ace-ace", {**ACE, "vault": "One"})
    _plugin_binding(home, "ace-inline", {**ACE, "vault": "Two"})
    _installed(home, "canopy@canopy")       # neither ace dir is an installed plugin
    r, ef, _ = _run(tmp_path, slug="ace", have_key=True)
    assert r.returncode == 0
    assert "AGENT_VAULT" not in (ef.read_text() if ef.exists() else "")
    lines = [ln for ln in r.stdout.splitlines() if "AGENT_VAULT" in ln]
    assert len(lines) == 1 and "ace-ace" in lines[0] and "ace-inline" in lines[0]


def test_the_home_dot_dir_binding_wins_over_plugin_data(tmp_path):
    home = tmp_path / "home"
    _plugin_binding(home, "ace-ace", {**ACE, "vault": "FromPluginData"})
    r, ef, _ = _run(tmp_path, slug="ace", have_key=True, binding=ACE)
    assert ef.read_text() == "export AGENT_VAULT=Agent-ACE\nexport AGENT_SLUG=ace\n"
