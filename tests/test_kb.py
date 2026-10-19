import io
import json

import pytest

from orchestrator.kb import KB, KBError, build, chunk_text
from orchestrator.kb_mcp import serve
from orchestrator import kb_cli


def _bundle(tmp_path, items, grants=None, name="acme"):
    b = tmp_path / f"bundle-{len(items)}-{name}"
    (b / "tables").mkdir(parents=True)
    manifest = {"name": name, "publisher": "Acme Foundation", "license": "CC BY 4.0",
                "tables": {"grants": {"description": "Every grant.", "date_column": "Date",
                                      "amount_column": "Amount", "filter_columns": ["Recipient", "Countries"]}}}
    (b / "manifest.json").write_text(json.dumps(manifest))
    (b / "items.jsonl").write_text("\n".join(json.dumps(i) for i in items))
    rows = grants if grants is not None else [
        {"Grant": "Water pilot", "Recipient": "Dimagi Inc", "Amount": 1069324, "Date": "2025-12-03", "Countries": "Nigeria"},
        {"Grant": "Cholera RCT", "Recipient": "Johns Hopkins", "Amount": 740851.8, "Date": "2025-03-14", "Countries": "Bangladesh"},
        {"Grant": "Old grant", "Recipient": "Dimagi Inc", "Amount": 49721.62, "Date": "2023-11-14", "Countries": "Nigeria"},
    ]
    (b / "tables" / "grants.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    return b


ITEMS = [
    {"id": "p:1", "type": "podcast_episode", "title": "Tackling diarrheal disease", "url": "https://x/1", "date": "2026-07-23",
     "text": "A key way that cholera is transmitted is through contaminated water.\n\nWASHmobile follows up households.", "sha256": "a"},
    {"id": "b:1", "type": "blog_post", "title": "Safe water update", "url": "https://x/2", "date": "2025-10-29",
     "text": "Chlorination pilots in six countries.", "sha256": "b"},
]


def test_chunk_text_splits_long_text_with_overlap():
    text = "\n\n".join(f"para {i} " + "x" * 500 for i in range(10))
    chunks = chunk_text(text, size=1200, overlap=100)
    assert len(chunks) > 3
    assert all(len(c) <= 1200 + 600 for c in chunks)
    assert chunk_text("") == []
    assert chunk_text("y" * 5000, size=1000, overlap=100)[1].startswith("y")


def test_build_search_get(tmp_path):
    db = tmp_path / "kb.sqlite"
    s = build(_bundle(tmp_path, ITEMS), db)
    assert s["items"] == 2 and s["tables"] == {"grants": 3} and s["baseline"]
    kb = KB(db)
    hits = kb.search("cholera water")
    assert hits[0]["id"] == "p:1" and hits[0]["url"] == "https://x/1"
    # AND finds nothing for these two together; OR fallback still answers
    assert {h["id"] for h in kb.search("cholera chlorination")} == {"p:1", "b:1"}
    assert kb.search("chlorination", types=["podcast_episode"]) == []
    assert kb.search("water", since="2026-01-01")[0]["id"] == "p:1"
    item = kb.get("https://x/1", max_chars=20)
    assert item["id"] == "p:1" and "truncated" in item
    with pytest.raises(KBError):
        kb.get("nope")
    with pytest.raises(KBError):
        kb.search("   ")


def test_table_filters_and_totals(tmp_path):
    db = tmp_path / "kb.sqlite"
    build(_bundle(tmp_path, ITEMS), db)
    kb = KB(db)
    r = kb.table("grants", {"Recipient": "dimagi"}, since="2024-01-01")
    assert r["matched"] == 1 and r["total_amount"] == 1069324
    r = kb.table("grants", sort="-Amount", min_amount=100000)
    assert [x["Grant"] for x in r["rows"]] == ["Water pilot", "Cholera RCT"]
    with pytest.raises(KBError):
        kb.table("missing")


def test_whats_new_tracks_new_and_changed_across_builds(tmp_path):
    db = tmp_path / "kb.sqlite"
    build(_bundle(tmp_path, ITEMS), db)
    assert KB(db).whats_new()["note"].startswith("baseline")
    items2 = [dict(ITEMS[0], sha256="a2"), ITEMS[1],
              {"id": "g:9", "type": "grant_page", "title": "New grant", "url": "https://x/9", "date": "2026-10-01", "text": "t", "sha256": "c"}]
    s = build(_bundle(tmp_path, items2, name="acme2"), db, previous=db)
    assert (s["new"], s["changed"]) == (1, 1)
    changes = {c["item_id"]: c["change"] for c in KB(db).whats_new()["changes"]}
    assert changes == {"g:9": "new", "p:1": "changed"}
    # history survives a third, quiet rebuild
    build(_bundle(tmp_path, items2, name="acme3"), db, previous=db)
    assert len(KB(db).whats_new()["changes"]) == 2


def test_build_refuses_missing_table(tmp_path):
    b = _bundle(tmp_path, ITEMS)
    (b / "tables" / "grants.jsonl").unlink()
    with pytest.raises(KBError):
        build(b, tmp_path / "kb.sqlite")


def _rpc(kb_path, *msgs):
    stdin = io.StringIO("\n".join(json.dumps(m) for m in msgs) + "\n")
    stdout = io.StringIO()
    serve(lambda: (KB(kb_path), ""), stdin=stdin, stdout=stdout)
    return [json.loads(l) for l in stdout.getvalue().splitlines()]


def test_mcp_lists_table_tools_and_answers_calls(tmp_path):
    db = tmp_path / "kb.sqlite"
    build(_bundle(tmp_path, ITEMS), db)
    out = _rpc(db,
               {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
               {"jsonrpc": "2.0", "method": "notifications/initialized"},
               {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
               {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "grants", "arguments": {"Recipient": "Dimagi"}}},
               {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "search", "arguments": {"query": "cholera"}}},
               {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "get", "arguments": {}}},
               {"jsonrpc": "2.0", "id": 6, "method": "bogus"})
    assert [m["id"] for m in out] == [1, 2, 3, 4, 5, 6]
    assert out[0]["result"]["serverInfo"]["name"] == "canopy-kb-acme"
    names = {t["name"] for t in out[1]["result"]["tools"]}
    assert {"search", "get", "whats_new", "table", "grants"} <= names
    grants = json.loads(out[2]["result"]["content"][0]["text"])
    assert grants["matched"] == 2
    assert json.loads(out[3]["result"]["content"][0]["text"])[0]["url"] == "https://x/1"
    assert out[4]["result"]["isError"] is True
    assert out[5]["error"]["code"] == -32601


