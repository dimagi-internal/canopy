"""`canopy agent set|add --status` must reject a value the board has no status for.

`agent set` passed `--status` to the board raw, and the board coerces an unknown value to
"suggested". So `--status blocked` on an in-progress card demoted it to an unaccepted
proposal and printed the patched task as success (dimagi-internal/canopy#659, observed on
hal's board 2026-09-17). `agent add` normalised first, but its normaliser ALSO fell back
to "suggested" for anything it did not recognise — same demotion, one hop earlier.

A wrong answer rather than an error: the closeout reads the board by status, so a silently
demoted card changes what the next turn is told it has outstanding.
"""
import click
import pytest
from click.testing import CliRunner

import orchestrator.agent_cli as agent_cli
from orchestrator.agent_cli import (
    TASK_STATUSES,
    agent,
    check_task_status,
    normalize_task_status,
)


class RecordingClient:
    def __init__(self):
        self.patched = []
        self.created = []

    def list_tasks(self):
        return [{"ext_id": "T27", "status": "in_progress"}]

    def patch_task(self, task_id, **fields):
        # Like the real client: an omitted option arrives as None and is not sent.
        fields = {k: v for k, v in fields.items() if v is not None}
        self.patched.append((task_id, fields))
        return {"ext_id": task_id, **fields}

    def create_tasks(self, tasks):
        self.created.extend(tasks)
        return list(tasks)


@pytest.fixture
def client(monkeypatch):
    c = RecordingClient()
    monkeypatch.setattr(agent_cli, "_client", lambda slug: c)
    return c


class TestSetRejectsAnUnknownStatus:
    def test_the_issue_repro_writes_nothing(self, client):
        """`pending` is not a status and not a synonym — it must not reach the board."""
        res = CliRunner().invoke(agent, [
            "set", "--slug", "hal", "--task-id", "T27", "--status", "pending",
            "--next-action", "wait for the workload",
        ])
        assert res.exit_code != 0
        assert "not a board status" in res.output
        assert "Nothing was written" in res.output
        assert client.patched == [], "a rejected status must not patch ANY field"

    def test_the_error_names_every_valid_status(self, client):
        res = CliRunner().invoke(agent, ["set", "--slug", "hal", "--task-id", "T27",
                                         "--status", "paused"])
        for s in TASK_STATUSES:
            assert s in res.output

    def test_blocked_maps_to_in_progress_instead_of_being_coerced(self, client):
        """The exact value from the report: it now lands where the vocabulary says."""
        res = CliRunner().invoke(agent, ["set", "--slug", "hal", "--task-id", "T27",
                                         "--status", "blocked"])
        assert res.exit_code == 0, res.output
        assert client.patched == [("T27", {"status": "in_progress"})]

    @pytest.mark.parametrize("given,token", [
        ("done", "done"), ("Shipped", "done"), ("in-progress", "in_progress"),
        ("declined", "declined"), ("suggested", "suggested"), ("backlog", "suggested"),
    ])
    def test_valid_spellings_are_sent_as_the_canonical_token(self, client, given, token):
        res = CliRunner().invoke(agent, ["set", "--slug", "hal", "--task-id", "T27",
                                         "--status", given])
        assert res.exit_code == 0, res.output
        assert client.patched[-1][1]["status"] == token

    def test_omitting_status_leaves_it_alone(self, client):
        res = CliRunner().invoke(agent, ["set", "--slug", "hal", "--task-id", "T27",
                                         "--score", "A"])
        assert res.exit_code == 0, res.output
        assert "status" not in client.patched[-1][1]


class TestAddRejectsAnUnknownStatus:
    def test_unknown_status_creates_no_task(self, client):
        res = CliRunner().invoke(agent, ["add", "--slug", "hal", "--title", "x",
                                         "--status", "pending"])
        assert res.exit_code != 0
        assert "not a board status" in res.output
        assert client.created == []

    def test_default_is_still_suggested(self, client):
        res = CliRunner().invoke(agent, ["add", "--slug", "hal", "--title", "x"])
        assert res.exit_code == 0, res.output
        assert client.created[-1]["status"] == "suggested"


def test_the_reader_stays_lenient():
    """Filtering cards already on the board must never raise on an odd stored value."""
    assert normalize_task_status("something-legacy") == "suggested"
    assert normalize_task_status(None) == "suggested"


def test_the_writer_is_strict():
    with pytest.raises(click.ClickException):
        check_task_status("")
    with pytest.raises(click.ClickException):
        check_task_status("pending")
