"""Materialize a stored agent's revision into a local git repo with ONE deterministic commit.

    canopy agent materialize --slug <slug> --dest <dir> [--revision N|live] [--runner]
    canopy agent sync --slug <slug> [--runner] [--dest <dir>]

A stored agent has no GitHub repo: its definition is a file tree with revisions in
canopy-web (docs/architecture/stored-agents.md). Everything downstream of "a git clone at
AGENT_ROOT/<slug>" — worktrees, `claude plugin marketplace add <dir>`, the fleet updater's
`refresh_plugin` / `off_release_branch` — keeps working because this module turns the
revision into exactly that:

  * fetch ``GET /api/agents/<slug>/definition/archive?revision=N|live`` (a gzip tar, files
    only) with headers X-Canopy-Revision / X-Canopy-Root-Sha256 / X-Canopy-Published-At;
  * verify the root (``agent_definition.manifest_root``) BEFORE anything is written;
  * write the blobs, a tree and one ORPHAN commit into the dest's object store — author and
    committer ``canopy <agents@canopy.dimagi.com>``, both dates = Published-At, message
    ``canopy revision <N>`` — so one revision is one sha on every machine;
  * point ``refs/remotes/origin/main`` at it (so `off_release_branch` passes), origin's url at
    ``canopy://<workspace>/<slug>`` (never fetched by git), and only then swap the working tree
    with ``git reset --hard``: tracked files replaced exactly, tracked files absent from the
    revision deleted, untracked and ignored files (``.env``, ``node_modules``, state) kept.
    A dest that does not exist yet is built in a sibling staging dir and renamed into place.

Nothing is written when the download or the root check fails, and nothing at all when the
dest is already at that revision (the JSON line then says ``"changed": false``).
"""
from __future__ import annotations

import datetime as dt
import email.utils
import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from orchestrator import canopy_web
from orchestrator.agent_definition import file_entry, manifest_root, path_problem

AUTHOR = "canopy"
AUTHOR_EMAIL = "agents@canopy.dimagi.com"
ORIGIN_SCHEME = "canopy://"
#: A tar bigger than this (decompressed) is refused before it is read — the policy caps a
#: tree at 200 files / 2 MB; twice that is slack, not an invitation.
MAX_ARCHIVE_FILES = 400
MAX_ARCHIVE_BYTES = 4 * 1024 * 1024

#: (url, headers) -> (status, {lower-case header: value}, body bytes). Raises OSError when
#: there is no answer at all.
Fetch = Callable[[str, dict], "tuple[int, dict, bytes]"]


class MaterializeError(RuntimeError):
    """The revision could not be fetched, verified or written. Nothing was changed."""


@dataclass
class Archive:
    revision: int
    root_sha256: str
    published_at: str
    files: dict = field(default_factory=dict)   # path -> (bytes, executable)
    workspace: str = ""


# ── fetching ─────────────────────────────────────────────────────────────────────────────

def urllib_fetch(url: str, headers: dict) -> "tuple[int, dict, bytes]":
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=canopy_web.request_timeout(None) + 60) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read()


def resolve_token(runner: bool, base_url: str) -> str:
    """The token to read a definition with. ``runner``: the runner's (``$CANOPY_TOKEN``, as
    `agent prepare --runner` uses), else this box's runner session. Otherwise the person's
    (or, in a turn, the agent's) canopy-web login, then the runner session as a last resort."""
    if runner:
        tok = os.environ.get("CANOPY_TOKEN", "").strip()
        if tok:
            return tok
    try:
        return canopy_web.resolve_token(None)
    except RuntimeError as e:
        from orchestrator import runner_session

        tok = runner_session.token(base_url)
        if tok:
            return tok
        raise MaterializeError(f"no canopy-web token to read the definition with: {e}") from e


def archive_path(slug: str, revision: str = "live") -> str:
    q = urllib.parse.urlencode({"revision": str(revision or "live")})
    return f"/api/agents/{urllib.parse.quote(slug)}/definition/archive?{q}"


