"""agent_task_guard: a turn holding an agent task cannot end before its result is accepted.

Evidence (2026-10-27, Jonathan designing agent tasks): "we will likely have you respond too
soon" — an agent that answers in chat or summarises and stops leaves the calling page waiting.
"""
import importlib.util
import io
import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1] / "plugins/canopy/hooks"
_SPEC = importlib.util.spec_from_file_location("agent_task_guard", _ROOT / "agent_task_guard.py")
g = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(g)

SID = "632136fd-ed49-4fb6-bcac-1df69e0c597f"
TURN = "b8489f84-bd40-4c33-bb0d-8868938d0c33"
TASK = "5f0c1a2e-0000-4000-8000-000000000001"


@pytest.fixture()
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "CALLER_ROOT", str(tmp_path))
    return tmp_path


def _turn(root, task=None):
    (root / "presence").mkdir(exist_ok=True)
    (root / "presence" / f"{SID}.json").write_text(json.dumps({"turn_id": TURN, "present": False}))
    env = {"trigger": {"origin": "api"}}
    if task is not None:
        env["agent_task"] = task
    (root / f"{TURN}.json").write_text(json.dumps(env))


def _stop(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": SID})))
    assert g.main() == 0
    out = capsys.readouterr().out
    return json.loads(out) if out else None


@pytest.mark.parametrize("status", ["queued", "running"])
def test_an_open_task_blocks_the_stop(root, monkeypatch, capsys, status):
    _turn(root, {"id": TASK, "name": "gmail-triage.review"})
    monkeypatch.setattr(g, "task_status", lambda _id: status)
    out = _stop(monkeypatch, capsys)
    assert out["decision"] == "block"
    assert "submit_agent_task_result" in out["reason"] and TASK in out["reason"]


@pytest.mark.parametrize("status", ["done", "failed", "cancelled", ""])
def test_a_finished_or_unreadable_task_lets_it_stop(root, monkeypatch, capsys, status):
    _turn(root, {"id": TASK, "name": "x"})
    monkeypatch.setattr(g, "task_status", lambda _id: status)
    assert _stop(monkeypatch, capsys) is None


def test_no_agent_task_never_asks_canopy(root, monkeypatch, capsys):
    _turn(root, None)

    def boom(_id):
        raise AssertionError("must not call canopy-web for a turn with no task")
    monkeypatch.setattr(g, "task_status", boom)
    assert _stop(monkeypatch, capsys) is None


def test_no_presence_record_is_silent(root, monkeypatch, capsys):
    assert _stop(monkeypatch, capsys) is None


def test_blocks_are_bounded_then_the_server_backstop_owns_it(root, monkeypatch, capsys):
    _turn(root, {"id": TASK, "name": "x"})
    monkeypatch.setattr(g, "task_status", lambda _id: "running")
    outs = [_stop(monkeypatch, capsys) for _ in range(g.MAX_BLOCKS + 2)]
    assert [bool(o) for o in outs] == [True] * g.MAX_BLOCKS + [False, False]


def test_a_broken_guard_never_wedges_the_session(root, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert g.main() == 0 and capsys.readouterr().out == ""


def test_hooks_json_registers_it_on_stop():
    hooks = json.loads((_ROOT / "hooks.json").read_text())["hooks"]["Stop"]
    cmds = [h["command"] for e in hooks for h in e["hooks"]]
    assert any("agent_task_guard.py" in c for c in cmds)
