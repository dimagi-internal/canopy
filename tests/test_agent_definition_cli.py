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


# ── git: convert --with-history, clone, credential, export ──────────────────────────────

def _commit(repo, msg="c"):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=x", "-c", "user.email=x@x",
                    "-c", "commit.gpgsign=false", "commit", "-qm", msg], check=True)


def test_convert_with_history_sends_a_bundle_of_main(web, tmp_path):
    repo = _repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "main"], check=True)
    _commit(repo, "first")
    res = cli("agent", "definition", "convert", "--from", str(repo), "--drop-refused",
              "--with-history")
    assert res.exit_code == 0, res.output
    (_, _, body), = [c for c in web.calls if c[0] == "POST"]
    bundle = tmp_path / "b.bundle"
    bundle.write_bytes(base64.b64decode(body["bundle_b64"]))
    heads = subprocess.run(["git", "bundle", "list-heads", str(bundle)], capture_output=True,
                           text=True, check=True).stdout
    assert heads.strip().endswith("refs/heads/main")
    assert json.loads(res.output)["history_bundle_bytes"] == bundle.stat().st_size


def test_convert_with_history_refuses_above_the_cap_with_the_files_only_hint(web, tmp_path,
                                                                             monkeypatch):
    from orchestrator import agent_definition_cli as dc
    repo = _repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "main"], check=True)
    _commit(repo)
    monkeypatch.setattr(dc, "MAX_CONVERT_BODY", 100)
    res = CliRunner().invoke(main, ["agent", "definition", "convert", "--from", str(repo),
                                    "--drop-refused", "--with-history"])
    assert res.exit_code != 0 and "drop --with-history" in res.output
    assert not [c for c in web.calls if c[0] == "POST"]


def test_convert_with_history_needs_main(web, tmp_path):
    repo = _repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "trunk"], check=True)
    _commit(repo)
    res = CliRunner().invoke(main, ["agent", "definition", "convert", "--from", str(repo),
                                    "--drop-refused", "--with-history"])
    assert res.exit_code != 0 and "no `main` branch" in res.output


@pytest.fixture
def gitsrv(tmp_path, monkeypatch):
    from tests.git_http_fake import GitHttpFake, http_backend
    if not http_backend():
        pytest.skip("needs git http-backend")
    srv = GitHttpFake(tmp_path / "srv", token="PERSON-TOKEN-1").start()
    srv.publish("connect", "helper", {"CLAUDE.md": (b"# helper\n", False)}, 1)
    monkeypatch.setenv("CANOPY_WEB_API_URL", srv.base)
    monkeypatch.setattr(canopy_web, "resolve_token", lambda t=None, **kw: "PERSON-TOKEN-1")

    def call(method, path, body=None, **kw):
        if path == "/api/agents/helper/runtime":
            return {"definition": {"source": "stored", "git_url": srv.url("connect", "helper")}}
        srv.api.append((method, path, body))
        return {"ok": True}
    srv.api = []
    monkeypatch.setattr(canopy_web, "call", call)
    yield srv
    srv.stop()


def test_clone_makes_a_working_copy_and_stores_no_token(gitsrv, tmp_path, monkeypatch):
    from orchestrator import agent_definition_cli as dc
    monkeypatch.setattr(dc, "_helper_command", lambda: "!canopy-test-helper")
    dest = tmp_path / "wc"
    res = cli("agent", "definition", "clone", "--slug", "helper", str(dest))
    assert res.exit_code == 0, res.output
    assert (dest / "CLAUDE.md").read_text() == "# helper\n"
    assert "git -C" in res.output and "pull" in res.output and "nothing is stored" in res.output
    cfg = (dest / ".git" / "config").read_text()
    assert "PERSON-TOKEN-1" not in cfg and "Bearer" not in cfg
    assert f'[credential "{gitsrv.base}"]' in cfg and "canopy-test-helper" in cfg
    assert "r1" in subprocess.run(["git", "-C", str(dest), "tag"], capture_output=True,
                                  text=True).stdout


def test_credential_helper_answers_only_for_canopy(gitsrv):
    host = gitsrv.base.split("://", 1)[1]
    res = CliRunner().invoke(main, ["agent", "definition", "credential", "get"],
                             input=f"protocol=http\nhost={host}\n")
    assert res.output == "username=canopy\npassword=PERSON-TOKEN-1\n"
    other = CliRunner().invoke(main, ["agent", "definition", "credential", "get"],
                               input="protocol=https\nhost=github.com\n")
    assert other.output == ""
    assert CliRunner().invoke(main, ["agent", "definition", "credential", "store"],
                              input=f"protocol=http\nhost={host}\npassword=x\n").output == ""


