"""`canopy secret` — this session's chat secrets, spent without being read."""
from __future__ import annotations

import sys

import pytest
from click.testing import CliRunner

from orchestrator import secret_cli

SID = "eb742bd8-e16c-439e-a3d7-c8a5a769df60"
VALUE = "ghp_not_a_real_token_0123456789"
KEY = "chk_this_chat"


@pytest.fixture()
def served(monkeypatch):
    calls = []

    def fake_call(method, path, body=None, **kw):
        calls.append((method, path, (kw.get("headers") or {}).get("X-Canopy-Chat-Key")))
        if path.endswith("/GH_TOKEN"):
            return {"name": "GH_TOKEN", "value": VALUE}
        return [{"name": "GH_TOKEN", "last_used_at": None, "expires_at": "2026-09-25T13:30:00+00:00"}]

    monkeypatch.setattr(secret_cli.canopy_web, "call", fake_call)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    # The cloud runner's form by default; the file forms have their own tests.
    monkeypatch.setenv("CANOPY_CHAT_KEY", KEY)
    monkeypatch.setattr(secret_cli, "KEY_ROOT", secret_cli.pathlib.Path("/nonexistent-canopy-chat"))
    return calls


def _run(capfd, *args):
    with pytest.raises(SystemExit) as exc:
        secret_cli.exec_cmd.main(list(args), standalone_mode=False)
    out, err = capfd.readouterr()
    return exc.value.code, out, err


def test_list_asks_for_THIS_sessions_secrets_and_shows_no_value(served):
    r = CliRunner().invoke(secret_cli.secret_group, ["list"])
    assert r.exit_code == 0, r.output
    assert "GH_TOKEN" in r.output and VALUE not in r.output
    assert served == [("GET", "/api/session-secrets/key", KEY)]


def test_exec_fetches_from_this_session_and_masks_stdout(served, capfd):
    code, out, err = _run(capfd, "GH_TOKEN", "--", sys.executable, "-c",
                          "import os; print('token=' + os.environ['GH_TOKEN'])")
    assert code == 0
    assert out.strip() == "token=***"
    assert VALUE not in out + err
    assert served == [("GET", "/api/session-secrets/key/GH_TOKEN", KEY)]


def test_stdin_mode_pipes_it_in_and_masks_stderr_too(served, capfd):
    code, out, err = _run(capfd, "--stdin", "GH_TOKEN", "--", sys.executable, "-c",
                          "import sys, os; v = sys.stdin.read(); "
                          "sys.stderr.write('got ' + v + '\\n'); print('GH_TOKEN' in os.environ)")
    assert code == 0
    assert err.strip() == "got ***"
    assert out.strip() == "False"  # stdin mode does not ALSO export it
    assert VALUE not in out + err


def test_a_custom_env_name(served, capfd):
    _, out, _ = _run(capfd, "--env", "GITHUB_PAT", "GH_TOKEN", "--", sys.executable, "-c",
                     "import os; print(len(os.environ['GITHUB_PAT']))")
    assert out.strip() == str(len(VALUE))


def test_the_childs_exit_code_is_propagated(served, capfd):
    code, _, _ = _run(capfd, "GH_TOKEN", "--", sys.executable, "-c", "raise SystemExit(7)")
    assert code == 7


def test_with_no_chat_key_it_refuses_before_any_fetch(served, monkeypatch):
    """No fallback: the old way (naming the Claude session id, as the chat's agent
    login) is gone. A session holding no key is not driving a chat."""
    monkeypatch.delenv("CANOPY_CHAT_KEY")
    r = CliRunner().invoke(secret_cli.secret_group, ["list"])
    assert r.exit_code != 0 and "no chat key" in r.output
    assert served == []


def test_a_bad_name_is_refused_before_any_fetch(served):
    with pytest.raises(Exception):
        secret_cli.exec_cmd.main(["canopy-secret://x/GH_TOKEN", "--", "true"], standalone_mode=False)
    assert served == []


def test_a_fetch_failure_names_the_problem_and_not_a_value():
    def boom(*a, **k):
        raise secret_cli.canopy_web.CanopyError("GET … -> 404: no such secret")

    with pytest.raises(Exception) as exc:
        secret_cli.fetch_value(KEY, "GH_TOKEN", call=boom)
    assert "404" in str(exc.value) and "30 minutes" in str(exc.value)


def test_no_verb_prints_a_value_and_none_names_another_session():
    assert set(secret_cli.secret_group.commands) == {"list", "exec"}
    params = {p.name for c in secret_cli.secret_group.commands.values() for p in c.params}
    assert not params & {"session", "session_id", "chat", "ref"}



# ---- the chat key: canopy's permission for THIS chat, preferred when present ----

def test_a_cloud_turns_key_from_the_environment_is_presented(served, monkeypatch):
    monkeypatch.setenv("CANOPY_CHAT_KEY", "chk_cloud")
    r = CliRunner().invoke(secret_cli.secret_group, ["list"])
    assert r.exit_code == 0, r.output
    assert served == [("GET", "/api/session-secrets/key", "chk_cloud")]


def test_a_laptop_sessions_key_is_found_by_its_session_id(served, monkeypatch, tmp_path):
    (tmp_path / "session").mkdir()
    (tmp_path / "session" / f"{SID}.key").write_text("chk_laptop\n")
    monkeypatch.setattr(secret_cli, "KEY_ROOT", tmp_path)
    monkeypatch.delenv("CANOPY_CHAT_KEY")
    assert secret_cli.fetch_value(secret_cli.this_chat(), "GH_TOKEN") == VALUE
    assert served == [("GET", "/api/session-secrets/key/GH_TOKEN", "chk_laptop")]


def test_a_laptop_sessions_key_is_found_from_its_emdash_worktree(served, monkeypatch, tmp_path):
    home = tmp_path / "home"
    wt = home / "emdash" / "worktrees" / "hal" / "emdash" / "hal-chat-x1-q7r2k"
    wt.mkdir(parents=True)
    keys = tmp_path / "keys"
    (keys / "task").mkdir(parents=True)
    (keys / "task" / "hal-chat-x1.key").write_text("chk_task")
    monkeypatch.setattr(secret_cli.pathlib.Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(secret_cli, "KEY_ROOT", keys)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    monkeypatch.delenv("CANOPY_CHAT_KEY")
    assert secret_cli.chat_key(cwd=str(wt / "src")) == "chk_task"
