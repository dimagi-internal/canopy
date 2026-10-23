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
  downloading one only when its md5 or version moved (Google Docs export as
  Markdown, Sheets as xlsx, Slides as PDF), checks every spreadsheet's column headers
  (`canopy_kb.sensitive`) and holds one that names or identifies people, reads the
  rest (shared readers: `canopy_kb.prep.extract`) and publishes the difference;
  canopy-web builds the index. It needs nothing on the machine: what it learnt last
  time (each file's revision, what reading it found) is the curation state on
  canopy-web, and each text a step result there (`canopy_kb.curate`), so any machine
  syncs as incrementally as the last one did. A file that was removed, trashed,
  excluded or held is simply not published, so the next version drops it.

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

from orchestrator.kb_engine import KBError, engine

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
        out.unlink(missing_ok=True)  # gog refuses to overwrite: an edited file is read again
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


#: Text formats read as their bytes; the rest by canopy_kb.prep's readers, whose
#: REVISIONs and lock (`canopy_kb.prep.steps`) cover them here too.
_PLAIN = ("md", "txt", "csv")


def read_step(kind: str):
    """The curation step that reads a Drive file of this kind (`canopy_kb.curate`)."""
    from canopy_kb.curate import Step
    from canopy_kb.prep import steps as prep_steps
    if kind in _PLAIN:
        return Step(f"drive:read:{kind}", "1", {"kind": kind})  # its bytes, as text
    return prep_steps.reader(kind)


def _read(kind: str, path: Path) -> tuple[str, str]:
    """(text, error) from a downloaded file."""
    if kind in _PLAIN:
        return path.read_bytes().decode("utf-8", "replace"), ""
    from canopy_kb.prep.extract import extract_one
    res = extract_one(kind, str(path))
    return res.get("text") or "", res.get("error") or res.get("unavailable") or ""


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sync(ref: str, repo: Path, source: str = "", push: bool = True, gog: Gog | None = None, store=None) -> dict:
    """Read the approved files, hold spreadsheets with personal-data headers, and publish
    the difference, from an empty disk (canopy-web#1925). What a sync needs from the last
    one lives on canopy-web: the curation state remembers each file's revision and what
    reading it found, and the text it read is a step result. So an unchanged file is not
    downloaded at all, and canopy-web builds the index; no machine keeps a cache or a
    kb.sqlite. A held spreadsheet's text is never stored: only the verdict is."""
    import tempfile

    engine()
    from canopy_kb.curate import Curation
    from canopy_kb.prep import steps as prep_steps
    from canopy_kb.sensitive import header_flags, spreadsheet_headers

    from orchestrator import canopy_web
    from orchestrator.kb_remote import RemoteStore

    prep_steps.check()
    gog = gog or Gog(repo)
    store = store or RemoteStore(ref)
    report = {"read": 0, "downloaded": 0, "held": [], "unreadable": [], "indexed": 0}
    docs: dict[str, dict] = {}
    texts: dict[str, str] = {}      # item id -> text read this run
    keys: dict[str, str] = {}       # item id -> its text's step result
    with Curation(store) as cur, tempfile.TemporaryDirectory(prefix="kb-drive-") as td:
        seen = cur.state.section("drive")
        listed: set[str] = set()
        for s in _sources(ref, source):
            files = canopy_web.call("GET", f"{_base(ref)}/sources/{s['id']}/files?readable=true") or []
            for f in files:
                fid, mime = f["file_id"], f.get("mime_type") or ""
                listed.add(fid)
                fmt, kind = _reader_for(mime)
                if not kind:
                    report["unreadable"].append(f.get("name"))
                    continue
                report["read"] += 1
                stamp = f.get("md5") or f.get("drive_version") or f.get("modified_time") or ""
                key = read_step(kind).key(file=fid, stamp=stamp)
                entry = seen.get(fid) or {}
                released = bool(f.get("hold_released"))
                need_text = entry.get("key") != key or (entry.get("headers") and released and not entry.get("text_sha"))
                if need_text:
                    path = Path(td) / f"{fid}.{fmt or kind}"
                    gog.download(fid, path, fmt)
                    body = path.read_bytes()
                    report["downloaded"] += 1
                    entry = {"key": key, "stamp": stamp, "sha256": hashlib.sha256(body).hexdigest(),
                             "bytes": len(body), "fetched_at": _now(), "kind": kind}
                    if kind in ("xlsx", "xls", "csv"):
                        entry["headers"] = header_flags(spreadsheet_headers(path, kind))
                    if not entry.get("headers") or released:  # read it; a held file's text is never kept
                        text, err = _read(kind, path)
                        if text.strip():
                            cur.put(key, {"text": text})
                            texts[DOC_PREFIX + fid] = text
                            entry |= {"text_sha": _text_sha(text), "text_sha256": hashlib.sha256(
                                text.encode("utf-8")).hexdigest()}
                        else:
                            entry["error"] = err or "no text"
                    path.unlink(missing_ok=True)
                    seen[fid] = entry
                held = entry.get("headers") or []
                if held and not released:
                    if f.get("held_headers") != held:
                        canopy_web.call("POST", f"{_base(ref)}/sources/{s['id']}/files/{fid}/hold", {"headers": held})
                    report["held"].append({"file": f.get("name"), "headers": held})
                    continue
                if not entry.get("text_sha"):
                    report["unreadable"].append(f"{f.get('name')}: {entry.get('error') or 'no text'}")
                    continue
                docs[DOC_PREFIX + fid] = _item(s, f, entry)
                keys[DOC_PREFIX + fid] = key
        for fid in set(seen) - listed:
            seen.pop(fid)  # no longer readable (removed, excluded, trashed): forget it
        report["indexed"] = len(docs)
        if not push:
            return report

        def text_of(ids: list[str]) -> dict[str, str]:
            got = cur.results([keys[i] for i in ids if i not in texts])
            out = {}
            for i in ids:
                if i in texts:
                    out[i] = texts[i]
                elif keys[i] in got:
                    out[i] = got[keys[i]]["text"]
                else:
                    raise KBError(f"{i}: its text is not stored on canopy-web; sync again to re-read it")
            return out

        report["push"] = cur.publish(docs, text_of, _parts(ref), allow_removals=True)
    return report


def _text_sha(text: str) -> str:
    """The address a record gives its text (`canopy_kb.records`)."""
    from canopy_kb import records
    return records.sha(text)


def _item(source: dict, f: dict, entry: dict) -> dict:
    """A Drive file as a published record (`drive:<file id>`, its text by reference), its
    source recorded."""
    fid = f["file_id"]
    revision = f.get("drive_version") or f.get("md5") or ""
    name = f.get("name") or fid
    return {
        "id": DOC_PREFIX + fid, "type": ITEM_TYPES.get(f.get("mime_type") or "", "file"), "title": name,
        "url": f.get("web_url") or f"https://drive.google.com/file/d/{fid}/view",
        "date": (f.get("modified_time") or "")[:10], "text_sha": entry["text_sha"],
        "sha256": entry["text_sha256"], "fetched_at": f.get("modified_time") or "",
        "meta": {"source": "drive", "source_name": source.get("name"), "folder": f.get("path") or "",
                 "drive": {"file_id": fid, "revision": revision, "mime_type": f.get("mime_type") or ""}},
        "original": {"sha256": entry["sha256"], "filename": name, "content_type": f.get("mime_type") or "",
                     "bytes": entry.get("bytes") or 0, "source_url": f.get("web_url") or "",
                     "fetched_at": f.get("modified_time") or ""},
    }


def _parts(ref: str) -> dict:
    """The corpus-wide parts of a Drive KB: its manifest and (no) aliases."""
    return {"manifest": {
        "name": re.sub(r"[^a-z0-9-]+", "-", ref.split("/")[-1]), "publisher": "Google Drive folders",
        "license": "private: the folders' owner decides who reads it",
        "guide": ["Documents are files from approved Google Drive folders; `original` gives the Drive link."],
        "tables": {}}, "aliases": {}}

