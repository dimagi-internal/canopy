"""canopy's `kb` commands: resolving refs, the Drive copy, publishing to canopy-web, and the
stdio MCP server. The engine itself (canopy-web's `canopy_kb`) is tested where it lives;
here a stand-in engine proves canopy only routes to it, and one test runs the real engine
when it is installed."""
import io
import json
import sqlite3
import sys
import types

import pytest

from orchestrator import kb_cli, kb_engine
from orchestrator.kb_engine import KBError
from orchestrator.kb_mcp import serve


def _kb_file(path, items=2, name="acme"):
    """A minimal built knowledge base: what push/pull check without the engine."""
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    db.execute("INSERT INTO meta VALUES ('summary', ?)", (json.dumps({"schema": 3, "name": name, "items": items}),))
    db.commit()
    db.close()
    return path


class _EngineError(Exception):
    pass


_EngineError.__name__ = "KBError"


@pytest.fixture()
def fake_engine(monkeypatch):
    """A `canopy_kb` stand-in recording what canopy asks of it."""
    calls = []

    class FakeKB:
        def __init__(self, path):
            calls.append(("open", str(path)))
            self.manifest = {"name": "acme"}

    def run(kb, name, args, semantic=None, rerank=None):
        calls.append(("run", name, args))
        if name == "kb_info":
            return "acme — 2 documents"
        if name == "grants":
            return {"matched": 1}
        raise _EngineError(f"unknown tool {name!r}")

    mod = types.ModuleType("canopy_kb")
    mod.KB = FakeKB
    mod.build = lambda bundle, out, previous=None: calls.append(("build", str(bundle), str(out), previous)) or {"items": 2}
    mod.tools = types.ModuleType("canopy_kb.tools")
    mod.tools.tool_specs = lambda kb: [{"name": "kb_info"}, {"name": "grants"}]
    mod.tools.run = run
    monkeypatch.setitem(sys.modules, "canopy_kb", mod)
    monkeypatch.setitem(sys.modules, "canopy_kb.tools", mod.tools)
    return calls


def test_build_and_open_go_to_the_engine(fake_engine, tmp_path):
    assert kb_engine.build(tmp_path / "bundle", tmp_path / "kb.sqlite") == {"items": 2}
    kb, warning = kb_cli.open_kb(str(_kb_file(tmp_path / "kb.sqlite")), tmp_path)
    assert warning == "" and kb.manifest["name"] == "acme"
    assert [c[0] for c in fake_engine] == ["build", "open"]


def test_without_the_engine_build_says_how_to_install_it(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "canopy_kb", None)  # import canopy_kb -> ImportError
    with pytest.raises(KBError, match="packages/canopy_kb"):
        kb_engine.build(tmp_path, tmp_path / "kb.sqlite")


def test_an_engine_error_is_reported_as_canopys(monkeypatch, tmp_path):
    mod = types.ModuleType("canopy_kb")

    def boom(*a):
        raise _EngineError("manifest declares table 'grants' but it is missing")
    mod.build = boom
    monkeypatch.setitem(sys.modules, "canopy_kb", mod)
    with pytest.raises(KBError, match="grants"):
        kb_engine.build(tmp_path, tmp_path / "kb.sqlite")


def test_summary_reads_a_build_without_the_engine_and_refuses_anything_else(tmp_path):
    assert kb_engine.summary(_kb_file(tmp_path / "kb.sqlite"))["items"] == 2
    (tmp_path / "junk.sqlite").write_text("not sqlite at all " * 100)
    with pytest.raises(KBError):
        kb_engine.summary(tmp_path / "junk.sqlite")
    sqlite3.connect(tmp_path / "empty.sqlite").execute("CREATE TABLE t (x)")
    with pytest.raises(KBError):
        kb_engine.summary(tmp_path / "empty.sqlite")


def _rpc(*msgs, warning=""):
    stdin = io.StringIO("\n".join(json.dumps(m) for m in msgs) + "\n")
    stdout = io.StringIO()
    serve(lambda: (sys.modules["canopy_kb"].KB("x"), warning), stdin=stdin, stdout=stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines()]


def test_mcp_serves_the_engines_tools_text_as_text(fake_engine):
    out = _rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
               {"jsonrpc": "2.0", "method": "notifications/initialized"},
               {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
               {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kb_info", "arguments": {}}},
               {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "grants", "arguments": {"Recipient": "D"}}},
               {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "drop", "arguments": {}}},
               {"jsonrpc": "2.0", "id": 6, "method": "bogus"})
    assert [m["id"] for m in out] == [1, 2, 3, 4, 5, 6]
    assert out[0]["result"]["serverInfo"]["name"] == "canopy-kb-acme"
    assert [t["name"] for t in out[1]["result"]["tools"]] == ["kb_info", "grants"]
    assert out[2]["result"]["content"][0]["text"] == "acme — 2 documents"
    assert json.loads(out[3]["result"]["content"][0]["text"]) == {"matched": 1}
    assert out[4]["result"]["isError"] is True and "unknown tool" in out[4]["result"]["content"][0]["text"]
    assert out[5]["error"]["code"] == -32601