def test_kb_ref_kinds(tmp_path):
    f = tmp_path / "kb.sqlite"
    f.write_text("")
    assert kb_cli._kind(str(f)) == "local"
    assert kb_cli._kind("eva/P12") == "project"
    assert kb_cli._kind("dimagi/givewell-kb") == "workspace-project"
    assert kb_cli._kind("1-_WsXnvM2lTeRs3KLld2sgimQpz_otCF") == "folder"
    with pytest.raises(KBError):
        kb_cli._kind("not a ref")


def test_search_boost_reorders_by_type(tmp_path):
    items = [{"id": "d:1", "type": "pdf_document", "title": "cholera cholera", "url": "https://x/d", "text": "cholera cholera cholera", "sha256": "d"},
             {"id": "g:1", "type": "grant_page", "title": "A grant", "url": "https://x/g", "text": "a cholera trial", "sha256": "g"}]
    b = _bundle(tmp_path, items, name="boost")
    assert KB_first(b, tmp_path, None) == "d:1"
    m = json.loads((b / "manifest.json").read_text())
    m["search_boost"] = {"grant_page": 3.0, "pdf_document": 0.5}
    (b / "manifest.json").write_text(json.dumps(m))
    assert KB_first(b, tmp_path, "boosted") == "g:1"


def KB_first(bundle, tmp_path, tag):
    db = tmp_path / f"kb-{tag}.sqlite"
    build(bundle, db)
    return KB(db).search("cholera")[0]["id"]


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


def test_push_gzips_and_pull_caches(tmp_path, monkeypatch):
    db = tmp_path / "kb.sqlite"
    build(_bundle(tmp_path, ITEMS), db)
    _FakeDrive.files = {}
    monkeypatch.setattr(kb_cli, "_Drive", _FakeDrive)
    monkeypatch.setattr(kb_cli, "CACHE_ROOT", tmp_path / "cache")
    folder = "1-_WsXnvM2lTeRs3KLld2sgimQpz_otCF"
    out = kb_cli.push(folder, tmp_path, db)
    assert out["replaced"] is False
    assert _FakeDrive.files[(folder, kb_cli.KB_REMOTE)]["data"][:2] == b"\x1f\x8b"
    first = kb_cli.pull(folder, tmp_path)
    assert first["pulled"] is True
    assert KB(first["path"]).search("cholera")[0]["id"] == "p:1"
    assert kb_cli.pull(folder, tmp_path)["pulled"] is False      # unchanged in Drive
    assert kb_cli.push(folder, tmp_path, db)["replaced"] is True
    assert kb_cli.pull(folder, tmp_path)["pulled"] is True       # newer copy in Drive
    kb, warning = kb_cli.open_kb(folder, tmp_path, max_age_hours=24)
    assert warning == "" and kb.summary["items"] == 2


def test_get_grep_returns_matching_lines_only(tmp_path):
    db = tmp_path / "kb.sqlite"
    build(_bundle(tmp_path, ITEMS), db)
    d = KB(db).get("p:1", grep="WASH\\w+", context=0)
    assert d["match_count"] == 1 and "text" not in d
    assert d["matches"][0]["text"].startswith("WASHmobile")
    with pytest.raises(KBError):
        KB(db).get("p:1", grep="(")


def test_push_of_a_workspace_project_also_publishes_it_to_canopy(tmp_path, monkeypatch):
    """canopy-web then serves it as the project's own remote MCP server."""
    import hashlib

    from orchestrator import canopy_web

    db = tmp_path / "kb.sqlite"
    build(_bundle(tmp_path, ITEMS), db)
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


def test_a_drive_folder_push_publishes_nothing(tmp_path, monkeypatch):
    db = tmp_path / "kb.sqlite"
    build(_bundle(tmp_path, ITEMS), db)
    _FakeDrive.files = {}
    monkeypatch.setattr(kb_cli, "_Drive", _FakeDrive)
    monkeypatch.setattr(kb_cli, "publish_to_canopy", lambda *a: (_ for _ in ()).throw(AssertionError("published")))
    assert "mcp_url" not in kb_cli.push("1-_WsXnvM2lTeRs3KLld2sgimQpz_otCF", tmp_path, db)
