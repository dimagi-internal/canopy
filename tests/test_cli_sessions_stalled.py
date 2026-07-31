import json
from pathlib import Path

from click.testing import CliRunner

from orchestrator.cli import main
from orchestrator.session_stall import StallVerdict
from orchestrator.stall_judge import AWAITING_CONTINUE, GATE_OUTBOUND


def _write_transcript(path: Path, stop_reason: str, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"type": "user", "message": {"content": "do the thing"},
         "timestamp": "2026-07-30T10:00:00Z", "sessionId": path.stem, "cwd": "/tmp/proj"},
        {"type": "assistant",
         "message": {"stop_reason": stop_reason,
                     "content": [{"type": "text", "text": text}]},
         "timestamp": "2026-07-30T10:01:00Z", "sessionId": path.stem, "cwd": "/tmp/proj"},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _fake_classify(verdicts):
    def fake(records_by_id, *, runner=None, model="haiku"):
        return {sid: verdicts[sid] for sid, _ in records_by_id}
    return fake


def test_stalled_reports_a_waiting_session(tmp_path, monkeypatch):
    _write_transcript(tmp_path / ".claude" / "projects" / "-tmp-proj" / "sess-a.jsonl",
                      "end_turn", "Done seeding. Next I'll re-render.")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        "orchestrator.session_stall.classify_sessions",
        _fake_classify({"sess-a": StallVerdict(
            "stalled", AWAITING_CONTINUE, 0.8, "stated next step", "end_turn")}))

    res = CliRunner().invoke(main, ["sessions", "stalled", "--json-output"])
    assert res.exit_code == 0, res.output
    rows = json.loads(res.output)
    assert len(rows) == 1
    assert rows[0]["class"] == AWAITING_CONTINUE
    assert rows[0]["auto_send"] is True
    assert rows[0]["reason"] == "stated next step"


def test_stalled_omits_a_working_session(tmp_path, monkeypatch):
    _write_transcript(tmp_path / ".claude" / "projects" / "-tmp-proj" / "sess-b.jsonl",
                      "tool_use", "checking now")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        "orchestrator.session_stall.classify_sessions",
        _fake_classify({"sess-b": StallVerdict("working", "working", 1.0, "", "tool_use")}))

    res = CliRunner().invoke(main, ["sessions", "stalled", "--json-output"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output) == []


def test_gate_outbound_is_reported_but_not_auto_send(tmp_path, monkeypatch):
    _write_transcript(tmp_path / ".claude" / "projects" / "-tmp-proj" / "sess-c.jsonl",
                      "end_turn", "Approve and I'll send it to the partner.")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        "orchestrator.session_stall.classify_sessions",
        _fake_classify({"sess-c": StallVerdict(
            "stalled", GATE_OUTBOUND, 0.9, "wants send approval", "end_turn")}))

    res = CliRunner().invoke(main, ["sessions", "stalled", "--json-output"])
    rows = json.loads(res.output)
    assert rows[0]["class"] == GATE_OUTBOUND
    assert rows[0]["auto_send"] is False
