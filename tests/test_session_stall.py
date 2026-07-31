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


def test_multiple_stalled_sessions_are_each_keyed_to_the_right_verdict():
    # Two+ stalled sessions, judged in different classes by the (fake) model.
    # The `zip(stalled_ids, stalled_stop_reasons, judgments)` pairing inside
    # classify_sessions is the only thing keeping each verdict on its own
    # session id — a mis-zip or an index-[0]-always bug would nudge the wrong
    # session in production. Order the model's response differently from
    # input order (index 1 before index 0) so a bug that just returned
    # judgments in call order, rather than respecting `index`, would also be
    # caught.
    payload = json.dumps([
        {"index": 1, "class": BLOCKED_HUMAN, "confidence": 0.9, "reason": "needs a login"},
        {"index": 0, "class": AWAITING_CONTINUE, "confidence": 0.8, "reason": "stated next step"},
    ])
    out = classify_sessions([
        ("sess-first", [_asst("Next I'll re-render.")]),
        ("sess-second", [_asst("I need you to log in.")]),
    ], runner=_runner_returning(payload))

    assert out["sess-first"].klass == AWAITING_CONTINUE
    assert out["sess-first"].reason == "stated next step"
    assert out["sess-second"].klass == BLOCKED_HUMAN
    assert out["sess-second"].reason == "needs a login"


def test_an_empty_input_makes_no_call():
    def boom(prompt, model):
        raise AssertionError("no sessions, no call")
    assert classify_sessions([], runner=boom) == {}


def test_all_working_makes_no_call():
    def boom(prompt, model):
        raise AssertionError("nothing stalled, no call")
    out = classify_sessions([("s1", [_asst("x", "tool_use")])], runner=boom)
    assert out["s1"].klass == WORKING


# ── chunking / retry hardening (Item 3: production inherits the backtest's
# fail-loud-but-not-fail-total contract, via stall_judge's shared machinery)
# ────────────────────────────────────────────────────────────────────────────
# These monkeypatch `orchestrator.stall_judge.classify_tails` directly (a
# module attribute, looked up at call time by `classify_sessions`' internal
# `_classify` closure) rather than going through `runner=`, so the fakes can
# return `Judgment`s / raise directly instead of round-tripping through a
# JSON prompt.

def test_classify_sessions_chunks_by_batch_size(monkeypatch):
    from orchestrator.stall_judge import Judgment

    calls: list[list[str]] = []

    def fake_classify_tails(tails, *, runner=None, model="haiku"):
        calls.append(list(tails))
        return [Judgment(AWAITING_CONTINUE, 0.5, "r") for _ in tails]

    monkeypatch.setattr("orchestrator.stall_judge.classify_tails", fake_classify_tails)

    records_by_id = [(f"s{i}", [_asst(f"tail{i}")]) for i in range(5)]
    out = classify_sessions(records_by_id, batch_size=2)

    assert [len(c) for c in calls] == [2, 2, 1]
    assert len(out) == 5
    for i in range(5):
        assert out[f"s{i}"].klass == AWAITING_CONTINUE


def test_classify_sessions_retries_a_chunk_that_fails_once_then_succeeds(monkeypatch):
    from orchestrator.stall_judge import Judgment

    calls = {"n": 0}

    def flaky_classify_tails(tails, *, runner=None, model="haiku"):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("expected 1 items in model output, got 0")
        return [Judgment(AWAITING_CONTINUE, 0.5, "r") for _ in tails]

    monkeypatch.setattr("orchestrator.stall_judge.classify_tails", flaky_classify_tails)

    out = classify_sessions([("s1", [_asst("Next I'll re-render.")])])
    assert out["s1"].klass == AWAITING_CONTINUE
    assert calls["n"] == 2  # one failure, one retry that succeeded


def test_classify_sessions_skips_a_chunk_that_never_recovers_others_still_land(monkeypatch):
    # The clean --hours 168 run died on exactly this flake class before
    # `classify_sessions` was hardened -- confirm a permanently-failing
    # chunk drops ONLY its own sessions, absent (not fabricated) from the
    # result, while a healthy chunk's sessions still land, correctly keyed.
    from orchestrator.stall_judge import Judgment

    def flaky_classify_tails(tails, *, runner=None, model="haiku"):
        if tails[0] == "bad_tail":
            raise ValueError("expected 1 items in model output, got 0")
        return [Judgment(AWAITING_CONTINUE, 0.5, "r") for _ in tails]

    monkeypatch.setattr("orchestrator.stall_judge.classify_tails", flaky_classify_tails)

    records_by_id = [("bad", [_asst("bad_tail")]), ("good", [_asst("good_tail")])]
    stats: dict = {}
    out = classify_sessions(records_by_id, batch_size=1, retries=1, stats=stats)

    assert "bad" not in out  # absent, never a fabricated verdict
    assert out["good"].klass == AWAITING_CONTINUE
    assert stats["chunks"] == 2
    assert stats["chunks_failed"] == 1
    assert stats["sessions_skipped"] == 1


def test_classify_sessions_does_not_retry_or_swallow_a_runtime_error(monkeypatch):
    calls = {"n": 0}

    def broken_classify_tails(tails, *, runner=None, model="haiku"):
        calls["n"] += 1
        raise RuntimeError("claude binary not found")

    monkeypatch.setattr("orchestrator.stall_judge.classify_tails", broken_classify_tails)

    with pytest.raises(RuntimeError):
        classify_sessions([("s1", [_asst("Next I'll re-render.")])])
    assert calls["n"] == 1  # not retried


def test_classify_sessions_skips_a_session_with_an_empty_tail():
    # hands_back_to_human only inspects stop_reason, never content shape, so
    # an end_turn record with no text blocks is not provably unreachable --
    # sending it to the model would fabricate a case, same discipline as
    # collect_handbacks' empty-tail guard. One such session existed in the
    # live corpus and the old unchunked call sent it regardless.
    def boom(prompt, model):
        raise AssertionError("an empty tail must never reach the model")
    out = classify_sessions([("s1", [_asst("")])], runner=boom)
    assert "s1" not in out


def test_classify_sessions_truncates_a_long_tail_keeping_the_end(monkeypatch):
    from orchestrator.stall_judge import TAIL_MAX_CHARS, Judgment

    captured: dict = {}

    def fake_classify_tails(tails, *, runner=None, model="haiku"):
        captured["tails"] = list(tails)
        return [Judgment(AWAITING_CONTINUE, 0.5, "r") for _ in tails]

    monkeypatch.setattr("orchestrator.stall_judge.classify_tails", fake_classify_tails)

    long_tail = ("x" * (TAIL_MAX_CHARS + 500)) + "TAIL_MARKER_KEPT"
    classify_sessions([("s1", [_asst(long_tail)])])

    assert len(captured["tails"][0]) == TAIL_MAX_CHARS
    assert captured["tails"][0].endswith("TAIL_MARKER_KEPT")


def test_classify_sessions_default_stats_is_none_and_still_works():
    from orchestrator.stall_judge import Judgment

    def fake_runner(prompt, model):
        return json.dumps([{"index": 0, "class": AWAITING_CONTINUE,
                             "confidence": 0.5, "reason": "r"}])

    out = classify_sessions([("s1", [_asst("Next I'll re-render.")])],
                            runner=fake_runner)  # no stats=...
    assert out["s1"].klass == AWAITING_CONTINUE
