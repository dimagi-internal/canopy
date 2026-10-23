"""`canopy kb workdir save|restore`: a curator's working cache kept on canopy-web, so a
refresh can run on a machine that has never seen the corpus."""
from __future__ import annotations

import gzip
import hashlib
import io
import json

import pytest

from orchestrator import canopy_web, kb_cli, kb_workdir
from orchestrator.kb_engine import KBError

REF = "connect/givewell-kb"
BASE = "/api/workspace-projects/connect/givewell-kb/kb/workdir"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class Server:
    """canopy-web's working-cache routes and the bucket behind them, in memory."""

    def __init__(self, originals=()):
        self.originals = {_sha(b): b for b in originals}
        self.workdir: dict[str, bytes] = {}  # sha256 of the file -> its gzip
        self.pointer: dict = {}
        self.puts = 0

    def call(self, method, path, body=None, **kw):
        if (method, path) == ("GET", BASE):
            return dict(self.pointer)
        if (method, path) == ("POST", f"{BASE}/have"):
            return {"originals": [s for s in body["shas"] if s in self.originals],
                    "workdir": [s for s in body["shas"] if s in self.workdir and s not in self.originals]}
        if (method, path) == ("POST", f"{BASE}/blobs"):
            return {"have": 0, "expires_in": 60, "uploads": [
                {"sha256": b["sha256"], "url": f"put://{b['sha256']}", "headers": {}}
                for b in body["blobs"] if b["sha256"] not in self.workdir]}
        if (method, path) == ("POST", f"{BASE}/links"):
            return {"expires_in": 60, "links": [
                {"sha256": f["sha256"], "store": f["store"], "url": f"get://{f['store']}/{f['sha256']}"}
                for f in body["files"]]}
        if (method, path) == ("PUT", BASE):
            cur = self.pointer.get("manifest")
            if cur and cur != body["manifest"] and body.get("base") != cur:
                raise canopy_web.CanopyError(f"PUT {BASE} -> 409 saved since")
            self.pointer = {"manifest": body["manifest"], "files": body["files"], "bytes": body["bytes"],
                            "version": 3, "saved_by": "eva"}
            return dict(self.pointer)
        raise AssertionError((method, path))

    def put(self, url, path, headers):
        self.puts += 1
        self.workdir[url.removeprefix("put://")] = path.read_bytes()

    def urlopen(self, url, timeout=0):
        store, sha = url.removeprefix("get://").split("/")
        if store == "originals":
            if sha not in self.originals:
                raise OSError("403")
            return io.BytesIO(self.originals[sha])
        return io.BytesIO(self.workdir[sha])


@pytest.fixture()
def server(monkeypatch):
    s = Server(originals=[b"%PDF grant report"])
    monkeypatch.setattr(canopy_web, "call", s.call)
    monkeypatch.setattr(kb_cli, "_put", s.put)
    monkeypatch.setattr("urllib.request.urlopen", s.urlopen)
    return s


def _cache(root, files):
    for rel, body in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(body)
    return root


FILES = {"raw/http/ab": b"%PDF grant report", "raw/http.jsonl": b'{"url": "u", "etag": "x"}\n',
         "ocr.json": b'{"ab": "scanned text"}', "cards.json": b'{"p:1": {"summary": "s"}}',
         "kb.sqlite": b"derived", "bundle/items.jsonl": b"derived"}


def test_a_fresh_machine_restores_what_another_saved_and_the_originals_are_not_stored_twice(server, tmp_path):
    a = _cache(tmp_path / "a", FILES)
    saved = kb_workdir.save(REF, a, exclude=("kb.sqlite", "bundle/"))
    assert saved["files"] == 4 and saved["held_by_originals"] == 1
    assert _sha(FILES["raw/http/ab"]) not in server.workdir  # the PDF is an original already
    assert gzip.decompress(server.workdir[_sha(FILES["ocr.json"])]) == FILES["ocr.json"]

    b = tmp_path / "b"  # never saw the corpus
    got = kb_workdir.restore(REF, b)
    assert got["restored"] and got["fetched"] == 4 and got["unavailable_count"] == 0
    for rel in ("raw/http/ab", "raw/http.jsonl", "ocr.json", "cards.json"):
        assert (b / rel).read_bytes() == FILES[rel]
    assert not (b / "kb.sqlite").exists() and not (b / "bundle").exists()

    # nothing changed: a second save uploads only nothing new, and a restore fetches nothing
    puts = server.puts
    assert kb_workdir.save(REF, b)["uploaded"] == 0 and server.puts == puts
    assert kb_workdir.restore(REF, b)["fetched"] == 0


