"""dispatch_outcomes: the dispatcher's report card, read off a machine-started session.

DISPATCH_MARKER was added to SUBTRACT — to stop a dispatched brief being mined as the human
shouting at the agent (canopy #488). This lens is the addition it also enables: the marker is the
only thing in a transcript that says "a machine started this session", so the span after it is the
outcome of the dispatcher's judgment, and a genuine human turn inside that span is a person having
to come back to work that was supposed to run unattended.

That closes a loop that has never existed. Ada dispatches fix briefs every cycle and learns
nothing about which were worth sending — a brief built on a stale window and one that shipped
clean look identical to her the next morning.

The tests below are all structural: who authored which entry, and whether a merge tool call fired.
No phrase list decides a verdict, which is the property that makes it safe to grade an agent on.
"""
import json

import pytest

from orchestrator.agent_review import (
    DISPATCH_MARKER,
    DISPATCH_VERDICTS,
    dispatch_outcomes,
)


def human(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def brief(text="FINDING: hal drops a checklist step.\n\nEVIDENCE: ..."):
    return human(f"{text}\n\n{DISPATCH_MARKER}")


_TOOL_ID = iter(f"tu_{n}" for n in range(1000))


def assistant(text="", tool=None):
    content = []
    if text:
        content.append({"type": "text", "text": text})
    if tool:
        # `id` is not decoration — extract_tool_calls keys the call/result pairing on it.
        content.append({"type": "tool_use", "id": next(_TOOL_ID), "name": "Bash",
                        "input": {"command": tool}})
    return {"type": "assistant", "message": {"role": "assistant", "content": content}}


def tool_result(text="ok"):
    """A tool RESULT comes back as a `user`-role entry whose content is a list of blocks. It must
    never count as a person intervening — this is the same trap overclaim_signals documents."""
    return {"type": "user",
            "message": {"role": "user", "content": [{"type": "tool_result", "content": text}]}}


# ── is this even a dispatched session? ───────────────────────────────────────────────────────

def test_an_ordinary_session_is_not_graded():
    """Most sessions a person started themselves. Returning a verdict for those would grade a
    dispatcher who never sent anything."""
    assert dispatch_outcomes([human("go fix the thing"), assistant("ok")]) is None


def test_a_marked_session_is_graded():
    assert dispatch_outcomes([brief(), assistant("on it")]) is not None


def test_anchors_on_the_marked_turn_not_the_first_turn():
    """A dispatch can land MID-session when the runner resolves onto a live session instead of
    spawning a fresh one (the missing-thread_key failure). The real conversation before it is not
    someone arguing with a brief that had not arrived yet."""
    out = dispatch_outcomes([
        human("morning, what's on today?"),
        assistant("three things"),
        human("start with the second"),
        brief(),
        assistant("working"),
    ])
    assert out["verdict"] == "ran_unattended"
    assert out["n_human_turns_after"] == 0


# ── the signal: a human turn in a machine-started session ────────────────────────────────────

def test_ran_unattended_is_the_quiet_case():
    out = dispatch_outcomes([brief(), assistant("done", tool="git commit -m x")])
    assert out["verdict"] == "ran_unattended"
    assert out["interventions"] == []


def test_a_human_who_argues_makes_it_contested():
    """The headline case: work sent to run unattended that Jonathan had to come back and fight."""
    out = dispatch_outcomes([
        brief(),
        assistant("I'll rewrite the whole module"),
        human("No, stop. That's not what I asked for."),
    ])
    assert out["verdict"] == "contested"
    assert out["n_human_turns_after"] == 1
    assert "strong_correction" in out["interventions"][0]["kinds"]
    assert "not what i asked" in out["interventions"][0]["quote"].lower()


def test_a_human_who_merely_steers_is_not_an_argument():
    """A person answering a question is weaker evidence than a person overriding one. Collapsing
    the two would make every dispatch that asked anything look like a bad send."""
    out = dispatch_outcomes([brief(), assistant("which repo?"), human("the labs one")])
    assert out["verdict"] == "human_touched"
    assert out["interventions"] == []
    assert out["n_human_turns_after"] == 1


def test_shipped_clean_needs_an_actual_merge_call():
    """Read off the TOOL CALL, never the prose — an agent that merely SAYS it merged is exactly
    what overclaim_signals exists to catch."""
    out = dispatch_outcomes([brief(), assistant("shipping", tool="gh pr merge 55 --squash")])
    assert out["verdict"] == "shipped_clean"
    assert out["shipped"] is True

    claimed = dispatch_outcomes([brief(), assistant("Merged and verified!")])
    assert claimed["shipped"] is False
    assert claimed["verdict"] == "ran_unattended"


def test_an_argument_outranks_a_merge():
    """It landed, but a person had to fight it there. Reporting that as a clean success is how a
    scorecard becomes worthless."""
    out = dispatch_outcomes([
        brief(),
        assistant("merging", tool="gh pr merge 55"),
        human("Why did you merge that? I never approved it."),
    ])
    assert out["verdict"] == "contested"
    assert out["shipped"] is True


# ── the things that must NOT read as a person intervening ────────────────────────────────────

def test_tool_results_are_not_human_turns():
    out = dispatch_outcomes([brief(), assistant("running", tool="ls"), tool_result("a\nb")])
    assert out["n_human_turns_after"] == 0
    assert out["verdict"] == "ran_unattended"


def test_hook_feedback_is_not_a_human_turn():
    """Replayed Stop-hook feedback is a `user` entry nobody typed. A close-out rail is written to
    be forceful, so counting it would make every rail-firing session look contested (canopy #490)."""
    hook = human("Stop hook feedback: This turn is ending without its close-out. Run it NOW.")
    hook["isMeta"] = True
    out = dispatch_outcomes([brief(), assistant("wrapping up"), hook])
    assert out["n_human_turns_after"] == 0
    assert out["verdict"] == "ran_unattended"


def test_harness_boilerplate_is_not_a_human_turn():
    out = dispatch_outcomes([
        brief(),
        human("<system-reminder>DO NOT respond to these messages</system-reminder>"),
    ])
    assert out["n_human_turns_after"] == 0


def test_a_second_dispatched_brief_is_not_a_person_intervening():
    """Two briefs into one session is the dispatcher talking twice, not a human arguing. Counting
    it would have the sender grading itself down for its own second message."""
    out = dispatch_outcomes([brief(), assistant("done"), brief("FINDING: and another thing")])
    assert out["n_human_turns_after"] == 0
    assert out["verdict"] == "ran_unattended"


# ── the report card's own honesty ────────────────────────────────────────────────────────────

def test_brief_excerpt_quotes_the_ask_without_the_marker():
    """The marker renders as nothing wherever a prompt is displayed; it should not leak into a
    report a human reads either."""
    out = dispatch_outcomes([brief("FINDING: the thing is broken.")])
    assert out["brief_excerpt"] == "FINDING: the thing is broken."
    assert DISPATCH_MARKER not in out["brief_excerpt"]


def test_every_emitted_verdict_is_registered():
    """A verdict string nothing declares is one no consumer can branch on."""
    cases = [
        [brief()],
        [brief(), human("the labs one")],
        [brief(), human("No, stop.")],
        [brief(), assistant("go", tool="gh pr merge 1")],
    ]
    for entries in cases:
        assert dispatch_outcomes(entries)["verdict"] in DISPATCH_VERDICTS


def test_the_undetected_verdict_is_declared_but_never_emitted():
    """`rejected_on_revalidation` — the agent re-validates and finds the finding already fixed —
    is the outcome most worth counting and the one a regex cannot separate from the brief's own
    'already fixed' wording. Registered so the gap is visible, detected by nothing rather than
    guessed at (the call overclaim_signals made for `verify_late`)."""
    assert "rejected_on_revalidation" in DISPATCH_VERDICTS
    already_fixed = dispatch_outcomes([
        brief(),
        assistant("Re-validated: this was already fixed on main. Reporting back and stopping."),
    ])
    assert already_fixed["verdict"] == "ran_unattended", (
        "if this ever returns rejected_on_revalidation, the detector shipped — give it its own "
        "tests and delete this one"
    )


# ── it rides in the corpus the reviewer actually reads ───────────────────────────────────────

def test_corpus_carries_the_outcome(tmp_path):
    from orchestrator.agent_review import friction_signals

    t = tmp_path / "s.jsonl"
    t.write_text("\n".join(json.dumps(e) for e in [
        brief(), assistant("ok"), human("No, that's wrong."),
    ]))
    sig = friction_signals(t)
    assert sig["dispatch_outcome"]["verdict"] == "contested"


def test_corpus_omits_it_for_an_ordinary_session(tmp_path):
    from orchestrator.agent_review import friction_signals

    t = tmp_path / "s.jsonl"
    t.write_text("\n".join(json.dumps(e) for e in [human("hi"), assistant("hello")]))
    assert friction_signals(t)["dispatch_outcome"] is None


def test_the_reviewer_is_told_to_blame_the_sender(tmp_path):
    """The one lens whose findings belong to a DIFFERENT agent than the one under review. If the
    prompt doesn't say so, every contested dispatch gets filed against the victim."""
    from orchestrator.agent_review import build_review_prompt

    prompt = build_review_prompt(tmp_path, [])
    assert "dispatch_outcome" in prompt
    assert "DISPATCHER" in prompt
    assert "contested" in prompt


@pytest.mark.parametrize("verdict", ["contested", "shipped_clean"])
def test_the_reviewer_is_told_about_both_directions(tmp_path, verdict):
    """A fleet that only ever surfaces failures teaches its conductor nothing about what to keep
    doing, so the good verdict has to reach the reviewer too."""
    from orchestrator.agent_review import build_review_prompt

    assert verdict in build_review_prompt(tmp_path, [])


# ── the phrasings that were invisible until this lens was fired at a real session ────────────
# Found 2026-08-18: this lens's own first live run scored a session whose human turn was
# "I can't follow what the fuck you're doing" as a neutral steer. `i don't follow` was in the
# confusion list; `i can't follow` was not. Inability is the more common phrasing when someone
# is genuinely lost, so the gap was not an edge case — it was the common case.

@pytest.mark.parametrize("said", [
    "I can't follow what the fuck you're doing",
    "I cannot follow this",
    "i can't tell what you changed",
    "what the hell are you doing",
    "this is not making any sense",
])
def test_a_confused_human_reads_as_contested(said):
    out = dispatch_outcomes([brief(), assistant("..."), human(said)])
    assert out["verdict"] == "contested", f"{said!r} should not read as a neutral steer"
    assert "confusion" in out["interventions"][0]["kinds"]


@pytest.mark.parametrize("said", [
    "the labs one",
    "yes, go ahead",
    "use the second approach and ship it",
    "I can follow that, thanks",
])
def test_an_ordinary_reply_is_still_not_an_argument(said):
    """Widening the confusion list is only safe if it stays deaf to normal steering — a lens that
    calls every reply an argument is the false-positive failure this module keeps relearning."""
    out = dispatch_outcomes([brief(), assistant("..."), human(said)])
    assert out["verdict"] == "human_touched", f"{said!r} should not read as an argument"


# ── provenance: the agent must be able to SEE who sent it ────────────────────────────────────
# The marker is an HTML comment — inert and invisible, which is right for a tooling label and
# useless for the agent holding the prompt. Three consequences, the third a safety hole: it skips
# the mandated re-validation, it can't tell who to report back to, and it may read another agent's
# words as HUMAN APPROVAL for an outbound action.

def test_the_receiving_agent_is_told_who_sent_it_in_plain_words():
    from orchestrator.agent_dispatch import stamp_dispatched

    out = stamp_dispatched("FINDING: the thing is broken.", sender="ada")
    visible = out.replace(DISPATCH_MARKER, "")
    assert "ada" in visible
    assert "not typed by a human" in visible


def test_the_provenance_says_it_is_not_human_approval():
    """The fleet guardrail is 'outbound actions require human approval'. If one agent's words are
    indistinguishable from the human's, another agent can approve its own send."""
    from orchestrator.agent_dispatch import stamp_dispatched

    assert "NOT human approval" in stamp_dispatched("do it", sender="ada")


def test_the_provenance_says_verify_not_execute():
    from orchestrator.agent_dispatch import stamp_dispatched

    out = stamp_dispatched("do it", sender="ada")
    assert "VERIFY" in out and "re-validate" in out


def test_the_ask_still_comes_first():
    """canopy's contract is that the brief is the first thing the agent reads; provenance is a
    footer, which for a constraint is the more heavily weighted position anyway."""
    from orchestrator.agent_dispatch import stamp_dispatched

    assert stamp_dispatched("FINDING: xyz", sender="ada").startswith("FINDING: xyz")


def test_the_sender_is_machine_readable():
    from orchestrator.agent_dispatch import dispatched_by, stamp_dispatched

    assert dispatched_by(stamp_dispatched("x", sender="ada")) == "ada"
    assert dispatched_by(stamp_dispatched("x")) == ""
    assert dispatched_by("an ordinary prompt") == ""


def test_an_unknown_sender_degrades_to_the_bare_marker():
    """Mislabelling which agent sent a brief is worse than not labelling it — the outcome lens
    would hand one agent's report card to another."""
    from orchestrator.agent_dispatch import stamp_dispatched

    assert stamp_dispatched("x").endswith(DISPATCH_MARKER)
    assert "another canopy agent" in stamp_dispatched("x")


def test_idempotent_across_sender_and_bare_forms():
    """A prompt stamped by one helper and passed through another must not collect two — and the
    sender-carrying marker must be recognised by a caller that only knows the bare one."""
    from orchestrator.agent_dispatch import stamp_dispatched

    with_sender = stamp_dispatched("brief", sender="ada")
    assert stamp_dispatched(with_sender) == with_sender
    assert stamp_dispatched(with_sender, sender="hal") == with_sender


def test_a_sender_marked_brief_is_still_stripped_from_corrections():
    """The regression this whole marker exists to prevent — it must survive the format change."""
    from orchestrator.agent_dispatch import stamp_dispatched
    from orchestrator.agent_review import human_corrections

    shouty = "STOP. Never send that without approval."
    assert human_corrections([human(shouty)]), "sanity: this should score as a correction"
    assert human_corrections([human(stamp_dispatched(shouty, sender="ada"))]) == []


def test_the_outcome_names_the_sender():
    from orchestrator.agent_dispatch import stamp_dispatched

    out = dispatch_outcomes([
        human(stamp_dispatched("FINDING: x", sender="ada")),
        assistant("..."),
        human("No, stop."),
    ])
    assert out["dispatched_by"] == "ada"
    assert out["verdict"] == "contested"


def test_an_older_unattributed_brief_is_reported_as_unattributed():
    out = dispatch_outcomes([brief(), assistant("...")])
    assert out["dispatched_by"] == ""


def test_local_agent_slug_reads_the_agents_own_declaration(tmp_path):
    from orchestrator.agent_dispatch import local_agent_slug

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "gating.json").write_text(json.dumps({"slug": "ada"}))
    nested = tmp_path / "deep" / "worktree"
    nested.mkdir(parents=True)
    assert local_agent_slug(nested) == "ada", "must walk up — agents run from worktree subdirs"
    assert local_agent_slug(tmp_path.parent) == ""


