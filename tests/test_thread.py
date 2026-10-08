"""The pure half of agent threads: keys, the message prompt, the reply block, the moderator's
decision (every branch), and reading a closed thread."""
import datetime as dt
import json

from orchestrator import thread as T

TID = "thr-0123456789ab"
NOW = dt.datetime(2026, 10, 7, 12, 0, tzinfo=dt.timezone.utc)


def blk(n, frm, position, says="ok", proposal=None):
    b = {"thread": TID, "n": n, "from": frm, "says": says, "position": position}
    if proposal is not None:
        b["proposal"] = proposal
    return b


def msg(n, speaker, block=None, status="done"):
    return {"n": n, "speaker": speaker, "turn_id": f"t{n}", "status": status,
            "created_at": "2026-10-07T11:00:00Z", "finished_at": None, "content_hidden": False,
            "prompt": "p", "block": block, "reply_source": "closeout" if block else "none",
            "reply_error": ""}


def thread(messages=(), **kw):
    base = {"id": TID, "kind": "agreement", "purpose": "Settle echo's change to \"Q4 brief\"",
            "participants": [{"agent": "eva", "role": "author"},
                             {"agent": "echo", "role": "asker"}],
            "moderator": "ada", "parent": {"huddle": "work-fleet-20261007", "title": "Q4 brief",
                                           "lead": "eva"},
            "context": "PROPOSAL VERBATIM\nAMEND: public material only",
            "max_messages": 4, "messages_used": len(messages),
            "deadline_at": "2026-10-07T13:00:00Z", "status": "open", "outcome": {},
            "created_at": "2026-10-07T10:30:00Z", "closed_at": None,
            "messages": list(messages)}
    base.update(kw)
    return base


# ── keys ─────────────────────────────────────────────────────────────────────────
def test_keys_and_tag():
    assert T.thread_key(TID) == f"thread:{TID}"
    assert T.idempotency_key(TID, 2) == f"thread-{TID}-n2"
    assert T.closeout_session_id(TID, 3) == f"thread:{TID}:3"
    assert T.message_origin_ref(TID, 1, "eva") == {
        "kind": "thread_message", "thread": TID, "n": 1, "speaker": "eva",
        "thread_key": f"thread:{TID}"}


# ── block extraction ─────────────────────────────────────────────────────────────
def test_extract_takes_the_last_matching_thread_fence():
    text = (f"```thread\n{json.dumps(blk(1, 'eva', 'counter', 'first'))}\n```\nthen\n"
            f"```thread\n{json.dumps(blk(1, 'eva', 'agree', 'second'))}\n```")
    b, err = T.extract_block(text, TID, 1)
    assert err == "" and b["says"] == "second"


def test_extract_ignores_another_thread_or_message():
    other = {**blk(1, "eva", "agree"), "thread": "thr-other"}
    text = f"```thread\n{json.dumps(other)}\n```\n```thread\n{json.dumps(blk(2, 'eva', 'agree'))}\n```"
    b, err = T.extract_block(text, TID, 1)
    assert b is None and "thread" in err


def test_extract_accepts_a_json_fence_and_a_bare_object():
    b, _ = T.extract_block(f"```json\n{json.dumps(blk(1, 'eva', 'agree'))}\n```", TID, 1)
    assert b and b["from"] == "eva"
    b, _ = T.extract_block(f"Here you go: {json.dumps(blk(1, 'eva', 'agree'))} — done.", TID, 1)
    assert b and b["position"] == "agree"


def test_extract_prefers_the_thread_fence_over_a_bare_object():
    text = (f"```thread\n{json.dumps(blk(1, 'eva', 'agree', 'fenced'))}\n```\n"
            f"{json.dumps(blk(1, 'eva', 'counter', 'bare'))}")
    assert T.extract_block(text, TID, 1)[0]["says"] == "fenced"


def test_extract_reports_bad_json():
    b, err = T.extract_block("```thread\n{not json}\n```", TID, 1)
    assert b is None and "not valid JSON" in err


def test_validate_block():
    assert T.validate_block(blk(1, "eva", "agree"), TID, 1, "eva") == []
    assert T.validate_block(blk(1, "eva", "Agrees"), TID, 1, "eva") == []   # lenient
    probs = T.validate_block({"thread": TID, "n": 1, "from": "echo", "says": " ",
                              "position": "maybe", "proposal": "x"}, TID, 1, "eva")
    assert any("from is 'echo'" in p for p in probs)
    assert "says must be non-empty prose" in probs
    assert any(p.startswith("position must be one of") for p in probs)
    assert "proposal must be a JSON object" in probs
    assert "missing says" in T.validate_block({"thread": TID, "n": 1, "from": "eva",
                                               "position": "agree"}, TID, 1, "eva")


