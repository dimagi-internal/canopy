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
GH = "github_pat_SECRET"
GH_OUT = f"export GH_TOKEN={GH}\nexport GIT_AUTHOR_NAME=eva-bot\n"


def _fake_canopy(tmp_path, out=KEY, rc=0, gh_rc=0):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "canopy-args"
    script = bindir / "canopy"
    gh_ok = (f"printf '%s' '{GH_OUT}'; echo 'git/gh act on GitHub as @eva-bot' >&2; exit 0"
             if gh_rc == 0 else "echo 'Error: canopy-web holds no GitHub credential for eva' >&2; exit 1")
    script.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\n"
                      f"[ \"$2\" = stage-delegated ] && exit 0\n"
                      f"if [ \"$2\" = github-env ]; then {gh_ok}; fi\n"
                      f"printf '%s\\n' '{out}'\n"
                      f"[ {rc} -ne 0 ] && echo 'no live runner you pair' >&2\nexit {rc}\n")
    script.chmod(0o755)
    return str(bindir), log


_STRIP = ("CANOPY_AGENT", "OP_SERVICE_ACCOUNT_TOKEN", "CLAUDE_ENV_FILE", "CANOPY_REQUESTED_BY",
          "CANOPY_ALLOW_OPERATOR_IDENTITY", "CANOPY_PROFILE", "GH_TOKEN")


def _run(tmp_path, *, slug="eva", have_key=False, out=KEY, rc=0, env_file=True, gh_rc=0, extra=None):
    bindir, log = _fake_canopy(tmp_path, out, rc, gh_rc)
    env = {k: v for k, v in os.environ.items() if k not in _STRIP}
    env["HOME"] = str(tmp_path / "home")
    env.update(extra or {})
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
    assert ef.read_text() == GH_OUT + f"export OP_SERVICE_ACCOUNT_TOKEN={KEY}\n"
    assert log.read_text().splitlines() == ["agent stage-delegated --slug eva",
                                            "agent github-env --slug eva",
                                            "agent op-token --slug eva"]
    assert KEY not in r.stdout + r.stderr          # never into the transcript
    assert GH not in r.stdout + r.stderr
    assert "@eva-bot" in r.stdout


def test_inert_outside_an_agent_session(tmp_path):
    r, ef, log = _run(tmp_path, slug="")
    assert r.returncode == 0 and r.stdout == "" and not ef.exists() and not log.exists()


def test_a_key_already_present_is_kept_but_borrowed_creds_are_still_staged(tmp_path):
    # A cloud turn has its key; it still gets what it borrows staged (canopy-web#1291).
    r, ef, log = _run(tmp_path, have_key=True, extra={"CANOPY_REQUESTED_BY": ""})
    assert r.returncode == 0 and not ef.exists()
    assert log.read_text().splitlines() == ["agent stage-delegated --slug eva"]


def test_a_callers_confined_turn_never_stages_borrowed_creds(tmp_path):
    r, ef, log = _run(tmp_path, extra={"CANOPY_PROFILE": "caller"})
    calls = log.read_text() if log.exists() else ""
    assert "stage-delegated" not in calls and "github-env" not in calls
    # ...and gets a GitHub REFUSAL, never the machine's login (the server's rule too).
    assert "quit=1" in ef.read_text() and "CANOPY_AGENT_GITHUB=none" in ef.read_text()


def test_failure_is_fail_open_and_says_so(tmp_path):
    r, ef, _ = _run(tmp_path, out="", rc=1)
    assert r.returncode == 0 and "OP_SERVICE_ACCOUNT_TOKEN" not in ef.read_text()
    assert "no live runner you pair" in r.stdout and "1Password app" in r.stdout


# ---- GitHub identity (canopy#832) -----------------------------------------------

def _sourced(ef: Path, *names):
    """What a Bash command would see after Claude Code sources the env file."""
    script = f". {ef}; " + "; ".join(f'printf "%s\\n" "${{{n}-<unset>}}"' for n in names)
    env = {"PATH": os.environ["PATH"], "GH_TOKEN": "owner-token",
           "GIT_CONFIG_PARAMETERS": "'credential.https://github.com.helper'='!emdash-gh-login'"}
    return subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True,
                          env=env).stdout.splitlines()


def test_no_github_identity_writes_a_refusal_not_a_fallback(tmp_path):
    r, ef, _ = _run(tmp_path, gh_rc=1)
    assert r.returncode == 0
    assert "no GitHub identity" in r.stdout and "holds no GitHub credential" in r.stdout
    gh_token, params, gh_dir = _sourced(ef, "GH_TOKEN", "GIT_CONFIG_PARAMETERS", "GH_CONFIG_DIR")
    assert gh_token == "<unset>"                       # the owner's token is gone
    assert "emdash-gh-login" not in params             # emdash's helper replaced
    assert "quit=1" in params and "/agents/eva/settings" in params
    assert gh_dir.endswith(".canopy/github/no-identity") and Path(gh_dir).is_dir()


def test_refusal_really_stops_git_from_using_any_other_helper(tmp_path):
    """End to end against real git: the refusal helper's message reaches the user and
    git asks no further helper (osxkeychain, gh) and does not prompt."""
    _, ef, _ = _run(tmp_path, gh_rc=1)
    (params,) = _sourced(ef, "GIT_CONFIG_PARAMETERS")
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path / "home"),
           "GIT_CONFIG_PARAMETERS": params, "GIT_TERMINAL_PROMPT": "0"}
    r = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                       capture_output=True, text=True, env=env, timeout=20)
    assert r.returncode != 0
    assert "refusing to act on GitHub" in r.stderr
    assert "password=" not in r.stdout


def test_operator_opt_in_leaves_github_alone(tmp_path):
    r, ef, log = _run(tmp_path, extra={"CANOPY_ALLOW_OPERATOR_IDENTITY": "1"})
    assert "github-env" not in log.read_text()
    assert "GIT_CONFIG_PARAMETERS" not in ef.read_text()
    assert "machine's own GitHub login" in r.stdout


def test_hook_runs_on_old_system_python_syntax():
    import ast
    ast.parse(HOOK.read_text(), feature_version=(3, 9))


def test_registered_on_session_start():
    hooks = json.loads((ROOT / "plugins/canopy/hooks/hooks.json").read_text())["hooks"]["SessionStart"]
    cmds = [h["command"] for m in hooks for h in m["hooks"]]
    assert any("agent_op_env.py" in c for c in cmds)
    entry = next(h for m in hooks for h in m["hooks"] if "agent_op_env.py" in h["command"])
    assert not entry.get("async"), "must finish before the first Bash command reads CLAUDE_ENV_FILE"