def fetch_archive(slug: str, *, revision: str = "live", base_url: str, token: str,
                  fetch: Optional[Fetch] = None) -> Archive:
    """Download and VERIFY one revision. Raises MaterializeError; never writes."""
    from orchestrator.provenance import provenance_headers

    headers = {**provenance_headers(), "Authorization": f"Bearer {token}",
               "Accept": "application/gzip"}
    url = base_url.rstrip("/") + archive_path(slug, revision)
    try:
        status, rh, body = (fetch or urllib_fetch)(url, headers)
    except OSError as e:
        raise MaterializeError(f"GET {archive_path(slug, revision)}: no answer ({e})") from e
    if not (200 <= status < 300):
        text = body[:300].decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
        raise MaterializeError(f"GET {archive_path(slug, revision)} -> {status}: {text}")
    rh = {k.lower(): v for k, v in (rh or {}).items()}
    try:
        number = int(str(rh.get("x-canopy-revision") or "").strip())
    except ValueError:
        raise MaterializeError("the archive carries no X-Canopy-Revision") from None
    root = str(rh.get("x-canopy-root-sha256") or "").strip().lower()
    published = str(rh.get("x-canopy-published-at") or "").strip()
    if not root or not published:
        raise MaterializeError("the archive lacks X-Canopy-Root-Sha256 or X-Canopy-Published-At")
    if str(revision) not in ("", "live") and str(number) != str(revision).strip():
        raise MaterializeError(f"asked for revision {revision}, canopy-web sent {number}")
    files = read_tar(body)
    got = manifest_root([file_entry(p, c, x) for p, (c, x) in files.items()])
    if got != root:
        raise MaterializeError(f"revision {number}'s files do not match its root: canopy-web "
                               f"says {root[:12]}…, the archive hashes to {got[:12]}… — "
                               "nothing was written")
    return Archive(revision=number, root_sha256=root, published_at=published, files=files,
                   workspace=str(rh.get("x-canopy-workspace") or "").strip())


def read_tar(data: bytes) -> dict:
    """{path: (bytes, executable)} from a gzip tar of regular files. Anything else — a link,
    a device, a path that escapes, a duplicate, a bomb — is refused."""
    try:
        tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    except (tarfile.TarError, OSError, EOFError) as e:
        raise MaterializeError(f"the archive is not a readable tar: {e}") from e
    files: dict = {}
    total = 0
    with tf:
        for m in tf.getmembers():
            name = m.name[2:] if m.name.startswith("./") else m.name
            name = name.rstrip("/") if m.isdir() else name
            if m.isdir():
                continue
            if not m.isreg():
                raise MaterializeError(f"{name}: not a regular file (links are refused)")
            why = path_problem(name)
            if why:
                raise MaterializeError(f"{name!r}: {why}")
            if name in files:
                raise MaterializeError(f"{name}: twice in the archive")
            total += m.size
            if len(files) >= MAX_ARCHIVE_FILES or total > MAX_ARCHIVE_BYTES:
                raise MaterializeError("the archive is larger than any stored agent may be")
            fh = tf.extractfile(m)
            content = fh.read() if fh else b""
            files[name] = (content, bool(m.mode & 0o111))
    return files


# ── git ──────────────────────────────────────────────────────────────────────────────────

def _git(repo: Path, *args: str, input: Optional[bytes] = None, env: Optional[dict] = None,
         check: bool = True) -> subprocess.CompletedProcess:
    # Config that would make the same input differ between machines is pinned off here.
    cmd = ["git", "-C", str(repo), "-c", "core.autocrlf=false", "-c", "core.safecrlf=false",
           "-c", "core.fileMode=true", *args]
    r = subprocess.run(cmd, input=input, capture_output=True,
                       env={**os.environ, **(env or {})}, timeout=120)
    if check and r.returncode != 0:
        err = (r.stderr or r.stdout or b"").decode("utf-8", "replace").strip()
        raise MaterializeError(f"git {' '.join(args[:2])}: {err.splitlines()[0] if err else r.returncode}")
    return r


def _out(r: subprocess.CompletedProcess) -> str:
    return (r.stdout or b"").decode("utf-8", "replace").strip()


def git_date(published_at: str) -> str:
    """X-Canopy-Published-At (ISO 8601, or an HTTP date) → git's ``@<unix> +0000``.
    Whole seconds, UTC: the same header gives the same commit everywhere."""
    s = (published_at or "").strip()
    when: Optional[dt.datetime] = None
    try:
        when = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(s)
        except (TypeError, ValueError):
            when = None
    if when is None:
        raise MaterializeError(f"unreadable X-Canopy-Published-At {s!r}")
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return f"{int(when.timestamp())} +0000"


def commit_object(tree: str, revision: int, published_at: str) -> bytes:
    """The commit's exact bytes: no parent, fixed identity, both dates = Published-At."""
    when = git_date(published_at)
    who = f"{AUTHOR} <{AUTHOR_EMAIL}> {when}"
    return (f"tree {tree}\nauthor {who}\ncommitter {who}\n\ncanopy revision {revision}\n"
            ).encode("utf-8")


