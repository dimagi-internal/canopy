"""`canopy agent set --waiting-on-email` puts a card on a person's "Waiting on you" page.

turn.md Step 2.9 and task-tracker.md tell every agent to "set waiting_on_email" on a card
whose next step is a person's, because a name in the next action (or `--assigned`) reaches
nobody's page. `agent set` had no way to do it, so an agent either dropped to the MCP
PATCH or, more often, left the ask in text where the owner never saw it.
"""
from click.testing import CliRunner

import orchestrator.agent_cli as agent_cli
from orchestrator.agent_cli import agent


class RecordingClient:
    def __init__(self):
        self.patched = []

    def patch_task(self, task_id, **fields):
        fields = {k: v for k, v in fields.items() if v is not None}
        self.patched.append((task_id, fields))
        return {"ext_id": task_id, **fields}


def _run(monkeypatch, *args):
    c = RecordingClient()
    monkeypatch.setattr(agent_cli, "_client", lambda slug: c)
    result = CliRunner().invoke(agent, ["set", "--slug", "hal", "--task-id", "T9", *args])
    return c, result


def test_sets_waiting_on_email(monkeypatch):
    c, result = _run(monkeypatch, "--waiting-on-email", " jj@example.org ")
    assert result.exit_code == 0, result.output
    assert c.patched == [("T9", {"waiting_on_email": "jj@example.org"})]


def test_empty_string_clears_it(monkeypatch):
    c, result = _run(monkeypatch, "--waiting-on-email", "")
    assert result.exit_code == 0, result.output
    assert c.patched == [("T9", {"waiting_on_email": ""})]


def test_omitted_leaves_it_alone(monkeypatch):
    c, result = _run(monkeypatch, "--status", "todo")
    assert result.exit_code == 0, result.output
    assert "waiting_on_email" not in c.patched[0][1]


def test_over_length_is_rejected_before_writing(monkeypatch):
    c, result = _run(monkeypatch, "--waiting-on-email", "a" * 250 + "@x.org")
    assert result.exit_code != 0
    assert "--waiting-on-email" in result.output
    assert c.patched == []
