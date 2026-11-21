"""Materializing a stored agent's revision (orchestrator/agent_materialize.py).

The contract (docs/architecture/stored-agents.md): one revision → one commit sha on every
machine; HEAD == main == origin/main; origin canopy://<ws>/<slug>; the root is verified
before anything is written; untracked/ignored files survive; tracked files absent from the
revision are deleted. canopy-web is faked at the HTTP layer.
"""
from __future__ import annotations

import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from orchestrator import agent_materialize as am
from orchestrator.agent_definition import file_entry, manifest_root

PUBLISHED = "2026-11-21T10:00:00Z"

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

#: Pinned: REV1 at revision 1 published PUBLISHED. If this moves, every machine's commit for
#: an already-published revision moves with it — which `refresh_plugin` reads as an update.
REV1_COMMIT = "2809c7641f59984cec51dc1f79fdfbce8c449a93"


def make_tar(files: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path, (content, executable) in sorted(files.items()):
            info = tarfile.TarInfo(path)
            info.size = len(content)
            info.mode = 0o755 if executable else 0o644
            info.mtime = 0
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def root_of(files: dict) -> str:
    return manifest_root([file_entry(p, c, x) for p, (c, x) in files.items()])


class FakeWeb:
    """GET /definition/archive (and /definition, /api/agents/<slug>/)."""

    def __init__(self, files: dict, revision: int = 1, *, root: str = "", workspace="connect"):
        self.set(files, revision, root=root)
        self.workspace = workspace
        self.archive_calls = 0

    def set(self, files, revision, *, root=""):
        self.files, self.revision = files, revision
        self.root = root or root_of(files)

    def fetch(self, url, headers):
        assert headers["Authorization"] == "Bearer tok"
        assert "/api/agents/helper/definition/archive?revision=" in url
        self.archive_calls += 1
        return 200, {"X-Canopy-Revision": str(self.revision), "X-Canopy-Root-Sha256": self.root,
                     "X-Canopy-Published-At": PUBLISHED,
                     "X-Canopy-Workspace": self.workspace}, make_tar(self.files)


@pytest.fixture(autouse=True)
def _no_live_root(monkeypatch):
    # By default canopy-web's /definition is unreadable (a runner token): fetch every time.
    monkeypatch.setattr(am, "live_root", lambda slug, **kw: {})


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def run(dest: Path, web: FakeWeb, **kw) -> dict:
    return am.materialize("helper", dest, base_url="https://web.test", token="tok",
                          fetch=web.fetch, **kw)


def test_same_revision_same_sha_on_two_machines(tmp_path):
    a = run(tmp_path / "a" / "helper", FakeWeb(REV1))
    b = run(tmp_path / "b" / "helper", FakeWeb(REV1))
    assert a["commit"] == b["commit"]
    assert a == {"slug": "helper", "revision": 1, "root_sha256": root_of(REV1),
                 "commit": a["commit"], "changed": True}


def test_commit_shape_is_pinned(tmp_path):
    out = run(tmp_path / "helper", FakeWeb(REV1))
    repo = tmp_path / "helper"
    raw = git(repo, "cat-file", "-p", out["commit"])
    assert "parent" not in raw
    assert "author canopy <agents@canopy.dimagi.com> 1795255200 +0000" in raw
    assert "committer canopy <agents@canopy.dimagi.com> 1795255200 +0000" in raw
    assert raw.endswith("canopy revision 1")
    modes = git(repo, "ls-tree", "-r", out["commit"])
    assert "100755 blob" in [ln for ln in modes.splitlines() if "bin/helper-email" in ln][0]
    assert all(ln.startswith(("100644", "100755")) for ln in modes.splitlines())
    assert out["commit"] == REV1_COMMIT


def test_refs_and_origin(tmp_path):
    out = run(tmp_path / "helper", FakeWeb(REV1))
    repo = tmp_path / "helper"
    assert git(repo, "rev-parse", "HEAD") == out["commit"]
    assert git(repo, "rev-parse", "refs/heads/main") == out["commit"]
    assert git(repo, "rev-parse", "refs/remotes/origin/main") == out["commit"]
    assert git(repo, "symbolic-ref", "HEAD") == "refs/heads/main"
    assert git(repo, "rev-parse", "--abbrev-ref", "origin/HEAD") == "origin/main"
    assert git(repo, "remote", "get-url", "origin") == "canopy://connect/helper"
    assert am.is_stored_clone(repo)
    assert (repo / "bin" / "helper-email").stat().st_mode & 0o111


def test_off_release_branch_and_refresh_see_head_on_origin_main(tmp_path):
    import importlib.util
    hook = Path(__file__).resolve().parents[1] / "plugins/canopy/hooks/fleet_session_start_update.py"
    spec = importlib.util.spec_from_file_location("fssu_mat", hook)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    run(tmp_path / "helper", FakeWeb(REV1))
    assert mod.off_release_branch(str(tmp_path / "helper")) == ""


def test_untracked_and_ignored_survive_and_removed_files_go(tmp_path):
    repo = tmp_path / "helper"
    web = FakeWeb(REV1)
    run(repo, web)
    (repo / ".env").write_text("SECRET=1\n")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "x.js").write_text("x")
    (repo / "scratch.txt").write_text("mine")
    web.set(REV2, 2)
    out = run(repo, web)
    assert out["changed"] is True and out["revision"] == 2
    assert not (repo / "skills").exists()                     # tracked, gone from rev 2
    assert (repo / "persona.md").read_text() == "voice\n"
    assert (repo / "CLAUDE.md").read_text() == "# helper v2\n"
    assert (repo / ".env").read_text() == "SECRET=1\n"
    assert (repo / "node_modules" / "x.js").exists()
    assert (repo / "scratch.txt").read_text() == "mine"
    assert git(repo, "rev-parse", "refs/remotes/origin/main") == out["commit"]
    assert git(repo, "rev-list", "--count", "HEAD") == "1"     # orphan, every time


