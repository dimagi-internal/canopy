"""Local use of an agent (canopy-web#1704): `canopy private-runner`, `canopy local`, and the
three hooks that make a person's own session a recorded local turn.

* SessionStart (`agent_op_env.py`) opens the local turn on a registered private runner and
  exports its id, so the credential reads that follow name it;
* UserPromptSubmit (`caller_context.py`) shows the turn's envelope once and renews it;
* SessionEnd (`local_turn_close.py`) uploads the transcript and finishes the turn.
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from orchestrator import canopy_web, local_cli

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "plugins/canopy/hooks"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(local_cli, "person_token", lambda slug="": "person-pat")
    return tmp_path


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def fake(method, path, body=None, **kw):
        seen.append((method, path, body, kw.get("token")))
        if path == "/api/private-runners/":
            return {"id": "pr-1", "name": body["name"], "host": body["host"],
                    "runner_id": body.get("runner")}
        if path.endswith("/private"):
            return {"name": "jj-mbp-cdp", **body}
        if path == "/api/harness/local-turns/":
            return {"turn_id": "11111111-2222-3333-4444-555555555555", "created": True,
                    "turn_mode": "manual", "turn_mode_basis": "local turn: aath — manual",
                    "url": "https://canopy/w/connect/agents/ace/turns?turn=1",
                    "envelope": {"relationship": "member", "turn_mode": {"mode": "manual"}}}
        return {}

    monkeypatch.setattr(canopy_web, "call", fake)
    return seen


def _cli(*args):
    r = CliRunner().invoke(local_cli.private_runner_group if args[0] == "pr" else local_cli.local_group,
                           list(args[1:]))
    assert r.exit_code == 0, r.output
    return r.output


def test_register_then_open_records_the_turn_as_the_person(home, calls):
    _cli("pr", "register", "--name", "aath-mbp")
    assert json.loads((home / ".canopy/private-runner.json").read_text())["id"] == "pr-1"
    out = json.loads(_cli("local", "open", "--slug", "ace", "--session-id", "sess-1", "--cwd", "/w"))
    assert out["turn_id"].startswith("1111") and out["private_runner"] == "aath-mbp"
    method, path, body, token = calls[-1]
    assert path == "/api/harness/local-turns/" and token == "person-pat"
    assert body == {"agent": "ace", "private_runner": "pr-1", "claude_session_id": "sess-1",
                    "cwd": "/w", "turn_mode": ""}
    env = json.loads(Path(out["envelope_path"]).read_text())
    assert env["turn_mode"]["mode"] == "manual"
    assert Path(out["envelope_path"]).parent == home / ".canopy/caller"   # where the hook may read


def test_open_refuses_on_a_machine_that_is_not_a_private_runner(home, calls):
    r = CliRunner().invoke(local_cli.local_group, ["open", "--slug", "ace", "--session-id", "s"])
    assert r.exit_code != 0 and "not registered for local use" in r.output
    assert "canopy agent use ace" in r.output
    assert calls == []


def test_close_uploads_the_transcript_in_batches_then_finishes(home, calls, monkeypatch):
    _cli("pr", "register", "--name", "aath-mbp")
    _cli("local", "open", "--slug", "ace", "--session-id", "s1")
    monkeypatch.setattr(local_cli, "TRANSCRIPT_BATCH_BYTES", 40)
    t = home / "t.jsonl"
    t.write_text("\n".join(json.dumps({"n": i, "pad": "x" * 10}) for i in range(5)) + "\n")
    _cli("local", "close", "--session-id", "s1", "--transcript", str(t))
    posts = [c for c in calls if c[1].endswith("/transcript")]
    assert sum(len(c[2]["lines"]) for c in posts) == 5 and len(posts) > 1
    assert len({c[2]["batch_id"] for c in posts}) == len(posts)      # each retry-safe
    finish = calls[-1]
    assert finish[1].endswith("/finish") and finish[2]["status"] == "done"
    state = json.loads((home / ".canopy/local/s1.json").read_text())
    assert state["closed_at"] and state["uploaded"] == 5


def test_use_sets_the_agent_for_a_project(home, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert local_cli.set_session_agent("ace", str(proj)) is True
    assert json.loads((proj / ".claude/settings.local.json").read_text())["env"]["CANOPY_AGENT"] == "ace"
    assert local_cli.set_session_agent("ace", str(proj)) is False   # idempotent


def test_local_use_is_an_alias_of_agent_use(home, monkeypatch):
    from orchestrator import agent_use

    seen = []

    def fake_run(slug, directory, deps):
        seen.append((slug, directory))
        return agent_use.UseReport(slug=slug, directory=directory, plugin=slug)

    monkeypatch.setattr(agent_use, "run", fake_run)
    monkeypatch.setattr(agent_use, "real_deps", lambda: None)
    out = _cli("local", "use", "ace", "--dir", str(home))
    assert seen == [("ace", str(home))]
    assert "run /ace:turn" in out


def test_register_is_hidden_from_help_but_still_works(home, calls):
    r = CliRunner().invoke(local_cli.private_runner_group, ["--help"])
    assert "\n  register" not in r.output and "canopy agent use" in r.output
    out = _cli("pr", "register", "--name", "aath-mbp")
    assert "registered for local use as 'aath-mbp'" in out


# --- the hooks ---------------------------------------------------------------------

def _load(name):
    spec = importlib.util.spec_from_file_location(name, HOOKS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_local_eligible_only_on_a_private_runner_and_never_for_a_runner_turn(tmp_path, monkeypatch):
    hook = _load("agent_op_env")
    for var in ("CANOPY_TURN_ID", "CANOPY_PROFILE", "EMDASH_TASK_ID"):
        monkeypatch.delenv(var, raising=False)
    assert not hook.local_eligible(str(tmp_path))
    (tmp_path / ".canopy").mkdir()
    (tmp_path / ".canopy/private-runner.json").write_text("{}")
    assert hook.local_eligible(str(tmp_path))
    monkeypatch.setenv("CANOPY_TURN_ID", "t")          # a cloud turn
    assert not hook.local_eligible(str(tmp_path))
    monkeypatch.delenv("CANOPY_TURN_ID")
    monkeypatch.setenv("EMDASH_TASK_ID", "x")          # emdash on a box that runs the runner app
    (tmp_path / ".canopy/runner.json").write_text("{}")
    assert not hook.local_eligible(str(tmp_path))


@pytest.mark.parametrize("marker", ["CANOPY_HEADLESS", "CANOPY_ONE_SHOT_TURN"])
def test_a_runner_headless_session_is_not_a_local_turn(tmp_path, monkeypatch, marker):
    """The runner app's headless engine starts claude with these and no turn id: its turn
    already exists, and a second, local one would move its credential reads to the license."""
    hook = _load("agent_op_env")
    for var in ("CANOPY_TURN_ID", "CANOPY_PROFILE", "EMDASH_TASK_ID", *hook.RUNNER_ENV_MARKERS):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".canopy").mkdir()
    (tmp_path / ".canopy/private-runner.json").write_text("{}")
    assert hook.local_eligible(str(tmp_path))
    monkeypatch.setenv(marker, "1")
    assert not hook.local_eligible(str(tmp_path))


def test_a_desktop_engine_worktree_is_not_a_local_turn(tmp_path, monkeypatch):
    """The Claude app (not the runner) starts a desktop-engine session, so no runner env
    reaches it; its worktree's `.canopy-desktop/` channel is the mark."""
    hook = _load("agent_op_env")
    for var in ("CANOPY_TURN_ID", "CANOPY_PROFILE", "EMDASH_TASK_ID", *hook.RUNNER_ENV_MARKERS):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".canopy").mkdir()
    (tmp_path / ".canopy/private-runner.json").write_text("{}")
    wt = tmp_path / "wt"
    wt.mkdir()
    assert hook.local_eligible(str(tmp_path), str(wt))
    (wt / ".canopy-desktop").mkdir()
    assert not hook.local_eligible(str(tmp_path), str(wt))


