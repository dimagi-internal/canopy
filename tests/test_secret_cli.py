"""`canopy secret exec` — the value reaches one child process and never our output."""
from __future__ import annotations

import sys

import pytest
from click.testing import CliRunner

from orchestrator import secret_cli

SID = "4d0a7706-c56d-4dae-b80b-62f8478c42e8"
REF = f"canopy-secret://{SID}/GH_TOKEN"
VALUE = "ghp_not_a_real_token_0123456789"


@pytest.fixture()
def served(monkeypatch):
    calls = []

    def fake_call(method, path, body=None, **kw):
        calls.append((method, path))
        return {"name": "GH_TOKEN", "value": VALUE}

    monkeypatch.setattr(secret_cli.canopy_web, "call", fake_call)
    return calls


def _run(capfd, *args):
    with pytest.raises(SystemExit) as exc:
        secret_cli.exec_cmd.main(list(args), standalone_mode=False)
    out, err = capfd.readouterr()
    return exc.value.code, out, err


def test_the_value_is_in_the_childs_env_and_masked_in_its_stdout(served, capfd):
    code, out, err = _run(capfd, REF, "--", sys.executable, "-c",
                          "import os; print('token=' + os.environ['GH_TOKEN'])")
    assert code == 0
    assert out.strip() == "token=***"
    assert VALUE not in out + err
    assert served == [("GET", f"/api/canopy-sessions/{SID}/secrets/GH_TOKEN/value")]


def test_stdin_mode_pipes_it_in_and_masks_stderr_too(served, capfd):
    code, out, err = _run(capfd, "--stdin", REF, "--", sys.executable, "-c",
                          "import sys, os; v = sys.stdin.read(); "
                          "sys.stderr.write('got ' + v + '\\n'); print('GH_TOKEN' in os.environ)")
    assert code == 0
    assert err.strip() == "got ***"
    assert out.strip() == "False"  # stdin mode does not ALSO export it
    assert VALUE not in out + err


def test_a_custom_env_name(served, capfd):
    code, out, _ = _run(capfd, "--env", "GITHUB_PAT", REF, "--", sys.executable, "-c",
                        "import os; print(len(os.environ['GITHUB_PAT']))")
    assert out.strip() == str(len(VALUE))


def test_the_childs_exit_code_is_propagated(served, capfd):
    code, _, _ = _run(capfd, REF, "--", sys.executable, "-c", "raise SystemExit(7)")
    assert code == 7


def test_a_malformed_reference_is_refused_before_any_fetch(served):
    with pytest.raises(Exception):
        secret_cli.exec_cmd.main(["canopy-secret://nope/GH_TOKEN", "--", "true"], standalone_mode=False)
    assert served == []


def test_a_fetch_failure_names_the_secret_and_not_a_value(monkeypatch):
    def boom(*a, **k):
        raise secret_cli.canopy_web.CanopyError("GET … -> 404: no such secret")

    with pytest.raises(Exception) as exc:
        secret_cli.fetch_value(SID, "GH_TOKEN", call=boom)
    assert "GH_TOKEN" in str(exc.value) and "404" in str(exc.value)


def test_there_is_no_verb_that_prints_a_value():
    assert set(secret_cli.secret_group.commands) == {"exec"}