def test_noop_when_already_there(tmp_path):
    repo = tmp_path / "helper"
    web = FakeWeb(REV1)
    first = run(repo, web)
    again = run(repo, web)
    assert again == {**first, "changed": False}


def test_noop_without_download_when_live_root_matches(tmp_path, monkeypatch):
    repo = tmp_path / "helper"
    web = FakeWeb(REV1)
    run(repo, web)
    monkeypatch.setattr(am, "live_root", lambda slug, **kw: {"number": 1, "root_sha256": web.root})
    out = run(repo, web)
    assert out["changed"] is False and web.archive_calls == 1


def test_edited_tracked_file_is_restored(tmp_path):
    repo = tmp_path / "helper"
    web = FakeWeb(REV1)
    run(repo, web)
    (repo / "CLAUDE.md").write_text("hand edit")
    out = run(repo, web)
    assert out["changed"] is True
    assert (repo / "CLAUDE.md").read_text() == "# helper\n"


def test_bad_root_writes_nothing(tmp_path):
    repo = tmp_path / "helper"
    web = FakeWeb(REV1)
    first = run(repo, web)
    web.set(REV2, 2, root="0" * 64)
    with pytest.raises(am.MaterializeError, match="do not match its root"):
        run(repo, web)
    assert (repo / "CLAUDE.md").read_text() == "# helper\n"
    assert git(repo, "rev-parse", "HEAD") == first["commit"]
    # A fresh dest: nothing at all, no staging dir left behind.
    with pytest.raises(am.MaterializeError):
        run(tmp_path / "fresh" / "helper", web)
    assert not (tmp_path / "fresh" / "helper").exists()
    assert not (tmp_path / "fresh").exists() or list((tmp_path / "fresh").iterdir()) == []


def test_http_error_writes_nothing(tmp_path):
    def refuse(url, headers):
        return 403, {}, b'{"detail":"no"}'
    with pytest.raises(am.MaterializeError, match="403"):
        am.materialize("helper", tmp_path / "helper", base_url="https://w", token="tok",
                       fetch=refuse)
    assert not (tmp_path / "helper").exists()


