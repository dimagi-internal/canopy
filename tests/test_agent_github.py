"""agent_github: an agent's laptop session acts on GitHub as the agent's configured
identity, never the machine owner's `gh` login (canopy#832)."""
import io
import json
import os
import subprocess
import urllib.error

import pytest
from click.testing import CliRunner

from orchestrator import agent_github as ag
from orchestrator.agent_github import GitHubIdentity, GitHubIdentityError

USER = {"login": "ace-bot", "name": "ACE", "id": 4242}


def _call(body=None, exc=None):
    seen = {}

    def call(method, path, token=None):
        seen.update(method=method, path=path, token=token)
        if exc:
            raise exc
        return body
    return call, seen


def test_resolves_the_agents_credential_from_canopy_web_and_asks_github_who_it_is():
    call, seen = _call({"github_token": "ghp_x"})
    ident = ag.resolve_identity("ace", call=call, token="op-pat", github_user=lambda t: USER)
    assert seen == {"method": "GET", "path": "/api/agents/ace/credentials/resolve", "token": "op-pat"}
    assert (ident.login, ident.name, ident.token) == ("ace-bot", "ACE", "ghp_x")
    assert ident.email == "4242+ace-bot@users.noreply.github.com"


def test_no_credential_is_an_error_naming_where_an_admin_sets_it():
    call, _ = _call({"github_token": ""})
    with pytest.raises(GitHubIdentityError, match="/agents/ace/settings"):
        ag.resolve_identity("ace", call=call, token="t", github_user=lambda t: USER)


def test_canopy_web_failure_is_an_error_not_a_fallback():
    call, _ = _call(exc=RuntimeError("403 no live runner you pair is assigned to this agent"))
    with pytest.raises(GitHubIdentityError, match="no live runner you pair"):
        ag.resolve_identity("ace", call=call, token="t", github_user=lambda t: USER)


def test_a_rejected_token_says_expired_or_revoked():
    def gh(_):
        raise urllib.error.HTTPError("u", 401, "Bad credentials", {}, io.BytesIO(b""))
    call, _ = _call({"github_token": "ghp_x"})
    with pytest.raises(GitHubIdentityError, match="expired or revoked"):
        ag.resolve_identity("ace", call=call, token="t", github_user=gh)


def _git_env(ident):
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp"),
           "GIT_TERMINAL_PROMPT": "0"}
    # round-trip through a real shell, the way Claude Code sources CLAUDE_ENV_FILE
    script = ag.export_lines(ag.session_env(ident)) + 'env -0'
    out = subprocess.run(["/bin/sh", "-c", script], capture_output=True, env=env).stdout
    return dict(kv.split("=", 1) for kv in out.decode().split("\0") if "=" in kv)


IDENT = GitHubIdentity(slug="ace", token="ghp_agent", login="ace-bot", name="ACE Bot", user_id=7)


def test_session_env_makes_git_hand_over_the_agents_token_and_no_other(tmp_path):
    env = _git_env(IDENT)
    r = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                       capture_output=True, text=True, env=env, timeout=20)
    assert r.returncode == 0, r.stderr
    assert "password=ghp_agent" in r.stdout and "username=x-access-token" in r.stdout


def test_session_env_authors_and_commits_as_the_agent(tmp_path):
    env = _git_env(IDENT)
    run = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, env=env, check=True,
                                    capture_output=True, text=True).stdout
    run("init", "-q")
    run("-c", "user.name=Owner", "-c", "user.email=owner@x", "commit", "-q", "--allow-empty", "-m", "x")
    assert run("log", "-1", "--format=%an <%ae>|%cn <%ce>") == (
        "ACE Bot <7+ace-bot@users.noreply.github.com>|ACE Bot <7+ace-bot@users.noreply.github.com>\n")
    assert env["GH_TOKEN"] == "ghp_agent" and env["CANOPY_AGENT_GITHUB"] == "ace-bot"


def test_git_config_parameters_quotes_like_git():
    assert ag.git_config_parameters([("a.b", "it's")]) == "'a.b'='it'\\''s'"


def test_cli_github_never_prints_the_token(monkeypatch):
    from orchestrator.agent_cli import agent
    monkeypatch.setattr(ag, "resolve_identity", lambda slug: IDENT)
    r = CliRunner().invoke(agent, ["github", "--slug", "ace", "--json-output"])
    assert r.exit_code == 0 and "ghp_agent" not in r.output
    assert json.loads(r.output)["login"] == "ace-bot"


def test_cli_github_env_exits_1_with_reason_when_none(monkeypatch):
    from orchestrator.agent_cli import agent

    def none(slug):
        raise GitHubIdentityError("canopy-web holds no GitHub credential for ace")
    monkeypatch.setattr(ag, "resolve_identity", none)
    r = CliRunner().invoke(agent, ["github-env", "--slug", "ace"])
    assert r.exit_code == 1 and "export" not in r.output
    assert "holds no GitHub credential" in r.output
