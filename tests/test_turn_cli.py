"""`canopy turn abort`: the turn ends FAILED on canopy-web and is locked here, filed or not."""
import json

import pytest
from click.testing import CliRunner

from orchestrator import canopy_web, turn_cli
from orchestrator.cli import main

TURN = "599fad3b-c647-4ace-b3e2-205573aa18d0"


@pytest.fixture()
def locks(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_cli, "LOCK_DIR", tmp_path)
    return tmp_path


def test_abort_files_and_locks(locks, monkeypatch):
    calls = []
    monkeypatch.setattr(canopy_web, "call",
                        lambda m, p, body=None, **k: calls.append((m, p, body)) or {"status": "failed"})
    res = CliRunner().invoke(main, ["turn", "abort", "--turn", TURN, "--tool", "mcp__x__y",
                                    "--reason", "unregistered callers"])
    assert res.exit_code == 0, res.output
    assert calls == [("POST", f"/api/harness/turns/{TURN}/abort",
                      {"reason": "unregistered callers", "tool": "mcp__x__y"})]
    out = json.loads(res.output)
    assert out["filed"] is True and out["status"] == "failed" and "Stop now" in out["next"]
    assert json.loads((locks / f"{TURN}.json").read_text())["filed"] is True


def test_a_refusal_still_locks_and_exits_3(locks, monkeypatch):
    def refuse(*a, **k):
        raise canopy_web.CanopyError("409 Turn not abortable")
    monkeypatch.setattr(canopy_web, "call", refuse)
    res = CliRunner().invoke(main, ["turn", "abort", "--turn", TURN, "--reason", "x"])
    assert res.exit_code == turn_cli.NOT_FILED
    out = json.loads(res.output)
    assert out["filed"] is False and "NOT filed on canopy-web" in out["next"]
    assert json.loads((locks / f"{TURN}.json").read_text())["filed"] is False


def test_the_turn_defaults_to_this_sessions(locks, monkeypatch):
    monkeypatch.setenv("CANOPY_TURN_ID", TURN)
    seen = []
    monkeypatch.setattr(canopy_web, "call", lambda m, p, body=None, **k: seen.append(p) or {})
    assert CliRunner().invoke(main, ["turn", "abort", "--reason", "x"]).exit_code == 0
    assert seen == [f"/api/harness/turns/{TURN}/abort"]


def test_no_turn_is_an_error(locks, monkeypatch):
    monkeypatch.setattr(turn_cli, "current_turn_id", lambda: "")
    res = CliRunner().invoke(main, ["turn", "abort", "--reason", "x"])
    assert res.exit_code != 0 and "no turn to abort" in res.output


def test_the_hook_and_the_cli_share_the_lock_directory():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "plugins/canopy/hooks/credential_guard.py"
    spec = importlib.util.spec_from_file_location("credential_guard", path)
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    assert Path(g.LOCK_ROOT) == Path.home() / ".canopy" / "turn-abort"
    assert Path.home() / ".canopy" / "turn-abort" == turn_cli.LOCK_DIR