def test_session_start_opens_the_local_turn_before_reading_credentials(tmp_path):
    """The turn id reaches the env file AND every later canopy call (the credential read)."""
    home = tmp_path / "home"
    (home / ".canopy").mkdir(parents=True)
    (home / ".canopy/private-runner.json").write_text('{"id": "pr-1"}')
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "args"
    (bindir / "canopy").write_text(
        "#!/bin/sh\n"
        f"echo \"$@ turn=$CANOPY_TURN_ID\" >> {log}\n"
        "[ \"$1\" = local ] && { echo '{\"turn_id\": \"tid-9\", \"private_runner\": \"aath-mbp\", "
        "\"turn_mode\": \"manual\", \"turn_mode_basis\": \"b\", \"url\": \"u\"}'; exit 0; }\n"
        "[ \"$2\" = op-token ] && { echo KEY; exit 0; }\n"
        "exit 0\n")
    (bindir / "canopy").chmod(0o755)
    env = {k: v for k, v in os.environ.items()
           if k not in ("CANOPY_TURN_ID", "OP_SERVICE_ACCOUNT_TOKEN", "CANOPY_PROFILE", "EMDASH_TASK_ID")}
    env.update(HOME=str(home), PATH=f"{bindir}{os.pathsep}{env['PATH']}", CANOPY_AGENT="ace",
               CLAUDE_ENV_FILE=str(tmp_path / "session.env"))
    r = subprocess.run([sys.executable, str(HOOKS / "agent_op_env.py")], input=json.dumps(
        {"session_id": "sess-1", "cwd": "/w", "source": "startup"}), capture_output=True,
        text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    lines = log.read_text().splitlines()
    assert lines[0].startswith("local open --slug ace --session-id sess-1")
    assert any(ln.startswith("agent op-token --slug ace turn=tid-9") for ln in lines)
    exported = (tmp_path / "session.env").read_text()
    assert "export CANOPY_TURN_ID=tid-9" in exported and "OP_SERVICE_ACCOUNT_TOKEN=KEY" in exported
    assert "local turn of ace" in r.stdout and "KEY" not in r.stdout


def test_the_prompt_hook_shows_the_local_envelope_once(tmp_path, monkeypatch):
    hook = _load("caller_context")
    caller = tmp_path / "caller"
    caller.mkdir()
    env = caller / "tid.json"
    env.write_text(json.dumps({"relationship": "member", "profile": "full",
                               "turn_mode": {"mode": "manual"}, "trigger": {"origin": "local",
                               "private_runner": "aath-mbp"}}))
    monkeypatch.setattr(hook, "CALLER_ROOT", str(caller))
    local = tmp_path / "local"
    local.mkdir()
    (local / "s1.json").write_text(json.dumps({"turn_id": "tid", "envelope_path": str(env)}))
    assert hook.local_turn("s1", root=str(local), renew=False) == (str(env), "tid")
    assert hook.local_turn("s1", root=str(local), renew=False) == (None, None)   # once
    text = hook.summarize(json.loads(env.read_text()), str(env), "tid")
    assert "private runner aath-mbp" in text and "turn mode: manual" in text


def test_the_prompt_hook_ignores_a_closed_or_foreign_session(tmp_path):
    hook = _load("caller_context")
    local = tmp_path / "local"
    local.mkdir()
    (local / "s1.json").write_text(json.dumps({"turn_id": "t", "closed_at": 1}))
    assert hook.local_turn("s1", root=str(local), renew=False) == (None, None)
    assert hook.local_turn("../etc/passwd", root=str(local), renew=False) == (None, None)


def test_session_end_is_inert_without_a_local_turn(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path), "PATH": "/nonexistent"}
    r = subprocess.run([sys.executable, str(HOOKS / "local_turn_close.py")],
                       input=json.dumps({"session_id": "s1"}), capture_output=True, text=True,
                       env=env, timeout=10)
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""