def test_a_helper_clone_pulls_with_plain_git(gitsrv, tmp_path, monkeypatch):
    """The installed helper is what a later `git pull` uses: 401 → helper → Basic(token)."""
    import os
    from orchestrator import agent_definition_cli as dc
    helper = tmp_path / "helper.sh"
    helper.write_text('#!/bin/sh\ntest "$1" = get || exit 0\ncat >/dev/null\n'
                      'echo username=canopy\necho password=PERSON-TOKEN-1\n')
    helper.chmod(0o755)
    monkeypatch.setattr(dc, "_helper_command", lambda: str(helper))
    dest = tmp_path / "wc"
    assert cli("agent", "definition", "clone", "--slug", "helper", str(dest)).exit_code == 0
    gitsrv.publish("connect", "helper", {"CLAUDE.md": (b"# v2\n", False)}, 2)
    r = subprocess.run(["git", "-C", str(dest), "pull", "-q", "--ff-only"], capture_output=True,
                       text=True, env={"PATH": os.environ["PATH"],
                                       "HOME": str(tmp_path), "GIT_TERMINAL_PROMPT": "0"})
    assert r.returncode == 0, r.stderr
    assert (dest / "CLAUDE.md").read_text() == "# v2\n"
    assert any(s["auth"].startswith("Basic ") for s in gitsrv.seen)


def test_export_calls_the_route_with_the_persons_github_token(web, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "gho_persons_own")
    res = cli("agent", "definition", "export", "--slug", "helper", "--to-github",
              "https://github.com/dimagi-internal/helper.git")
    assert res.exit_code == 0, res.output
    assert ("POST", "/api/agents/helper/definition/export",
            {"repo": "dimagi-internal/helper", "ref": "main",
             "github_token": "gho_persons_own"}) in web.calls
    assert "gho_persons_own" not in res.output


def test_export_without_a_github_token_points_at_push_myself(web, monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("PATH", "/nonexistent")  # no `gh` to ask
    res = CliRunner().invoke(main, ["agent", "definition", "export", "--slug", "helper",
                                    "--to-github", "o/helper"])
    assert res.exit_code != 0 and "--push-myself" in res.output
    assert not any(c[1].endswith("/export") for c in web.calls)


def test_export_refuses_a_bad_repo_name(web):
    res = CliRunner().invoke(main, ["agent", "definition", "export", "--slug", "helper",
                                    "--to-github", "not a repo"])
    assert res.exit_code != 0 and "OWNER/NAME" in res.output


def test_export_push_myself_pushes_history_then_detaches(gitsrv, tmp_path, monkeypatch):
    """The person's own git pushes canopy's main + tags to the (empty) GitHub repo — here a
    local bare repo standing in for github.com — with no canopy header; then detach."""
    gitsrv.publish("connect", "helper", {"CLAUDE.md": (b"# v2\n", False)}, 2)
    gh = tmp_path / "gh" / "o" / "helper.git"
    subprocess.run(["git", "init", "-q", "--bare", str(gh)], check=True)
    # Route https://github.com/ to the local dir for this test's git commands only.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{tmp_path / 'gh'}/.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/")
    res = cli("agent", "definition", "export", "--slug", "helper", "--to-github", "o/helper",
              "--push-myself")
    assert res.exit_code == 0, res.output
    log = subprocess.run(["git", "-C", str(gh), "log", "--format=%s", "main"],
                         capture_output=True, text=True).stdout.split("\n")
    assert log[:2] == ["revision 2", "revision 1"]
    tags = subprocess.run(["git", "-C", str(gh), "tag"], capture_output=True, text=True).stdout
    assert tags.split() == ["r1", "r2"]
    assert gitsrv.api == [("POST", "/api/agents/helper/definition/detach",
                           {"repo_url": "https://github.com/o/helper"})]
    # The repo now holds main: a second export is refused before anything is pushed.
    again = CliRunner().invoke(main, ["agent", "definition", "export", "--slug", "helper",
                                      "--to-github", "o/helper", "--push-myself"])
    assert again.exit_code != 0 and "not empty" in again.output
