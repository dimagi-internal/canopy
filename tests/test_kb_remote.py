"""`kb_remote.RemoteStore`: canopy_kb.curate's host over canopy-web's routes, with bytes
going straight to storage on presigned links."""
import gzip
import io

import pytest

from orchestrator import kb_remote
from orchestrator.kb_engine import KBError


class Web:
    """canopy-web's curation routes over a dict, presigning links to a fake storage."""

    def __init__(self):
        self.stored = {"results": {}, "records": {}, "originals": {}, "served": {}, "upload": {}}
        self.served_sha = ""
        self.posts = []

    def __call__(self, method, path, body=None, **kw):
        route = path.split("/kb/", 1)[1]
        self.posts.append((method, route))
        if route == "objects/have":
            return {"have": [k for k in body["keys"] if k in self.stored[body["store"]]]}
        if route == "objects/links":
            return {"links": [{"key": k, "url": f"https://s3/{body['store']}/{k}"} for k in body["keys"]]}
        if route in ("results", "blobs"):
            store = "results" if route == "results" else "records"
            return {"uploads": [{"sha256": b["sha256"], "url": f"https://s3/{store}/{b['sha256']}", "headers": {}}
                                for b in body["blobs"] if b["sha256"] not in self.stored[store]]}
        if route == "originals":
            return {"uploads": [{"sha256": f["sha256"], "url": f"https://s3/originals/{f['sha256']}", "headers": {}}
                                for f in body["files"] if f["sha256"] not in self.stored["originals"]]}
        if route == "file":
            return {"url": "https://s3/served/kb", "sha256": self.served_sha}
        if route == "upload":
            return {"key": "k1", "url": "https://s3/upload/k1", "headers": {}}
        if route == "publish":
            self.published = body
            return {"kb_version_number": 2}
        if route == "curation":
            return {"sha": "r" * 64} if method == "GET" else {"sha": body["sha"]}
        raise AssertionError(route)


class _Resp(io.BytesIO):
    status = 200


@pytest.fixture()
def web(monkeypatch):
    w = Web()

    def urlopen(req, timeout=0):
        _, store, key = req.full_url.rsplit("/", 2)
        if req.get_method() == "PUT":
            w.stored[store][key] = req.data.read() if hasattr(req.data, "read") else req.data
            return _Resp(b"")
        if key not in w.stored[store]:
            import urllib.error
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)
        return _Resp(w.stored[store][key])

    monkeypatch.setattr(kb_remote.urllib.request, "urlopen", urlopen)
    return w


def test_results_go_to_storage_once_and_come_back_by_key(web):
    s = kb_remote.RemoteStore("connect/givewell-kb", call=web)
    blob = gzip.compress(b'{"text":"read"}')
    assert s.put_results({"a" * 64: blob}) == 1
    assert s.put_results({"a" * 64: blob}) == 0  # stored already: nothing sent
    assert s.have("results", ["a" * 64, "b" * 64]) == {"a" * 64}
    assert s.get("results", ["a" * 64, "b" * 64]) == {"a" * 64: blob}  # a missing key is just absent
    assert s.put_originals({"c" * 64: (b"%PDF", "application/pdf")}) == 1 and web.stored["originals"]["c" * 64] == b"%PDF"
    assert s.curation()["sha"] == "r" * 64


def test_a_ref_must_be_a_workspace_project():
    with pytest.raises(KBError, match="workspace project"):
        kb_remote.RemoteStore("givewell", call=lambda *a, **k: {})


def test_a_refresh_downloads_the_served_file_checked_and_publishes_a_built_one(web, tmp_path):
    import hashlib
    body = gzip.compress(b"SQLite format 3\0 the served file")
    web.stored["served"]["kb"], web.served_sha = body, hashlib.sha256(body).hexdigest()
    s = kb_remote.RemoteStore("connect/givewell-kb", call=web)
    got = s.served(tmp_path / "base.sqlite")
    assert got.read_bytes() == b"SQLite format 3\0 the served file"
    web.served_sha = "0" * 64
    with pytest.raises(KBError, match="does not match"):
        s.served(tmp_path / "again.sqlite")
    gz = tmp_path / "built.gz"
    gz.write_bytes(b"built")
    assert s.publish_file(gz, "f" * 64, {"docs": {}, "parts": {}}, {"added": ["x"], "changed": [], "removed": []}) \
        == {"version": 2}
    assert web.published["changes"]["added"] == ["x"]
    assert web.stored["upload"]["k1"] == b"built" and web.published["sha256"] == "f" * 64

