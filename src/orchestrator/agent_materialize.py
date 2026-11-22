"""Bring a stored agent's definition into a local git repo: a clone of canopy-web's git repo.

    canopy agent materialize --slug <slug> --dest <dir> [--revision N|live] [--runner]
    canopy agent sync --slug <slug> [--runner] [--dest <dir>]

A stored agent has no GitHub repo: its definition is a git repository HOSTED by canopy-web
(docs/architecture/stored-agents.md), served read-only over smart HTTP at
``<canopy base>/git/<workspace>/<slug>.git``. ``refs/heads/main`` is the live revision and
``refs/tags/r<N>`` marks published revision N. Everything downstream of "a git clone at
AGENT_ROOT/<slug>" — worktrees, `claude plugin marketplace add <dir>`, the fleet updater's
`refresh_plugin` / `off_release_branch` — keeps working because the dest IS such a clone:

  * origin = the canopy git url (``definition.git_url`` from canopy-web);
  * ``git fetch`` with the token passed for that one command only — never written to
    ``.git/config`` or put in the url (`auth_env`);
  * ``git reset --hard origin/main`` (or ``r<N>``): tracked files replaced exactly, tracked
    files absent from the revision deleted, untracked and ignored files (``.env``,
    ``node_modules``, state) kept. A dest that does not exist yet is built in a sibling
    staging dir and renamed into place.

A dest materialized by the old archive writer (origin ``canopy://<ws>/<slug>``, one orphan
commit) is converted in place: origin is pointed at the git url, fetched, reset. An agent
that was EXPORTED to GitHub (``source: repo``) has its dir's origin pointed at that repo
instead, and the caller's ordinary repo path takes over.

Nothing in the working tree changes when the fetch fails, and nothing at all when the dest
is already at the revision (the JSON line then says ``"changed": false``).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
from pathlib import Path
from typing import Optional

from orchestrator import canopy_web
from orchestrator.agent_definition import file_entry, manifest_root

#: The origin of a dir written by the archive writer: recognised, so such a dir is
#: converted in place rather than refused.
ORIGIN_SCHEME = "canopy://"
GIT_PATH = "/git/"
GIT_TIMEOUT = 300
_GIT_URL_RE = re.compile(r"^https?://[^/]+(/[^?#]*)?/git/[^/]+/[^/]+\.git/?$")


class MaterializeError(RuntimeError):
    """The definition could not be fetched or checked out. The working tree is unchanged."""


class ExportedAgent(MaterializeError):
    """The agent's definition lives in a repo (``source: repo``), not in canopy-web."""

    def __init__(self, slug: str, repo_url: str):
        super().__init__(f"{slug} is not a stored agent: its definition lives at "
                         f"{repo_url or 'a repository'} — clone or pull that instead")
        self.repo_url = repo_url


# ── urls ────────────────────────────────────────────────────────────────────────────────

def git_url_for(base_url: str, workspace: str, slug: str) -> str:
    return f"{base_url.rstrip('/')}{GIT_PATH}{workspace}/{slug}.git"


def is_canopy_git_url(url: str, base_url: Optional[str] = None) -> bool:
    """Whether ``url`` is a stored agent's canopy git url: it starts with the canopy base +
    ``/git/`` (or is the legacy ``canopy://``)."""
    url = (url or "").strip()
    return url.startswith(ORIGIN_SCHEME) or url.startswith(
        canopy_web.resolve_base_url(base_url) + GIT_PATH)


def _host_of(url: str) -> str:
    """``scheme://host[:port]/`` — what the auth header is scoped to, so a redirect to any
    other host never carries it."""
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}/"


# ── git with a token, for one command ───────────────────────────────────────────────────

def auth_env(url: str, token: str, env: Optional[dict] = None) -> dict:
    """The environment for ONE git command that talks to ``url`` as ``token``.

    The token rides ``http.<host>/.extraHeader`` = ``Authorization: Bearer <token>``, set
    through git's command-scoped config (GIT_CONFIG_COUNT / _KEY_n / _VALUE_n, the
    environment form of ``git -c``): it is never written to ``.git/config`` or the url, and
    unlike ``-c`` it is not in the process's argv for ``ps`` to show. Credential helpers
    and the terminal prompt are off for that command, so a refusal fails rather than asks.
    """
    base = dict(os.environ if env is None else env)
    try:
        n = int(base.get("GIT_CONFIG_COUNT") or 0)
    except ValueError:
        n = 0
    pairs = [("credential.helper", "")]
    if token:
        pairs.insert(0, (f"http.{_host_of(url)}.extraHeader", f"Authorization: Bearer {token}"))
    for i, (k, v) in enumerate(pairs, start=n):
        base[f"GIT_CONFIG_KEY_{i}"] = k
        base[f"GIT_CONFIG_VALUE_{i}"] = v
    base["GIT_CONFIG_COUNT"] = str(n + len(pairs))
    base["GIT_TERMINAL_PROMPT"] = "0"
    return base