def test_norm_position():
    assert T.norm_position("Agrees") == "agree"
    assert T.norm_position("counter-proposal") == "counter"
    assert T.norm_position("Declined") == "decline"
    assert T.norm_position("asks") == "question"


# ── the prompt ───────────────────────────────────────────────────────────────────
def test_prompt_carries_purpose_context_the_whole_thread_role_budget_and_filing():
    th = thread([msg(1, "eva", blk(1, "eva", "counter", "Public only, but gdoc by 10/9",
                                   {"title": "Q4 brief", "why": "revised"})),
                 msg(2, "echo", blk(2, "echo", "question", "Can it be 10/8?"))])
    text = T.render_message_prompt(th, "eva", 3, NOW)
    assert "Thread thr-0123456789ab — message 3 of at most 4. You are eva, the author." in text
    assert "Settle echo's change" in text
    assert "PROPOSAL VERBATIM\nAMEND: public material only" in text
    assert "#### Message 1 — eva (author) · position: counter\nPublic only, but gdoc by 10/9" in text
    assert '"why": "revised"' in text
    assert "#### Message 2 — echo (asker) · position: question\nCan it be 10/8?" in text
    assert "After this message 1 more may be sent" in text
    assert "2026-10-07 13:00 UTC" in text
    assert '"thread": "thr-0123456789ab", "n": 3, "from": "eva"' in text
    assert (f'canopy agent turn --slug eva --session-id "thread:{TID}:3" '
            f'--title "thread {TID} message 3"') in text
    assert "agent-core/thread.md" in text and "READ-ONLY TURN" in text
    assert "{{" not in text.replace("`{}`", "")


def test_prompt_for_the_last_message_and_an_opening_message():
    th = thread()
    text = T.render_message_prompt(th, "eva", 1, NOW)
    assert "(nothing yet — you open the thread)" in text
    last = T.render_message_prompt(thread(max_messages=1), "eva", 1, NOW)
    assert "This is the LAST message the thread allows" in last


def test_prompt_shows_a_message_that_never_arrived():
    th = thread([msg(1, "eva", None, status="failed")])
    th["messages"][0]["reply_error"] = "runner lost"
    text = T.render_message_prompt(th, "eva", 2, NOW)
    assert "#### Message 1 — eva: no usable reply — failed (runner lost)" in text


# ── the moderator's decision ─────────────────────────────────────────────────────
def test_first_message_goes_to_the_author():
    th = thread(participants=[{"agent": "echo", "role": "asker"},
                              {"agent": "eva", "role": "author"}])
    assert T.decide(th, NOW) == {"action": "send", "n": 1, "to": "eva"}


def test_wait_while_the_latest_message_is_out():
    th = thread([msg(1, "eva", None, status="done")])      # done = spawned, not replied
    d = T.decide(th, NOW)
    assert d["action"] == "wait" and d["n"] == 1 and d["speaker"] == "eva"


def test_alternates_after_a_reply():
    th = thread([msg(1, "eva", blk(1, "eva", "counter", proposal={"title": "Q4 brief"}))])
    assert T.decide(th, NOW) == {"action": "send", "n": 2, "to": "echo"}


def test_both_agree_settles_agreed_with_the_latest_proposal():
    p1, p2 = {"title": "Q4 brief", "why": "v1"}, {"title": "Q4 brief", "why": "v2"}
    th = thread([msg(1, "eva", blk(1, "eva", "counter", proposal=p1)),
                 msg(2, "echo", blk(2, "echo", "counter", proposal=p2)),
                 msg(3, "eva", blk(3, "eva", "agree", "fine by me")),
                 msg(4, "echo", blk(4, "echo", "agree", "done"))])
    d = T.decide(th, NOW)
    assert d == {"action": "close", "status": "settled",
                 "outcome": {"result": "agreed", "why": "done", "proposal": p2}}


def test_author_agrees_with_revision_then_asker_agrees_in_two_messages():
    rev = {"title": "Q4 brief", "why": "public only"}
    th = thread([msg(1, "eva", blk(1, "eva", "agree", "taking the change", rev)),
                 msg(2, "echo", blk(2, "echo", "agree", "great"))])
    d = T.decide(th, NOW)
    assert d["status"] == "settled" and d["outcome"]["result"] == "agreed"
    assert d["outcome"]["proposal"] == rev


def test_agreed_without_any_proposal_omits_it():
    th = thread([msg(1, "eva", blk(1, "eva", "agree")), msg(2, "echo", blk(2, "echo", "agree"))])
    assert "proposal" not in T.decide(th, NOW)["outcome"]