@pytest.mark.parametrize("name", ["../escape", "/abs", ".git/config", "a/../../b"])
def test_tar_paths_that_escape_are_refused(name):
    with pytest.raises(am.MaterializeError):
        am.read_tar(make_tar({name: (b"x", False)}))


def test_tar_symlink_refused():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tf.addfile(info)
    with pytest.raises(am.MaterializeError, match="not a regular file"):
        am.read_tar(buf.getvalue())


def test_requested_revision_must_match(tmp_path):
    web = FakeWeb(REV1, 3)
    with pytest.raises(am.MaterializeError, match="asked for revision 2"):
        run(tmp_path / "helper", web, revision="2")


def test_existing_github_clone_is_converted_in_place(tmp_path):
    repo = tmp_path / "helper"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "old.txt").write_text("tracked before")
    (repo / ".env").write_text("keep")
    subprocess.run(["git", "-C", str(repo), "add", "old.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=x", "-c", "user.email=x@x",
                    "commit", "-qm", "old"], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    "https://github.com/o/helper"], check=True)
    out = run(repo, FakeWeb(REV1))
    assert not (repo / "old.txt").exists()
    assert (repo / ".env").read_text() == "keep"
    assert git(repo, "remote", "get-url", "origin") == "canopy://connect/helper"
    assert out["changed"] is True


def test_git_date_forms():
    assert am.git_date("2026-11-21T10:00:00Z") == "1795255200 +0000"
    assert am.git_date("2026-11-21T10:00:00.987654+00:00") == "1795255200 +0000"
    assert am.git_date("2026-11-21T11:00:00+01:00") == "1795255200 +0000"
    assert am.git_date("Fri, 21 Nov 2026 10:00:00 GMT") == "1795255200 +0000"
    with pytest.raises(am.MaterializeError):
        am.git_date("yesterday")


# ── the CLI ──────────────────────────────────────────────────────────────────────────────

def _cli(monkeypatch, web):
    real = am.materialize

    def fake(slug, dest, **kw):
        return real(slug, dest, base_url="https://web.test", token="tok", fetch=web.fetch,
                    **{k: v for k, v in kw.items() if k in ("revision",)})
    monkeypatch.setattr(am, "materialize", fake)


def test_cli_materialize_prints_one_json_line(tmp_path, monkeypatch):
    from orchestrator.cli import main
    _cli(monkeypatch, FakeWeb(REV1))
    res = CliRunner().invoke(main, ["agent", "materialize", "--slug", "helper", "--dest",
                                    str(tmp_path / "helper"), "--runner"])
    assert res.exit_code == 0, res.output
    lines = res.output.strip().splitlines()
    assert len(lines) == 1
    body = json.loads(lines[0])
    assert set(body) == {"slug", "revision", "root_sha256", "commit", "changed"}


def test_cli_sync_runner_uses_agent_root(tmp_path, monkeypatch):
    from orchestrator.cli import main
    web = FakeWeb(REV1)
    _cli(monkeypatch, web)
    monkeypatch.setenv("AGENT_ROOT", str(tmp_path / "agents"))
    r1 = CliRunner().invoke(main, ["agent", "sync", "--runner", "--slug", "helper"])
    assert r1.exit_code == 0, r1.output
    assert json.loads(r1.output)["changed"] is True
    assert (tmp_path / "agents" / "helper" / "CLAUDE.md").exists()
    r2 = CliRunner().invoke(main, ["agent", "sync", "--runner", "--slug", "helper"])
    assert json.loads(r2.output)["changed"] is False


def test_cli_sync_refuses_a_github_clone(tmp_path, monkeypatch):
    from orchestrator.cli import main
    repo = tmp_path / "helper"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/o/r"],
                   check=True)
    _cli(monkeypatch, FakeWeb(REV1))
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
