"""`canopy kb drive`: the inventory reads metadata only; sync reads only what the owner
approved, holds spreadsheets with personal-data headers, and publishes the rest."""
import json

import pytest

from orchestrator import kb_drive

ROOT = "root-1"
TEAM = [{"type": "user", "emailAddress": "hal@dimagi-ai.com"}, {"type": "user", "emailAddress": "JJ@dimagi.com"}]


class FakeGog:
    """A Drive tree in a dict. Downloads write each file's fixture bytes and are counted."""

    def __init__(self, tree, bodies=None, changes=None):
        self.tree, self.bodies, self.changes = tree, bodies or {}, changes
        self.downloads, self.listed = [], []

    def root(self, folder):
        return self.tree[folder]

    def children(self, folder):
        self.listed.append(folder)
        return [f for f in self.tree.values() if folder in (f.get("parents") or [])]

    def start_token(self):
        return "t2"

    def changes_since(self, token):
        return (self.changes if self.changes is not None else [{"fileId": "x"}]), "t3"

    def download(self, file_id, out, fmt=""):
        self.downloads.append((file_id, fmt))
        out.write_bytes(self.bodies[file_id])


def _tree():
    folder = "application/vnd.google-apps.folder"
    return {
        ROOT: {"id": ROOT, "name": "KMC (synthetic)", "mimeType": folder, "permissions": TEAM},
        "doc": {"id": "doc", "name": "Protocol", "mimeType": "application/vnd.google-apps.document",
                "parents": [ROOT], "version": "4", "owners": [{"emailAddress": "hal@dimagi-ai.com"}],
                "webViewLink": "https://docs.google.com/document/d/doc", "permissions": TEAM},
        "sheet": {"id": "sheet", "name": "Patient list", "mimeType": "text/csv", "parents": [ROOT], "md5Checksum": "m",
                  "permissions": TEAM},
        "trash": {"id": "trash", "name": "old", "mimeType": "text/plain", "parents": [ROOT], "trashed": True},
    }


def _calls(monkeypatch, sources, readable=(), inventory=None):
    from orchestrator import canopy_web
    calls = []

    def fake(method, path, body=None, **kw):
        calls.append((method, path.split("/kb/", 1)[1], body))
        if path.endswith("/kb/sources"):
            return sources
        if path.endswith("/inventory"):
            (inventory if inventory is not None else []).append(body)
            return {"files": 2}
        if path.endswith("files?readable=true"):
            return list(readable)
        if path.endswith("/hold"):
            return {}
        raise AssertionError(path)
    monkeypatch.setattr(canopy_web, "call", fake)
    return calls


SOURCE = {"id": 7, "kind": "drive_folders", "name": "kmc", "config": {"folders": [ROOT]}, "page_token": ""}


def test_the_inventory_posts_metadata_with_who_can_open_each_file(monkeypatch):
    posted = []
    _calls(monkeypatch, [SOURCE], inventory=posted)
    gog = FakeGog(_tree())
    out = kb_drive.inventory("connect/kmc-kb", None, gog=gog)
    assert out == {"kmc": {"files": 2}} and gog.downloads == []  # nothing is opened
    body = posted[0]
    assert body["page_token"] == "t2"
    files = {f["id"]: f for f in body["files"]}
    assert set(files) == {ROOT, "doc", "sheet"}  # trashed files are not listed
    assert files["doc"]["readers"] == ["user:hal@dimagi-ai.com", "user:jj@dimagi.com"]
    assert (files["doc"]["parent"], files["doc"]["version"], files["sheet"]["md5"]) == (ROOT, "4", "m")


def test_a_quiet_changes_feed_skips_the_walk(monkeypatch):
    _calls(monkeypatch, [dict(SOURCE, page_token="t1")])
    gog = FakeGog(_tree(), changes=[])
    assert kb_drive.inventory("connect/kmc-kb", None, gog=gog) == {"kmc": {"unchanged": True, "changes": 0}}
    assert gog.listed == []


def _readable(**over):
    rows = [
        {"file_id": "doc", "name": "Protocol", "mime_type": "application/vnd.google-apps.document", "drive_version": "4",
         "web_url": "https://docs.google.com/document/d/doc", "modified_time": "2026-10-22T10:00:00Z",
         "path": "KMC (synthetic)", "held_headers": [], "hold_released": False},
        {"file_id": "sheet", "name": "Patient list", "mime_type": "text/csv", "md5": "m", "web_url": "https://x/sheet",
         "modified_time": "2026-10-22T10:00:00Z", "path": "KMC (synthetic)", "held_headers": [], "hold_released": False},
    ]
    for r in rows:
        r.update(over.get(r["file_id"], {}))
    return rows


def test_sync_reads_only_approved_files_holds_personal_data_and_publishes_the_rest(monkeypatch, tmp_path):
    pytest.importorskip("canopy_kb", reason="canopy_kb (canopy-web packages/canopy_kb) not installed")
    calls = _calls(monkeypatch, [SOURCE], readable=_readable())
    pushed = []
    monkeypatch.setattr("orchestrator.kb_cli.push", lambda ref, repo, path, **kw: pushed.append(kw) or {"published": "delta"})
    gog = FakeGog(_tree(), bodies={"doc": b"# Protocol\n\nSkin to skin for 20 hours a day.",
                                   "sheet": b"Mother name,DOB,Weight\nA,2026-01-01,1800\n"})
    out = kb_drive.sync("connect/kmc-kb", None, tmp_path, gog=gog)
    assert gog.downloads == [("doc", "md"), ("sheet", "")]
    assert out["held"] == [{"file": "Patient list", "headers": ["Mother name", "DOB"]}]
    assert ("POST", "sources/7/files/sheet/hold", {"headers": ["Mother name", "DOB"]}) in calls
    items = [json.loads(line) for line in open(tmp_path / "bundle" / "items.jsonl")]
    assert [i["id"] for i in items] == ["drive:doc"]
    assert items[0]["meta"]["drive"] == {"file_id": "doc", "revision": "4",
                                          "mime_type": "application/vnd.google-apps.document"}
    assert "path" not in items[0]["original"] and items[0]["original"]["source_url"].startswith("https://docs.google")
    assert pushed[0]["bundle"] == tmp_path / "bundle"

    # unchanged files are not downloaded again; a released hold indexes the sheet
    gog.downloads.clear()
    _calls(monkeypatch, [SOURCE], readable=_readable(sheet={"held_headers": ["Mother name", "DOB"], "hold_released": True}))
    out = kb_drive.sync("connect/kmc-kb", None, tmp_path, gog=gog)
    assert gog.downloads == [] and out["held"] == []
    assert [json.loads(line)["id"] for line in open(tmp_path / "bundle" / "items.jsonl")] == ["drive:doc", "drive:sheet"]

    # an edited doc is re-read; a file no longer readable leaves the bundle
    gog.bodies["doc"] = b"# Protocol v2\n\nSkin to skin, now 24 hours."
    _calls(monkeypatch, [SOURCE], readable=_readable(doc={"drive_version": "5"})[:1])
    kb_drive.sync("connect/kmc-kb", None, tmp_path, gog=gog)
    assert gog.downloads == [("doc", "md")]
    items = [json.loads(line) for line in open(tmp_path / "bundle" / "items.jsonl")]
    assert [i["id"] for i in items] == ["drive:doc"] and "24 hours" in items[0]["text"]
