"""A knowledge base's host as a refresh sees it: canopy-web, over canopy's own client.

`RemoteStore` is `canopy_kb.curate.Store` for a workspace project (`<workspace>/<handle>`):
step results and originals by key (presigned, straight to storage, never through
canopy-web's web task), the head version and the curation state it was built from, and
the whole-file publish made from it (canopy-web refuses one made from a version that is
no longer the head). A curator's refresh (e.g. eva's GiveWell refresh)
builds a `canopy_kb.curate.Curation` over it, and runs from an empty disk on any machine
the agent's identity works on (canopy-web#1925).

Identity is canopy's usual: the agent's PAT (`canopy_web.resolve_token`), so run it from
the agent's repo.
"""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import re
import time
import urllib.error
import urllib.request

from orchestrator.kb_engine import KBError

WORKERS = 16
_REF = re.compile(r"^([a-z0-9][a-z0-9-]*)/([a-z0-9][a-z0-9-]{1,58}[a-z0-9])$")


def _http(method: str, url: str, data: bytes | None = None, headers: dict | None = None, tries: int = 4) -> bytes:
    for n in range(tries):
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={**(headers or {}), **({"Content-Length": str(len(data))} if data else {})})
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (403, 404) and method == "GET":
                raise KeyError(url) from e
            if e.code < 500 or n == tries - 1:
                raise KBError(f"storage {method} failed: HTTP {e.code}") from e
        except OSError as e:
            if n == tries - 1:
                raise KBError(f"storage {method} failed: {e}") from e
        time.sleep(2 ** n)
    raise AssertionError("unreachable")


class RemoteStore:
    def __init__(self, ref: str, call=None):
        m = _REF.match(ref or "")
        if not m:
            raise KBError(f"{ref!r} is not a workspace project (<workspace>/<handle>)")
        if call is None:
            from orchestrator import canopy_web
            call = canopy_web.call
        self.call = call
        self.base = f"/api/workspace-projects/{m.group(1)}/{m.group(2)}/kb"

    # objects
    def have(self, store: str, keys: list[str]) -> set[str]:
        if not keys:
            return set()
        return set((self.call("POST", f"{self.base}/objects/have", {"store": store, "keys": keys}) or {}).get("have")
                   or [])

    def get(self, store: str, keys: list[str]) -> dict[str, bytes]:
        if not keys:
            return {}
        links = (self.call("POST", f"{self.base}/objects/links", {"store": store, "keys": keys}) or {}).get("links") or []

        def fetch(link):
            try:
                return link["key"], _http("GET", link["url"])
            except KeyError:
                return link["key"], None

        out = {}
        with cf.ThreadPoolExecutor(WORKERS) as ex:
            for k, body in ex.map(fetch, links):
                if body is not None:
                    out[k] = body
        return out

    def _put(self, route: str, blobs: dict[str, bytes]) -> int:
        if not blobs:
            return 0
        meta = [{"sha256": k, "gz_sha256": hashlib.sha256(v).hexdigest(), "bytes": len(v)} for k, v in blobs.items()]
        ups = (self.call("POST", f"{self.base}/{route}", {"blobs": meta}) or {}).get("uploads") or []
        with cf.ThreadPoolExecutor(WORKERS) as ex:
            list(ex.map(lambda u: _http("PUT", u["url"], blobs[u["sha256"]], u.get("headers") or {}), ups))
        return len(ups)

    def put_results(self, blobs: dict[str, bytes]) -> int:
        return self._put("results", blobs)

    def put_originals(self, files: dict[str, tuple[bytes, str]]) -> int:
        n = 0
        keys = sorted(files)
        for i in range(0, len(keys), 1000):
            batch = keys[i:i + 1000]
            meta = [{"sha256": k, "bytes": len(files[k][0]), "content_type": files[k][1]} for k in batch]
            ups = (self.call("POST", f"{self.base}/originals", {"files": meta}) or {}).get("uploads") or []
            with cf.ThreadPoolExecutor(WORKERS) as ex:
                list(ex.map(lambda u: _http("PUT", u["url"], files[u["sha256"]][0], u.get("headers") or {}), ups))
            n += len(ups)
        return n

    # versions
    def head(self) -> dict:
        """The version the project serves and the curation state it was built from
        ({version: None} before the first)."""
        from orchestrator import canopy_web
        try:
            log = self.call("GET", f"{self.base}/versions?limit=20") or []
        except canopy_web.CanopyError as e:
            if "-> 404" in str(e):
                return {"version": None, "curation": None}
            raise
        cur = next((v for v in log if v.get("current")), None)
        if cur is None:
            return {"version": None, "curation": None}
        if "curation_sha" not in cur:
            raise KBError("canopy-web predates versions that carry their curation state (canopy-web#1986): "
                          "deploy it first")
        return {"version": cur["number"], "curation": cur.get("curation_sha") or None}

    def served(self, out):
        """The kb.sqlite the project serves now, downloaded and checked (None before its
        first version). A refresh applies what changed to it and publishes the result."""
        import gzip
        import shutil
        from pathlib import Path

        from orchestrator import canopy_web
        try:
            f = self.call("GET", f"{self.base}/file") or {}
        except canopy_web.CanopyError as e:
            if "-> 404" in str(e) or "-> 422" in str(e):
                return None
            raise
        if not f.get("url"):
            return None
        out = Path(out)
        gz = out.with_suffix(".gz")
        req = urllib.request.Request(f["url"])
        digest = hashlib.sha256()
        with urllib.request.urlopen(req, timeout=1800) as resp, open(gz, "wb") as fh:
            for chunk in iter(lambda: resp.read(1 << 20), b""):
                digest.update(chunk)
                fh.write(chunk)
        if digest.hexdigest() != f.get("sha256"):
            raise KBError("the served knowledge base does not match its published hash")
        with gzip.open(gz, "rb") as src, open(out, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        gz.unlink(missing_ok=True)
        return out

    def publish_file(self, gz, sha256: str, *, base: int | None, curation: str, changes: dict | None = None) -> dict:
        """Upload a built kb.sqlite.gz and publish it as a version made from `base`, with
        the curation state it was built from and how many documents' content changed."""
        from orchestrator import canopy_web
        target = self.call("POST", f"{self.base}/upload") or {}
        if not target.get("url"):
            raise KBError(f"canopy-web gave no upload target for {self.base}")
        with open(gz, "rb") as fh:
            req = urllib.request.Request(target["url"], data=fh, method="PUT", headers={
                **(target.get("headers") or {}), "Content-Length": str(__import__("os").path.getsize(gz))})
            with urllib.request.urlopen(req, timeout=3600) as resp:
                if resp.status >= 300:
                    raise KBError(f"upload to storage failed: HTTP {resp.status}")
        body = {"key": target["key"], "sha256": sha256, "base": base, "curation": curation}
        if changes is not None:
            body["changes"] = changes
        try:
            proj = self.call("POST", f"{self.base}/publish", body) or {}
        except canopy_web.CanopyError as e:
            if "-> 409" in str(e):
                raise KBError(f"another version landed since this refresh started; refresh again: {e}") from e
            raise
        return {"version": proj.get("kb_version_number")}
