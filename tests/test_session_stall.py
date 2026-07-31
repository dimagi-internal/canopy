import json

import pytest

from orchestrator.session_stall import (
    hands_back_to_human, last_assistant_record, final_assistant_text, is_working,
    StallVerdict, WORKING, classify_sessions,
)
from orchestrator.stall_judge import AWAITING_CONTINUE, BLOCKED_HUMAN


def _asst(text: str, stop_reason: str | None = "end_turn") -> dict:
    return {"type": "assistant",
            "message": {"stop_reason": stop_reason,
                        "content": [{"type": "text", "text": text}]}}


def _user(text: str) -> dict:
    return {"type": "user", "message": {"content": text}}


def test_tool_use_is_not_a_handback():
    assert hands_back_to_human(_asst("running it", "tool_use")) is False


def test_end_turn_is_a_handback():
    assert hands_back_to_human(_asst("done", "end_turn")) is True


def test_max_tokens_is_a_handback():
    # Any terminal reason means the model stopped and a person is next.
    assert hands_back_to_human(_asst("...", "max_tokens")) is True


def test_missing_stop_reason_is_not_an_ending():
    # A writer that omits the field must not end every turn.
    assert hands_back_to_human(_asst("hm", None)) is False


def test_user_record_is_never_a_handback():
    assert hands_back_to_human(_user("keep going")) is False


def test_last_assistant_record_ignores_trailing_user_records():
    recs = [_asst("first"), _user("keep going"), _asst("second")]
    assert final_assistant_text([recs[0]]) == "first"
    assert last_assistant_record(recs)["message"]["content"][0]["text"] == "second"


def test_final_assistant_text_joins_text_blocks_only():
    rec = {"type": "assistant", "message": {"stop_reason": "end_turn", "content": [
        {"type": "text", "text": "hello"},
        {"type": "tool_use", "name": "Bash", "input": {}},
        {"type": "text", "text": "world"},
    ]}}
    assert final_assistant_text([rec]) == "hello\nworld"


def test_is_working_follows_the_last_assistant_record():
    assert is_working([_asst("a", "end_turn"), _asst("b", "tool_use")]) is True
    assert is_working([_asst("a", "tool_use"), _asst("b", "end_turn")]) is False


def test_is_working_is_false_for_an_empty_transcript():
    assert is_working([]) is False


def _runner_returning(payload):
    def runner(prompt, model):
        runner.prompt = prompt
        return payload
    return runner


def test_working_sessions_are_classified_without_any_model_call():
    def boom(prompt, model):
        raise AssertionError("a working session must not cost an LLM call")
    out = classify_sessions([("s1", [_asst("checking", "tool_use")])], runner=boom)
    assert out["s1"].state == "working"
    assert out["s1"].klass == WORKING


def test_a_stalled_session_is_judged_and_keyed_by_id():
    payload = json.dumps([{"index": 0, "class": AWAITING_CONTINUE,
                           "confidence": 0.8, "reason": "stated next step"}])
    out = classify_sessions([("s1", [_asst("Next I'll re-render.")])],
                            runner=_runner_returning(payload))
    assert out["s1"].state == "stalled"
    assert out["s1"].klass == AWAITING_CONTINUE
    assert out["s1"].stop_reason == "end_turn"
    assert out["s1"].reason == "stated next step"


def test_only_stalled_sessions_are_sent_to_the_model():
    payload = json.dumps([{"index": 0, "class": BLOCKED_HUMAN,
                           "confidence": 0.9, "reason": "needs a login"}])
    runner = _runner_returning(payload)
    out = classify_sessions([
        ("working1", [_asst("checking", "tool_use")]),
        ("stalled1", [_asst("I need you to log in.")]),
    ], runner=runner)
    assert out["working1"].klass == WORKING
    assert out["stalled1"].klass == BLOCKED_HUMAN
    # The working session's text must never reach the prompt.
    assert "checking" not in runner.prompt
    assert "log in" in runner.prompt


def test_an_empty_input_makes_no_call():
    def boom(prompt, model):
        raise AssertionError("no sessions, no call")
    assert classify_sessions([], runner=boom) == {}


def test_all_working_makes_no_call():
    def boom(prompt, model):
        raise AssertionError("nothing stalled, no call")
    out = classify_sessions([("s1", [_asst("x", "tool_use")])], runner=boom)
    assert out["s1"].klass == WORKING
