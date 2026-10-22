"""Tests for the canopy-web agent client (identity resolution + catalog; no live network)."""
import json
import subprocess

import pytest

from canopy_agent_factory import AgentSpec, create_agent
from orchestrator.agent_web import (
    AgentWebError,
    catalog_from_repo,
    gh_blob_base,
    normalize_repo_url,
    push_tasks,
    register,
    repo_url,
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


def test_push_tasks_posts_bare_list_to_tasks_endpoint(tmp_path, monkeypatch):
    repo = _agent(tmp_path)
    calls = []

    def transport(method, url, headers, body):
        calls.append((method, url, json.loads(body) if body else None))
        return 200, "{}"

    monkeypatch.setenv("CANOPY_WEB_PAT", "t")
    monkeypatch.setenv("CANOPY_WEB_API_URL", "https://x.test")
    monkeypatch.setattr("orchestrator.canopy_web.urllib_transport", transport)

    tasks = [{"title": "x", "ask_kind": "review"},
             {"ext_id": "T4", "title": "y", "ask_kind": "question"}]
    push_tasks(repo, tasks)

    assert len(calls) == 1
    method, url, body = calls[0]
    assert method == "POST"
    assert url == "https://x.test/api/agents/echo/tasks/"
    # BARE list, not wrapped in a key; a named ext_id is keyed so a re-push replays
    assert body == [{"title": "x", "ask_kind": "review"},
                    {"ext_id": "T4", "title": "y", "ask_kind": "question",
                     "idempotency_key": "echo:T4"}]


def _origin(repo, url):
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "remote", "add", "origin", url], cwd=repo, check=True)


@pytest.mark.parametrize("raw", [
    "dimagi-internal/echo", "https://github.com/dimagi-internal/echo",
    "https://github.com/dimagi-internal/echo.git", "git@github.com:dimagi-internal/echo.git",
])
def test_normalize_repo_url(raw):
    assert normalize_repo_url(raw) == "https://github.com/dimagi-internal/echo"


def test_normalize_repo_url_refuses_non_github():
    assert normalize_repo_url("https://gitlab.com/a/b") == ""
    assert normalize_repo_url("") == ""


def test_repo_url_from_the_origin(tmp_path):
    repo = _agent(tmp_path)
    assert repo_url(repo) == ""
    _origin(repo, "git@github.com:dimagi-internal/echo.git")
    assert repo_url(repo) == "https://github.com/dimagi-internal/echo"


def _fake_server(monkeypatch, recorded=None, refuse_repo=False):
    """`recorded` None = the agent does not exist (404)."""
    from orchestrator import agent_web
    posts = []

    def call(path, body=None, method="POST"):
        if method == "GET":
            if recorded is None:
                raise AgentWebError(f"GET {path} -> 404: not found")
            return {"slug": "echo", "repo_url": recorded}
        posts.append(body)
        if refuse_repo and "repo_url" in body:
            raise AgentWebError("POST /api/agents/ -> 403: changing repo_url on echo "
                                "requires being its owner or one of its admins")
        return {"slug": "echo"}

    monkeypatch.setattr(agent_web, "_call", call)
    return posts


def test_register_records_the_repo_for_a_new_agent(tmp_path, monkeypatch):
    repo = _agent(tmp_path)
    _origin(repo, "https://github.com/dimagi-internal/echo.git")
    posts = _fake_server(monkeypatch)
    register(repo)
    assert posts[-1]["repo_url"] == "https://github.com/dimagi-internal/echo"


def test_register_fills_a_missing_repo_but_never_moves_one(tmp_path, monkeypatch):
    repo = _agent(tmp_path)
    _origin(repo, "git@github.com:someone/echo-fork.git")
    posts = _fake_server(monkeypatch, recorded="")
    register(repo)
    assert posts[-1]["repo_url"] == "https://github.com/someone/echo-fork"
    posts = _fake_server(monkeypatch, recorded="https://github.com/dimagi-internal/echo")
    register(repo)
    assert "repo_url" not in posts[-1]


def test_register_without_rights_to_the_repo_still_registers(tmp_path, monkeypatch):
    repo = _agent(tmp_path)
    _origin(repo, "https://github.com/dimagi-internal/echo")
    posts = _fake_server(monkeypatch, recorded="", refuse_repo=True)
    assert register(repo) == {"slug": "echo"}
    assert len(posts) == 2 and "repo_url" not in posts[-1]