def write_commit(repo: Path, archive: Archive) -> str:
    """Blobs, tree and the orphan commit into ``repo``'s object store. The working tree and
    every ref are untouched. Returns the commit sha."""
    index = Path(_out(_git(repo, "rev-parse", "--absolute-git-dir"))) / "canopy-materialize.index"
    try:
        index.unlink()
    except FileNotFoundError:
        pass
    lines = []
    for path in sorted(archive.files):
        content, executable = archive.files[path]
        blob = _out(_git(repo, "hash-object", "-w", "--no-filters", "--stdin", input=content))
        lines.append(f"{'100755' if executable else '100644'} {blob}\t{path}\n")
    env = {"GIT_INDEX_FILE": str(index)}
    try:
        _git(repo, "update-index", "--add", "--index-info",
             input="".join(lines).encode("utf-8"), env=env)
        tree = _out(_git(repo, "write-tree", env=env)) if lines else _empty_tree(repo)
    finally:
        try:
            index.unlink()
        except FileNotFoundError:
            pass
    body = commit_object(tree, archive.revision, archive.published_at)
    return _out(_git(repo, "hash-object", "-t", "commit", "-w", "--stdin", input=body))


def _empty_tree(repo: Path) -> str:
    return _out(_git(repo, "hash-object", "-t", "tree", "-w", "--stdin", input=b""))


def _head(repo: Path) -> str:
    r = _git(repo, "rev-parse", "--verify", "-q", "HEAD^{commit}", check=False)
    return _out(r) if r.returncode == 0 else ""


def _config(repo: Path, key: str) -> str:
    r = _git(repo, "config", "--get", key, check=False)
    return _out(r) if r.returncode == 0 else ""


def _dirty(repo: Path) -> bool:
    r = _git(repo, "status", "--porcelain", "--untracked-files=no", check=False)
    return r.returncode != 0 or bool(_out(r))


def origin_url(workspace: str, slug: str) -> str:
    return f"{ORIGIN_SCHEME}{workspace or 'unknown'}/{slug}"


def is_stored_clone(path: Path) -> bool:
    """Whether ``path`` is a materialized stored agent (origin ``canopy://…``)."""
    if not (Path(path) / ".git").exists():
        return False
    return _config(Path(path), "remote.origin.url").startswith(ORIGIN_SCHEME)


def recorded(path: Path) -> dict:
    """What the last materialize recorded in ``path``'s git config ({} when nothing)."""
    if not (Path(path) / ".git").exists():
        return {}
    rev = _config(Path(path), "canopy.revision")
    return {"revision": int(rev) if rev.isdigit() else None,
            "root_sha256": _config(Path(path), "canopy.root-sha256"),
            "commit": _head(Path(path))} if rev or _config(Path(path), "canopy.root-sha256") else {}


def _init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        subprocess.run(["git", "-c", "init.defaultObjectFormat=sha1", "init", "-q", str(repo)],
                       capture_output=True, check=True, timeout=60)


def _point(repo: Path, commit: str, url: str) -> None:
    """The refs and remote that make `commit` look like a clone of origin/main."""
    _git(repo, "update-ref", "refs/remotes/origin/main", commit)
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    if _git(repo, "remote", "get-url", "origin", check=False).returncode == 0:
        _git(repo, "remote", "set-url", "origin", url)
    else:
        _git(repo, "remote", "add", "origin", url)
    _git(repo, "config", "branch.main.remote", "origin")
    _git(repo, "config", "branch.main.merge", "refs/heads/main")


class _DestLock:
    """One materialize per dest at a time (a runner's claim and the session-start updater can
    both sync the same dir). Blocking, POSIX only; elsewhere a no-op."""

    def __init__(self, dest: Path):
        self.path = dest.parent / f".{dest.name}.materialize.lock"
        self.fh = None

    def __enter__(self):
        try:
            import fcntl
        except ImportError:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "w", encoding="utf-8")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self.fh is not None:
            self.fh.close()
        return False


def write_revision(dest: Path, archive: Archive, *, slug: str, workspace: str) -> "tuple[str, bool]":
    """(commit, changed). Stage objects and refs first, swap the working tree last."""
    dest = Path(dest).expanduser()
    with _DestLock(dest):
        return _write_revision(dest, archive, slug=slug, workspace=workspace)