def test_mcp_says_when_it_serves_a_stale_cache(fake_engine):
    out = _rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "kb_info", "arguments": {}}},
               warning="could not refresh from Drive")
    assert out[0]["result"]["content"][0]["text"].startswith("warning: could not refresh from Drive")


def test_kb_ref_kinds(tmp_path):
    f = tmp_path / "kb.sqlite"
    f.write_text("")
    assert kb_cli._kind(str(f)) == "local"
    assert kb_cli._kind("eva/P12") == "project"
    assert kb_cli._kind("dimagi/givewell-kb") == "workspace-project"
    assert kb_cli._kind("1-_WsXnvM2lTeRs3KLld2sgimQpz_otCF") == "folder"
    with pytest.raises(KBError):
        kb_cli._kind("not a ref")


class _FakeDrive:
    """gog drive ls/download/upload against a dict, so push → pull round-trips offline."""
    files: dict = {}

    def __init__(self, repo, runner=None):
        pass

    def find(self, folder, name):
        f = self.files.get((folder, name))
        return {"id": f"{folder}/{name}", "modifiedTime": f["mod"]} if f else None

    def gog(self, *args):
        if args[1] == "upload":
            src = args[2]
            if args[3] == "--replace":
                folder, name = args[4].split("/", 1)
            else:
                name, folder = args[4], args[6]
            prev = self.files.get((folder, name), {"mod": 0})
            self.files[(folder, name)] = {"data": open(src, "rb").read(), "mod": str(int(prev["mod"]) + 1)}
        elif args[1] == "download":
            folder, name = args[2].split("/", 1)
            open(args[4], "wb").write(self.files[(folder, name)]["data"])


def test_push_gzips_and_pull_caches_without_the_engine(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "canopy_kb", None)
    db = _kb_file(tmp_path / "kb.sqlite")
    _FakeDrive.files = {}
    monkeypatch.setattr(kb_cli, "_Drive", _FakeDrive)
    monkeypatch.setattr(kb_cli, "CACHE_ROOT", tmp_path / "cache")
    folder = "1-_WsXnvM2lTeRs3KLld2sgimQpz_otCF"
    out = kb_cli.push(folder, tmp_path, db)
    assert out["replaced"] is False and out["summary"]["items"] == 2
    assert _FakeDrive.files[(folder, kb_cli.KB_REMOTE)]["data"][:2] == b"\x1f\x8b"
    first = kb_cli.pull(folder, tmp_path)
    assert first["pulled"] is True and kb_engine.summary(first["path"])["name"] == "acme"
    assert kb_cli.pull(folder, tmp_path)["pulled"] is False      # unchanged in Drive
    assert kb_cli.push(folder, tmp_path, db)["replaced"] is True
    assert kb_cli.pull(folder, tmp_path)["pulled"] is True       # newer copy in Drive


def test_push_refuses_a_file_that_is_not_a_knowledge_base(tmp_path, monkeypatch):
    monkeypatch.setattr(kb_cli, "_Drive", _FakeDrive)
    (tmp_path / "x.sqlite").write_text("nope")
    with pytest.raises(KBError):
        kb_cli.push("1-_WsXnvM2lTeRs3KLld2sgimQpz_otCF", tmp_path, tmp_path / "x.sqlite")


def test_push_of_a_workspace_project_also_publishes_it_to_canopy(tmp_path, monkeypatch):
    """canopy-web then serves it as the project's own remote MCP server."""
    import hashlib

    from orchestrator import canopy_web

    db = _kb_file(tmp_path / "kb.sqlite")
    _FakeDrive.files = {}
    monkeypatch.setattr(kb_cli, "_Drive", _FakeDrive)
    calls, stored = [], {}

    def fake_call(method, path, body=None, **kw):
        calls.append((method, path, body))
        if path == "/api/workspace-projects/connect/givewell-kb/":
            return {"drive_folder_id": "1-_WsXnvM2lTeRs3KLld2sgimQpz_otCF"}
        if path.endswith("/kb/upload"):
            return {"key": "workspace-projects/connect/givewell-kb/kb/a.sqlite.gz", "url": "https://s3/put",
                    "headers": {"Content-Type": "application/gzip"}}
        if path.endswith("/kb/publish"):
            return {"mcp_path": "/api/mcp/projects/connect/givewell-kb/"}
        raise AssertionError(path)

    monkeypatch.setattr(canopy_web, "call", fake_call)
    monkeypatch.setattr(kb_cli, "_put", lambda url, path, headers: stored.update(data=path.read_bytes()))

    out = kb_cli.push("connect/givewell-kb", tmp_path, db)

    publish = calls[-1]
    assert publish[1].endswith("/kb/publish")
    assert publish[2]["sha256"] == hashlib.sha256(stored["data"]).hexdigest()
    assert stored["data"][:2] == b"\x1f\x8b"
    assert out["mcp_url"].endswith("/api/mcp/projects/connect/givewell-kb/")


