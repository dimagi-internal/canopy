"""`canopy agent set --append-notes` — add a turn's log without restating the history.

`--notes` replaces the field wholesale (the board PATCH does), and a multi-turn task's
notes ARE its history. Without `--append-notes` the only way to append is read-modify-write
by hand, and passing just the new entry to `--notes` succeeds while deleting every earlier
turn's log (a ~7,000-char triage log is enough to hit it).

The append's READ must resolve the same refs as its PATCH: a task KEY (`CON-351`) as well
as an ext_id. A read that misses the card starts from an empty baseline and the append
writes the new entry alone over the card's history, so the read goes through the task
detail GET and a miss refuses instead of appending onto nothing.
"""
import pytest
from click.testing import CliRunner

from orchestrator import agent_cli
from orchestrator.agent_cli import _appended_notes, agent
from orchestrator.agent_client import CanopyError


class FakeClient:
    """The board as the server resolves it: a ref is a task key OR an ext_id, any case."""

    def __init__(self, tasks):
        self._tasks = tasks
        self.patches = []

    def get_task(self, ref):
        want = str(ref).strip().casefold()
        for t in self._tasks:
            if want in (str(t.get("ext_id") or "").casefold(), str(t.get("key") or "").casefold()):
                return dict(t)
        raise CanopyError(f"GET /api/agents/x/tasks/{ref}/ -> 404: task {ref} not found")

    def patch_task(self, ref, **fields):
        if ref.startswith("NOPE"):
            raise CanopyError(f"PATCH /api/agents/x/tasks/{ref}/ -> 404: not found")
        self.patches.append((ref, fields))
        return {"key": ref, **fields}


def test_append_keeps_existing_notes():
    client = FakeClient([{"ext_id": "T7", "notes": "--- day 1 ---\nshipped A\n"}])
    assert _appended_notes(client, "T7", "--- day 2 ---\nshipped B") == (
        "--- day 1 ---\nshipped A\n\n--- day 2 ---\nshipped B"
    )


def test_append_onto_empty_notes_has_no_leading_blank_line():
    assert _appended_notes(FakeClient([{"ext_id": "T7", "notes": None}]), "T7", "first") == "first"
    assert _appended_notes(FakeClient([{"ext_id": "T7"}]), "T7", "first") == "first"


def test_append_reads_the_right_card():
    client = FakeClient([{"ext_id": "T6", "notes": "other"}, {"ext_id": "T7", "notes": "mine"}])
    assert _appended_notes(client, "T7", "new") == "mine\n\nnew"


def test_append_by_task_key_keeps_existing_notes():
    """A KEY, not the ext_id, must still append onto the existing notes."""
    client = FakeClient([{"key": "CON-351", "ext_id": "inline-chlorination-mali-edits-20261102",
                          "notes": "15 KB of history"}])
    assert _appended_notes(client, "CON-351", "anything") == "15 KB of history\n\nanything"
    assert _appended_notes(client, "con-351", "anything") == "15 KB of history\n\nanything"


def test_append_to_an_unreadable_card_refuses_instead_of_wiping():
    """'Could not read the card' must never become 'the card had no notes'."""
    with pytest.raises(CanopyError):
        _appended_notes(FakeClient([{"ext_id": "T7", "notes": "keep me"}]), "NOPE-1", "x")


def test_empty_append_is_refused_not_a_silent_noop():
    import click

    with pytest.raises(click.ClickException):
        _appended_notes(FakeClient([{"ext_id": "T7", "notes": "x"}]), "T7", "   ")


def test_notes_and_append_notes_are_mutually_exclusive():
    res = CliRunner().invoke(agent, [
        "set", "--slug", "hal", "--task-id", "T1",
        "--notes", "a", "--append-notes", "b",
    ])
    assert res.exit_code != 0
    assert "not both" in res.output


def test_cli_append_by_key_patches_the_full_history(monkeypatch):
    """End to end through `agent set`: the PATCH body still starts with the old notes."""
    client = FakeClient([{"key": "CON-351", "ext_id": "long-slug", "notes": "turn 1\n\nturn 2"}])
    monkeypatch.setattr(agent_cli, "_client", lambda slug, **_: client)
    res = CliRunner().invoke(agent, [
        "set", "--slug", "ace", "--task-id", "CON-351", "--append-notes", "turn 3",
    ])
    assert res.exit_code == 0, res.output
    [(ref, fields)] = client.patches
    assert ref == "CON-351"
    assert fields["notes"] == "turn 1\n\nturn 2\n\nturn 3"


def test_cli_append_to_a_missing_card_writes_nothing(monkeypatch):
    client = FakeClient([{"key": "CON-1", "ext_id": "T1", "notes": "x"}])
    monkeypatch.setattr(agent_cli, "_client", lambda slug, **_: client)
    res = CliRunner().invoke(agent, [
        "set", "--slug", "ace", "--task-id", "NOPE-9", "--append-notes", "y",
    ])
    assert res.exit_code != 0
    assert "no task 'NOPE-9'" in res.output
    assert client.patches == []
