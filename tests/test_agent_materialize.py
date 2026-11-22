"""Materializing a stored agent (orchestrator/agent_materialize.py): a clone of canopy-web's
git repo for the agent.

The contract (docs/architecture/stored-agents.md): origin = the canopy git url; the token is
passed per command and never lands in .git/config or the url; `reset --hard origin/main`
(or r<N>); untracked/ignored files survive and tracked files absent from the revision go;
the JSON line {slug, revision, root_sha256, commit, changed}; a `canopy://` dir from the old
archive writer is converted in place; an exported agent's dir is pointed at its repo.

canopy-web's git endpoint is REAL git (`git http-backend`) behind the auth rule
(tests/git_http_fake.py); its JSON API is faked by passing `git_url` / `info`.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from orchestrator import agent_materialize as am
from orchestrator.agent_definition import file_entry, manifest_root
from tests.git_http_fake import GitHttpFake, http_backend

pytestmark = pytest.mark.skipif(not http_backend(), reason="needs git http-backend")

REV1 = {
    "CLAUDE.md": (b"# helper\n", False),
    "skills/turn/SKILL.md": (b"---\nname: turn\ndescription: d\n---\nbody\n", False),
    ".gitignore": (b".env\nnode_modules/\n", False),
    "bin/helper-email": (b"#!/usr/bin/env python3\nprint('x')\n", True),
}
REV2 = {
    "CLAUDE.md": (b"# helper v2\n", False),
    ".gitignore": (b".env\nnode_modules/\n", False),
    "bin/helper-email": (b"#!/usr/bin/env python3\nprint('x')\n", True),
    "persona.md": (b"voice\n", False),
}


TOK = "cnpy-T0KEN-4f9e1c"


def root_of(files: dict) -> str:
    return manifest_root([file_entry(p, c, x) for p, (c, x) in files.items()])


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


@pytest.fixture
def srv(tmp_path):
    fake = GitHttpFake(tmp_path / "srv", token=TOK).start()
    yield fake
    fake.stop()


def run(dest: Path, srv: GitHttpFake, **kw) -> dict:
    kw.setdefault("git_url", srv.url("connect", "helper"))
    return am.materialize("helper", dest, base_url=srv.base, token=TOK, **kw)


def test_clone_is_at_live_with_origin_the_git_url(tmp_path, srv):
    sha = srv.publish("connect", "helper", REV1, 1)
    dest = tmp_path / "agents" / "helper"
    out = run(dest, srv)
    assert out == {"slug": "helper", "revision": 1, "root_sha256": root_of(REV1),
                   "commit": sha, "changed": True}
    assert git(dest, "rev-parse", "HEAD") == git(dest, "rev-parse", "origin/main") == sha
    assert git(dest, "symbolic-ref", "HEAD") == "refs/heads/main"
    assert git(dest, "remote", "get-url", "origin") == srv.url("connect", "helper")
    assert git(dest, "symbolic-ref", "refs/remotes/origin/HEAD") == "refs/remotes/origin/main"
    assert os.access(dest / "bin" / "helper-email", os.X_OK)
    assert am.is_stored_clone(dest)
    assert am.recorded(dest) == {"revision": 1, "root_sha256": root_of(REV1), "commit": sha}
    # No staging dir left behind; the lock sits beside the dest.
    assert sorted(p.name for p in dest.parent.iterdir()) == [".helper.materialize.lock", "helper"]


def test_the_token_is_never_written_to_the_repo(tmp_path, srv):
    srv.publish("connect", "helper", REV1, 1)
    dest = tmp_path / "helper"
    run(dest, srv)
    assert all(s["auth"] == f"Bearer {TOK}" for s in srv.seen)
    for f in (dest / ".git").rglob("*"):
        if f.is_file():
            assert TOK.encode() not in f.read_bytes(), f
    assert "Bearer" not in (dest / ".git" / "config").read_text()
    assert TOK not in git(dest, "remote", "get-url", "origin")


def test_a_wrong_token_is_refused_and_writes_nothing(tmp_path, srv):
    srv.publish("connect", "helper", REV1, 1)
    dest = tmp_path / "helper"
    with pytest.raises(am.MaterializeError):
        am.materialize("helper", dest, base_url=srv.base, token="nope",
                       git_url=srv.url("connect", "helper"))
    assert not dest.exists()
    assert [p.name for p in tmp_path.iterdir() if p.name != "srv"] == [".helper.materialize.lock"]


def test_untracked_and_ignored_survive_and_removed_files_go(tmp_path, srv):
    srv.publish("connect", "helper", REV1, 1)
    dest = tmp_path / "helper"
    run(dest, srv)
    (dest / ".env").write_text("SECRET=1")
    (dest / "node_modules").mkdir()
    (dest / "node_modules" / "x.js").write_text("x")
    (dest / "scratch.txt").write_text("mine")
    sha2 = srv.publish("connect", "helper", REV2, 2)
    out = run(dest, srv)
    assert out["changed"] is True and out["revision"] == 2 and out["commit"] == sha2
    assert out["root_sha256"] == root_of(REV2)
    assert not (dest / "skills" / "turn" / "SKILL.md").exists()
    assert (dest / "persona.md").read_text() == "voice\n"
    assert (dest / ".env").read_text() == "SECRET=1"
    assert (dest / "node_modules" / "x.js").exists()
    assert (dest / "scratch.txt").read_text() == "mine"
    # History, not one orphan per revision: r2's parent is r1.
    assert git(dest, "rev-parse", "HEAD^") == git(dest, "rev-parse", "r1")


def test_noop_when_already_there(tmp_path, srv):
    srv.publish("connect", "helper", REV1, 1)
    dest = tmp_path / "helper"
    first = run(dest, srv)
    again = run(dest, srv)
    assert again == {**first, "changed": False}


def test_edited_tracked_file_is_restored(tmp_path, srv):
    srv.publish("connect", "helper", REV1, 1)
    dest = tmp_path / "helper"
    run(dest, srv)
    (dest / "CLAUDE.md").write_text("local edit")
    out = run(dest, srv)
    assert out["changed"] is True
    assert (dest / "CLAUDE.md").read_text() == "# helper\n"


def test_a_published_revision_by_number(tmp_path, srv):
    sha1 = srv.publish("connect", "helper", REV1, 1)
    srv.publish("connect", "helper", REV2, 2)
    dest = tmp_path / "helper"
    out = run(dest, srv, revision="1")
    assert out["commit"] == sha1 and out["revision"] == 1
    assert out["root_sha256"] == root_of(REV1)
    with pytest.raises(am.MaterializeError, match="no published revision 7"):
        run(dest, srv, revision="7")


def test_legacy_canopy_origin_dir_is_converted_in_place(tmp_path, srv):
    """A dir the archive writer made: origin canopy://connect/helper, one orphan commit, no
    git url recorded anywhere. It becomes a clone of the git url — workspace read from the
    old origin when canopy-web's answer lacks git_url — with .env kept."""
    dest = tmp_path / "helper"
    subprocess.run(["git", "init", "-q", "-b", "main", str(dest)], check=True)
    (dest / "CLAUDE.md").write_text("orphan")
    (dest / ".env").write_text("KEEP=1")
    git(dest, "add", "CLAUDE.md")
    git(dest, "-c", "user.name=canopy", "-c", "user.email=a@b", "-c", "commit.gpgsign=false",
        "commit", "-qm", "canopy revision 1")
    git(dest, "remote", "add", "origin", "canopy://connect/helper")
    git(dest, "config", "canopy.slug", "helper")
    git(dest, "config", "canopy.revision", "1")
    assert am.is_stored_clone(dest)
    sha = srv.publish("connect", "helper", REV1, 1)
    out = am.materialize("helper", dest, base_url=srv.base, token=TOK,
                         info={"source": "stored", "revision": 1})
    assert out["commit"] == sha and out["changed"] is True and out["revision"] == 1
    assert git(dest, "remote", "get-url", "origin") == srv.url("connect", "helper")
    assert (dest / "CLAUDE.md").read_text() == "# helper\n"
    assert (dest / ".env").read_text() == "KEEP=1"
    assert am.is_stored_clone(dest)