def _originals_bundle(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "a").write_bytes(b"%PDF-1.4 report")
    (raw / "b").write_bytes(b"PK workbook")
    b = tmp_path / "bundle"
    b.mkdir()
    items = [{"id": "pdf:1", "original": {"sha256": "a" * 64, "content_type": "application/pdf", "path": str(raw / "a")}},
             {"id": "pdf:1-copy", "original": {"sha256": "a" * 64, "content_type": "application/pdf", "path": str(raw / "a")}},
             {"id": "sheet:1", "original": {"sha256": "b" * 64, "content_type": "application/vnd.ms-excel",
                                            "path": str(raw / "b")}},
             {"id": "blog:1", "text": "no original"}]
    (b / "items.jsonl").write_text("\n".join(json.dumps(i) for i in items))
    return b


def test_push_uploads_only_the_originals_canopy_lacks_before_publishing(tmp_path, monkeypatch):
    from orchestrator import canopy_web

    db = _kb_file(tmp_path / "kb.sqlite")
    _FakeDrive.files = {}
    monkeypatch.setattr(kb_cli, "_Drive", _FakeDrive)
    calls, puts = [], []

    def fake_call(method, path, body=None, **kw):
        calls.append(path)
        if path == "/api/workspace-projects/connect/givewell-kb/":
            return {"drive_folder_id": "1-_WsXnvM2lTeRs3KLld2sgimQpz_otCF"}
        if path.endswith("/kb/originals"):
            assert {f["sha256"] for f in body["files"]} == {"a" * 64, "b" * 64}  # once each
            return {"have": 1, "uploads": [{"sha256": "b" * 64, "url": "https://s3/b",
                                            "headers": {"x-amz-checksum-sha256": "x"}}]}
        if path.endswith("/kb/upload"):
            return {"key": "k", "url": "https://s3/put", "headers": {}}
        if path.endswith("/kb/publish"):
            return {"mcp_path": "/api/mcp/projects/connect/givewell-kb/"}
        raise AssertionError(path)

    monkeypatch.setattr(canopy_web, "call", fake_call)
    monkeypatch.setattr(kb_cli, "_put", lambda url, path, headers: puts.append((url, path.read_bytes(), headers)))

    out = kb_cli.push("connect/givewell-kb", tmp_path, db, originals=_originals_bundle(tmp_path))

    assert puts[0] == ("https://s3/b", b"PK workbook", {"x-amz-checksum-sha256": "x"})
    assert out["originals"] == {"files": 2, "already_stored": 1, "uploaded": 1, "bytes_uploaded": 11}
    assert calls.index("/api/workspace-projects/connect/givewell-kb/kb/originals") < calls.index(
        "/api/workspace-projects/connect/givewell-kb/kb/publish")


def test_push_refuses_originals_that_are_not_on_disk_or_have_nowhere_to_go(tmp_path, monkeypatch):
    db = _kb_file(tmp_path / "kb.sqlite")
    b = _originals_bundle(tmp_path)
    with pytest.raises(KBError, match="workspace project"):
        kb_cli.push("1-_WsXnvM2lTeRs3KLld2sgimQpz_otCF", tmp_path, db, originals=b)
    (tmp_path / "raw" / "b").unlink()
    with pytest.raises(KBError, match="1 originals named"):
        kb_cli.collect_originals(b)


def test_a_drive_folder_push_publishes_nothing(tmp_path, monkeypatch):
    db = _kb_file(tmp_path / "kb.sqlite")
    _FakeDrive.files = {}
    monkeypatch.setattr(kb_cli, "_Drive", _FakeDrive)
    monkeypatch.setattr(kb_cli, "publish_to_canopy", lambda *a: (_ for _ in ()).throw(AssertionError("published")))
    assert "mcp_url" not in kb_cli.push("1-_WsXnvM2lTeRs3KLld2sgimQpz_otCF", tmp_path, db)


def test_the_real_engine_builds_and_serves_through_canopy(tmp_path):
    pytest.importorskip("canopy_kb", reason="canopy_kb (canopy-web packages/canopy_kb) not installed")
    b = tmp_path / "bundle"
    b.mkdir()
    (b / "manifest.json").write_text(json.dumps({"name": "acme", "publisher": "Acme"}))
    (b / "items.jsonl").write_text(json.dumps({
        "id": "p:1", "type": "podcast_episode", "title": "Cholera", "url": "https://x/1", "date": "2026-07-23",
        "text": "# Cholera\n\nCholera spreads through contaminated water.", "sha256": "a",
        "card": {"summary": "A podcast on cholera.", "orgs": ["Acme"]}}))
    s = kb_engine.build(b, tmp_path / "kb.sqlite")
    assert s["items"] == 1 and kb_engine.summary(tmp_path / "kb.sqlite")["schema"] >= 3
    out = _rpc_real(tmp_path / "kb.sqlite",
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "search", "arguments": {"query": "cholera water"}}})
    assert out[0]["result"]["content"][0]["text"].startswith("[1] p:1")


def _rpc_real(path, *msgs):
    stdin = io.StringIO("\n".join(json.dumps(m) for m in msgs) + "\n")
    stdout = io.StringIO()
    serve(lambda: (kb_engine.open_file(path), ""), stdin=stdin, stdout=stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines()]
