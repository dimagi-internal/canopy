# tests/test_cli_agent_publish.py
"""CLI tests for `canopy agent-publish tasks` (creates a batch of tasks, asks included)."""
import json

import pytest
from click.testing import CliRunner

from canopy_agent_factory import AgentSpec, create_agent
from orchestrator.cli import main


@pytest.fixture
def fake_http(monkeypatch):
    calls = []
    responses = {}

    def transport(method, url, headers, body):
        calls.append((method, url, json.loads(body) if body else None))
        return responses.get((method, url.split("/api/")[1]), (200, "{}"))

    monkeypatch.setenv("CANOPY_WEB_PAT", "t")
    monkeypatch.setenv("CANOPY_WEB_API_URL", "https://x.test")
    monkeypatch.setattr("orchestrator.canopy_web.urllib_transport", transport)
    return calls, responses


def _agent_repo(tmp_path):
    spec = AgentSpec(
        slug="echo", display_name="Echo", mandate="be the marketing agent.",
        mailbox="echo@dimagi-ai.com",
    )
    create_agent(spec, tmp_path / "echo")
    return tmp_path / "echo"


def test_agent_publish_tasks_cli(fake_http, tmp_path):
    calls, _ = fake_http
    repo = _agent_repo(tmp_path)
    tasks_json = tmp_path / "tasks.json"
    tasks = [{"title": "x", "ask_kind": "review", "ask_body": "ship it?"}]
    tasks_json.write_text(json.dumps(tasks))

    r = CliRunner().invoke(
        main, ["agent-publish", "tasks", "--repo", str(repo), str(tasks_json)]
    )

    assert r.exit_code == 0, r.output
    # register() + push_tasks() -> two calls; the second is the tasks POST
    task_calls = [c for c in calls if c[1].endswith("/tasks/")]
    assert len(task_calls) == 1
    method, url, body = task_calls[0]
    assert method == "POST"
    assert url == "https://x.test/api/agents/echo/tasks/"
    assert body == tasks  # a bare list


def test_agent_publish_tasks_rejects_non_list(fake_http, tmp_path):
    repo = _agent_repo(tmp_path)
    bad_json = tmp_path / "bad.json"
    bad_json.write_text(json.dumps({"not": "a list"}))

    r = CliRunner().invoke(
        main, ["agent-publish", "tasks", "--repo", str(repo), str(bad_json)]
    )

    assert r.exit_code != 0
    assert "tasks file must be a JSON list" in r.output


@pytest.mark.parametrize("verb", ["items", "work"])
def test_the_old_verbs_are_gone(verb):
    """items -> tasks; work products -> a project's links (`agent project-set --append-link`)."""
    r = CliRunner().invoke(main, ["agent-publish", verb, "--help"])
    assert r.exit_code != 0
