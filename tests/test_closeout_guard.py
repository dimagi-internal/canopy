"""closeout_guard: an unattended turn may not end "safe to close" unless its verdict is CLOSED."""
import importlib.util
import io
import json
from pathlib import Path

import pytest

from orchestrator import closeout as co

_ROOT = Path(__file__).resolve().parents[1] / "plugins/canopy/hooks"
_SPEC = importlib.util.spec_from_file_location("closeout_guard", _ROOT / "closeout_guard.py")
g = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(g)

SID = "8b0c7d2e-1f8e-4b55-9d0c-5d8a2b7e9f10"
CLAIM = "Added a comment to #2768.\n\n✅ Session complete — safe to close."
INCOMPLETE_REC = {"incident": {"state": "ALARM", "checked_via": "alarm_mail:x"},
                  "cause": {"status": "likely"}, "remedy": "none",
                  "open_items": [{"what": "pull per-path latency", "owner": "someone with AWS SSO"}],
                  "closeout_steps": [{"name": "hal-turn-close", "exit_code": None}]}
CLOSED_REC = {"incident": {"state": "OK", "checked_via": "describe_alarm"},
              "cause": {"status": "unknown"}, "remedy": "none_needed",
              "closeout_steps": [{"name": "hal-turn-close", "exit_code": 0}]}


@pytest.fixture()
def roots(tmp_path, monkeypatch):
    caller, closeout = tmp_path / "caller", tmp_path / "closeout"
    (caller / "presence").mkdir(parents=True)
    monkeypatch.setattr(g, "CALLER_ROOT", str(caller))
    monkeypatch.setattr(g, "CLOSEOUT_ROOT", str(closeout))
    return caller, closeout


def _presence(roots, present=False, updated_at=1.0, turn_id="t1"):
    (roots[0] / "presence" / f"{SID}.json").write_text(json.dumps(
        {"origin": "canopy_web_chat" if present else "email", "present": present,
         "turn_id": turn_id, "updated_at": updated_at}))


def _record(roots, record, now=10.0, turn_id="t1"):
    return co.write(record, SID, turn_id=turn_id, root=str(roots[1]), now=now)


def _stop(monkeypatch, capsys, **data):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": SID, **data})))
    assert g.main() == 0
    out = capsys.readouterr().out
    return json.loads(out) if out else None


def test_safe_to_close_on_an_incomplete_verdict_is_blocked(roots, monkeypatch, capsys):
    _presence(roots)
    doc = _record(roots, INCOMPLETE_REC)
    out = _stop(monkeypatch, capsys, last_assistant_message=CLAIM)
    assert out["decision"] == "block"
    assert "INCOMPLETE" in out["reason"] and doc["status_line"] in out["reason"]
    assert "someone with AWS SSO" in out["reason"]


@pytest.mark.parametrize("record", [
    {**CLOSED_REC, "remedy": "merged_not_live", "open_items": [
        {"what": "deploy live", "owner": {"agent": "hal", "task": "T1", "recheck_at": "10:00Z"}}]},
    {**CLOSED_REC, "open_items": [{"what": "x", "owner": {"person": "@jjackson", "decision": "d"}}]},
])
def test_monitoring_and_needs_you_may_not_claim_safe_to_close_either(roots, monkeypatch, capsys, record):
    _presence(roots)
    _record(roots, record)
    assert _stop(monkeypatch, capsys, last_assistant_message=CLAIM)["decision"] == "block"


def test_the_verdicts_own_line_passes(roots, monkeypatch, capsys):
    _presence(roots)
    doc = _record(roots, INCOMPLETE_REC)
    assert _stop(monkeypatch, capsys, last_assistant_message="Summary.\n\n" + doc["status_line"]) is None


def test_closed_may_say_safe_to_close(roots, monkeypatch, capsys):
    _presence(roots)
    _record(roots, CLOSED_REC)
    assert _stop(monkeypatch, capsys, last_assistant_message=CLAIM) is None


def test_a_token_that_lies_about_the_verdict_is_blocked(roots, monkeypatch, capsys):
    _presence(roots)
    _record(roots, INCOMPLETE_REC)
    msg = "🔁 Session monitoring — fine. [closeout: MONITORING]"
    out = _stop(monkeypatch, capsys, last_assistant_message=msg)
    assert out["decision"] == "block" and "[closeout: MONITORING]" in out["reason"]


def test_no_record_is_silent(roots, monkeypatch, capsys):
    _presence(roots)
    assert _stop(monkeypatch, capsys, last_assistant_message=CLAIM) is None


def test_present_is_left_to_status_line_guard(roots, monkeypatch, capsys):
    _presence(roots, present=True)
    _record(roots, INCOMPLETE_REC)
    assert _stop(monkeypatch, capsys, last_assistant_message=CLAIM) is None


def test_a_record_from_an_earlier_turn_of_a_reused_session_is_ignored(roots, monkeypatch, capsys):
    _record(roots, INCOMPLETE_REC, now=10.0)
    _presence(roots, updated_at=20.0)  # a later prompt arrived after the record
    assert _stop(monkeypatch, capsys, last_assistant_message=CLAIM) is None


def test_a_record_for_another_turn_is_ignored(roots, monkeypatch, capsys):
    _record(roots, INCOMPLETE_REC, turn_id="old")
    _presence(roots, turn_id="new")
    assert _stop(monkeypatch, capsys, last_assistant_message=CLAIM) is None


def test_once_only(roots, monkeypatch, capsys):
    _presence(roots)
    _record(roots, INCOMPLETE_REC)
    assert _stop(monkeypatch, capsys, last_assistant_message=CLAIM, stop_hook_active=True) is None


def test_reads_the_transcript_when_no_last_message(roots, monkeypatch, capsys, tmp_path):
    _presence(roots)
    _record(roots, INCOMPLETE_REC)
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": CLAIM}]}}) + "\n")
    assert _stop(monkeypatch, capsys, transcript_path=str(t))["decision"] == "block"


@pytest.mark.parametrize("stdin", ["", "not json", "[]"])
def test_garbage_never_blocks(roots, monkeypatch, capsys, stdin):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    assert g.main() == 0 and capsys.readouterr().out == ""


def test_the_hook_and_the_engine_agree_on_the_token():
    assert g.TOKEN.pattern == co.TOKEN_RE.pattern


def test_it_is_registered_at_stop():
    hooks = json.loads((_ROOT / "hooks.json").read_text())["hooks"]["Stop"]
    assert any("closeout_guard.py" in h["command"] for e in hooks for h in e["hooks"])
