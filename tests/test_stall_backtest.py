# tests/test_stall_backtest.py
from orchestrator.stall_backtest import (
    BacktestCase, Handback, collect_handbacks, grade, human_text, score,
)
from orchestrator.stall_judge import AWAITING_CONTINUE, QUESTION_OPEN


def _asst(text, stop_reason="end_turn"):
    return {"type": "assistant",
            "message": {"stop_reason": stop_reason,
                        "content": [{"type": "text", "text": text}]}}


def _user(text):
    return {"type": "user", "message": {"content": text}}


NEXT_STEP = "Seeding is done. Next I'll re-render the walkthrough."


def test_collects_a_handback_and_its_reply():
    hbs = collect_handbacks([_asst(NEXT_STEP), _user("keep going")])
    assert hbs == [Handback(tail=NEXT_STEP, reply="keep going")]


def test_tool_use_records_are_not_handbacks():
    assert collect_handbacks([_asst("working", "tool_use"), _user("keep going")]) == []


def test_a_trailing_handback_with_no_reply_is_skipped():
    assert collect_handbacks([_asst(NEXT_STEP)]) == []


def test_multiple_handbacks_all_collected():
    hbs = collect_handbacks([
        _asst(NEXT_STEP), _user("keep going"),
        _asst(NEXT_STEP), _user("do the ddd runs on prod"),
    ])
    assert len(hbs) == 2 and hbs[1].reply == "do the ddd runs on prod"


def test_harness_injections_are_not_human_replies():
    for junk in ("<task-notification>x</task-notification>",
                 "<command-message>ace:turn</command-message>",
                 "Base directory for this skill: /x",
                 "Caveat: the messages below...",
                 "[Request interrupted by user]",
                 "[Image: original 1080x2400]"):
        assert human_text(_user(junk)) is None
    assert human_text(_user("keep going")) == "keep going"


def test_tool_results_are_not_human_replies():
    rec = {"type": "user", "message": {"content": [
        {"type": "tool_result", "content": "ok"}]}}
    assert human_text(rec) is None


def test_a_handback_followed_only_by_junk_is_skipped():
    assert collect_handbacks([_asst(NEXT_STEP),
                              _user("<task-notification>x</task-notification>")]) == []


def test_grade_pairs_each_classification_with_its_reply_judgment():
    hbs = [Handback(NEXT_STEP, "keep going"), Handback("A or B?", "use A")]

    def fake_classify(tails, model="haiku"):
        assert tails == [NEXT_STEP, "A or B?"]        # tails only — no replies leak in
        from orchestrator.stall_judge import Judgment
        return [Judgment(AWAITING_CONTINUE, 0.8, "next step"),
                Judgment(QUESTION_OPEN, 0.9, "asked a question")]

    def fake_judge(replies, model="haiku"):
        assert replies == ["keep going", "use A"]     # replies only — no tails leak in
        return [True, False]

    cases = grade(hbs, classify=fake_classify, judge=fake_judge)
    assert cases[0] == BacktestCase(AWAITING_CONTINUE, True, True, NEXT_STEP, "keep going")
    assert cases[1].would_send is False


def test_grade_of_nothing_calls_neither_model():
    def boom(items, model="haiku"):
        raise AssertionError("no handbacks, no calls")
    assert grade([], classify=boom, judge=boom) == []


def test_score_computes_precision_per_class():
    cases = [
        BacktestCase(AWAITING_CONTINUE, True, True, "t", "keep going"),
        BacktestCase(AWAITING_CONTINUE, True, True, "t", "yes"),
        BacktestCase(AWAITING_CONTINUE, True, False, "t", "no, use prod instead"),
    ]
    result = score(cases)
    per = result["per_class"][AWAITING_CONTINUE]
    assert per["tp"] == 2 and per["fp"] == 1
    assert abs(per["precision"] - 2 / 3) < 1e-9
    assert result["overall"]["n"] == 3


def test_score_counts_a_miss_as_recall_loss_not_a_false_positive():
    cases = [BacktestCase(QUESTION_OPEN, False, True, "t", "keep going")]
    result = score(cases)
    assert result["overall"]["fp"] == 0
    assert result["overall"]["tp"] == 0
    assert result["overall"]["recall"] == 0.0


def test_score_of_nothing_is_zero_not_a_crash():
    result = score([])
    assert result["overall"]["n"] == 0
    assert result["overall"]["precision"] == 0.0
