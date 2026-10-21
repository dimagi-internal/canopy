"""`canopy kb …` — build, store and serve a knowledge base (engine: canopy-web's
`canopy_kb`, loaded by `orchestrator.kb_engine`).

Four commands, because only these are work a prompt cannot do: `build` (index + dated
diff), `push` / `pull` (the gzipped copy in the project's Drive folder — and for a
workspace project, `push` also publishes it to canopy-web, which then serves it as the
project's own remote MCP server) and `mcp` (serve a local copy over stdio). Reading is the MCP tools' job, and HOW to read a given library — what to
cite, which table beats prose — is a skill's (e.g. Eva's `givewell-kb`). There are
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


def push(ref: str, repo: Path, path: Path) -> dict:
    """Upload a built kb.sqlite into the KB's Drive folder (curator only)."""
    built = summary(path)
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
        served = publish_to_canopy(ref, gzpath) if _kind(ref) == "workspace-project" else None
    out = {"kb": ref, "folder": folder, "replaced": bool(f), "bytes": size, "summary": built}
    if served:
        out["mcp_url"] = served
    return out


def _put(url: str, path: Path, headers: dict) -> None:
    import urllib.request

    with open(path, "rb") as fh:
        req = urllib.request.Request(url, data=fh, method="PUT",
                                     headers={**headers, "Content-Length": str(path.stat().st_size)})
        with urllib.request.urlopen(req, timeout=600) as resp:
            if resp.status >= 300:
                raise KBError(f"upload to storage failed: HTTP {resp.status}")


def publish_to_canopy(ref: str, gzpath: Path) -> str:
    """Publish a workspace project's KB to canopy-web, which then serves it as the
    project's own MCP server — readers call it remotely and download nothing.
    The bytes go straight to storage on a presigned PUT. Returns the MCP URL."""
    import hashlib

    from orchestrator import canopy_web

    ws, handle = _WORKSPACE_REF.match(ref).groups()
    base = f"/api/workspace-projects/{ws}/{handle}/kb"
    target = canopy_web.call("POST", f"{base}/upload") or {}
    if not target.get("url"):
        raise KBError(f"canopy-web gave no upload target for {ref}")
    _put(target["url"], gzpath, target.get("headers") or {})
    digest = hashlib.sha256()
    with open(gzpath, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    proj = canopy_web.call("POST", f"{base}/publish", {"key": target["key"], "sha256": digest.hexdigest()}) or {}
    return canopy_web.resolve_base_url(None).rstrip("/") + (proj.get("mcp_path") or "")


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
@_guard
def push_cmd(kb_ref, repo, max_age_hours, path):
    """Upload a built kb.sqlite to the KB's Drive folder (curators)."""
    _emit(push(kb_ref, repo, path))


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
