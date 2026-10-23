"""A curator's working cache, kept on canopy-web so a refresh can run on any machine.

A refresh is incremental because of what it kept from the last one: the fetched bytes
and their ETags (so a refetch is conditional), the extracted text, the OCR, the catalog
cards. Kept only on the box that ran it, a refresh anywhere else starts from nothing —
hours of crawling and every model call again — and the box itself becomes something
that cannot be lost. So after a publish the curator SAVES the cache to canopy-web, and a
refresh RESTORES it first:

    canopy kb workdir restore --kb connect/givewell-kb ~/.eva/kb/givewell
    …refresh…
    canopy kb workdir save --kb connect/givewell-kb ~/.eva/kb/givewell --exclude bundle/ --exclude kb.sqlite

Files are stored by sha256, so a save uploads only what changed, and never a file the
KB's originals already hold (a fetched PDF is the same bytes `push --originals` stored).
Correctness never depends on the cache: a file that cannot be restored is just fetched
again in full. A marker in the directory (`.canopy-workdir.json`) records what was last
saved or restored, so a restore replaces only files this machine has not changed since,
and a save names the cache it builds on — another machine having saved in between is a
conflict (restore, then save), never an older cache silently replacing a newer one.
"""
from __future__ import annotations

import concurrent.futures as cf
import fnmatch
import gzip
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from orchestrator.kb_engine import KBError

MARKER = ".canopy-workdir.json"
#: Never saved: the marker itself and files caught mid-write.
ALWAYS_EXCLUDE = (MARKER, "*.part", "*.download", "*.tmp")
BATCH = 1000  # canopy-web's per-request limit
WORKERS = 8
FORMAT = 1


def _base(ref: str) -> str:
    from orchestrator.kb_cli import _WORKSPACE_REF

    m = _WORKSPACE_REF.match(ref or "")
    if not m:
        raise KBError(f"a working cache is kept for a workspace project (<workspace>/<handle>), not {ref!r}")
    ws, handle = m.groups()
    return f"/api/workspace-projects/{ws}/{handle}/kb/workdir"


def _call(method: str, path: str, body=None):
    from orchestrator import canopy_web

    try:
        return canopy_web.call(method, path, body) or {}
    except canopy_web.CanopyError as e:
        if "-> 404" in str(e) or "-> 405" in str(e):
            raise KBError("this canopy-web does not keep working caches yet (it predates them)") from e
        if "-> 409" in str(e):
            raise KBError("another machine saved this working cache since this one restored it: "
                          "run `canopy kb workdir restore`, then save again") from e
        raise


def _excluded(rel: str, patterns) -> bool:
    for p in patterns:
        if p.endswith("/"):
            if rel == p[:-1] or rel.startswith(p):
                return True
        elif fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel.rsplit("/", 1)[-1], p):
            return True
    return False


def _walk(root: Path, exclude) -> list[str]:
    patterns = ALWAYS_EXCLUDE + tuple(exclude)
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        reldir = Path(dirpath).relative_to(root).as_posix()
        reldir = "" if reldir == "." else reldir + "/"
        dirnames[:] = sorted(d for d in dirnames if not _excluded(reldir + d + "/", patterns))
        for name in sorted(filenames):
            rel = reldir + name
            p = Path(dirpath) / name
            if not p.is_symlink() and p.is_file() and not _excluded(rel, patterns):
                out.append(rel)
    return out


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _hash(root: Path, paths, cache: dict) -> dict[str, tuple[str, int]]:
    """{path: (sha256, bytes)}, re-reading only files whose size or mtime moved."""
    def one(rel):
        st = (root / rel).stat()
        hit = cache.get(rel)
        if hit and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
            return rel, hit[2], st.st_size, st.st_mtime_ns
        return rel, _sha_file(root / rel), st.st_size, st.st_mtime_ns

    out = {}
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        for rel, sha, size, mtime in ex.map(one, paths):
            out[rel] = (sha, size)
            cache[rel] = [size, mtime, sha]
    return out


