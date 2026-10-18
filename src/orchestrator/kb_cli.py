"""`canopy kb …` — build, fetch and query a knowledge base (see `orchestrator.kb`).

Which KB: `--kb` (or $CANOPY_KB) is one of
  * a path to a kb.sqlite                       (local; nothing is fetched)
  * `<agent>/<project ref>`, e.g. `eva/P12`     (the project's Drive folder, from canopy-web)
  * a Drive folder id                            (the folder holding kb.sqlite)
A remote KB is cached at ~/.canopy/kb/<name>/kb.sqlite; `pull` refreshes it, and the
query commands pull on their own when the cache is older than --max-age-hours.
Drive is read as the AGENT whose repo you run from (`--repo`, default: cwd), through the
same identity and gog client rule every canopy Drive call uses.

`canopy kb mcp --kb eva/P12` serves the same queries as MCP tools over stdio, so an agent
registers it once in its .mcp.json and gets search/get/tables/whats_new natively.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import subprocess
import sys
import gzip
import shutil
import tempfile
from pathlib import Path

import click

from orchestrator.kb import KB, KBError, build

CACHE_ROOT = Path(os.environ.get("CANOPY_KB_CACHE", Path.home() / ".canopy" / "kb"))
KB_FILE = "kb.sqlite"
KB_REMOTE = "kb.sqlite.gz"  # what lives in Drive: SQLite text compresses ~3-4x, and every reader downloads it
_DRIVE_ID = re.compile(r"^[A-Za-z0-9_-]{20,}$")
_PROJECT_REF = re.compile(r"^([a-z0-9][a-z0-9-]*)/([A-Za-z]*\d+)$")


def _emit(obj) -> None:
    click.echo(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


# -- resolving and fetching ----------------------------------------------------------

def _kind(ref: str) -> str:
    if ref.endswith(".sqlite") or Path(ref).expanduser().is_file():
        return "local"
    if _PROJECT_REF.match(ref):
        return "project"
    if _DRIVE_ID.match(ref):
        return "folder"
    raise KBError(f"--kb {ref!r}: expected a kb.sqlite path, <agent>/<project ref>, or a Drive folder id")


def _cache_dir(ref: str) -> Path:
    return CACHE_ROOT / re.sub(r"[^A-Za-z0-9_.-]+", "_", ref)


def _folder_for(ref: str) -> str:
    if _kind(ref) == "folder":
        return ref
    from orchestrator import canopy_web
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
    """Fetch the KB's kb.sqlite into the local cache if Drive has a newer one."""
    if _kind(ref) == "local":
        return {"kb": ref, "path": str(Path(ref).expanduser()), "pulled": False, "reason": "local file"}
    folder = _folder_for(ref)
    d = _Drive(repo)
    f = d.find(folder, KB_REMOTE) or d.find(folder, KB_FILE)
    if not f:
        raise KBError(f"no {KB_REMOTE} in Drive folder {folder} (has the curator pushed a build?)")
    cdir = _cache_dir(ref)
    cdir.mkdir(parents=True, exist_ok=True)
    stamp_path = cdir / "pulled.json"
    stamp = json.loads(stamp_path.read_text()) if stamp_path.exists() else {}
    remote_mod = f.get("modifiedTime") or f.get("modified") or ""
    if not force and (cdir / KB_FILE).exists() and stamp.get("file_id") == f["id"] and stamp.get("modified") == remote_mod:
        stamp.update(checked_at=_now())
        stamp_path.write_text(json.dumps(stamp, indent=2))
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
    KB(tmp)  # refuse a file that is not a readable KB before replacing the cache
    tmp.replace(cdir / KB_FILE)
    stamp = {"file_id": f["id"], "folder": folder, "modified": remote_mod, "pulled_at": _now(), "checked_at": _now()}
    stamp_path.write_text(json.dumps(stamp, indent=2))
    return {"kb": ref, "path": str(cdir / KB_FILE), "pulled": True, "modified": remote_mod}