def test_an_agree_followed_by_a_counter_is_not_agreement():
    th = thread([msg(1, "eva", blk(1, "eva", "agree")),
                 msg(2, "echo", blk(2, "echo", "counter", proposal={"title": "x"}))])
    assert T.decide(th, NOW) == {"action": "send", "n": 3, "to": "eva"}


def test_one_speaker_agreeing_twice_is_not_agreement():
    th = thread([msg(1, "eva", blk(1, "eva", "agree")),
                 msg(2, "echo", None, status="failed"),
                 msg(3, "eva", blk(3, "eva", "agree"))], max_messages=6)
    assert T.decide(th, NOW)["action"] == "send"


def test_any_decline_settles_not_agreed():
    th = thread([msg(1, "eva", blk(1, "eva", "counter")),
                 msg(2, "echo", blk(2, "echo", "decline", "the gdoc is a must"))])
    d = T.decide(th, NOW)
    assert d["status"] == "settled"
    assert d["outcome"] == {"result": "not_agreed", "why": "echo declined: the gdoc is a must",
                            "declined_by": "echo"}


def test_budget_used_without_agreement_closes_out_of_budget():
    th = thread([msg(1, "eva", blk(1, "eva", "counter")), msg(2, "echo", blk(2, "echo", "counter")),
                 msg(3, "eva", blk(3, "eva", "counter")), msg(4, "echo", blk(4, "echo", "question"))])
    assert T.decide(th, NOW) == {"action": "close", "status": "out_of_budget",
                                 "outcome": {"result": "not_agreed", "why": "ran out of messages"}}


def test_budget_counts_messages_used_from_the_server():
    th = thread([msg(1, "eva", blk(1, "eva", "counter"))], messages_used=4)
    assert T.decide(th, NOW)["status"] == "out_of_budget"


def test_deadline_passed_with_a_message_out_closes_timed_out():
    th = thread([msg(1, "eva", None, status="running")], deadline_at="2026-10-07T11:59:00Z")
    assert T.decide(th, NOW) == {"action": "close", "status": "timed_out",
                                 "outcome": {"result": "not_agreed", "why": "ran out of time"}}


def test_agreement_reached_just_before_the_deadline_still_settles():
    th = thread([msg(1, "eva", blk(1, "eva", "agree")), msg(2, "echo", blk(2, "echo", "agree"))],
                deadline_at="2026-10-07T11:00:00Z")
    assert T.decide(th, NOW)["outcome"]["result"] == "agreed"


def test_a_failed_message_is_asked_of_the_same_speaker_again():
    th = thread([msg(1, "eva", blk(1, "eva", "counter")), msg(2, "echo", None, status="failed")])
    assert T.decide(th, NOW) == {"action": "send", "n": 3, "to": "echo"}


def test_a_malformed_reply_is_asked_again_and_does_not_count():
    bad = {"thread": TID, "n": 2, "from": "echo", "says": "", "position": "agree"}
    th = thread([msg(1, "eva", blk(1, "eva", "agree")), msg(2, "echo", bad)])
    assert T.decide(th, NOW) == {"action": "send", "n": 3, "to": "echo"}


def test_closed_thread_is_left_alone():
    th = thread(status="settled", outcome={"result": "agreed"})
    assert T.decide(th, NOW) == {"action": "closed", "status": "settled",
                                 "outcome": {"result": "agreed"}}


def test_generic_kind_round_robins_and_needs_everyone():
    th = thread([msg(1, "a", blk(1, "a", "agree")), msg(2, "b", blk(2, "b", "agree"))],
                kind="planning", max_messages=6,
                participants=[{"agent": "a", "role": "x"}, {"agent": "b", "role": "y"},
                              {"agent": "c", "role": "z"}])
    assert T.decide(th, NOW) == {"action": "send", "n": 3, "to": "c"}
    th["messages"].append(msg(3, "c", blk(3, "c", "agree")))
    th["messages_used"] = 3
    assert T.decide(th, NOW)["outcome"]["result"] == "agreed"


def test_resumable_the_decision_depends_only_on_the_record():
    th = thread([msg(1, "eva", blk(1, "eva", "counter"))])
    assert T.decide(json.loads(json.dumps(th)), NOW) == T.decide(th, NOW)


# ── reading a closed thread ──────────────────────────────────────────────────────
def test_result_and_why():
    assert T.result_of(thread()) == "open"
    assert T.result_of(thread(status="settled", outcome={"result": "agreed"})) == "agreed"
    assert T.result_of(thread(status="settled", outcome={"result": "not_agreed"})) == "not_agreed"
    assert T.result_of(thread(status="timed_out")) == "not_agreed"
    assert T.why_of(thread(status="out_of_budget")) == "ran out of messages"
    assert T.why_of(thread(status="settled", outcome={"why": "echo declined: no"})) == "echo declined: no"
