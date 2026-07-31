import pytest

from orchestrator.session_stall import (
    hands_back_to_human, last_assistant_record, final_assistant_text, is_working,
    is_mechanical,
)


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


# Verbatim from the 24h census (spec §1) — typos included on purpose.
MECHANICAL = [
    "keep going", "Keep going.", "keep going, are we good to close out?",
    "try again", "yes", "yeah go ahead", "yeah go for it", "do the plan",
    "okay merge it", "are you still working?", "good to close out?",
    "are we ready to close out this session?", "is this session good to close out?",
    "are we deployed and ready to close out this session?",
    "okay should I close this out now?", "are we good to closeo ut this session?",
    "finish whatever you are woring on and then lets close out this session",
]

SUBSTANTIVE = [
    "No I disagree - for ace-web, we can give someone membership to an opp if we need to",
    "why do you think sophie has no connect account?",
    "do all the ddd runs on prod not local",
    "wait why didn't we run deliver? we aren't ready to send back an iteration",
    "I feel like you are making this too complicated, why do you need something deployed to AWS",
    "okay i'm lost, what is next?",
    "no not supply, the arc for continuous monitoring",
]


@pytest.mark.parametrize("text", MECHANICAL)
def test_mechanical_prompts_are_recognised(text):
    assert is_mechanical(text) is True, text


@pytest.mark.parametrize("text", SUBSTANTIVE)
def test_substantive_prompts_are_not_mechanical(text):
    assert is_mechanical(text) is False, text


def test_long_text_is_never_mechanical_even_if_it_starts_with_a_cue():
    # "yes, but ..." carries a redirection; length is the cheap guard.
    assert is_mechanical("yes but first rewrite the whole seeding path to be programmatic "
                         "and then check the arc for continuous monitoring instead") is False


def test_empty_is_not_mechanical():
    assert is_mechanical("") is False
    assert is_mechanical("   ") is False