def test_a_save_after_a_refresh_sends_only_what_changed(server, tmp_path):
    a = _cache(tmp_path / "a", FILES)
    kb_workdir.save(REF, a, exclude=("kb.sqlite", "bundle/"))
    (a / "cards.json").write_bytes(b'{"p:1": {"summary": "s"}, "p:2": {"summary": "new"}}')
    again = kb_workdir.save(REF, a, exclude=("kb.sqlite", "bundle/"))
    assert again["uploaded"] == 2  # the changed file and the new manifest


def test_restore_keeps_this_machines_own_changes_and_replaces_stale_files(server, tmp_path):
    a = _cache(tmp_path / "a", FILES)
    kb_workdir.save(REF, a, exclude=("kb.sqlite", "bundle/"))
    b = tmp_path / "b"
    kb_workdir.restore(REF, b)
    (a / "ocr.json").write_bytes(b'{"ab": "better text"}')  # machine a refreshes and saves
    kb_workdir.save(REF, a, exclude=("kb.sqlite", "bundle/"))
    (b / "cards.json").write_bytes(b'{"edited": "on b"}')  # b has unsaved work of its own

    got = kb_workdir.restore(REF, b)
    assert (b / "ocr.json").read_bytes() == b'{"ab": "better text"}'  # b had not touched it
    assert (b / "cards.json").read_bytes() == b'{"edited": "on b"}' and got["kept_local"] == ["cards.json"]


def test_a_save_from_a_stale_machine_is_a_conflict_not_an_overwrite(server, tmp_path):
    a = _cache(tmp_path / "a", FILES)
    b = _cache(tmp_path / "b", {"cards.json": b"{}"})
    kb_workdir.save(REF, a)
    with pytest.raises(KBError, match="restore"):
        kb_workdir.save(REF, b)  # b never restored a's cache


def test_a_file_that_cannot_be_restored_is_left_for_the_refresh_to_fetch(server, tmp_path):
    a = _cache(tmp_path / "a", FILES)
    kb_workdir.save(REF, a, exclude=("kb.sqlite", "bundle/"))
    server.originals.clear()  # e.g. an original not uploaded yet
    got = kb_workdir.restore(REF, tmp_path / "b")
    assert got["unavailable"] == ["raw/http/ab"] and not (tmp_path / "b" / "raw/http/ab").exists()
    assert (tmp_path / "b" / "ocr.json").exists()


def test_nothing_saved_yet_is_not_an_error(server, tmp_path):
    assert kb_workdir.restore(REF, tmp_path / "x") == {
        "restored": False, "reason": "no working cache saved yet: this refresh starts from what is here"}
    with pytest.raises(KBError, match="workspace project"):
        kb_workdir.restore("eva/P12", tmp_path / "x")


def test_the_cli_wires_both_commands(server, tmp_path):
    from click.testing import CliRunner

    a = _cache(tmp_path / "a", {"cards.json": b"{}"})
    r = CliRunner().invoke(kb_cli.kb_group, ["workdir", "save", "--kb", REF, str(a), "--exclude", "kb.sqlite"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["saved"] is True
    r = CliRunner().invoke(kb_cli.kb_group, ["workdir", "restore", "--kb", REF, str(tmp_path / "b")])
    assert r.exit_code == 0 and json.loads(r.output)["fetched"] == 1


def test_push_dry_run_only_asks_for_a_plan(monkeypatch, tmp_path):
    from click.testing import CliRunner

    calls = []
    monkeypatch.setattr(kb_cli, "_records", lambda: type("R", (), {"state": staticmethod(lambda b: {"docs": {}})}))
    monkeypatch.setattr(canopy_web, "call", lambda m, p, b=None, **k: calls.append((m, p)) or {"unchanged": True, "base": 1})
    db = tmp_path / "kb.sqlite"
    db.write_bytes(b"x")
    monkeypatch.setattr(kb_cli, "summary", lambda p: {})
    r = CliRunner().invoke(kb_cli.kb_group, ["push", "--kb", REF, str(db), "--bundle", str(tmp_path), "--dry-run"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["unchanged"] is True
    assert calls == [("POST", "/api/workspace-projects/connect/givewell-kb/kb/plan")]