def _read_marker(root: Path) -> dict:
    try:
        return json.loads((root / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_marker(root: Path, manifest: str, files: dict[str, str], hashes: dict) -> None:
    keep = {k: v for k, v in hashes.items() if (root / k).exists()}
    tmp = root / (MARKER + ".tmp")
    tmp.write_text(json.dumps({"manifest": manifest, "files": files, "hashes": keep}), encoding="utf-8")
    tmp.replace(root / MARKER)


def _gzip_to(src: Path, tdir: Path) -> tuple[Path, str]:
    """A deterministic gzip of `src` (no name, no mtime) and its sha256."""
    out = Path(tempfile.mkstemp(dir=tdir, suffix=".gz")[1])
    with open(src, "rb") as fin, open(out, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            shutil.copyfileobj(fin, gz, 1 << 20)
    return out, _sha_file(out)


def save(ref: str, root: Path, exclude=()) -> dict:
    """Save `root` as this KB's working cache: upload what canopy-web lacks, then point
    the KB at the new manifest (built on the cache this machine last restored or saved)."""
    from orchestrator.kb_cli import _put

    root = Path(root).expanduser()
    if not root.is_dir():
        raise KBError(f"no working cache at {root}")
    base = _base(ref)
    marker = _read_marker(root)
    cache = dict(marker.get("hashes") or {})
    hashed = _hash(root, _walk(root, exclude), cache)
    by_sha: dict[str, str] = {}
    for rel, (sha, _size) in hashed.items():
        by_sha.setdefault(sha, rel)
    shas = sorted(by_sha)
    in_originals: set[str] = set()
    stored: set[str] = set()
    for i in range(0, len(shas), BATCH):
        got = _call("POST", f"{base}/have", {"shas": shas[i:i + BATCH]})
        in_originals |= set(got.get("originals") or [])
        stored |= set(got.get("workdir") or [])
    manifest = {"format": FORMAT, "files": [
        {"path": rel, "sha256": sha, "bytes": size, "store": "originals" if sha in in_originals else "workdir"}
        for rel, (sha, size) in sorted(hashed.items())]}
    uploaded = sent = 0
    with tempfile.TemporaryDirectory(dir=root) as td:
        tdir = Path(td)
        mpath = tdir / "manifest.json"
        mpath.write_bytes(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode())
        msha = _sha_file(mpath)
        todo = [(sha, root / by_sha[sha]) for sha in shas if sha not in in_originals and sha not in stored]
        todo.append((msha, mpath))
        for i in range(0, len(todo), BATCH):
            with cf.ThreadPoolExecutor(WORKERS) as ex:
                packed = dict(zip([s for s, _ in todo[i:i + BATCH]],
                                  ex.map(lambda t: _gzip_to(t[1], tdir), todo[i:i + BATCH])))
            blobs = [{"sha256": s, "gz_sha256": g, "bytes": p.stat().st_size} for s, (p, g) in packed.items()]
            got = _call("POST", f"{base}/blobs", {"blobs": blobs})
            ups = got.get("uploads") or []
            with cf.ThreadPoolExecutor(WORKERS) as ex:
                list(ex.map(lambda u: _put(u["url"], packed[u["sha256"]][0], u.get("headers") or {}), ups))
            uploaded += len(ups)
            sent += sum(packed[u["sha256"]][0].stat().st_size for u in ups)
            for p, _ in packed.values():
                p.unlink(missing_ok=True)
    total = sum(size for _, size in hashed.values())
    saved = _call("PUT", base, {"manifest": msha, "base": marker.get("manifest"), "files": len(hashed),
                                "bytes": total})
    _write_marker(root, msha, {rel: sha for rel, (sha, _) in hashed.items()}, cache)
    return {"saved": True, "manifest": msha, "files": len(hashed), "bytes": total,
            "held_by_originals": sum(1 for f in manifest["files"] if f["store"] == "originals"),
            "uploaded": uploaded, "bytes_uploaded": sent, "version": saved.get("version")}


def _download(url: str, dst: Path, sha: str, gzipped: bool) -> int:
    """Fetch `url` into `dst` (atomically), checked against `sha`. Returns its size."""
    import urllib.request

    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.parent / f".{dst.name}.{sha[:12]}.part"
    try:
        with urllib.request.urlopen(url, timeout=600) as resp, open(part, "wb") as out:
            src = gzip.GzipFile(fileobj=resp) if gzipped else resp
            shutil.copyfileobj(src, out, 1 << 20)
        if _sha_file(part) != sha:
            raise KBError(f"{dst.name}: the stored copy does not match its sha256")
        part.replace(dst)
        return dst.stat().st_size
    finally:
        part.unlink(missing_ok=True)


def _links(base: str, wanted: list[tuple[str, str]]) -> dict[str, str]:
    out = {}
    for i in range(0, len(wanted), BATCH):
        got = _call("POST", f"{base}/links",
                    {"files": [{"sha256": s, "store": st} for s, st in wanted[i:i + BATCH]]})
        out.update({ln["sha256"]: ln["url"] for ln in got.get("links") or []})
    return out


def restore(ref: str, root: Path, force: bool = False) -> dict:
    """Bring `root` up to the working cache saved last: fetch what is missing, and
    replace what this machine has not changed since it last saved or restored (`force`:
    replace those too). Never deletes a file."""
    root = Path(root).expanduser()
    base = _base(ref)
    pointer = _call("GET", base)
    msha = pointer.get("manifest")
    if not msha:
        return {"restored": False, "reason": "no working cache saved yet: this refresh starts from what is here"}
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as td:
        mpath = Path(td) / "manifest.json"
        _download(_links(base, [(msha, "workdir")])[msha], mpath, msha, gzipped=True)
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    if manifest.get("format") != FORMAT:
        raise KBError(f"working-cache manifest format {manifest.get('format')} is newer than this canopy: update it")
    files = manifest.get("files") or []
    marker = _read_marker(root)
    synced = marker.get("files") or {}
    cache = dict(marker.get("hashes") or {})
    present = [f["path"] for f in files if (root / f["path"]).is_file()]
    local = _hash(root, present, cache)
    fetch, kept_local = [], []
    for f in files:
        have = local.get(f["path"])
        if have and have[0] == f["sha256"]:
            continue
        if have and not force and synced.get(f["path"]) != have[0]:
            kept_local.append(f["path"])  # changed here since the last sync: this machine's work
            continue
        fetch.append(f)
    first: dict[str, dict] = {}
    for f in fetch:
        first.setdefault(f["sha256"], f)
    links = _links(base, [(s, f["store"]) for s, f in first.items()])
    fetched_bytes, unavailable, done = 0, [], {}

    def get(f):
        try:
            return f, _download(links[f["sha256"]], root / f["path"], f["sha256"], gzipped=f["store"] == "workdir")
        except Exception as e:  # noqa: BLE001 - a file not restored is fetched again by the refresh
            return f, e

    with cf.ThreadPoolExecutor(WORKERS) as ex:
        for f, got in ex.map(get, first.values()):
            if isinstance(got, Exception):
                unavailable.append(f["path"])
            else:
                fetched_bytes += got
                done[f["sha256"]] = f["path"]
    for f in fetch:  # the same bytes at a second path: copy, don't download twice
        src = done.get(f["sha256"])
        if src and src != f["path"]:
            (root / f["path"]).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / src, root / f["path"])
    _hash(root, [f["path"] for f in fetch if (root / f["path"]).is_file()], cache)
    _write_marker(root, msha, {f["path"]: f["sha256"] for f in files}, cache)
    return {"restored": True, "manifest": msha, "version": pointer.get("version"),
            "saved_at": pointer.get("saved_at"), "saved_by": pointer.get("saved_by"),
            "files": len(files), "fetched": len(fetch) - len(unavailable), "bytes_fetched": fetched_bytes,
            "kept_local": kept_local[:20], "kept_local_count": len(kept_local),
            "unavailable": unavailable[:20], "unavailable_count": len(unavailable)}