def test_existing_github_clone_is_converted_in_place(tmp_path, srv):
    dest = tmp_path / "helper"
    subprocess.run(["git", "init", "-q", str(dest)], check=True)
    (dest / "old.txt").write_text("tracked before")
    (dest / ".env").write_text("keep")
    git(dest, "add", "old.txt")
    git(dest, "-c", "user.name=x", "-c", "user.email=x@x", "-c", "commit.gpgsign=false",
        "commit", "-qm", "old")
    git(dest, "remote", "add", "origin", "https://github.com/o/helper")
    srv.publish("connect", "helper", REV1, 1)
    out = run(dest, srv)
    assert not (dest / "old.txt").exists()
    assert (dest / ".env").read_text() == "keep"
    assert git(dest, "remote", "get-url", "origin") == srv.url("connect", "helper")
    assert out["changed"] is True


def test_exported_agent_dir_is_repointed_at_its_repo(tmp_path, srv):
    srv.publish("connect", "helper", REV1, 1)
    dest = tmp_path / "helper"
    run(dest, srv)
    head = git(dest, "rev-parse", "HEAD")
    out = am.materialize("helper", dest, base_url=srv.base, token=TOK,
                         info={"source": "repo", "repo_url": "https://github.com/o/helper"})
    assert out["source"] == "repo" and out["commit"] == head and out["changed"] is False
    assert git(dest, "remote", "get-url", "origin") == "https://github.com/o/helper.git"
    assert not am.is_stored_clone(dest)
    assert am.recorded(dest) == {}
    with pytest.raises(am.ExportedAgent, match="github.com/o/helper"):
        am.materialize("helper", tmp_path / "fresh", base_url=srv.base, token=TOK,
                       info={"source": "repo", "repo_url": "https://github.com/o/helper"})


