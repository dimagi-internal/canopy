"""`canopy kb …` — build, store and serve a knowledge base (engine: canopy-web's
`canopy_kb`, loaded by `orchestrator.kb_engine`).

Four commands, because only these are work a prompt cannot do: `build` (index + dated
diff), `push` / `pull` (a workspace project's KB goes to canopy-web, which serves it as
the project's own remote MCP server — `push` sends only the documents that changed, as a
new version, and the whole file only for a first build; any other KB's copy lives in its
Drive folder) and `mcp` (serve a local copy over stdio). Reading is the MCP tools' job,
and HOW to read a given library — what to cite, which table beats prose — is a skill's (e.g. Eva's `givewell-kb`). There are
deliberately no read subcommands duplicating the tools.

Which KB: `--kb` (or $CANOPY_KB) is one of
  * a path to a kb.sqlite                       (local; nothing is fetched)
  * `<workspace>/<handle>`, e.g. `connect/givewell-kb`  (a workspace project — the usual case)
  * `<agent>/<project ref>`, e.g. `eva/P12`     (the project's Drive folder, from canopy-web)
  * a Drive folder id                            (the folder holding kb.sqlite)
A remote KB is cached at ~/.canopy/kb/<name>/kb.sqlite; `pull` refreshes it, and `mcp`
pulls on its own when the cache is older than --max-age-hours.
Drive is read as the AGENT whose repo you run from (`--repo`, default: cwd), through the
same identity and gog client rule every canopy Drive call uses.

A workspace project's push can carry the bundle's ORIGINALS (`--originals <bundle>`): the
files each item's text was read from (PDFs, spreadsheets, pages). They go to canopy-web's
store by sha256 before the build is published, only those it lacks, and the project's
`original` tool hands readers the files. The text is the index; the original is the record.

`canopy kb mcp --kb connect/givewell-kb` serves the same tools canopy-web serves, over stdio
(offline use; readers normally add the project's remote MCP URL). A script that wants a
query uses `canopy_kb.KB` (or `open_kb` here) directly.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import subprocess
import gzip
import shutil
import tempfile
from pathlib import Path

import click

from orchestrator.kb_engine import KBError, build, open_file, summary

CACHE_ROOT = Path(os.environ.get("CANOPY_KB_CACHE", Path.home() / ".canopy" / "kb"))
KB_FILE = "kb.sqlite"
KB_REMOTE = "kb.sqlite.gz"  # what lives in Drive: SQLite text compresses ~3-4x, and every reader downloads it
_DRIVE_ID = re.compile(r"^[A-Za-z0-9_-]{20,}$")
_PROJECT_REF = re.compile(r"^([a-z0-9][a-z0-9-]*)/([A-Za-z]*\d+)$")
# A workspace project's address: owned by the workspace, kept current by a steward
# agent, so it survives a change of steward (canopy-web workspace projects).
_WORKSPACE_REF = re.compile(r"^([a-z0-9][a-z0-9-]*)/([a-z0-9][a-z0-9-]{1,58}[a-z0-9])$")


def _emit(obj) -> None:
    click.echo(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


# -- resolving and fetching ----------------------------------------------------------

def _kind(ref: str) -> str:
    if ref.endswith(".sqlite") or Path(ref).expanduser().is_file():
        return "local"
    if _PROJECT_REF.match(ref):
        return "project"
    if _WORKSPACE_REF.match(ref):
        return "workspace-project"
    if _DRIVE_ID.match(ref):
        return "folder"
    raise KBError(f"--kb {ref!r}: expected a kb.sqlite path, <workspace>/<handle>, "
                  "<agent>/<project ref>, or a Drive folder id")


def _cache_dir(ref: str) -> Path:
    return CACHE_ROOT / re.sub(r"[^A-Za-z0-9_.-]+", "_", ref)


def _folder_for(ref: str) -> str:
    if _kind(ref) == "folder":
        return ref
    from orchestrator import canopy_web
    if _kind(ref) == "workspace-project":
        ws, handle = _WORKSPACE_REF.match(ref).groups()
        proj = canopy_web.call("GET", f"/api/workspace-projects/{ws}/{handle}/") or {}
    else:
        agent, pref = _PROJECT_REF.match(ref).groups()
        proj = canopy_web.call("GET", f"/api/agents/{agent}/projects/{pref}/") or {}
    fid = proj.get("drive_folder_id") or ""
    if not fid:
        raise KBError(f"project {ref} has no Drive folder on canopy-web")
    return fid


class _Drive:
    def __init__(self, repo: Path, runner=subprocess.run):
        from orchestrator.agent_email import reconcile_client
        from orchestrator.agent_gdoc import resolve_gdoc_identity
        self.identity = resolve_gdoc_identity(Path(repo))
        reconcile_client(self.identity, runner=runner, apply=True)
        self.runner = runner

    def gog(self, *args: str):
        cmd = ["gog", *args, "--account", self.identity.account, "--client", self.identity.client,
               "-j", "--results-only"]
        r = self.runner(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise KBError(f"`gog {' '.join(args[:2])}` failed as {self.identity.account}: "
                          f"{(r.stderr or r.stdout or '').strip()[:300]}")
        out = (r.stdout or "").strip()
        try:
            return json.loads(out) if out else None
        except json.JSONDecodeError:
            return out

    def find(self, folder: str, name: str) -> dict | None:
        rows = self.gog("drive", "ls", "--parent", folder,
                        "--query", f"name = '{name}' and trashed = false") or []
        return rows[0] if rows else None


def pull(ref: str, repo: Path, force: bool = False) -> dict:
    """Fetch the KB's kb.sqlite into the local cache if a newer one is published: a
    workspace project's from canopy-web (what its MCP serves), any other's from Drive."""
    if _kind(ref) == "local":
        return {"kb": ref, "path": str(Path(ref).expanduser()), "pulled": False, "reason": "local file"}
    if _kind(ref) == "workspace-project":
        return _pull_from_canopy(ref, force)
    folder = _folder_for(ref)
    d = _Drive(repo)
    f = d.find(folder, KB_REMOTE) or d.find(folder, KB_FILE)
    if not f:
        raise KBError(f"no {KB_REMOTE} in Drive folder {folder} (has the curator pushed a build?)")
    cdir = _cache_dir(ref)
    cdir.mkdir(parents=True, exist_ok=True)
    stamp_path = cdir / "pulled.json"
    stamp = json.loads(stamp_path.read_text(encoding="utf-8")) if stamp_path.exists() else {}
    remote_mod = f.get("modifiedTime") or f.get("modified") or ""
    if not force and (cdir / KB_FILE).exists() and stamp.get("file_id") == f["id"] and stamp.get("modified") == remote_mod:
        stamp.update(checked_at=_now())
        stamp_path.write_text(json.dumps(stamp, indent=2), encoding="utf-8")
        return {"kb": ref, "path": str(cdir / KB_FILE), "pulled": False, "reason": "cache is current"}
    tmp = cdir / (KB_FILE + ".download")
    raw = cdir / (KB_FILE + ".raw")
    d.gog("drive", "download", f["id"], "--out", str(raw))
    with open(raw, "rb") as fh:
        gz = fh.read(2) == b"\x1f\x8b"
    if gz:
        with gzip.open(raw, "rb") as src, open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        raw.unlink()
    else:
        raw.replace(tmp)
    summary(tmp)  # refuse a file that is not a knowledge base before replacing the cache
    tmp.replace(cdir / KB_FILE)
    stamp = {"file_id": f["id"], "folder": folder, "modified": remote_mod, "pulled_at": _now(), "checked_at": _now()}
    stamp_path.write_text(json.dumps(stamp, indent=2), encoding="utf-8")
    return {"kb": ref, "path": str(cdir / KB_FILE), "pulled": True, "modified": remote_mod}


def push(ref: str, repo: Path, path: Path, originals: Path | None = None) -> dict:
    """Publish a built kb.sqlite (curator only).

    A workspace project's goes to canopy-web, which serves it as the project's own
    MCP: its originals first (when given), then the whole file, as a new version made
    from the one it serves now. canopy-web never builds an index (canopy-web#1971). A
    curator that refreshes incrementally does it through `canopy_kb.curate` (e.g. eva's
    GiveWell refresh), which also skips an unchanged publish;
    this is the hand-built file's path. Nothing is written to Drive (a project that
    wants a Drive copy turns its replica on, and canopy-web writes it). Any other KB's
    store is its Drive folder."""
    built = summary(path)
    kind = _kind(ref)
    if originals is not None and kind != "workspace-project":
        raise KBError("--originals needs a workspace project (<workspace>/<handle>): canopy-web stores them")
    if kind != "workspace-project":
        return _push_to_drive(ref, repo, path, built)
    out = {"kb": ref, "summary": built}
    if originals is not None:
        out["originals"] = upload_originals(ref, originals)
    out.update(publish_to_canopy(ref, path))
    return out


def _push_to_drive(ref: str, repo: Path, path: Path, built: dict) -> dict:
    folder = _folder_for(ref)
    d = _Drive(repo)
    f = d.find(folder, KB_REMOTE)
    with tempfile.TemporaryDirectory() as tdir:
        gzpath = Path(tdir) / KB_REMOTE
        _gzip(path, gzpath)
        if f:
            d.gog("drive", "upload", str(gzpath), "--replace", f["id"])
        else:
            d.gog("drive", "upload", str(gzpath), "--name", KB_REMOTE, "--parent", folder)
        size = gzpath.stat().st_size
    return {"kb": ref, "folder": folder, "replaced": bool(f), "bytes": size, "summary": built}


def _gzip(src: Path, dst: Path) -> None:
    with open(src, "rb") as fh, gzip.open(dst, "wb", compresslevel=6) as out:
        shutil.copyfileobj(fh, out, 1 << 20)


ORIGINALS_BATCH = 1000  # canopy-web's per-request limit
ORIGINALS_WORKERS = 8


def collect_originals(bundle: Path) -> dict[str, dict]:
    """sha256 -> {path, bytes, content_type} for every item's `original` in a bundle. A
    file that is missing on disk refuses the push: a published build would point at it."""
    found: dict[str, dict] = {}
    missing = []
    with open(Path(bundle) / "items.jsonl", encoding="utf-8") as fh:
        for line in fh:
            if '"original"' not in line:
                continue
            o = json.loads(line).get("original")
            if not isinstance(o, dict) or not o.get("sha256") or o["sha256"] in found:
                continue
            p = Path(o.get("path") or "")
            if not o.get("path") or not p.is_file():
                missing.append(o.get("path") or o["sha256"])
                continue
            found[o["sha256"]] = {"path": p, "bytes": p.stat().st_size,
                                  "content_type": o.get("content_type") or "application/octet-stream"}
    if missing:
        raise KBError(f"{len(missing)} originals named in {bundle}/items.jsonl are not on disk "
                      f"(first: {missing[0]}); re-run the curator's fetch, or push without --originals")
    return found


def upload_originals(ref: str, bundle: Path) -> dict:
    """Upload the originals canopy-web's store lacks for this workspace project (by sha256,
    so an unchanged file never uploads twice). Run before publishing the build."""
    import concurrent.futures as cf

    from orchestrator import canopy_web

    ws, handle = _WORKSPACE_REF.match(ref).groups()
    files = collect_originals(bundle)
    shas = sorted(files)
    have = uploaded = sent = 0
    for i in range(0, len(shas), ORIGINALS_BATCH):
        batch = [{"sha256": h, "bytes": files[h]["bytes"], "content_type": files[h]["content_type"]}
                 for h in shas[i:i + ORIGINALS_BATCH]]
        try:
            got = canopy_web.call("POST", f"/api/workspace-projects/{ws}/{handle}/kb/originals", {"files": batch}) or {}
        except canopy_web.CanopyError as e:
            if "-> 404" in str(e) or "-> 405" in str(e):
                raise KBError("this canopy-web does not take originals yet (it predates them); "
                              "push without --originals until it is deployed") from e
            raise
        have += got.get("have", 0)
        ups = got.get("uploads") or []
        with cf.ThreadPoolExecutor(ORIGINALS_WORKERS) as ex:
            list(ex.map(lambda u: _put(u["url"], files[u["sha256"]]["path"], u.get("headers") or {}), ups))
        uploaded += len(ups)
        sent += sum(files[u["sha256"]]["bytes"] for u in ups)
    return {"files": len(shas), "already_stored": have, "uploaded": uploaded, "bytes_uploaded": sent}


def _put(url: str, path: Path, headers: dict) -> None:
    import urllib.request

    with open(path, "rb") as fh:
        req = urllib.request.Request(url, data=fh, method="PUT",
                                     headers={**headers, "Content-Length": str(path.stat().st_size)})
        with urllib.request.urlopen(req, timeout=600) as resp:
            if resp.status >= 300:
                raise KBError(f"upload to storage failed: HTTP {resp.status}")




def publish_to_canopy(ref: str, path: Path) -> dict:
    """Publish a workspace project's KB to canopy-web: the whole file this machine built,
    as a version made from the one the project serves now."""
    from orchestrator import canopy_web
    from orchestrator.kb_remote import RemoteStore

    ws, handle = _WORKSPACE_REF.match(ref).groups()
    base = f"/api/workspace-projects/{ws}/{handle}/kb"
    mcp_url = canopy_web.resolve_base_url(None).rstrip("/") + f"/api/mcp/projects/{ws}/{handle}/"
    return _publish_full(base, path, RemoteStore(ref).head()["version"], mcp_url)


def _publish_full(base: str, path: Path, head: int | None, mcp_url: str) -> dict:
    import hashlib

    from orchestrator import canopy_web

    with tempfile.TemporaryDirectory() as tdir:
        gzpath = Path(tdir) / KB_REMOTE
        _gzip(path, gzpath)
        target = canopy_web.call("POST", f"{base}/upload") or {}
        if not target.get("url"):
            raise KBError(f"canopy-web gave no upload target for {base}")
        _put(target["url"], gzpath, target.get("headers") or {})
        digest = hashlib.sha256()
        with open(gzpath, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        try:
            proj = canopy_web.call("POST", f"{base}/publish",
                                   {"key": target["key"], "sha256": digest.hexdigest(), "base": head}) or {}
        except canopy_web.CanopyError as e:
            if "-> 409" in str(e):
                raise KBError(f"another version was published while this one uploaded; push again: {e}") from e
            raise
        size = gzpath.stat().st_size
    return {"published": "full", "version": proj.get("kb_version_number"), "bytes_uploaded": size,
            "mcp_url": mcp_url if proj.get("mcp_path") is None else
            canopy_web.resolve_base_url(None).rstrip("/") + (proj.get("mcp_path") or "")}


def _pull_from_canopy(ref: str, force: bool) -> dict:
    """The file a workspace project serves now, from canopy-web, cached by its sha256."""
    import urllib.request

    from orchestrator import canopy_web

    ws, handle = _WORKSPACE_REF.match(ref).groups()
    got = canopy_web.call("GET", f"/api/workspace-projects/{ws}/{handle}/kb/file") or {}
    cdir = _cache_dir(ref)
    cdir.mkdir(parents=True, exist_ok=True)
    stamp_path = cdir / "pulled.json"
    stamp = json.loads(stamp_path.read_text(encoding="utf-8")) if stamp_path.exists() else {}
    if not force and (cdir / KB_FILE).exists() and stamp.get("sha256") == got.get("sha256"):
        stamp.update(checked_at=_now())
        stamp_path.write_text(json.dumps(stamp, indent=2), encoding="utf-8")
        return {"kb": ref, "path": str(cdir / KB_FILE), "pulled": False, "reason": "cache is current",
                "version": got.get("version")}
    raw, tmp = cdir / (KB_FILE + ".gz.download"), cdir / (KB_FILE + ".download")
    with urllib.request.urlopen(got["url"], timeout=600) as resp, open(raw, "wb") as out:
        shutil.copyfileobj(resp, out, 1 << 20)
    with gzip.open(raw, "rb") as src, open(tmp, "wb") as dst:
        shutil.copyfileobj(src, dst, 1 << 20)
    raw.unlink()
    summary(tmp)
    tmp.replace(cdir / KB_FILE)
    stamp = {"sha256": got.get("sha256"), "version": got.get("version"), "pulled_at": _now(), "checked_at": _now()}
    stamp_path.write_text(json.dumps(stamp, indent=2), encoding="utf-8")
    return {"kb": ref, "path": str(cdir / KB_FILE), "pulled": True, "version": got.get("version")}


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def open_kb(ref: str, repo: Path, max_age_hours: float = 24.0):
    """Open a KB, pulling first when the cache is missing or stale. Returns (kb, warning)."""
    if _kind(ref) == "local":
        return open_file(Path(ref).expanduser()), ""
    cdir = _cache_dir(ref)
    stamp_path = cdir / "pulled.json"
    stale = True
    if (cdir / KB_FILE).exists() and stamp_path.exists():
        checked = json.loads(stamp_path.read_text(encoding="utf-8")).get("checked_at", "")
        try:
            age = _dt.datetime.now(_dt.timezone.utc) - _dt.datetime.strptime(checked, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
            stale = age.total_seconds() > max_age_hours * 3600
        except ValueError:
            stale = True
    warning = ""
    if stale:
        try:
            pull(ref, repo)
        except Exception as e:  # serve the cache rather than nothing, but say so
            if not (cdir / KB_FILE).exists():
                raise
            warning = f"could not refresh from Drive ({e}); serving the cached copy"
    return open_file(cdir / KB_FILE), warning


# -- CLI -----------------------------------------------------------------------------

def _kb_option(f):
    f = click.option("--kb", "kb_ref", default=lambda: os.environ.get("CANOPY_KB", ""),
                     help="kb.sqlite path, <agent>/<project ref>, or Drive folder id ($CANOPY_KB)")(f)
    f = click.option("--repo", type=click.Path(path_type=Path), default=Path("."),
                     help="agent repo whose Google identity reads Drive (default: cwd)")(f)
    f = click.option("--max-age-hours", type=float, default=24.0, help="re-check Drive after this long")(f)
    return f


def _guard(fn):
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        except KBError as e:
            raise click.ClickException(str(e))
    return wrapper


@click.group("kb")
def kb_group():
    """Knowledge bases: build, store and serve a shared, cited library (read it through `mcp`)."""


@kb_group.command("build")
@click.argument("bundle", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--out", type=click.Path(path_type=Path), required=True, help="kb.sqlite to write")
@click.option("--previous", type=click.Path(path_type=Path), default=None,
              help="last build, for change history (default: --out if it exists)")
@_guard
def build_cmd(bundle, out, previous):
    """Build kb.sqlite from a curator's bundle directory."""
    if previous is None and out.exists():
        previous = out
    _emit(build(bundle, out, previous))


