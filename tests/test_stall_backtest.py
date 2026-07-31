# tests/test_stall_backtest.py
import pytest

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


def test_command_name_invocations_are_not_human_replies():
    # Real shape measured 2026-07-30: a slash-command invocation lands as a
    # `user` record whose content starts with `<command-name>`, which the
    # pre-existing `<command-message` prefix alone does not catch (18 of
    # 645 pairs, 2.8%, slipped through -- read as MECHANICAL by the
    # reply-judge because there's no substantive text in it at all, and
    # each phantom also consumed `pending`, discarding the real reply that
    # followed).
    assert human_text(_user(
        "<command-name>/model</command-name>\n<command-message>model</command-message>"
    )) is None
    # Defensive sibling: an args-only variant with no `<command-message`.
    assert human_text(_user(
        "<command-name>/deploy</command-name>\n<command-args>--prod</command-args>"
    )) is None


def test_skill_and_command_payloads_are_not_human_replies():
    # Real corpus examples, verbatim, measured 2026-07-30 (~22 of 962
    # handbacks, ~2%, were contaminated by exactly this pattern). Neither
    # carries a `_HARNESS_PREFIXES` marker of its own -- the harness's
    # preamble line lands in a separate record from the skill body.
    ddd_upload_body = (
        "# DDD Upload\n\n"
        "Uploads a converged DDD run's artifacts to canopy-web so they package together\n"
        "under the run — a single navigable view (video, deck, narrative, links) at\n"
        "`/ddd/<narrative-slug>/<run_id>`.\n\n"
        "## Arguments\n\n"
        "- `<run_id>` — converged run identifier.\n"
        "- `--video <video_path>` — path to the hero video `.mp4`.\n\n"
        "## Process\n\n"
        "Read the ddd-upload SKILL.md and follow it:\n\n"
        "```bash\npython3 -c \"import ...\n```"
    )
    # Full verbatim body (not the truncated preview quoted in the original
    # bug report) -- needed so this fixture genuinely exercises the
    # early-fence signal's `_EARLY_FENCE_MIN_LEN` floor the way the real
    # corpus record does (315 chars of the truncated preview alone would
    # UNDER-shoot that floor and no longer be structurally distinguishable
    # from a short human reply, which is the whole point of the floor).
    labs_login_body = (
        "Run the labs walkthrough login script:\n\n"
        "```bash\n"
        "bash ~/.claude/plugins/cache/ace/ace/$(cat ~/.claude/plugins/marketplaces/ace/VERSION)"
        "/bin/ace-labs-walkthrough-login\n"
        "```\n\n"
        "This reuses `mcp/connect/auth/hq-oauth-login.ts` (the same headless Connect OAuth "
        "driver `ace-connect` uses) and adds a labs-side click-through "
        "(`mcp/connect-labs/auth/labs-oauth-login.ts`). No labs-side auth bypass is needed "
        "— labs has no `/auth/e2e-login/` shared-secret endpoint (only the ace-web mount does).\n\n"
        "After login, cookies are imported into the gstack `browse` profile so "
        "`/canopy:walkthrough` runs against `https://labs.connect.dimagi.com/...` URLs "
        "are authenticated.\n\n"
        "Re-run when:\n"
        "- `~/.ace/labs-session.json` is missing\n"
        "- A walkthrough run hits a redirect to `/labs/login/` mid-scene (session expired)\n"
        "- You changed the `ACE_HQ_USERNAME`/`PASSWORD` in `.env` and need to "
        "re-authenticate as a different user\n\n"
        "Auth surface:\n"
        "- **Connect-Labs (this script):** `~/.ace/labs-session.json`, cookies for "
        "`labs.connect.dimagi.com`\n"
        "- **Connect (existing):** `~/.ace/connect-session.json`, cookies for "
        "`connect.dimagi.com` + `www.commcarehq.org`\n"
        "- **OCS (existing):** `~/.ace/ocs-session-<team>.json`\n\n"
        "Both Connect and Labs OAuth flows share the same `hqOAuthLogin` infra, so a "
        "fresh login establishes both sessions in one bounce."
    )
    # labs_login_body has NO markdown heading anywhere -- it's the case the
    # original three-signal fix (heading+fence / ARGUMENTS: / "# /") does
    # not catch; it needs the early-fence signal.
    assert "#" not in labs_login_body
    assert len(labs_login_body) >= 400  # exercises the length floor, not just position
    assert human_text(_user(ddd_upload_body)) is None
    assert human_text(_user(labs_login_body)) is None
    # A real human pasting a short command is NOT caught -- the fence has
    # to appear within the early window AND the whole reply has to be long
    # enough to look like a stripped skill body, not a short human reply.
    assert human_text(_user("looks fine, ship it")) == "looks fine, ship it"