def _git(repo: Optional[Path], *args: str, auth: "Optional[tuple[str, str]]" = None,
         input: Optional[bytes] = None, check: bool = True,
         timeout: int = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    cmd = ["git", *(["-C", str(repo)] if repo is not None else []), *args]
    env = auth_env(*auth) if auth else None
    try:
        r = subprocess.run(cmd, input=input, capture_output=True, env=env, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise MaterializeError(f"git {' '.join(args[:2])}: {e}") from e
    if check and r.returncode != 0:
        raise MaterializeError(f"git {' '.join(a for a in args[:2] if '://' not in a)}: "
                               f"{_err(r)}")
    return r


def _err(r: subprocess.CompletedProcess) -> str:
    text = (r.stderr or r.stdout or b"").decode("utf-8", "replace").strip()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return (lines[-1] if lines else str(r.returncode))[:300]


def _out(r: subprocess.CompletedProcess) -> str:
    return (r.stdout or b"").decode("utf-8", "replace").strip()


def _refused(e: Exception) -> bool:
    s = str(e)
    return any(k in s for k in ("401", "403", "Authentication failed", "could not read Username"))


# ── reading a commit ────────────────────────────────────────────────────────────────────

def tree_root(repo: Path, commit: str) -> str:
    """``manifest_root`` of ``commit``'s tree — what canopy-web reports as root_sha256."""
    ls = _out(_git(repo, "ls-tree", "-r", "-z", "--full-tree", commit)).split("\0")
    rows = []
    for rec in ls:
        if not rec:
            continue
        meta, _, path = rec.partition("\t")
        mode, kind, sha = meta.split()
        if kind == "blob":
            rows.append((path, sha, mode == "100755"))
    entries = []
    if rows:
        r = _git(repo, "cat-file", "--batch",
                 input=("\n".join(sha for _, sha, _ in rows) + "\n").encode())
        data, pos = r.stdout, 0
        for path, _sha, executable in rows:
            nl = data.index(b"\n", pos)
            size = int(data[pos:nl].split()[2])
            content = data[nl + 1:nl + 1 + size]
            pos = nl + 1 + size + 1
            entries.append(file_entry(path, content, executable))
    return manifest_root(entries)


def revision_of(repo: Path, commit: str) -> Optional[int]:
    """N of the ``r<N>`` tag on ``commit`` (the highest, when a rollback re-tagged it)."""
    r = _git(repo, "tag", "--points-at", commit, check=False)
    nums = [int(t[1:]) for t in _out(r).split() if re.fullmatch(r"r\d+", t)]
    return max(nums) if nums else None


def _head(repo: Path) -> str:
    r = _git(repo, "rev-parse", "--verify", "-q", "HEAD^{commit}", check=False)
    return _out(r) if r.returncode == 0 else ""


def _config(repo: Path, key: str) -> str:
    r = _git(repo, "config", "--get", key, check=False)
    return _out(r) if r.returncode == 0 else ""


def _dirty(repo: Path) -> bool:
    r = _git(repo, "status", "--porcelain", "--untracked-files=no", check=False)
    return r.returncode != 0 or bool(_out(r))


def origin_of(path: Path) -> str:
    if not (Path(path) / ".git").exists():
        return ""
    return _config(Path(path), "remote.origin.url")


def is_stored_clone(path: Path) -> bool:
    """Whether ``path`` is a stored agent's dir: origin is a canopy git url (or the legacy
    ``canopy://`` of the archive writer). A dir materialized from ANOTHER canopy-web (a
    staging base) is recognised by its shape plus the ``canopy.slug`` it records."""
    url = origin_of(path)
    if is_canopy_git_url(url):
        return True
    return bool(_GIT_URL_RE.match(url)) and bool(_config(Path(path), "canopy.slug"))


def recorded(path: Path) -> dict:
    """What the last materialize recorded in ``path``'s git config ({} when nothing)."""
    if not (Path(path) / ".git").exists():
        return {}
    rev = _config(Path(path), "canopy.revision")
    root = _config(Path(path), "canopy.root-sha256")
    if not (rev or root):
        return {}
    return {"revision": int(rev) if rev.isdigit() else None, "root_sha256": root,
            "commit": _head(Path(path))}


def _legacy_workspace(path: Path) -> str:
    url = origin_of(path)
    if url.startswith(ORIGIN_SCHEME):
        return url[len(ORIGIN_SCHEME):].split("/", 1)[0]
    return ""


# ── canopy-web: where the repo is ───────────────────────────────────────────────────────

def definition_info(slug: str, *, base_url: str, token: str) -> dict:
    """``{source, git_url, repo_url, workspace, revision, root_sha256}`` from canopy-web:
    ``/runtime`` (which a runner token reads), then the agent detail for what it lacks."""
    info: dict = {}
    reads = (lambda: canopy_web.call("GET", f"/api/agents/{slug}/runtime",
                                     base_url=base_url, token=token),
             lambda: canopy_web.call("GET", f"/api/agents/{slug}/",
                                     base_url=base_url, token=token))
    for read in reads:
        try:
            body = read() or {}
        except Exception:  # noqa: BLE001 — a token may read one and not the other
            continue
        if not isinstance(body, dict):
            continue
        d = body.get("definition") if isinstance(body.get("definition"), dict) else {}
        ws = body.get("workspace")
        ws = ws.get("slug") if isinstance(ws, dict) else ws
        for k, v in (("source", d.get("source")), ("git_url", d.get("git_url")),
                     ("repo_url", body.get("repo_url") or d.get("repo_url")),
                     ("workspace", ws), ("revision", d.get("revision")),
                     ("root_sha256", d.get("root_sha256"))):
            if v not in (None, "") and k not in info:
                info[k] = v
        if info.get("git_url") and info.get("source"):
            break
    return info


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


# ── writing the dest ────────────────────────────────────────────────────────────────────

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


def _set_origin(repo: Path, url: str) -> None:
    if _git(repo, "remote", "get-url", "origin", check=False).returncode == 0:
        _git(repo, "remote", "set-url", "origin", url)
    else:
        _git(repo, "remote", "add", "origin", url)
    _git(repo, "config", "remote.origin.fetch", "+refs/heads/main:refs/remotes/origin/main")
    _git(repo, "config", "branch.main.remote", "origin")
    _git(repo, "config", "branch.main.merge", "refs/heads/main")


def fetch(repo: Path, url: str, token: str) -> None:
    """``main`` and the ``r<N>`` tags from the canopy repo, as ``token``."""
    _git(repo, "fetch", "--quiet", "--prune", "--tags", "origin",
         "+refs/heads/main:refs/remotes/origin/main", auth=(url, token))
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")


def checkout(dest: Path, *, slug: str, git_url: str, token: str,
             revision: str = "live") -> dict:
    """Bring ``dest`` to ``revision`` from ``git_url``. Returns the JSON line's dict."""
    dest = Path(dest).expanduser()
    with _DestLock(dest):
        return _checkout(dest, slug=slug, git_url=git_url, token=token, revision=revision)


def _checkout(dest: Path, *, slug: str, git_url: str, token: str, revision: str) -> dict:
    fresh = not dest.exists() or (dest.is_dir() and not any(dest.iterdir()))
    work = dest
    if fresh:
        dest.parent.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix=f".{dest.name}.materialize-", dir=str(dest.parent)))
    try:
        if not (work / ".git").exists():
            _git(None, "init", "-q", str(work))
        _set_origin(work, git_url)
        fetch(work, git_url, token)
        rev = str(revision or "live").strip()
        ref = "refs/remotes/origin/main" if rev == "live" else f"refs/tags/r{rev}"
        r = _git(work, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}", check=False)
        target = _out(r)
        if r.returncode != 0 or not target:
            raise MaterializeError(f"{slug} has no {'live' if rev == 'live' else 'published'} "
                                   f"revision {'' if rev == 'live' else rev}".rstrip())
        changed = fresh or _head(work) != target or _dirty(work)
        if changed:
            # The swap: HEAD onto main, index and tracked files to the commit. Untracked and
            # ignored files are not git's to touch and stay.
            _git(work, "symbolic-ref", "HEAD", "refs/heads/main")
            _git(work, "reset", "-q", "--hard", target)
        number = revision_of(work, target)
        root = tree_root(work, target)
        for key, value in (("canopy.slug", slug),
                           ("canopy.revision", "" if number is None else str(number)),
                           ("canopy.root-sha256", root)):
            if value:
                _git(work, "config", key, value)
            else:
                _git(work, "config", "--unset-all", key, check=False)
        if fresh:
            if dest.exists():
                dest.rmdir()
            os.replace(work, dest)
    except BaseException:
        if fresh and work != dest:
            shutil.rmtree(work, ignore_errors=True)
        raise
    return {"slug": slug, "revision": number, "root_sha256": root, "commit": target,
            "changed": changed}


