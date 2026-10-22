"""`canopy kb drive …` — a knowledge base made from Google Drive folders, read only as
far as their owner approves.

Two commands, run by the KB's steward as its own Google identity (`--repo`):

* `inventory` lists every file and folder under each `drive_folders` source of the
  project as METADATA only — names, types, sizes, owners, who can open each, md5 /
  version, modified times — and posts it to canopy-web. Nothing is opened. The
  project's owner then reviews it on canopy-web (`/w/<ws>/projects/<handle>/kb`):
  switches files and folders on or off, sees what is flagged and skipped, and
  sets a folder to admit new files or queue them. Drive's changes feed (a page token
  kept on the source) says whether anything changed since the last listing; when
  nothing did, the walk is skipped.
* `sync` reads ONLY the files canopy-web says are approved (`?readable=true`),
  re-downloading one only when its md5 or version moved (Google Docs export as
  Markdown, Sheets as xlsx, Slides as PDF), checks every spreadsheet's column headers
  (`canopy_kb.sensitive`) and holds one that names or identifies people, turns the
  rest into the bundle's items (shared readers: `canopy_kb.prep.extract`), builds,
  and pushes the difference (`canopy kb push --bundle`). A file that was removed,
  trashed, excluded or held is simply not in the bundle, so the next version drops it.

Originals are not copied: each item's `original` names the Drive file and the
revision it was read at, and the `original` tool hands readers the Drive link (opened
with the access the folder's owner gave them).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import subprocess
from pathlib import Path

from orchestrator.kb_engine import KBError

FOLDER = "application/vnd.google-apps.folder"
_G = "application/vnd.google-apps."
#: How each Google-native type is exported, and the reader its export needs.
EXPORTS = {_G + "document": ("md", "md"), _G + "spreadsheet": ("xlsx", "xlsx"), _G + "presentation": ("pdf", "pdf")}
#: The reader for an uploaded file, by mime type.
READERS = {"application/pdf": "pdf", "text/markdown": "md", "text/plain": "txt", "text/csv": "csv",
           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
           "application/vnd.ms-excel": "xls",
           "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
           "application/msword": "doc"}
ITEM_TYPES = {_G + "document": "gdoc", _G + "spreadsheet": "sheet", _G + "presentation": "slides",
              "application/pdf": "pdf_document"}
FIELDS = ("nextPageToken,files(id,name,mimeType,size,modifiedTime,md5Checksum,version,owners(emailAddress),"
          "parents,webViewLink,trashed,permissions(type,emailAddress,domain))")
DOC_PREFIX = "drive:"


class Gog:
    """`gog` as the steward's Google identity (the same identity and client rule every
    canopy Drive call uses: `kb_cli._Drive`)."""

    def __init__(self, repo: Path, runner=subprocess.run):
        from orchestrator.kb_cli import _Drive
        d = _Drive(repo, runner=runner)
        self.identity, self.runner = d.identity, runner

    def run(self, *args: str, results_only: bool = False):
        cmd = ["gog", *args, "--account", self.identity.account, "--client", self.identity.client, "-j"]
        if results_only:
            cmd.append("--results-only")
        r = self.runner(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise KBError(f"`gog {' '.join(args[:3])}` failed as {self.identity.account}: "
                          f"{(r.stderr or r.stdout or '').strip()[:300]}")
        out = (r.stdout or "").strip()
        return json.loads(out) if out else {}

    def children(self, folder: str) -> list[dict]:
        out, page = [], ""
        while True:
            args = ["drive", "ls", "--parent", folder, "--max", "1000", "--fields", FIELDS,
                    "--query", f"'{folder}' in parents and trashed = false"]
            if page:
                args += ["--page", page]
            got = self.run(*args)
            out += got.get("files") or []
            page = got.get("nextPageToken") or ""
            if not page:
                return out

    def root(self, folder: str) -> dict:
        """A source folder's own metadata (Files.Get, as `ls` gives its children)."""
        return self.run("drive", "raw", folder)

    def permissions(self, file_id: str) -> list[dict]:
        out, page = [], ""
        while True:
            got = self.run("drive", "permissions", file_id, "--max", "100", *(["--page", page] if page else []))
            out += got.get("permissions") or []
            page = got.get("nextPageToken") or ""
            if not page:
                return out

    def start_token(self) -> str:
        got = self.run("drive", "changes", "start-token")
        return str(got.get("startPageToken") or "")

    def changes_since(self, token: str) -> tuple[list[dict], str]:
        got = self.run("drive", "changes", "list", "--token", token, "--all", "--include-removed")
        return list(got.get("changes") or []), str(got.get("newStartPageToken") or got.get("nextPageToken") or "")

    def download(self, file_id: str, out: Path, fmt: str = "") -> None:
        args = ["drive", "download", file_id, "--out", str(out)]
        if fmt:
            args += ["--format", fmt]
        self.run(*args)


