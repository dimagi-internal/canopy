"""Local sessions on your private runner sign in with the box's own key (canopy-web#1705)."""
import json

import pytest

from orchestrator import private_runner_session as prs


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CANOPY_RUNNER_CONFIG", raising=False)
    monkeypatch.setattr("orchestrator.runner_pin.DEFAULT_RUNNER_CONFIG", tmp_path / ".canopy/runner.json")
    (tmp_path / ".canopy").mkdir()
    return tmp_path


def _link(home, private="rid-1", runner="rid-1"):
    (home / ".canopy/runner.json").write_text(json.dumps({"runner_id": runner}))
    (home / ".canopy/private-runner.json").write_text(json.dumps({"id": "pr-1", "runner_id": private}))


def test_a_linked_private_runner_uses_the_box_token(home):
    _link(home)
    assert prs.token(mint=lambda base: "crt_box") == "crt_box"


@pytest.mark.parametrize("private,runner", [("", "rid-1"), ("rid-1", "rid-2")])
def test_no_link_means_no_box_token(home, private, runner):
    _link(home, private=private, runner=runner)
    called = []
    assert prs.token(mint=lambda base: called.append(base) or "crt") == "" and called == []


def test_a_box_that_cannot_sign_in_falls_back(home):
    _link(home)

    def broken(base):
        raise RuntimeError("no approved key")
    assert prs.token(mint=broken) == ""


def test_local_session_calls_prefer_the_box_token(home, monkeypatch):
    from orchestrator import local_cli

    monkeypatch.setattr(prs, "token", lambda: "crt_box")
    monkeypatch.setattr(local_cli, "person_token", lambda slug="": "pat")
    assert local_cli.session_token("hal") == "crt_box"
    monkeypatch.setattr(prs, "token", lambda: "")
    assert local_cli.session_token("hal") == "pat"


def test_a_credential_read_in_a_local_turn_signs_in_as_the_box(home, monkeypatch):
    from orchestrator import canopy_web, cred_resolve

    monkeypatch.setenv("CANOPY_LOCAL_TURN", "1")
    monkeypatch.setattr(prs, "token", lambda: "crt_box")
    seen = {}

    def call(method, path, *a, token=None, **kw):
        seen["token"] = token
        return {"values": {}}
    monkeypatch.setattr(canopy_web, "call", call)
    cred_resolve.resolve("hal", token="pat", turn_id="t-1")
    assert seen["token"] == "crt_box"
