import json

import pytest

from orchestrator.stall_judge import (
    AUTO_SEND_CLASSES, AWAITING_CONTINUE, BLOCKED_HUMAN, DONE_CLAIMED, ERRORED,
    GATE_OUTBOUND, PLAN_PENDING, QUESTION_OPEN,
    Judgment, build_classify_prompt, build_reply_prompt, classify_tails,
    judge_replies, parse_batch_json,
)


def _runner_returning(payload):
    """A fake `claude -p`: records the prompt it saw, returns a canned body."""
    seen = {}

    def runner(prompt, model):
        seen["prompt"] = prompt
        seen["model"] = model
        return payload
    runner.seen = seen
    return runner


def test_v1_auto_send_envelope_is_exactly_three_classes():
    assert AUTO_SEND_CLASSES == frozenset(
        {AWAITING_CONTINUE, PLAN_PENDING, DONE_CLAIMED})


def test_gate_outbound_is_never_auto_send():
    for klass in (GATE_OUTBOUND, QUESTION_OPEN, BLOCKED_HUMAN, ERRORED):
        assert klass not in AUTO_SEND_CLASSES


def test_classify_prompt_lists_every_class_and_indexes_each_item():
    prompt = build_classify_prompt(["agent said A", "agent said B"])
    for klass in (AWAITING_CONTINUE, PLAN_PENDING, DONE_CLAIMED, QUESTION_OPEN,
                  BLOCKED_HUMAN, GATE_OUTBOUND, ERRORED):
        assert klass in prompt
    assert "agent said A" in prompt and "agent said B" in prompt
    assert '"index": 0' in prompt or "index 0" in prompt


def test_classify_prompt_never_mentions_the_human_reply():
    # The leakage guard: this prompt must be derivable in production, where no
    # human reply exists yet. (Exercised here to confirm it builds cleanly
    # from tails alone.)
    build_classify_prompt(["I've finished seeding. Next I'll re-render."])
    # Concrete: the caller cannot smuggle a reply in, because the signature
    # accepts only tails. Assert the signature, not the wording.
    import inspect
    assert list(inspect.signature(build_classify_prompt).parameters) == ["tails"]


def test_reply_prompt_takes_only_replies():
    import inspect
    assert list(inspect.signature(build_reply_prompt).parameters) == ["replies"]
    prompt = build_reply_prompt(["keep going"])
    assert "keep going" in prompt


def test_parse_batch_json_extracts_an_array_from_prose_and_fences():
    raw = 'Sure!\n```json\n[{"index": 0, "class": "awaiting_continue"}]\n```\nDone.'
    assert parse_batch_json(raw, 1) == [{"index": 0, "class": "awaiting_continue"}]


def test_parse_batch_json_raises_on_a_count_mismatch():
    raw = '[{"index": 0}]'
    with pytest.raises(ValueError, match="expected 2"):
        parse_batch_json(raw, 2)


def test_parse_batch_json_raises_on_unparseable_output():
    with pytest.raises(ValueError):
        parse_batch_json("the model apologised and returned nothing", 1)


def test_classify_tails_maps_verdicts_back_by_index_not_by_order():
    # A model that returns items out of order must still be read correctly.
    payload = json.dumps([
        {"index": 1, "class": BLOCKED_HUMAN, "confidence": 0.9, "reason": "needs a login"},
        {"index": 0, "class": AWAITING_CONTINUE, "confidence": 0.8, "reason": "stated next step"},
    ])
    out = classify_tails(["a", "b"], runner=_runner_returning(payload))
    assert [j.klass for j in out] == [AWAITING_CONTINUE, BLOCKED_HUMAN]
    assert out[1].reason == "needs a login"


def test_classify_tails_rejects_an_unknown_class():
    payload = json.dumps([{"index": 0, "class": "vibes", "confidence": 1.0, "reason": "x"}])
    with pytest.raises(ValueError, match="vibes"):
        classify_tails(["a"], runner=_runner_returning(payload))


def test_classify_tails_propagates_a_runner_failure():
    # Fail loud: there is no deterministic fallback, so a swallowed error would
    # fabricate a measurement.
    def boom(prompt, model):
        raise RuntimeError("claude -p failed")
    with pytest.raises(RuntimeError):
        classify_tails(["a"], runner=boom)


def test_classify_tails_on_an_empty_list_makes_no_call():
    def boom(prompt, model):
        raise AssertionError("must not call the model for zero items")
    assert classify_tails([], runner=boom) == []


def test_judge_replies_returns_bools_in_input_order():
    payload = json.dumps([
        {"index": 0, "mechanical": True},
        {"index": 1, "mechanical": False},
    ])
    assert judge_replies(["keep going", "no, use prod"],
                         runner=_runner_returning(payload)) == [True, False]


def test_judge_replies_on_an_empty_list_makes_no_call():
    def boom(prompt, model):
        raise AssertionError("must not call the model for zero items")
    assert judge_replies([], runner=boom) == []


def test_judgment_carries_a_reason():
    payload = json.dumps([{"index": 0, "class": DONE_CLAIMED,
                           "confidence": 0.7, "reason": "claims the PR merged"}])
    out = classify_tails(["all done"], runner=_runner_returning(payload))
    assert isinstance(out[0], Judgment)
    assert out[0].reason == "claims the PR merged"
    assert 0.0 <= out[0].confidence <= 1.0