def _listing(f: dict) -> dict:
    """A Drive file as canopy-web's inventory takes it: metadata, never contents."""
    readers = []
    for p in f.get("permissions") or []:
        kind = p.get("type")
        if kind == "anyone":
            readers.append("anyone")
        elif kind == "domain" and p.get("domain"):
            readers.append(f"domain:{p['domain'].lower()}")
        elif kind in ("user", "group") and p.get("emailAddress"):
            readers.append(f"{kind}:{p['emailAddress'].lower()}")
    size = f.get("size")
    return {"id": f["id"], "parent": (f.get("parents") or [""])[0], "name": f.get("name") or "",
            "mime_type": f.get("mimeType") or "", "size": int(size) if str(size or "").isdigit() else None,
            "owner": ((f.get("owners") or [{}])[0].get("emailAddress") or "").lower(),
            "modified_time": f.get("modifiedTime") or "", "md5": f.get("md5Checksum") or "",
            "version": str(f.get("version") or ""), "web_url": f.get("webViewLink") or "", "readers": readers}


def _base(ref: str) -> str:
    from orchestrator.kb_cli import _WORKSPACE_REF
    m = _WORKSPACE_REF.match(ref or "")
    if not m:
        raise KBError(f"--kb {ref!r}: a Drive source belongs to a workspace project (<workspace>/<handle>)")
    return f"/api/workspace-projects/{m.group(1)}/{m.group(2)}/kb"


def _sources(ref: str, name: str = "") -> list[dict]:
    from orchestrator import canopy_web
    out = [s for s in canopy_web.call("GET", f"{_base(ref)}/sources") or [] if s.get("kind") == "drive_folders"]
    if name:
        out = [s for s in out if s.get("name") == name]
    if not out:
        raise KBError(f"{ref} has no drive_folders source{' named ' + name if name else ''} "
                      "(add one: POST …/kb/sources {kind: drive_folders, name, config: {folders: [ids]}})")
    return out


def _with_readers(gog: Gog, f: dict) -> dict:
    """A shared drive's listing leaves `permissions` out (Drive populates it only for My
    Drive items), so ask for the file's own; without them the folder-vs-file sharing check
    could never fire."""
    if "permissions" not in f:
        f = {**f, "permissions": gog.permissions(f["id"])}
    return f


def walk(gog: Gog, roots: list[str]) -> list[dict]:
    """Every file and folder under `roots`, the roots included, as listings."""
    seen: dict[str, dict] = {}
    todo = []
    for r in roots:
        meta = gog.root(r)
        if not meta or meta.get("mimeType") != FOLDER:
            raise KBError(f"{r} is not a Drive folder this identity can open")
        seen[r] = _listing(_with_readers(gog, meta))
        todo.append(r)
    while todo:
        folder = todo.pop()
        for f in gog.children(folder):
            if f.get("trashed") or f["id"] in seen:
                continue
            row = _listing(_with_readers(gog, f))
            row["parent"] = folder  # the parent we reached it through (a file can have several)
            seen[f["id"]] = row
            if f.get("mimeType") == FOLDER:
                todo.append(f["id"])
    return list(seen.values())


def inventory(ref: str, repo: Path, source: str = "", force: bool = False, gog: Gog | None = None) -> dict:
    """List each Drive source's folders (metadata only) and post it to canopy-web."""
    from orchestrator import canopy_web

    gog = gog or Gog(repo)
    out = {}
    for s in _sources(ref, source):
        token = s.get("page_token") or ""
        if token and not force:
            changes, new_token = gog.changes_since(token)
            if not changes:
                out[s["name"]] = {"unchanged": True, "changes": 0}
                continue
        next_token = gog.start_token()  # before the walk: a change during it shows up next time
        files = walk(gog, [str(f) for f in (s.get("config") or {}).get("folders") or []])
        counts = canopy_web.call("POST", f"{_base(ref)}/sources/{s['id']}/inventory",
                                 {"files": files, "page_token": next_token}) or {}
        out[s["name"]] = counts
    return out


# -- sync: read what is approved, build the bundle, push the difference ----------------

def _reader_for(mime: str) -> tuple[str, str]:
    """(gog export format or "", reader kind) for a Drive file, ("", "") when unreadable."""
    if mime in EXPORTS:
        return EXPORTS[mime]
    return "", READERS.get(mime, "")


