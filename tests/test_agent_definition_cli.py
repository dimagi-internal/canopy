"""`canopy agent definition …` and `canopy create-agent --stored` against a faked canopy-web."""
from __future__ import annotations

import base64
import json
import subprocess

import pytest
from click.testing import CliRunner

from orchestrator import canopy_web
from orchestrator.cli import main


class Web:
    def __init__(self):
        self.calls = []
        self.text = {}

    def call(self, method, path, body=None, **kw):
        self.calls.append((method, path, body))
        if path == "/api/agents/helper/definition":
            return {"source": "stored", "live": {"number": 3, "root_sha256": "ab"},
                    "draft": {"number": 4}}
        if path.endswith("/revisions/3"):
            return {"number": 3, "tree": {"CLAUDE.md": {"sha256": "x", "size": 9,
                                                        "executable": False},
                                          "bin/helper-email": {"sha256": "y", "size": 5,
                                                               "executable": True}}}
        return {"ok": True, "path": path}

    def call_text(self, method, path, **kw):
        self.calls.append((method, path, None))
        return self.text.get(path, "")


@pytest.fixture
def web(monkeypatch):
    w = Web()
    monkeypatch.setattr(canopy_web, "call", w.call)
    monkeypatch.setattr(canopy_web, "call_text", w.call_text)
    return w


def cli(*args):
    return CliRunner().invoke(main, list(args), catch_exceptions=False)


def test_files_resolves_live(web):
    res = cli("agent", "definition", "files", "--slug", "helper")
    assert res.exit_code == 0, res.output
    assert "x        5  bin/helper-email" in res.output and "CLAUDE.md" in res.output
    assert ("GET", "/api/agents/helper/definition/revisions/3", None) in web.calls


def test_cat_and_diff(web):
    web.text["/api/agents/helper/definition/revisions/3/files/CLAUDE.md"] = "# hi\n"
    assert cli("agent", "definition", "cat", "--slug", "helper", "CLAUDE.md").output == "# hi\n"
    web.text["/api/agents/helper/definition/revisions/4/diff?against=3"] = json.dumps(
        {"files": [{"path": "CLAUDE.md", "diff": "--- a\n+++ b\n"}]})
    out = cli("agent", "definition", "diff", "--slug", "helper").output
    assert "+++ b" in out


def test_edit_creates_a_draft_on_live(web, tmp_path):
    f = tmp_path / "c.md"
    f.write_text("new body\n")
    res = cli("agent", "definition", "edit", "--slug", "helper", "--set", "CLAUDE.md", str(f),
              "--delete", "docs/old.md", "-m", "tighten")
    assert res.exit_code == 0, res.output
    post = [c for c in web.calls if c[0] == "POST"][0]
    assert post[1] == "/api/agents/helper/definition/drafts"
    assert post[2] == {"base": 3, "message": "tighten", "changes": [
        {"path": "CLAUDE.md", "content": "new body\n"}, {"path": "docs/old.md", "content": None}]}


def test_edit_is_refused_before_upload(web, tmp_path):
    f = tmp_path / "x"
    f.write_text("x")
    res = CliRunner().invoke(main, ["agent", "definition", "edit", "--slug", "helper", "--set",
                                    "hooks/hooks.json", str(f), "-m", "m"])
    assert res.exit_code != 0 and "hooks/ is refused" in res.output
    assert not [c for c in web.calls if c[0] == "POST"]


def test_publish_rollback_discard(web):
    cli("agent", "definition", "publish", "--slug", "helper", "4")
    cli("agent", "definition", "rollback", "--slug", "helper", "2")
    cli("agent", "definition", "discard", "--slug", "helper", "4")
    posts = [(p, b) for m, p, b in web.calls if m == "POST"]
    assert posts == [("/api/agents/helper/definition/revisions/4/publish", {}),
                     ("/api/agents/helper/definition/rollback", {"revision": 2}),
                     ("/api/agents/helper/definition/revisions/4/discard", {})]


def _repo(tmp_path):
    repo = tmp_path / "helper"
    (repo / ".claude-plugin").mkdir(parents=True)
    (repo / ".claude-plugin" / "plugin.json").write_text('{"name": "helper"}')
    (repo / "skills" / "turn").mkdir(parents=True)
    (repo / "skills" / "turn" / "SKILL.md").write_text("---\nname: turn\n---\n")
    (repo / "CLAUDE.md").write_text("# helper\n")
    (repo / "bin").mkdir()
    (repo / "bin" / "helper-email").write_text("#!/bin/sh\n")
    (repo / "bin" / "_env.py").write_text("x")
    (repo / "bin" / "helper-email").chmod(0o755)
    (repo / "bin" / "_env.py").chmod(0o755)
    (repo / ".env").write_text("SECRET=untracked\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "CLAUDE.md", "skills", "bin",
                    ".claude-plugin"], check=True)
    return repo


def test_convert_uploads_tracked_editable_files_only(web, tmp_path):
    repo = _repo(tmp_path)
    res = CliRunner().invoke(main, ["agent", "definition", "convert", "--from", str(repo)])
    assert res.exit_code != 0 and "bin/_env.py" in res.output and "--drop-refused" in res.output
    res = cli("agent", "definition", "convert", "--from", str(repo), "--drop-refused",
              "-m", "move to canopy")
    assert res.exit_code == 0, res.output
    (method, path, body), = [c for c in web.calls if c[0] == "POST"]
    assert path == "/api/agents/helper/definition/convert"
    assert body["message"] == "move to canopy"
    assert sorted(f["path"] for f in body["files"]) == ["CLAUDE.md", "skills/turn/SKILL.md"]
    assert base64.b64decode(body["files"][0]["content_b64"]) == b"# helper\n"
    summary = json.loads(res.output)
    assert set(summary["generated_by_canopy_web"]) == {".claude-plugin/plugin.json",
                                                       "bin/helper-email"}


def test_convert_dry_run_posts_nothing(web, tmp_path):
    res = cli("agent", "definition", "convert", "--from", str(_repo(tmp_path)), "--dry-run")
    assert json.loads(res.output)["refused"][0].startswith("bin/_env.py")
    assert not [c for c in web.calls if c[0] == "POST"]


def test_create_agent_stored_posts_and_writes_nothing(web, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(main, ["create-agent", "connect-helper", "--mandate", "Help workers",
                                    "--stored", "--workspace", "connect"])
    assert res.exit_code == 0, res.output
    assert web.calls == [("POST", "/api/agents/stored", {
        "name": "Connect Helper", "slug": "connect-helper", "mandate": "Help workers",
        "workspace": "connect"})]
    assert list(tmp_path.iterdir()) == []
