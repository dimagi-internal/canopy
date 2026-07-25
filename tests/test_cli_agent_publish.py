# tests/test_cli_agent_publish.py
"""CLI tests for `canopy agent-publish items` (posts a review-items batch)."""
import json

import pytest
from click.testing import CliRunner

from orchestrator.agent_factory import AgentSpec, create_agent
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


def test_agent_publish_items_cli(fake_http, tmp_path):
    calls, _ = fake_http
    repo = _agent_repo(tmp_path)
    items_json = tmp_path / "items.json"
    items = [{"title": "x", "kind": "review"}]
    items_json.write_text(json.dumps(items))

    r = CliRunner().invoke(
        main, ["agent-publish", "items", "--repo", str(repo), str(items_json)]
    )

    assert r.exit_code == 0, r.output
    # register() + push_items() -> two calls; the second is the items POST
    item_calls = [c for c in calls if c[1].endswith("/items/")]
    assert len(item_calls) == 1
    method, url, body = item_calls[0]
    assert method == "POST"
    assert url == "https://x.test/api/agents/echo/items/"
    assert body == items


def test_agent_publish_items_rejects_non_list(fake_http, tmp_path):
    repo = _agent_repo(tmp_path)
    bad_json = tmp_path / "bad.json"
    bad_json.write_text(json.dumps({"not": "a list"}))

    r = CliRunner().invoke(
        main, ["agent-publish", "items", "--repo", str(repo), str(bad_json)]
    )

    assert r.exit_code != 0
    assert "items file must be a JSON list" in r.output