def test_session_end_closes_with_the_transcript(tmp_path):
    home = tmp_path / "home"
    (home / ".canopy/local").mkdir(parents=True)
    (home / ".canopy/local/s1.json").write_text("{}")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "args"
    (bindir / "canopy").write_text(f"#!/bin/sh\necho \"$@\" > {log}\n")
    (bindir / "canopy").chmod(0o755)
    t = tmp_path / "t.jsonl"
    t.write_text("{}\n")
    env = {**os.environ, "HOME": str(home), "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
    r = subprocess.run([sys.executable, str(HOOKS / "local_turn_close.py")],
                       input=json.dumps({"session_id": "s1", "transcript_path": str(t)}),
                       capture_output=True, text=True, env=env, timeout=10)
    assert r.returncode == 0
    # Detached (Claude Code cancels a SessionEnd hook as it exits): the child finishes
    # after the hook has returned.
    import time
    for _ in range(50):
        if log.exists() and log.read_text().strip():
            break
        time.sleep(0.1)
    assert log.read_text().strip() == f"local close --session-id s1 --transcript {t}"


def test_session_end_is_registered():
    hooks = json.loads((HOOKS / "hooks.json").read_text())["hooks"]
    cmds = [h["command"] for entry in hooks["SessionEnd"] for h in entry["hooks"]]
    assert any("local_turn_close.py" in c for c in cmds)


def test_open_catches_up_a_dead_session_that_never_closed(home, calls):
    """A killed terminal never ran SessionEnd: the next session's open uploads the old
    transcript (recorded at its open) and finishes its turn."""
    _cli("pr", "register", "--name", "aath-mbp")
    old = home / "old.jsonl"
    old.write_text('{"a": 1}\n{"b": 2}\n')
    _cli("local", "open", "--slug", "ace", "--session-id", "old", "--transcript", str(old))
    state = json.loads((home / ".canopy/local/old.json").read_text())
    state["renewed_at"] = state["opened_at"] = 0          # silent for a long time
    (home / ".canopy/local/old.json").write_text(json.dumps(state))
    _cli("local", "open", "--slug", "ace", "--session-id", "new")
    up = [c for c in calls if c[1].endswith("/transcript")]
    assert up and up[0][2]["lines"] == ['{"a": 1}', '{"b": 2}']
    finish = [c for c in calls if c[1].endswith("/finish")]
    assert finish and finish[0][2]["status"] == "failed"     # it died: not `done`
    assert json.loads((home / ".canopy/local/old.json").read_text())["closed_at"]
    assert "closed_at" not in json.loads((home / ".canopy/local/new.json").read_text())


def test_a_live_session_is_not_caught_up(home, calls):
    _cli("pr", "register", "--name", "aath-mbp")
    _cli("local", "open", "--slug", "ace", "--session-id", "one")
    _cli("local", "open", "--slug", "ace", "--session-id", "two")    # a second terminal
    assert not any(c[1].endswith("/finish") for c in calls)


def _runner_json(home, runner_id="rid-1"):
    (home / ".canopy").mkdir(exist_ok=True)
    (home / ".canopy/runner.json").write_text(json.dumps({"runner_id": runner_id}))


def test_register_links_this_machines_runner(home, calls, monkeypatch):
    monkeypatch.delenv("CANOPY_RUNNER_CONFIG", raising=False)
    monkeypatch.setattr("orchestrator.runner_pin.DEFAULT_RUNNER_CONFIG", home / ".canopy/runner.json")
    _runner_json(home)
    _cli("pr", "register", "--name", "jj-mbp")
    assert calls[-1][2]["runner"] == "rid-1"
    assert json.loads((home / ".canopy/private-runner.json").read_text())["runner_id"] == "rid-1"


def test_register_without_a_runner_sends_no_link(home, calls, monkeypatch):
    monkeypatch.setattr("orchestrator.runner_pin.DEFAULT_RUNNER_CONFIG", home / "nope.json")
    _cli("pr", "register", "--name", "aath-mbp")
    assert "runner" not in calls[-1][2]


@pytest.mark.parametrize("mode,private", [("private", True), ("shared", False)])
def test_set_makes_this_runner_private_or_shared(home, calls, monkeypatch, mode, private):
    monkeypatch.setattr("orchestrator.runner_pin.DEFAULT_RUNNER_CONFIG", home / ".canopy/runner.json")
    _runner_json(home)
    _cli("pr", "set", mode)
    method, path, body, token = calls[-1]
    assert (method, path, token) == ("PUT", "/api/harness/runners/rid-1/private", "person-pat")
    assert body == {"private": private}


def test_set_needs_a_runner(home, calls, monkeypatch):
    monkeypatch.setattr("orchestrator.runner_pin.DEFAULT_RUNNER_CONFIG", home / "nope.json")
    r = CliRunner().invoke(local_cli.private_runner_group, ["set", "private"])
    assert r.exit_code != 0 and "has no runner" in r.output


# --- renew: a turn that ended is re-opened for the same session ----------------------

def _renew_calls(monkeypatch, calls, status, new_turn="99999999-0000-0000-0000-000000000000"):
    inner = canopy_web.call

    def fake(method, path, body=None, **kw):
        if path.endswith("/renew"):
            calls.append((method, path, body, kw.get("token")))
            if status == 404:
                raise canopy_web.CanopyError(f"POST {path} -> 404: not found")
            return {"turn_id": path.split("/")[-2], "status": status, "lease_expires_at": None}
        out = inner(method, path, body, **kw)
        if path == "/api/harness/local-turns/" and len([c for c in calls if c[1] == path]) > 1:
            out = {**out, "turn_id": new_turn, "envelope": {"turn_mode": {"mode": "manual"}}}
        return out

    monkeypatch.setattr(canopy_web, "call", fake)
    return new_turn


def test_renew_of_a_live_turn_only_renews(home, calls, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_ENV_FILE", str(tmp_path / "session.env"))
    _cli("pr", "register", "--name", "aath-mbp")
    _cli("local", "open", "--slug", "ace", "--session-id", "s1", "--cwd", "/w")
    _renew_calls(monkeypatch, calls, "running")
    _cli("local", "renew", "--session-id", "s1", "--force")
    assert [c[1] for c in calls].count("/api/harness/local-turns/") == 1
    assert json.loads((home / ".canopy/local/s1.json").read_text())["turn_id"].startswith("1111")


@pytest.mark.parametrize("status", ["lost", "done", 404])
def test_renew_of_an_ended_turn_reopens_and_exports_the_new_id(home, calls, monkeypatch, tmp_path, status):
    """Idle past the 8h lease, the turn was swept `lost`: the session must not go on naming it."""
    env_file = tmp_path / "session.env"
    monkeypatch.setenv("CLAUDE_ENV_FILE", str(env_file))
    _cli("pr", "register", "--name", "aath-mbp")
    _cli("local", "open", "--slug", "ace", "--session-id", "s1", "--cwd", "/w", "--auto")
    state = json.loads((home / ".canopy/local/s1.json").read_text())
    state["envelope_shown"] = True
    (home / ".canopy/local/s1.json").write_text(json.dumps(state))
    monkeypatch.delenv("CLAUDE_ENV_FILE")          # the prompt hook's renew has none
    new_turn = _renew_calls(monkeypatch, calls, status)
    _cli("local", "renew", "--session-id", "s1", "--force")
    reopen = [c for c in calls if c[1] == "/api/harness/local-turns/"][-1]
    assert reopen[2] == {"agent": "ace", "private_runner": "pr-1", "claude_session_id": "s1",
                         "cwd": "/w", "turn_mode": "auto"}
    state = json.loads((home / ".canopy/local/s1.json").read_text())
    assert state["turn_id"] == new_turn and state["envelope_shown"] is False
    assert state["previous_turn_ids"] == ["11111111-2222-3333-4444-555555555555"]
    assert Path(state["envelope_path"]).name == f"{new_turn}.json"
    assert env_file.read_text().splitlines()[-1] == f"export CANOPY_TURN_ID={new_turn}"


def test_renew_warns_when_the_turn_cannot_be_reopened(home, calls, monkeypatch):
    _cli("pr", "register", "--name", "aath-mbp")
    _cli("local", "open", "--slug", "ace", "--session-id", "s1")
    inner = canopy_web.call

    def fake(method, path, body=None, **kw):
        if path.endswith("/renew"):
            return {"status": "lost"}
        if path == "/api/harness/local-turns/":
            raise canopy_web.CanopyError("POST /api/harness/local-turns/ -> 403: no license")
        return inner(method, path, body, **kw)

    monkeypatch.setattr(canopy_web, "call", fake)
    r = CliRunner().invoke(local_cli.local_group, ["renew", "--session-id", "s1", "--force"])
    assert r.exit_code == 0
    assert "is lost and a new one could not be opened" in r.output
    assert json.loads((home / ".canopy/local/s1.json").read_text())["turn_id"].startswith("1111")


def test_vault_device_falls_back_to_this_computers_runner_id(home, monkeypatch):
    # No runner app (no ~/.canopy/runner.json): the device key enrolls against the runner
    # id canopy-web gave this computer when it was registered for local use.
    from orchestrator import vault_cli
    (home / ".canopy").mkdir(exist_ok=True)
    (home / ".canopy/private-runner.json").write_text('{"id": "pr-1", "runner_id": "comp-9"}')
    assert vault_cli._computer_runner_id() == "comp-9"
    (home / ".canopy/private-runner.json").write_text('{"id": "pr-1"}')
    assert vault_cli._computer_runner_id() == ""
