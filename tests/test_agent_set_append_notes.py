"""`canopy agent set --append-notes` — add a turn's log without restating the history.

`--notes` replaces the field wholesale (the board PATCH does), and a multi-turn task's
notes ARE its history. Until this landed the only way to append was read-modify-write by
hand, and passing just the new entry to `--notes` succeeded while deleting every earlier
turn's log. Found on a hal turn, 2026-10-02, appending to a ~7,000-char triage log.
"""
import pytest
from click.testing import CliRunner

from orchestrator.agent_cli import _appended_notes, agent


class FakeClient:
    def __init__(self, tasks):
        self._tasks = tasks

    def list_tasks(self):
        return self._tasks


def test_append_keeps_existing_notes():
    client = FakeClient([{"id": 7, "notes": "--- day 1 ---\nshipped A\n"}])
    assert _appended_notes(client, 7, "--- day 2 ---\nshipped B") == (
        "--- day 1 ---\nshipped A\n\n--- day 2 ---\nshipped B"
    )


def test_append_onto_empty_notes_has_no_leading_blank_line():
    assert _appended_notes(FakeClient([{"id": 7, "notes": None}]), 7, "first") == "first"
    assert _appended_notes(FakeClient([{"id": 7}]), 7, "first") == "first"


def test_append_reads_the_right_card():
    client = FakeClient([{"id": 6, "notes": "other"}, {"id": 7, "notes": "mine"}])
    assert _appended_notes(client, 7, "new") == "mine\n\nnew"


def test_empty_append_is_refused_not_a_silent_noop():
    import click

    with pytest.raises(click.ClickException):
        _appended_notes(FakeClient([{"id": 7, "notes": "x"}]), 7, "   ")


def test_notes_and_append_notes_are_mutually_exclusive():
    res = CliRunner().invoke(agent, [
        "set", "--slug", "hal", "--task-id", "T1",
        "--notes", "a", "--append-notes", "b",
    ])
    assert res.exit_code != 0
    assert "not both" in res.output
