"""`canopy secret` — this session's chat secrets, spent without being read."""
from __future__ import annotations

import sys

import pytest
from click.testing import CliRunner

from cryptography.hazmat.primitives.asymmetric import ec

from orchestrator import device_key, secret_cli
from orchestrator import vault_zk as zk

SID = "eb742bd8-e16c-439e-a3d7-c8a5a769df60"
VALUE = "ghp_not_a_real_token_0123456789"
KEY = "chk_this_chat"


BINDING = "session:2d1c0f2e-0000-0000-0000-000000000001"


def _machine():
    """A software ECDH device key, as `device_key.load_ecdh` returns one."""
    priv = ec.generate_private_key(ec.SECP256R1())
    jwk = zk.jwk(priv.public_key())

    def derive(peer_point: bytes) -> bytes:
        return priv.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), peer_point))

    return device_key.EcdhKey(device_key.KIND_SOFTWARE, jwk, derive)


THIS_BOX = _machine()


def _sealed_to(box, value=VALUE, name="GH_TOKEN"):
    """What the sharer's browser stores: the value sealed to the chat's machine."""
    return zk.seal_value(BINDING, name, 0, value, [("device", box.jkt, box.public_jwk)])


@pytest.fixture()
def served(monkeypatch):
    calls = []

    def fake_call(method, path, body=None, **kw):
        calls.append((method, path, (kw.get("headers") or {}).get("X-Canopy-Chat-Key")))
        if path.endswith("/GH_TOKEN?sealed=true"):
            return {"name": "GH_TOKEN", "value_zk": _sealed_to(THIS_BOX), "binding_id": BINDING, "kv": 0}
        return [{"name": "GH_TOKEN", "last_used_at": None, "expires_at": "2026-09-25T13:30:00+00:00"}]

    monkeypatch.setattr(secret_cli.canopy_web, "call", fake_call)
    monkeypatch.setattr(device_key, "load_ecdh", lambda *a, **k: THIS_BOX)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    # The cloud runner's form by default; the file forms have their own tests.
    monkeypatch.setenv("CANOPY_CHAT_KEY", KEY)
    # A cloud runner exports its own GH_TOKEN; the child would inherit it, which is not
    # the secret being tested.
    monkeypatch.delenv("GH_TOKEN", raising=False)
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
    assert served == [("GET", "/api/session-secrets/key/GH_TOKEN?sealed=true", KEY)]


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
    assert served == [("GET", "/api/session-secrets/key/GH_TOKEN?sealed=true", "chk_laptop")]


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


# ---- sealed to the machine running the chat: canopy-web cannot read it ----

def _serving(body):
    return lambda method, path, body_=None, **kw: body


def test_the_value_comes_back_sealed_and_this_machine_opens_it(served):
    assert secret_cli.fetch_value(KEY, "GH_TOKEN") == VALUE


def test_a_value_sealed_to_another_machine_says_to_share_it_again(served):
    body = {"name": "GH_TOKEN", "value_zk": _sealed_to(_machine()), "binding_id": BINDING, "kv": 0}
    with pytest.raises(Exception) as exc:
        secret_cli.fetch_value(KEY, "GH_TOKEN", call=_serving(body))
    msg = str(exc.value)
    assert "another machine" in msg and "share it again" in msg and VALUE not in msg


def test_a_value_bound_to_another_chat_does_not_open(served):
    body = {"name": "GH_TOKEN", "value_zk": _sealed_to(THIS_BOX), "binding_id": "session:someone-else", "kv": 0}
    with pytest.raises(Exception) as exc:
        secret_cli.fetch_value(KEY, "GH_TOKEN", call=_serving(body))
    assert "could not open GH_TOKEN" in str(exc.value)


def test_a_machine_with_no_ecdh_key_says_so(served, monkeypatch):
    monkeypatch.setattr(device_key, "load_ecdh", lambda *a, **k: None)
    with pytest.raises(Exception) as exc:
        secret_cli.fetch_value(KEY, "GH_TOKEN")
    assert "cannot open it" in str(exc.value) and "ECDH" in str(exc.value)


def test_a_canopy_web_from_before_sealing_still_hands_the_value(served):
    """Rollout: this CLI ships first, so it must still work against the old server."""
    assert secret_cli.fetch_value(KEY, "GH_TOKEN", call=_serving({"name": "GH_TOKEN", "value": VALUE})) == VALUE