def _text_of(kind: str, path: Path) -> tuple[str, str]:
    """(text, error) from a downloaded file."""
    if kind in ("md", "txt", "csv"):
        return path.read_bytes().decode("utf-8", "replace"), ""
    from canopy_kb.prep.extract import extract_one
    res = extract_one(kind, str(path))
    return res.get("text") or "", res.get("error") or ""


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sync(ref: str, repo: Path, data: Path, source: str = "", push: bool = True, gog: Gog | None = None) -> dict:
    """Download the approved files that changed, hold spreadsheets with personal-data
    headers, write the bundle, build and push it. `data` is the working cache."""
    from canopy_kb.sensitive import header_flags, spreadsheet_headers

    from orchestrator import canopy_web
    from orchestrator.kb_cli import push as kb_push
    from orchestrator.kb_engine import build

    gog = gog or Gog(repo)
    data = Path(data)
    raw = data / "drive"
    raw.mkdir(parents=True, exist_ok=True)
    cache_path = data / "drive.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    items, report = [], {"read": 0, "downloaded": 0, "held": [], "unreadable": [], "indexed": 0}
    for s in _sources(ref, source):
        files = canopy_web.call("GET", f"{_base(ref)}/sources/{s['id']}/files?readable=true") or []
        for f in files:
            fid, mime = f["file_id"], f.get("mime_type") or ""
            fmt, kind = _reader_for(mime)
            if not kind:
                report["unreadable"].append(f.get("name"))
                continue
            report["read"] += 1
            stamp = f.get("md5") or f.get("drive_version") or f.get("modified_time") or ""
            entry = cache.get(fid) or {}
            path = raw / f"{fid}.{fmt or kind}"
            if entry.get("stamp") != stamp or not path.exists():
                gog.download(fid, path, fmt)
                body = path.read_bytes()
                entry = {"stamp": stamp, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body),
                         "fetched_at": _now(), "kind": kind}
                if kind in ("xlsx", "xls", "csv"):
                    entry["headers"] = header_flags(spreadsheet_headers(path, kind))
                cache[fid] = entry
                report["downloaded"] += 1
            held = entry.get("headers") or []
            if held and not f.get("hold_released"):
                if f.get("held_headers") != held:
                    canopy_web.call("POST", f"{_base(ref)}/sources/{s['id']}/files/{fid}/hold", {"headers": held})
                report["held"].append({"file": f.get("name"), "headers": held})
                continue
            text, err = _text_of(kind, path)
            if not text.strip():
                report["unreadable"].append(f"{f.get('name')}: {err or 'no text'}")
                continue
            items.append(_item(s, f, entry, text))
    cache_path.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    report["indexed"] = len(items)
    bundle = _write_bundle(ref, data / "bundle", items)
    out_db = data / "kb.sqlite"
    report["build"] = build(bundle, out_db, out_db if out_db.exists() else None)
    if push:
        report["push"] = kb_push(ref, repo, out_db, bundle=bundle)
    return report


def _item(source: dict, f: dict, entry: dict, text: str) -> dict:
    """A Drive file as an item of the bundle (`drive:<file id>`), its source recorded."""
    fid = f["file_id"]
    revision = f.get("drive_version") or f.get("md5") or ""
    name = f.get("name") or fid
    return {
        "id": DOC_PREFIX + fid, "type": ITEM_TYPES.get(f.get("mime_type") or "", "file"), "title": name,
        "url": f.get("web_url") or f"https://drive.google.com/file/d/{fid}/view",
        "date": (f.get("modified_time") or "")[:10], "text": text,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "fetched_at": f.get("modified_time") or "",
        "meta": {"source": "drive", "source_name": source.get("name"), "folder": f.get("path") or "",
                 "drive": {"file_id": fid, "revision": revision, "mime_type": f.get("mime_type") or ""}},
        "original": {"sha256": entry["sha256"], "filename": name, "content_type": f.get("mime_type") or "",
                     "bytes": entry.get("bytes") or 0, "source_url": f.get("web_url") or "",
                     "fetched_at": f.get("modified_time") or ""},
    }


def _write_bundle(ref: str, bundle: Path, items: list[dict]) -> Path:
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "tables").mkdir(exist_ok=True)
    manifest = bundle / "manifest.json"
    if not manifest.exists():
        manifest.write_text(json.dumps({
            "name": re.sub(r"[^a-z0-9-]+", "-", ref.split("/")[-1]), "publisher": "Google Drive folders",
            "license": "private: the folders' owner decides who reads it",
            "guide": ["Documents are files from approved Google Drive folders; `original` gives the Drive link."],
            "tables": {}}, indent=2), encoding="utf-8")
    items = sorted(items, key=lambda i: i["id"])
    (bundle / "items.jsonl").write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items),
                                        encoding="utf-8")
    if not (bundle / "aliases.json").exists():
        (bundle / "aliases.json").write_text("{}", encoding="utf-8")
    return bundle