def test_a_short_human_reply_with_an_early_code_fence_survives():
    # Finding 3 hardening: a short, plausible human reply that happens to
    # open with a pasted snippet must NOT be caught by the early-fence
    # signal -- only skill/command bodies, which run long, should be.
    reply = "try this instead:\n\n```\npytest -k stall_backtest -x\n```\nstill failing though"
    assert reply.find("```") <= 300
    assert len(reply) < 400
    assert human_text(_user(reply)) == reply


def test_a_leading_hash_without_a_space_is_not_a_heading():
    # Finding 3 hardening: the original heading check was `startswith("#")`,
    # which also matched things a human could plausibly type, like a
    # hashtag or an issue-number shorthand. Real markdown headings require
    # whitespace after the `#`.
    reply = "#urgent this needs a decision today, not a `next I'll` nudge"
    assert human_text(_user(reply)) == reply


def test_tool_results_are_not_human_replies():
    rec = {"type": "user", "message": {"content": [
        {"type": "tool_result", "content": "ok"}]}}
    assert human_text(rec) is None


def test_a_handback_followed_only_by_junk_is_skipped():
    assert collect_handbacks([_asst(NEXT_STEP),
                              _user("<task-notification>x</task-notification>")]) == []


def test_consecutive_handbacks_share_the_reply_only_via_the_last_one():
    # Regression guard for the reply-contamination bug: three end_turn
    # records in a row with no reply between them, then one human message.
    # Only the LAST handback is answerable -- the earlier two never got a
    # reply of their own and must be dropped, not back-filled with the
    # eventual reply. Real corpus impact measured 2026-07-30: 337 of 962
    # handbacks (35%) shared a reply with another handback before this fix.
    tail1 = "First: seeding is done."
    tail2 = "Second: rendering now."
    tail3 = "Third: about to upload."
    hbs = collect_handbacks([
        _asst(tail1), _asst(tail2), _asst(tail3), _user("go ahead"),
    ])
    assert hbs == [Handback(tail=tail3, reply="go ahead")]


def test_two_consecutive_human_replies_pair_on_the_second():
    # Symmetric twin of the replace-don't-queue rule above, on the REPLY
    # side: a handback followed by two consecutive user texts (no assistant
    # record between them) must pair with the SECOND -- the human's actual,
    # final answer -- not the first.
    hbs = collect_handbacks([
        _asst(NEXT_STEP), _user("yes send"), _user("actually wait, hold off"),
    ])
    assert hbs == [Handback(tail=NEXT_STEP, reply="actually wait, hold off")]


def test_an_edited_reply_grades_on_the_reversal_not_the_stale_draft():
    # Real corpus shape measured 2026-07-30 (18 of 645 pairs, 2.8%): a human
    # edits/re-sends before the agent replies, leaving BOTH messages in the
    # transcript. Pairing on the FIRST (as the old "first acceptable reply
    # wins" scan did) grades a REVERSAL as approval -- a false true-positive
    # in the dangerous direction. Only the LAST message before the agent
    # moves on again is the human's real answer.
    hbs = collect_handbacks([
        _asst(NEXT_STEP),
        _user("yes send"),
        _user("no just mark the thread as done no need to respond"),
        _asst(SECOND_STEP),
    ])
    assert len(hbs) == 1
    assert hbs[0].tail == NEXT_STEP
    assert hbs[0].reply == "no just mark the thread as done no need to respond"


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


def test_collect_handbacks_stats_counts_filtered_records():
    # Item 5 disclosure: the harness-prefix and skill-payload filters were
    # the one form of data loss with NO visibility anywhere. Measured in a
    # live window: 462 harness-prefix + 14 skill-payload rejections.
    skill_body = "# DDD Upload\n\nBody.\n\n```bash\necho hi\n```"
    hbs_records = [
        _asst(NEXT_STEP),
        _user("<command-name>/model</command-name>"),  # harness-prefix reject
        _user(skill_body),                              # skill-payload reject
        _user("keep going"),                             # the real answer
    ]
    stats: dict = {}
    hbs = collect_handbacks(hbs_records, stats=stats)
    assert hbs == [Handback(tail=NEXT_STEP, reply="keep going")]
    assert stats["harness_prefix_rejected"] == 1
    assert stats["skill_payload_rejected"] == 1