def repoint_to_repo(dest: Path, repo_url: str) -> None:
    """An EXPORTED agent's dir: origin becomes its repo (``https://github.com/o/n.git``) and
    the canopy markers go. The caller's ordinary repo path (fetch, reset/pull) takes over —
    the export carried main's whole history, so the local HEAD is an ancestor."""
    url = repo_url.strip().rstrip("/")
    if not url.endswith(".git"):
        url += ".git"
    dest = Path(dest)
    _git(dest, "remote", "set-url", "origin", url)
    _git(dest, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    for key in ("canopy.slug", "canopy.revision", "canopy.root-sha256"):
        _git(dest, "config", "--unset-all", key, check=False)


# ── the command ─────────────────────────────────────────────────────────────────────────

def materialize(slug: str, dest: Path, *, revision: str = "live", runner: bool = False,
                base_url: Optional[str] = None, token: Optional[str] = None,
                git_url: str = "", workspace: str = "",
                info: Optional[dict] = None) -> dict:
    """Bring ``dest`` to ``revision`` of ``slug``'s definition. Returns the JSON line's dict
    ``{slug, revision, root_sha256, commit, changed}``. Raises MaterializeError.

    ``git_url`` (the resolve payload's ``definition.git_url``) saves a canopy-web read.
    An exported agent (``source: repo``): a stored dir at ``dest`` is repointed at its repo
    and the dict says ``source: "repo"``, ``repo_url``; with no dir, ExportedAgent."""
    base = canopy_web.resolve_base_url(base_url)
    tok = token or resolve_token(runner, base)
    dest = Path(dest).expanduser()
    if not git_url:
        info = info if info is not None else definition_info(slug, base_url=base, token=tok)
        if str(info.get("source") or "") == "repo":
            repo_url = str(info.get("repo_url") or "")
            if repo_url and is_stored_clone(dest):
                repoint_to_repo(dest, repo_url)
                return {"slug": slug, "revision": None, "root_sha256": None,
                        "commit": _head(dest), "changed": False, "source": "repo",
                        "repo_url": repo_url}
            raise ExportedAgent(slug, repo_url)
        git_url = str(info.get("git_url") or "")
        if not git_url:
            ws = workspace or str(info.get("workspace") or "") or _legacy_workspace(dest)
            if not ws:
                raise MaterializeError(f"canopy-web gave no git url for {slug} (is it a stored "
                                       "agent, and may this token read it?)")
            git_url = git_url_for(base, ws, slug)
    try:
        return checkout(dest, slug=slug, git_url=git_url, token=tok, revision=revision)
    except MaterializeError as e:
        # Refused as whoever the token is (a person's PAT on a runner box, a revoked one):
        # read again as THIS box's runner, which canopy-web lets read the agents it serves.
        if not _refused(e) or token:
            raise
        from orchestrator import runner_session

        rtok = runner_session.token(base)
        if not rtok or rtok == tok:
            raise
        return checkout(dest, slug=slug, git_url=git_url, token=rtok, revision=revision)


def default_dest(slug: str, *, runner: bool, agent_root: Optional[str] = None) -> Path:
    """Where an agent's dir lives: ``$AGENT_ROOT/<slug>`` on a runner (the clone
    `agent prepare --runner` makes), ``~/.canopy/agents/<slug>`` on a person's laptop."""
    if runner or agent_root:
        root = agent_root or os.environ.get("AGENT_ROOT", "/opt/agents")
        return Path(root).expanduser() / slug
    return Path.home() / ".canopy" / "agents" / slug