def _write_revision(dest: Path, archive: Archive, *, slug: str, workspace: str) -> "tuple[str, bool]":
    fresh = not dest.exists() or (dest.is_dir() and not any(dest.iterdir()))
    work = dest
    if fresh:
        dest.parent.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix=f".{dest.name}.materialize-", dir=str(dest.parent)))
    try:
        _init(work)
        commit = write_commit(work, archive)
        changed = fresh or _head(work) != commit or _dirty(work)
        _point(work, commit, origin_url(workspace, slug))
        if changed:
            # The swap: HEAD onto main, index and tracked files to the commit. Untracked and
            # ignored files are not git's to touch and stay.
            _git(work, "symbolic-ref", "HEAD", "refs/heads/main")
            _git(work, "reset", "-q", "--hard", commit)
        for key, value in (("canopy.slug", slug), ("canopy.revision", str(archive.revision)),
                           ("canopy.root-sha256", archive.root_sha256)):
            _git(work, "config", key, value)
        if fresh:
            if dest.exists():
                dest.rmdir()
            os.replace(work, dest)
    except BaseException:
        if fresh and work != dest:
            shutil.rmtree(work, ignore_errors=True)
        raise
    return commit, changed


# ── the command ─────────────────────────────────────────────────────────────────────────

def _workspace_of(slug: str, *, base_url: str, token: str) -> str:
    try:
        agent = canopy_web.call("GET", f"/api/agents/{slug}/", base_url=base_url, token=token) or {}
    except Exception:  # noqa: BLE001 — the url is a label; never a reason to fail
        return ""
    ws = agent.get("workspace")
    return str((ws.get("slug") if isinstance(ws, dict) else ws) or "")


def live_root(slug: str, *, base_url: str, token: str) -> dict:
    """``GET /definition``'s live block ({} when it cannot be read — a runner token may not)."""
    try:
        body = canopy_web.call("GET", f"/api/agents/{slug}/definition",
                               base_url=base_url, token=token) or {}
    except Exception:  # noqa: BLE001
        return {}
    live = body.get("live") if isinstance(body, dict) else None
    return live if isinstance(live, dict) else {}


def materialize(slug: str, dest: Path, *, revision: str = "live", runner: bool = False,
                base_url: Optional[str] = None, token: Optional[str] = None,
                fetch: Optional[Fetch] = None, workspace: str = "",
                precheck: bool = True) -> dict:
    """Bring ``dest`` to ``revision`` of ``slug``'s definition. Returns the JSON line's dict
    ``{slug, revision, root_sha256, commit, changed}``. Raises MaterializeError."""
    base = canopy_web.resolve_base_url(base_url)
    tok = token or resolve_token(runner, base)
    dest = Path(dest).expanduser()
    if precheck and str(revision or "live") == "live" and (dest / ".git").exists():
        # The cheap no-op: canopy-web's live root is what this dir records, HEAD is on it and
        # nothing tracked was touched — no download. (A runner token may not read
        # /definition; then the archive is fetched and the commit compared.)
        live, rec = live_root(slug, base_url=base, token=tok), recorded(dest)
        if (live.get("root_sha256") and rec.get("commit")
                and str(live.get("root_sha256")).lower() == rec.get("root_sha256")
                and str(live.get("number")) == str(rec.get("revision"))
                and _out(_git(dest, "rev-parse", "-q", "--verify", "refs/remotes/origin/main",
                              check=False)) == rec["commit"]
                and not _dirty(dest)):
            return {"slug": slug, "revision": rec["revision"], "root_sha256": rec["root_sha256"],
                    "commit": rec["commit"], "changed": False}
    try:
        archive = fetch_archive(slug, revision=revision, base_url=base, token=tok, fetch=fetch)
    except MaterializeError as e:
        # Refused as whoever the token is (a person's PAT on a runner box, a revoked one):
        # read again as THIS box's runner, which canopy-web lets read the agents it serves.
        if not any(f"-> {c}:" in str(e) for c in (401, 403)) or token:
            raise
        from orchestrator import runner_session

        rtok = runner_session.token(base)
        if not rtok or rtok == tok:
            raise
        tok = rtok
        archive = fetch_archive(slug, revision=revision, base_url=base, token=tok, fetch=fetch)
    ws = workspace or archive.workspace or _workspace_of(slug, base_url=base, token=tok)
    commit, changed = write_revision(dest, archive, slug=slug, workspace=ws)
    return {"slug": slug, "revision": archive.revision, "root_sha256": archive.root_sha256,
            "commit": commit, "changed": changed}


def default_dest(slug: str, *, runner: bool, agent_root: Optional[str] = None) -> Path:
    """Where an agent's materialized dir lives: ``$AGENT_ROOT/<slug>`` on a runner (the
    clone `agent prepare --runner` makes), ``~/.canopy/agents/<slug>`` on a person's laptop."""
    if runner or agent_root:
        root = agent_root or os.environ.get("AGENT_ROOT", "/opt/agents")
        return Path(root).expanduser() / slug
    return Path.home() / ".canopy" / "agents" / slug
