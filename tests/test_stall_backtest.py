# tests/test_stall_backtest.py
from orchestrator.stall_backtest import (
    REPLY_MAX_CHARS, TAIL_MAX_CHARS,
    BacktestCase, Handback, collect_handbacks, grade, human_text, score,
)
from orchestrator.stall_judge import (
    AWAITING_CONTINUE, BLOCKED_HUMAN, DONE_CLAIMED, PLAN_PENDING, QUESTION_OPEN,
)


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


SECOND_STEP = "Rendered the walkthrough. Next I'll upload it to canopy-web."


def test_multiple_handbacks_all_collected():
    # Regression guard for the final_assistant_text trap: each handback's
    # tail must come from ITS OWN assistant record, not the transcript's
    # last one. Two DIFFERENT tails, each asserted independently, is what
    # makes that distinguishable — if `_tail_text` were swapped back for
    # `final_assistant_text(records)`, both tails would collapse to
    # SECOND_STEP and this would fail.
    hbs = collect_handbacks([
        _asst(NEXT_STEP), _user("keep going"),
        _asst(SECOND_STEP), _user("do the ddd runs on prod"),
    ])
    assert len(hbs) == 2
    assert hbs[0].tail == NEXT_STEP
    assert hbs[0].reply == "keep going"
    assert hbs[1].tail == SECOND_STEP
    assert hbs[1].reply == "do the ddd runs on prod"


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


def test_a_handback_with_an_empty_tail_is_skipped():
    # An end_turn record with no text blocks (or only whitespace) is not
    # provably unreachable -- hands_back_to_human only looks at stop_reason.
    # Grading an empty tail would silently hand the model a fabricated case.
    assert collect_handbacks([_asst(""), _user("keep going")]) == []
    assert collect_handbacks([_asst("   "), _user("keep going")]) == []


def test_collect_handbacks_truncates_a_long_tail_keeping_the_end():
    # Direction matters: the "next I'll..." / open-question signal lives at
    # the END of an agent's message, so truncation must discard the HEAD.
    head_marker = "HEAD_MARKER_DISCARDED"
    tail_marker = "TAIL_MARKER_KEPT"
    long_tail = head_marker + ("x" * 3000) + tail_marker

    hbs = collect_handbacks([_asst(long_tail), _user("keep going")])

    assert len(hbs) == 1
    assert len(hbs[0].tail) == TAIL_MAX_CHARS
    assert hbs[0].tail.endswith(tail_marker)
    assert head_marker not in hbs[0].tail


def test_collect_handbacks_truncates_a_long_reply_keeping_the_start():
    # Opposite direction from the tail: a reply long enough to truncate is
    # already substantive, so keeping the START can only ever discard MORE
    # evidence of substance -- it can never manufacture "mechanical."
    head_marker = "HEAD_MARKER_KEPT"
    tail_marker = "TAIL_MARKER_DISCARDED"
    long_reply = head_marker + ("x" * 2000) + tail_marker

    hbs = collect_handbacks([_asst(NEXT_STEP), _user(long_reply)])

    assert len(hbs) == 1
    assert len(hbs[0].reply) == REPLY_MAX_CHARS
    assert hbs[0].reply.startswith(head_marker)
    assert tail_marker not in hbs[0].reply


def test_collect_handbacks_leaves_short_values_untouched():
    hbs = collect_handbacks([_asst(NEXT_STEP), _user("keep going")])

    assert hbs == [Handback(tail=NEXT_STEP, reply="keep going")]
    assert "..." not in hbs[0].tail
    assert "..." not in hbs[0].reply


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


def test_grade_chunks_calls_by_batch_size():
    hbs = [Handback(f"tail{i}", f"reply{i}") for i in range(5)]
    classify_calls: list[list[str]] = []
    judge_calls: list[list[str]] = []

    def fake_classify(tails, model="haiku"):
        classify_calls.append(list(tails))
        from orchestrator.stall_judge import Judgment
        return [Judgment(AWAITING_CONTINUE, 0.5, "r") for _ in tails]

    def fake_judge(replies, model="haiku"):
        judge_calls.append(list(replies))
        return [True for _ in replies]

    grade(hbs, classify=fake_classify, judge=fake_judge, batch_size=2)

    assert len(classify_calls) == 3
    assert len(judge_calls) == 3
    assert [len(c) for c in classify_calls] == [2, 2, 1]
    assert [len(c) for c in judge_calls] == [2, 2, 1]


def test_grade_preserves_input_order_across_chunk_boundaries():
    # Distinct classes per item so a mis-ordered accumulation would be
    # caught: each fake response is derived from the item's OWN index, so
    # if chunks were assembled out of order, `cases` would come back with
    # klasses out of sequence relative to `hbs`.
    classes = [AWAITING_CONTINUE, PLAN_PENDING, DONE_CLAIMED, QUESTION_OPEN, BLOCKED_HUMAN]
    hbs = [Handback(f"tail{i}", f"reply{i}") for i in range(5)]

    def fake_classify(tails, model="haiku"):
        from orchestrator.stall_judge import Judgment
        return [Judgment(classes[int(t.removeprefix("tail"))], 0.5, "r")
                for t in tails]

    def fake_judge(replies, model="haiku"):
        return [True for _ in replies]

    cases = grade(hbs, classify=fake_classify, judge=fake_judge, batch_size=2)

    assert [c.klass for c in cases] == classes
    assert [c.tail for c in cases] == [f"tail{i}" for i in range(5)]


def test_grade_chunks_still_see_only_their_own_side():
    hbs = [Handback(f"tail{i}", f"reply{i}") for i in range(5)]

    def fake_classify(tails, model="haiku"):
        assert all(t.startswith("tail") for t in tails)
        assert not any("reply" in t for t in tails)  # no reply leakage per chunk
        from orchestrator.stall_judge import Judgment
        return [Judgment(AWAITING_CONTINUE, 0.5, "r") for _ in tails]

    def fake_judge(replies, model="haiku"):
        assert all(r.startswith("reply") for r in replies)
        assert not any("tail" in r for r in replies)  # no tail leakage per chunk
        return [True for _ in replies]

    grade(hbs, classify=fake_classify, judge=fake_judge, batch_size=2)


def test_grade_batch_size_larger_than_input_is_one_call():
    hbs = [Handback(NEXT_STEP, "keep going")]
    call_count = {"classify": 0, "judge": 0}

    def fake_classify(tails, model="haiku"):
        call_count["classify"] += 1
        from orchestrator.stall_judge import Judgment
        return [Judgment(AWAITING_CONTINUE, 0.5, "r") for _ in tails]

    def fake_judge(replies, model="haiku"):
        call_count["judge"] += 1
        return [True for _ in replies]

    grade(hbs, classify=fake_classify, judge=fake_judge, batch_size=1000)

    assert call_count == {"classify": 1, "judge": 1}


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
