"""The board's field caps must fail the SAME way from every command that writes a task.

`next_action` is capped at 300 characters and the two writers disagreed about what that
meant: `agent add` silently truncated (`next_action.strip()[:300]`) while `agent set`
passed the text through to the server's `string_too_long` 422. Same cap, opposite
failure modes, neither documented in `--help`.

Silent truncation is the worse half. A next-action that lost its tail still reads as
complete on the kanban card — no ellipsis, nothing errored — so the agent that wrote it
believes the whole instruction is recorded, and the next turn acts on one whose final
clause (often the actual constraint) is gone.

Observed during an ACE turn on 2026-08-19 (dimagi-internal/canopy#510, split out of
#508). The caps mirror canopy-web `apps/agents/schemas.py`.
"""
import click
import pytest

from orchestrator.agent_cli import (
    TASK_FIELD_LIMITS,
    check_task_field,
    parse_task_links,
    preview_for_card,
)


def test_a_field_at_the_limit_is_accepted_unchanged():
    """The cap is inclusive — exactly 300 is legal, and 301 is what the server rejects."""
    at_limit = "x" * 300
    assert check_task_field("next_action", at_limit) == at_limit


def test_over_length_raises_instead_of_silently_dropping_the_tail():
    """The whole defect: the tail must never disappear without the caller knowing."""
    with pytest.raises(click.ClickException) as excinfo:
        check_task_field("next_action", "x" * 342)
    assert "x" * 300 not in str(excinfo.value), "the error must not echo a truncated value"


def test_the_error_names_the_option_the_cap_and_the_overage():
    """An agent hitting this has to know WHAT to shorten and BY HOW MUCH, without
    reading the source or decoding a positional 422 `loc`."""
    with pytest.raises(click.ClickException) as excinfo:
        check_task_field("next_action", "x" * 342)
    message = str(excinfo.value)
    assert "--next-action" in message
    assert "342" in message
    assert "300" in message
    assert "42" in message, "say how much to cut, not just that it is too long"
    assert "not truncated" in message, "state that nothing was written"


def test_whitespace_is_stripped_before_the_length_is_judged():
    """Padding is not content — a value that fits once trimmed must not be rejected."""
    assert check_task_field("next_action", "  " + "x" * 299 + "  ") == "x" * 299


def test_an_uncapped_field_passes_through():
    """`notes`, `plan`, `rationale` and `review` are unbounded server-side; the checker
    must not invent a cap for them."""
    long_notes = "x" * 5000
    assert check_task_field("notes", long_notes) == long_notes


def test_empty_and_none_are_legal():
    assert check_task_field("next_action", "") == ""
    assert check_task_field("next_action", None) == ""


@pytest.mark.parametrize("field,limit", sorted(TASK_FIELD_LIMITS.items()))
def test_every_capped_field_rejects_one_character_over(field, limit):
    """Guards the table itself: adding a cap without wiring it is the bug returning."""
    assert check_task_field(field, "x" * limit) == "x" * limit
    with pytest.raises(click.ClickException):
        check_task_field(field, "x" * (limit + 1))


def test_the_caps_match_canopy_webs_schema():
    """These mirror `apps/agents/schemas.py` (AgentTaskIn / AgentTaskPatch). If the
    server moves and this table doesn't, the CLI starts rejecting values the board
    would have accepted — or waves through ones it won't."""
    assert TASK_FIELD_LIMITS["ext_id"] == 64
    assert TASK_FIELD_LIMITS["title"] == 300
    assert TASK_FIELD_LIMITS["next_action"] == 300
    assert TASK_FIELD_LIMITS["owner"] == 120
    assert TASK_FIELD_LIMITS["assigned"] == 120
    assert TASK_FIELD_LIMITS["confidence"] == 10
    assert TASK_FIELD_LIMITS["score"] == 8
    assert TASK_FIELD_LIMITS["source_url"] == 500


# ---- links -------------------------------------------------------------------------

def test_a_link_that_fits_parses_as_before():
    assert parse_task_links("Run|https://labs/run/2") == [
        {"label": "Run", "url": "https://labs/run/2"}
    ]


def test_an_over_length_url_raises_rather_than_becoming_a_dead_link():
    """A URL cut to fit still renders as a link on the card and goes nowhere."""
    with pytest.raises(click.ClickException) as excinfo:
        parse_task_links("Run|https://labs/" + "x" * 500)
    assert "--links" in str(excinfo.value)


def test_a_bare_over_length_url_raises_too():
    """The bare-url branch truncated independently of the labelled one."""
    with pytest.raises(click.ClickException):
        parse_task_links("https://labs/" + "x" * 500)


def test_an_over_length_label_raises():
    with pytest.raises(click.ClickException):
        parse_task_links("x" * 201 + "|https://labs/run/2")


# ---- the dispatch card's notes preview ----------------------------------------------

def test_a_short_brief_is_left_exactly_as_it_is():
    assert preview_for_card("do the thing", 2000) == "do the thing"


def test_a_long_brief_is_cut_but_says_so():
    """Shortening IS right here — the agent gets the full prompt in the turn payload —
    but an UNMARKED cut reads as the whole brief, which is the same lie the capped
    fields were telling."""
    out = preview_for_card("x" * 3000, 2000)
    assert out.startswith("x" * 2000)
    assert "truncated" in out
    assert "full brief" in out


def test_a_brief_exactly_at_the_preview_limit_is_not_marked():
    assert preview_for_card("x" * 2000, 2000) == "x" * 2000
