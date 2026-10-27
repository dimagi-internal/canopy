"""status_line_guard: no "Session complete/paused" line while the person is in the conversation.

Evidence (2026-10-27, ada): a turn began on Jonathan's email in auto mode; he joined in chat and
it ended three replies in a row on "✅ Session complete — safe to close." with promised work undone.
"""
import importlib.util
import io
import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1] / "plugins/canopy/hooks"
_SPEC = importlib.util.spec_from_file_location("status_line_guard", _ROOT / "status_line_guard.py")
g = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(g)

SID = "632136fd-ed49-4fb6-bcac-1df69e0c597f"
CLOSE = "Dispatched. Next turn I'll file the rest.\n\n✅ Session complete — safe to close."


@pytest.fixture()
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "CALLER_ROOT", str(tmp_path))
    return tmp_path


def _presence(root, origin, present):
    (root / "presence").mkdir(exist_ok=True)
    (root / "presence" / f"{SID}.json").write_text(
        json.dumps({"origin": origin, "present": present, "turn_id": "t", "updated_at": 0}))


def _stop(monkeypatch, capsys, **data):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": SID, **data})))
    assert g.main() == 0
    out = capsys.readouterr().out
    return json.loads(out) if out else None


def test_present_and_a_status_line_is_blocked(root, monkeypatch, capsys):
    _presence(root, "canopy_web_chat", True)
    out = _stop(monkeypatch, capsys, last_assistant_message=CLOSE)
    assert out["decision"] == "block" and "canopy_web_chat" in out["reason"]


def test_the_pause_line_is_blocked_too(root, monkeypatch, capsys):
    _presence(root, "slack", True)
    msg = "⏸ Session paused — waiting on you for: the merge."
    assert _stop(monkeypatch, capsys, last_assistant_message=msg)["decision"] == "block"


def test_absent_keeps_the_status_line(root, monkeypatch, capsys):
    _presence(root, "email", False)
    assert _stop(monkeypatch, capsys, last_assistant_message=CLOSE) is None


def test_present_without_a_status_line_stops_normally(root, monkeypatch, capsys):
    _presence(root, "canopy_web_chat", True)
    assert _stop(monkeypatch, capsys, last_assistant_message="Done — PR #12 merged.") is None


def test_it_never_loops(root, monkeypatch, capsys):
    _presence(root, "canopy_web_chat", True)
    assert _stop(monkeypatch, capsys, last_assistant_message=CLOSE, stop_hook_active=True) is None


def test_no_presence_record_is_silent(root, monkeypatch, capsys):
    assert _stop(monkeypatch, capsys, last_assistant_message=CLOSE) is None


def test_falls_back_to_the_transcript(root, tmp_path, monkeypatch, capsys):
    _presence(root, "canopy_web_chat", True)
    tp = tmp_path / "t.jsonl"
    tp.write_text("\n".join(json.dumps(r) for r in (
        {"type": "user", "message": {"content": "hi"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": CLOSE}]}},
        {"type": "attachment"})) + "\n")
    assert _stop(monkeypatch, capsys, transcript_path=str(tp))["decision"] == "block"


@pytest.mark.parametrize("stdin", ["", "not json", "[]"])
def test_garbage_never_blocks(root, monkeypatch, capsys, stdin):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    assert g.main() == 0 and capsys.readouterr().out == ""


def test_it_is_registered_at_stop():
    hooks = json.loads((_ROOT / "hooks.json").read_text())["hooks"]["Stop"]
    assert any("status_line_guard.py" in h["command"] for e in hooks for h in e["hooks"])