def test_the_bare_marker_is_always_emitted_verbatim():
    """THE back-compatibility contract, and the reason the sender rides in a second comment.

    A fleet has version skew by definition: other machines, cloud runners, and Ada's own vendored
    copy all hold `DISPATCH_MARKER` as a literal and test it with `in`. Fold the sender INTO the
    marker and every one of those stops recognising a stamped prompt — so the brief silently stops
    being suppressed, which is the exact bug the marker exists to prevent, reintroduced by the fix
    for it, only on the hosts nobody remembered to update.

    Verified live 2026-08-18 against the then-installed canopy (0.2.413, which predates senders):
    it suppressed a new-format brief correctly.
    """
    from orchestrator.agent_dispatch import stamp_dispatched

    for sender in ("ada", "hal", ""):
        out = stamp_dispatched("brief", sender=sender)
        assert DISPATCH_MARKER in out, f"sender={sender!r} broke the literal an old reader needs"


def test_an_old_style_bare_stamp_is_still_recognised():
    """The other direction: a prompt stamped before senders existed must not be re-stamped, and
    must still be read as dispatched."""
    from orchestrator.agent_dispatch import dispatched_by, stamp_dispatched

    old = f"brief\n\n{DISPATCH_MARKER}"
    assert stamp_dispatched(old, sender="ada") == old
    assert dispatched_by(old) == ""
    assert dispatch_outcomes([human(old)])["dispatched_by"] == ""
