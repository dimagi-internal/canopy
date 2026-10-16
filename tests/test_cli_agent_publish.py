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


# --- workspace: canopy-web 422s a NEW agent with no workspace for a caller in two ---------

_AMBIGUOUS = (422, json.dumps({"detail": "no unambiguous workspace to home this agent in; "
                                         "pass `workspace`: you are in dimagi and connect"}))


def _register_bodies(calls):
    return [b for m, u, b in calls if m == "POST" and u.endswith("/api/agents/")]


def test_register_workspace_flag_is_sent(fake_http, tmp_path):
    calls, _ = fake_http
    repo = _agent_repo(tmp_path)
    r = CliRunner().invoke(main, ["agent-publish", "register", "--repo", str(repo),
                                  "--workspace", "connect"])
    assert r.exit_code == 0, r.output
    assert [b.get("workspace") for b in _register_bodies(calls)] == ["connect"]


def test_register_without_workspace_sends_none(fake_http, tmp_path, monkeypatch):
    # An existing agent re-registered on every sync must not be moved by an env default.
    calls, _ = fake_http
    monkeypatch.setenv("CANOPY_WEB_WORKSPACE", "dimagi")
    repo = _agent_repo(tmp_path)
    r = CliRunner().invoke(main, ["agent-publish", "register", "--repo", str(repo)])
    assert r.exit_code == 0, r.output
    assert ["workspace" in b for b in _register_bodies(calls)] == [False]


def _ambiguous_then_ok(monkeypatch, calls):
    def transport(method, url, headers, body):
        b = json.loads(body) if body else None
        calls.append((method, url, b))
        if method == "POST" and url.endswith("/api/agents/") and not (b or {}).get("workspace"):
            return _AMBIGUOUS
        return (201, "{}")
    monkeypatch.setattr("orchestrator.canopy_web.urllib_transport", transport)


def test_ambiguous_create_retries_with_agent_json_workspace(fake_http, tmp_path, monkeypatch):
    calls, _ = fake_http
    _ambiguous_then_ok(monkeypatch, calls)
    monkeypatch.setenv("CANOPY_WEB_WORKSPACE", "dimagi")
    repo = _agent_repo(tmp_path)
    aj = repo / "config" / "agent.json"
    aj.write_text(json.dumps({**json.loads(aj.read_text()), "workspace": "connect"}))
    r = CliRunner().invoke(main, ["agent-publish", "register", "--repo", str(repo)])
    assert r.exit_code == 0, r.output
    assert [b.get("workspace") for b in _register_bodies(calls)] == [None, "connect"]


def test_ambiguous_create_retries_with_env_workspace(fake_http, tmp_path, monkeypatch):
    calls, _ = fake_http
    _ambiguous_then_ok(monkeypatch, calls)
    monkeypatch.setenv("CANOPY_WEB_WORKSPACE", "connect")
    repo = _agent_repo(tmp_path)
    r = CliRunner().invoke(main, ["agent-publish", "skills", "--repo", str(repo)])
    assert r.exit_code == 0, r.output
    assert [b.get("workspace") for b in _register_bodies(calls)] == [None, "connect"]


def test_ambiguous_create_with_no_fallback_says_how(fake_http, tmp_path, monkeypatch):
    calls, _ = fake_http
    _ambiguous_then_ok(monkeypatch, calls)
    monkeypatch.delenv("CANOPY_WEB_WORKSPACE", raising=False)
    repo = _agent_repo(tmp_path)
    r = CliRunner().invoke(main, ["agent-publish", "register", "--repo", str(repo)])
    assert r.exit_code != 0
    assert "--workspace <slug>" in r.output
    assert len(_register_bodies(calls)) == 1
