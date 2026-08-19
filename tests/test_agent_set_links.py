"""`canopy agent set` must be able to fix a task's links.

Links are the task field most likely to rot — they point at runs, run summaries and
docs, which advance — and until this landed `agent set` had no `--links` option at all.
The only CLI surface that accepted links was `agent add`, which builds a COMPLETE task
dict and pushes it through `sync_tasks`, so using it to correct one stale link blanked
`next_action`, `notes`, `owner`, `assigned`, `status` and `due` unless the caller
re-supplied every one of them. The one field that rots had no safe update path.

Found during an ACE turn on 2026-08-19 (dimagi-internal/canopy#508), correcting a board
card whose run-summary link still pointed at a superseded run.
"""
import pytest

from orchestrator.agent_cli import _appended_links, parse_task_links


class FakeClient:
    """Only the surface `_appended_links` touches."""

    def __init__(self, tasks):
        self._tasks = tasks

    def list_tasks(self):
        return self._tasks


def test_append_keeps_the_links_already_on_the_card():
    """The whole point: adding one link must not drop the others.

    The board's PATCH replaces `links` wholesale, so an append that forgot to read
    first would silently delete every existing link — the exact failure the replace-only
    path already had.
    """
    client = FakeClient([{"id": 91, "links": [{"label": "Thread", "url": "https://mail/x"}]}])
    out = _appended_links(client, 91, "Run summary|https://labs/run/2")
    assert [l["url"] for l in out] == ["https://mail/x", "https://labs/run/2"]
    assert out[0]["label"] == "Thread"


def test_append_is_idempotent_on_url_so_reruns_do_not_stack_duplicates():
    """A turn that re-attaches the same artifact is the common case, not an error."""
    client = FakeClient([{"id": 91, "links": [{"label": "Run", "url": "https://labs/run/2"}]}])
    out = _appended_links(client, 91, "Run summary|https://labs/run/2")
    assert len(out) == 1
    assert out[0]["label"] == "Run", "first label wins; a re-add must not relabel"


def test_append_onto_a_card_with_no_links_yet():
    client = FakeClient([{"id": 91}])
    assert _appended_links(client, 91, "https://labs/run/2") == [
        {"label": "link", "url": "https://labs/run/2"}
    ]


def test_append_ignores_other_cards_links():
    """Task ids are matched exactly — appending to one card must not inherit another's."""
    client = FakeClient([
        {"id": 90, "links": [{"label": "Other", "url": "https://other"}]},
        {"id": 91, "links": [{"label": "Mine", "url": "https://mine"}]},
    ])
    out = _appended_links(client, 91, "New|https://new")
    assert [l["url"] for l in out] == ["https://mine", "https://new"]


def test_links_and_append_link_are_mutually_exclusive():
    from click.testing import CliRunner

    from orchestrator.agent_cli import agent

    res = CliRunner().invoke(agent, [
        "set", "--slug", "ace", "--task-id", "T1",
        "--links", "a|https://a", "--append-link", "b|https://b",
    ])
    assert res.exit_code != 0
    assert "not both" in res.output


def test_empty_links_string_clears_rather_than_being_ignored():
    """`--links ""` is a real instruction (drop them all); only omitting the flag is a no-op.

    This is why the option defaults to None, not "": patch_task drops None fields, so a
    "" default would have made every unrelated `agent set` call silently wipe the links.
    """
    assert parse_task_links("") == []