def push(ref: str, repo: Path, path: Path) -> dict:
    """Upload a built kb.sqlite into the KB's Drive folder (curator only)."""
    KB(path)
    folder = _folder_for(ref)
    d = _Drive(repo)
    f = d.find(folder, KB_REMOTE)
    with tempfile.TemporaryDirectory() as tdir:
        gzpath = Path(tdir) / KB_REMOTE
        with open(path, "rb") as src, gzip.open(gzpath, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        if f:
            d.gog("drive", "upload", str(gzpath), "--replace", f["id"])
        else:
            d.gog("drive", "upload", str(gzpath), "--name", KB_REMOTE, "--parent", folder)
        size = gzpath.stat().st_size
    return {"kb": ref, "folder": folder, "replaced": bool(f), "bytes": size, "summary": KB(path).summary}


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def open_kb(ref: str, repo: Path, max_age_hours: float = 24.0) -> tuple[KB, str]:
    """Open a KB, pulling first when the cache is missing or stale. Returns (kb, warning)."""
    if _kind(ref) == "local":
        return KB(Path(ref).expanduser()), ""
    cdir = _cache_dir(ref)
    stamp_path = cdir / "pulled.json"
    stale = True
    if (cdir / KB_FILE).exists() and stamp_path.exists():
        checked = json.loads(stamp_path.read_text()).get("checked_at", "")
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
    return KB(cdir / KB_FILE), warning


# -- CLI -----------------------------------------------------------------------------

def _kb_option(f):
    f = click.option("--kb", "kb_ref", default=lambda: os.environ.get("CANOPY_KB", ""),
                     help="kb.sqlite path, <agent>/<project ref>, or Drive folder id ($CANOPY_KB)")(f)
    f = click.option("--repo", type=click.Path(path_type=Path), default=Path("."),
                     help="agent repo whose Google identity reads Drive (default: cwd)")(f)
    f = click.option("--max-age-hours", type=float, default=24.0, help="re-check Drive after this long")(f)
    return f


def _open(kb_ref: str, repo: Path, max_age_hours: float) -> KB:
    if not kb_ref:
        raise click.UsageError("name a knowledge base with --kb (or $CANOPY_KB)")
    kb, warning = open_kb(kb_ref, repo, max_age_hours)
    if warning:
        click.echo(f"warning: {warning}", err=True)
    return kb


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
    """Knowledge bases: build, fetch and query a shared, cited library."""


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
@_guard
def push_cmd(kb_ref, repo, max_age_hours, path):
    """Upload a built kb.sqlite to the KB's Drive folder (curators)."""
    _emit(push(kb_ref, repo, path))


@kb_group.command("info")
@_kb_option
@_guard
def info_cmd(kb_ref, repo, max_age_hours):
    """What this KB holds: sources, counts, tables, build date."""
    _emit(_open(kb_ref, repo, max_age_hours).info())


@kb_group.command("search")
@_kb_option
@click.argument("query")
@click.option("--type", "types", multiple=True, help="limit to item types (repeatable)")
@click.option("--since", default="")
@click.option("--until", default="")
@click.option("--limit", type=int, default=10)
@_guard
def search_cmd(kb_ref, repo, max_age_hours, query, types, since, until, limit):
    """Best passages for QUERY, each with its source URL. "Quote" exact phrases."""
    _emit(_open(kb_ref, repo, max_age_hours).search(query, list(types) or None, since, until, limit))


@kb_group.command("get")
@_kb_option
@click.argument("ref")
@click.option("--max-chars", type=int, default=20000)
@click.option("--passage", type=int, default=None)
@_guard
def get_cmd(kb_ref, repo, max_age_hours, ref, max_chars, passage):
    """One item (by id or URL): full text, metadata, fetch date."""
    _emit(_open(kb_ref, repo, max_age_hours).get(ref, max_chars, passage))


@kb_group.command("list")
@_kb_option
@click.option("--type", "types", multiple=True)
@click.option("--since", default="")
@click.option("--limit", type=int, default=50)
@_guard
def list_cmd(kb_ref, repo, max_age_hours, types, since, limit):
    """Items, newest first."""
    _emit(_open(kb_ref, repo, max_age_hours).list_items(list(types) or None, since, limit))


@kb_group.command("table")
@_kb_option
@click.argument("name", required=False)
@click.option("--match", multiple=True, help="COLUMN=substring (repeatable)")
@click.option("--text", default="", help="substring anywhere in the row")
@click.option("--since", default="")
@click.option("--until", default="")
@click.option("--min-amount", type=float, default=None)
@click.option("--sort", default="", help="column; prefix - for descending")
@click.option("--limit", type=int, default=50)
@_guard
def table_cmd(kb_ref, repo, max_age_hours, name, match, text, since, until, min_amount, sort, limit):
    """Query a structured table (no NAME: list the tables)."""
    kb = _open(kb_ref, repo, max_age_hours)
    if not name:
        _emit(kb.tables())
        return
    m = {}
    for kv in match:
        if "=" not in kv:
            raise click.UsageError(f"--match {kv!r}: expected COLUMN=substring")
        k, v = kv.split("=", 1)
        m[k] = v
    _emit(kb.table(name, m, text, since, until, min_amount, sort, limit))


@kb_group.command("whats-new")
@_kb_option
@click.option("--since", default="", help="ISO date/time")
@click.option("--limit", type=int, default=100)
@_guard
def whats_new_cmd(kb_ref, repo, max_age_hours, since, limit):
    """Items that appeared or changed since a date."""
    _emit(_open(kb_ref, repo, max_age_hours).whats_new(since, limit))


@kb_group.command("mcp")
@_kb_option
@click.option("--name", default="", help="server name (default: the KB's manifest name)")
def mcp_cmd(kb_ref, repo, max_age_hours, name):
    """Serve this KB as MCP tools over stdio."""
    from orchestrator.kb_mcp import serve
    if not kb_ref:
        raise click.UsageError("name a knowledge base with --kb (or $CANOPY_KB)")
    serve(lambda: open_kb(kb_ref, repo, max_age_hours), name=name)
