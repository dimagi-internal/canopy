"""Tests for the canopy-web agent client (identity resolution + catalog; no live network)."""
import json
import subprocess

import pytest

from orchestrator.agent_factory import AgentSpec, create_agent
from orchestrator.agent_web import (
    AgentWebError,
    catalog_from_repo,
    gh_blob_base,
    push_items,
    resolve_identity,
)


def _agent(tmp_path):
    spec = AgentSpec(
        slug="echo", display_name="Echo", mandate="be the marketing agent.",
        mailbox="echo@dimagi-ai.com",
    )
    create_agent(spec, tmp_path / "echo")
    return tmp_path / "echo"


def test_resolve_identity_from_generated_agent(tmp_path):
    repo = _agent(tmp_path)
    ident = resolve_identity(repo)
    assert ident["slug"] == "echo"
    # config/agent.json overrides apply
    assert ident["email"] == "echo@dimagi-ai.com"
    assert ident["name"] == "Echo"
    assert ident["persona"]


def test_resolve_identity_requires_plugin_json(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(AgentWebError):
        resolve_identity(tmp_path / "empty")


def test_resolve_identity_without_agent_json_falls_back(tmp_path):
    repo = _agent(tmp_path)
    (repo / "config" / "agent.json").unlink()
    ident = resolve_identity(repo)
    assert ident["slug"] == "echo"
    assert ident["name"] == "Echo"          # derived from slug
    assert ident["email"] == ""             # no override available


def test_catalog_from_repo_lists_skills(tmp_path):
    repo = _agent(tmp_path)
    cat = {e["name"] for e in catalog_from_repo(repo)}
    assert "turn" in cat
    assert "agent-turn-review" in cat


def test_gh_blob_base_parses_github_remotes(tmp_path):
    repo = _agent(tmp_path)
    # no remote yet -> empty
    assert gh_blob_base(repo) == ""
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:dimagi-internal/echo.git"],
        cwd=repo, check=True,
    )
    base = gh_blob_base(repo)
    assert base == "https://github.com/dimagi-internal/echo/blob/main/skills/{name}/SKILL.md"
    # catalog uses it to build per-skill URLs
    urls = {e["name"]: e["url"] for e in catalog_from_repo(repo)}
    assert urls["turn"].endswith("/skills/turn/SKILL.md")


def test_gh_blob_base_handles_https_remote(tmp_path):
    repo = _agent(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/foo/bar.git"],
        cwd=repo, check=True,
    )
    assert gh_blob_base(repo).startswith("https://github.com/foo/bar/blob/main/")


def test_push_items_posts_bare_list_to_items_endpoint(tmp_path, monkeypatch):
    repo = _agent(tmp_path)
    calls = []

    def transport(method, url, headers, body):
        calls.append((method, url, json.loads(body) if body else None))
        return 200, "{}"

    monkeypatch.setenv("CANOPY_WEB_PAT", "t")
    monkeypatch.setenv("CANOPY_WEB_API_URL", "https://x.test")
    monkeypatch.setattr("orchestrator.canopy_web.urllib_transport", transport)

    items = [{"title": "x", "kind": "review"}]
    push_items(repo, items)

    assert len(calls) == 1
    method, url, body = calls[0]
    assert method == "POST"
    assert url == "https://x.test/api/agents/echo/items/"
    assert body == items  # BARE list, not wrapped in a key