@kb_group.command("pull")
@_kb_option
@click.option("--force", is_flag=True)
@_guard
def pull_cmd(kb_ref, repo, max_age_hours, force):
    """Fetch the KB from Drive into the local cache."""
    _emit(pull(kb_ref, repo, force=force))


@kb_group.command("push")
@_kb_option
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--originals", type=click.Path(exists=True, file_okay=False, path_type=Path), default=None,
              help="the bundle the build came from: upload each item's original file first (workspace projects)")
@_guard
def push_cmd(kb_ref, repo, max_age_hours, path, originals):
    """Publish a built kb.sqlite (curators). A workspace project's goes to canopy-web, which
    serves it as the project's MCP: originals first (--originals), then the whole file,
    as a new version. Other KBs go to their Drive folder."""
    _emit(push(kb_ref, repo, path, originals))


@click.command("mcp-headers")
def mcp_headers_cmd():
    """Print the auth header for a canopy remote MCP server, as a `headersHelper`.

    For an agent repo's `.mcp.json` entry pointing at a project's own server:
    `{"type": "http", "url": "…/api/mcp/projects/<ws>/<handle>", "headersHelper":
    "canopy mcp-headers"}`. It runs the canopy plugin's own helper, so who the call
    acts as (the agent itself, a confined caller, else a browser sign-in) is decided
    in ONE place, not restated here. No helper found → `{}`, and the client signs in."""
    import glob

    roots = sorted(glob.glob(str(Path.home() / ".claude/plugins/cache/canopy/canopy/*/scripts/canopy-web-mcp-headers.js")),
                   key=lambda p: [int(x) if x.isdigit() else x for x in re.split(r"[./]", p)])
    roots += [str(Path.home() / ".claude/plugins/marketplaces/canopy/plugins/canopy/scripts/canopy-web-mcp-headers.js")]
    for helper in reversed(roots):
        if Path(helper).is_file():
            r = subprocess.run(["node", helper], capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                click.echo(r.stdout.strip())
                return
    click.echo("{}")


@kb_group.command("mcp")
@_kb_option
@click.option("--name", default="", help="server name (default: the KB's manifest name)")
def mcp_cmd(kb_ref, repo, max_age_hours, name):
    """Serve this KB as MCP tools over stdio."""
    from orchestrator.kb_mcp import serve
    if not kb_ref:
        raise click.UsageError("name a knowledge base with --kb (or $CANOPY_KB)")
    serve(lambda: open_kb(kb_ref, repo, max_age_hours), name=name)