def test_collect_handbacks_stats_accumulates_across_multiple_calls():
    # The CLI passes ONE shared dict across every session's transcript --
    # confirm counts accumulate rather than reset per call.
    stats: dict = {}
    collect_handbacks(
        [_asst(NEXT_STEP), _user("<command-name>/model</command-name>"), _user("keep going")],
        stats=stats)
    collect_handbacks(
        [_asst(SECOND_STEP), _user("<command-args>--x</command-args>"), _user("ok")],
        stats=stats)
    assert stats["harness_prefix_rejected"] == 2


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


def test_grade_retries_a_chunk_that_fails_once_then_succeeds():
    # Fail-loud must not mean fail-total: a single flaky response (the
    # model dropping an item, raised as ValueError by classify_tails) must
    # not discard the whole run if a retry recovers it.
    hbs = [Handback(NEXT_STEP, "keep going")]
    calls = {"classify": 0}

    def flaky_classify(tails, model="haiku"):
        calls["classify"] += 1
        if calls["classify"] == 1:
            raise ValueError("expected 1 items in model output, got 0")
        from orchestrator.stall_judge import Judgment
        return [Judgment(AWAITING_CONTINUE, 0.8, "next step") for _ in tails]

    def fake_judge(replies, model="haiku"):
        return [True for _ in replies]

    stats: dict = {}
    cases = grade(hbs, classify=flaky_classify, judge=fake_judge, stats=stats)

    assert len(cases) == 1
    assert cases[0].klass == AWAITING_CONTINUE
    assert calls["classify"] == 2  # one failure, one retry that succeeded
    assert stats["chunks_failed"] == 0
    assert stats["handbacks_skipped"] == 0


def test_grade_skips_a_chunk_that_never_recovers_others_still_land():
    # Corpus-measured failure mode (2026-07-30): 21 chunks, one comes back
    # short every time even after retries. That chunk's handbacks must be
    # dropped -- not the whole run, and not fabricated verdicts.
    hbs = [Handback(f"tail{i}", f"reply{i}") for i in range(3)]  # batch_size=2 -> 2 chunks

    def always_fails_first_chunk(tails, model="haiku"):
        if tails[0] == "tail0":
            raise ValueError("expected 2 items in model output, got 1")
        from orchestrator.stall_judge import Judgment
        return [Judgment(AWAITING_CONTINUE, 0.5, "r") for _ in tails]

    def fake_judge(replies, model="haiku"):
        return [True for _ in replies]

    stats: dict = {}
    cases = grade(hbs, classify=always_fails_first_chunk, judge=fake_judge,
                  batch_size=2, retries=1, stats=stats)

    # Only the surviving chunk's case is present, and in order.
    assert [c.tail for c in cases] == ["tail2"]
    assert stats["chunks"] == 2
    assert stats["chunks_failed"] == 1
    assert stats["handbacks_skipped"] == 2


def test_grade_does_not_retry_or_swallow_a_runtime_error():
    # A RuntimeError (e.g. the claude subprocess itself failing) means the
    # model path is broken, not that one response was malformed -- it must
    # propagate immediately, unretried, and never be treated as a skip.
    hbs = [Handback(NEXT_STEP, "keep going")]
    calls = {"classify": 0}

    def broken_classify(tails, model="haiku"):
        calls["classify"] += 1
        raise RuntimeError("claude binary not found")

    def fake_judge(replies, model="haiku"):
        raise AssertionError("judge must not be called after classify blows up")

    with pytest.raises(RuntimeError):
        grade(hbs, classify=broken_classify, judge=fake_judge)

    assert calls["classify"] == 1  # not retried


def test_grade_default_stats_is_none_and_still_works():
    hbs = [Handback(NEXT_STEP, "keep going")]

    def fake_classify(tails, model="haiku"):
        from orchestrator.stall_judge import Judgment
        return [Judgment(AWAITING_CONTINUE, 0.5, "r") for _ in tails]

    def fake_judge(replies, model="haiku"):
        return [True for _ in replies]

    cases = grade(hbs, classify=fake_classify, judge=fake_judge)  # no stats=...

    assert len(cases) == 1


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


def test_score_per_class_precision_is_none_when_never_auto_sent():
    # A class outside AUTO_SEND_CLASSES (would_send is always False) never
    # contributes a would-send decision -- tp + fp == 0 -- so it has no
    # precision to report, not a measured 0%. Printing 0.000 would read as
    # "this class is 0% accurate," a claim about a measurement never taken.
    cases = [BacktestCase(QUESTION_OPEN, False, True, "t", "keep going")]
    result = score(cases)
    per = result["per_class"][QUESTION_OPEN]
    assert per["tp"] == 0 and per["fp"] == 0
    assert per["precision"] is None


def test_score_of_nothing_is_zero_not_a_crash():
    result = score([])
    assert result["overall"]["n"] == 0
    assert result["overall"]["precision"] == 0.0