def test_git_url_comes_from_canopy_web(tmp_path, srv, monkeypatch):
    srv.publish("connect", "helper", REV1, 1)
    calls = []

    def call(method, path, body=None, **kw):
        calls.append(path)
        assert kw["token"] == TOK
        if path.endswith("/runtime"):
            return {"repo_url": "", "definition": {"source": "stored", "revision": 1,
                                                    "git_url": srv.url("connect", "helper")}}
        raise AssertionError(path)
    monkeypatch.setattr(am.canopy_web, "call", call)
    out = am.materialize("helper", tmp_path / "helper", base_url=srv.base, token=TOK)
    assert out["revision"] == 1 and calls == ["/api/agents/helper/runtime"]


def test_is_canopy_git_url(monkeypatch):
    monkeypatch.setenv("CANOPY_WEB_API_URL", "https://canopy.example")
    assert am.is_canopy_git_url("https://canopy.example/git/connect/helper.git")
    assert am.is_canopy_git_url("canopy://connect/helper")
    assert not am.is_canopy_git_url("https://github.com/o/helper.git")
    assert not am.is_canopy_git_url("https://evil.example/git/connect/helper.git")


def test_auth_env_appends_to_existing_command_config():
    env = am.auth_env("https://canopy.example/git/w/s.git", "T",
                      {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "a.b",
                       "GIT_CONFIG_VALUE_0": "c"})
    assert env["GIT_CONFIG_KEY_0"] == "a.b"
    assert env["GIT_CONFIG_KEY_1"] == "http.https://canopy.example/.extraHeader"
    assert env["GIT_CONFIG_VALUE_1"] == "Authorization: Bearer T"
    assert env["GIT_CONFIG_KEY_2"] == "credential.helper" and env["GIT_CONFIG_VALUE_2"] == ""
    assert env["GIT_CONFIG_COUNT"] == "3" and env["GIT_TERMINAL_PROMPT"] == "0"


# ── the CLI ──────────────────────────────────────────────────────────────────────────────

def _cli(monkeypatch, srv):
    real = am.materialize

    def fake(slug, dest, **kw):
        return real(slug, dest, base_url=srv.base, token=TOK,
                    git_url=srv.url("connect", slug),
                    **{k: v for k, v in kw.items() if k in ("revision",)})
    monkeypatch.setattr(am, "materialize", fake)


def test_cli_materialize_prints_one_json_line(tmp_path, monkeypatch, srv):
    from orchestrator.cli import main
    srv.publish("connect", "helper", REV1, 1)
    _cli(monkeypatch, srv)
    res = CliRunner().invoke(main, ["agent", "materialize", "--slug", "helper", "--dest",
                                    str(tmp_path / "helper"), "--runner"])
    assert res.exit_code == 0, res.output
    lines = res.output.strip().splitlines()
    assert len(lines) == 1
    body = json.loads(lines[0])
    assert set(body) == {"slug", "revision", "root_sha256", "commit", "changed"}


def test_cli_sync_runner_uses_agent_root(tmp_path, monkeypatch, srv):
    from orchestrator.cli import main
    srv.publish("connect", "helper", REV1, 1)
    _cli(monkeypatch, srv)
    monkeypatch.setenv("AGENT_ROOT", str(tmp_path / "agents"))
    monkeypatch.setenv("CANOPY_WEB_API_URL", srv.base)
    r1 = CliRunner().invoke(main, ["agent", "sync", "--runner", "--slug", "helper"])
    assert r1.exit_code == 0, r1.output
    assert json.loads(r1.output)["changed"] is True
    assert (tmp_path / "agents" / "helper" / "CLAUDE.md").exists()
    r2 = CliRunner().invoke(main, ["agent", "sync", "--runner", "--slug", "helper"])
    assert r2.exit_code == 0, r2.output
    assert json.loads(r2.output)["changed"] is False


def test_cli_sync_refuses_a_github_clone(tmp_path, monkeypatch, srv):
    from orchestrator.cli import main
    repo = tmp_path / "helper"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/o/r"],
                   check=True)
    _cli(monkeypatch, srv)
    res = CliRunner().invoke(main, ["agent", "sync", "--slug", "helper", "--dest", str(repo)])
    assert res.exit_code != 0 and "not a stored agent" in res.output


def test_cli_sync_manager_sync_still_posts(monkeypatch):
    from orchestrator import agent_cli
    from orchestrator.cli import main
    posted = {}

    class C:
        def post_sync(self, **kw):
            posted.update(kw)
            return {"id": 1}
    monkeypatch.setattr(agent_cli, "_client", lambda slug, **kw: C())
    res = CliRunner().invoke(main, ["agent", "sync", "--slug", "ada", "--doc-url", "u",
                                    "--title", "t", "--period-start", "2026-11-01",
                                    "--period-end", "2026-11-07"])
    assert res.exit_code == 0, res.output
    assert posted["doc_url"] == "u"
    half = CliRunner().invoke(main, ["agent", "sync", "--slug", "ada", "--doc-url", "u"])
    assert half.exit_code != 0 and "needs --doc-url" in half.output
