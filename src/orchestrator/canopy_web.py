"""Shared canopy-web transport + auth — the one place PAT/base-url resolution
and HTTP live. stdlib urllib only (the canopy plugin has no `requests` dep)."""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

# Every request says what made it and which turn/session it came from (see
# provenance.py). Imported by name so tests can monkeypatch it here.
from orchestrator.provenance import provenance_headers

DEFAULT_API = "https://canopy.dimagi.com"
TOKEN_FILE = Path.home() / ".claude" / "canopy" / "workbench-token"

Transport = Callable[[str, str, dict, Optional[bytes]], "tuple[int, str]"]


class CanopyError(RuntimeError):
    """A non-2xx response from canopy-web."""


def resolve_base_url(base_url: Optional[str]) -> str:
    if base_url:
        return base_url.rstrip("/")
    from_env = os.environ.get("CANOPY_WEB_API_URL", "").strip()
    if from_env:
        return from_env.rstrip("/")
    return DEFAULT_API


# Product apps that canopy-web scopes to a workspace. A path like
# ``/api/walkthroughs/…`` is rewritten to ``/api/w/<ws>/walkthroughs/…`` when a
# workspace is active; unscoped apps (sessions, system, me, …) are
# left alone. Mirrors WS_SCOPED_API_PREFIXES on the canopy-web frontend.
SCOPED_APPS = ("projects", "walkthroughs", "reviews", "shareouts", "ddd", "timeline")


def resolve_workspace(workspace: Optional[str]) -> Optional[str]:
    """The active canopy-web workspace slug, or None (→ flat routes → the org
    default). Precedence: explicit arg → env ``CANOPY_WEB_WORKSPACE`` → None.
    The DDD layer adds a per-repo config source on top of this (see
    ``scripts/ddd/auth.resolve_ddd_workspace``)."""
    if workspace:
        return workspace.strip() or None
    from_env = os.environ.get("CANOPY_WEB_WORKSPACE", "").strip()
    return from_env or None


def scoped_api_path(path: str, workspace: Optional[str] = None) -> str:
    """Rewrite a flat ``/api/<app>/…`` path to the tenant path
    ``/api/w/<ws>/<app>/…`` when a workspace is active and ``<app>`` is scoped.
    A no-op when there is no workspace, the path isn't under ``/api/``, or the
    app isn't workspace-scoped."""
    ws = resolve_workspace(workspace)
    if not ws or not path.startswith("/api/"):
        return path
    rest = path[len("/api"):]  # "/walkthroughs/…"
    app = rest.lstrip("/").split("/", 1)[0]
    if app not in SCOPED_APPS:
        return path
    return f"/api/w/{ws}{rest}"


def scoped_app_path(path: str, workspace: Optional[str] = None) -> str:
    """Rewrite a flat browser route (e.g. ``/ddd/<slug>/<run>``) to its tenant
    form ``/w/<ws>/ddd/<slug>/<run>`` when a workspace is active — so package /
    landing links a human clicks open in the right workspace. No-op when there
    is no workspace."""
    ws = resolve_workspace(workspace)
    if not ws or not path.startswith("/"):
        return path
    return f"/w/{ws}{path}"


# Plugins that carry a `.claude-plugin/plugin.json` but are NOT fleet agents. The
# canopy runtime (where every `python -m scripts.ddd.*` runs) sits inside the
# canopy plugin's own install dir, so without this every DDD call claimed to be
# "in the 'canopy' agent repo" and warned about a borrowed identity that is
# simply the operator's (0.2.528, M5).
_NON_AGENT_SLUGS = frozenset({"canopy"})


def _agent_slug_for_cwd(start: Optional[Path] = None) -> str:
    """The agent slug of the repo we're standing in, or "" if this isn't one.

    An agent turn runs INSIDE the agent's repo, so the repo is the identity: walk
    up for `.claude-plugin/plugin.json` and take its `name`.
    """
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        manifest = d / ".claude-plugin" / "plugin.json"
        if not manifest.is_file():
            continue
        try:
            slug = (json.loads(manifest.read_text(encoding="utf-8")) or {}).get("name") or ""
        except (OSError, ValueError):
            return ""
        return "" if slug in _NON_AGENT_SLUGS else slug
    return ""


# One warning per process: a single command makes many calls, and warning on each
# would train people to scroll past it.
_WARNED_BORROWED_IDENTITY = False


def _reset_identity_warning() -> None:
    """Test seam — clear the once-per-process latch."""
    global _WARNED_BORROWED_IDENTITY
    _WARNED_BORROWED_IDENTITY = False


def _warn_borrowed_identity(slug: str) -> None:
    """Say out loud that an agent is about to act as the operator.

    This fallback is legitimate (a human working in an agent repo must not be
    blocked), so this warns rather than refuses. What it must not be is SILENT:
    ACE ran for a full day attributed to a human because `~/.ace/.env` was never
    materialized, and nothing anywhere said so (dimagi-internal/ace#1005).
    """
    global _WARNED_BORROWED_IDENTITY
    if _WARNED_BORROWED_IDENTITY:
        return
    _WARNED_BORROWED_IDENTITY = True
    print(
        f"[canopy] WARNING: this is agent '{slug}''s session, but ~/.{slug}/.env has no "
        f"CANOPY_WEB_PAT — falling back to the operator's workbench-token.\n"
        f"[canopy] Calls will be attributed to the OPERATOR, not to '{slug}', and "
        f"will see the operator's workspaces.\n"
        f"[canopy] If you are {slug}: materialize its env "
        f"(`op inject -i .env.tpl -o ~/.{slug}/.env`) or set CANOPY_WEB_PAT.",
        file=sys.stderr,
    )


def _slug_env_pat(slug: str) -> str:
    """CANOPY_WEB_PAT out of agent ``slug``'s provisioned ``~/.<slug>/.env``, or ""."""
    if not slug or slug in _NON_AGENT_SLUGS or not _SLUG_RE.match(slug):
        return ""
    env_file = Path.home() / f".{slug}" / ".env"
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("CANOPY_WEB_PAT="):
                return line.partition("=")[2].strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def _agent_env_pat(start: Optional[Path] = None) -> str:
    """This agent's OWN PAT from `~/.<slug>/.env`, or "" if there isn't one.

    An agent turn runs INSIDE the agent's repo, so the repo is the identity: walk
    up from cwd for `.claude-plugin/plugin.json`, take its `name` as the slug, and
    read CANOPY_WEB_PAT out of that agent's provisioned env file.

    Without this, per-agent PATs only work where a runner happens to inject the
    env. The cloud runner does; the laptop runner drives emdash's UI over CDP and
    never builds an env at all — so on a laptop every agent silently fell through
    to the operator's own workbench-token and acted as the HUMAN, with nothing
    failing to reveal it. Resolving from the repo makes identity follow the agent
    on every host instead of depending on how it happened to be launched.
    """
    return _slug_env_pat(_agent_slug_for_cwd(start))


# Env vars that name the agent whose session this is. `CANOPY_AGENT` is set by
# every agent repo's settings.json and by the cloud runner for every agent turn;
# `CANOPY_AGENT_SLUG` is the harness's name for the same thing (run_store, shareout).
_AGENT_ENV_VARS = ("CANOPY_AGENT", "CANOPY_AGENT_SLUG")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$", re.I)


def agent_context_slug(start: Optional[Path] = None) -> str:
    """The agent this process is working FOR, or "" when it is a human's own.

    The env wins over the cwd walk-up because the cwd lies in exactly the case that
    matters: every `python -m scripts.ddd.*` runs with cwd = the canopy runtime
    (inside the canopy plugin, a non-agent), so an ACE session posting a narrative
    looked like nobody's — and fell through to the operator's token (ace#2805).
    """
    for var in _AGENT_ENV_VARS:
        slug = os.environ.get(var, "").strip()
        if slug and slug not in _NON_AGENT_SLUGS and _SLUG_RE.match(slug):
            return slug
    return _agent_slug_for_cwd(start)


class AgentIdentityError(RuntimeError):
    """An agent's session has no PAT of its own, and strict mode refused to borrow
    the operator's workbench-token."""


def resolve_token(token: Optional[str], *, agent_strict: bool = False) -> str:
    """The canopy-web PAT this process acts with.

    Precedence: explicit arg → env ``CANOPY_WEB_PAT`` → the agent's own PAT
    (``~/.<slug>/.env``, slug from :func:`agent_context_slug`'s env vars, then the
    cwd's agent repo) → the operator's ``TOKEN_FILE``.

    ``agent_strict=True`` turns that last fallback into a refusal whenever this is
    an agent's session: writes that publish under someone's name (DDD narratives,
    videos, walkthroughs) must not silently go out as the human who owns the
    laptop. Without it the fallback only warns — a human working in an agent repo
    must not be blocked from read-mostly CLI work.
    """
    if token:
        return token
    # Explicit env wins: it is how a runner pins the identity for a turn.
    from_env = os.environ.get("CANOPY_WEB_PAT", "").strip()
    if from_env:
        return from_env
    # Then the agent's own PAT, so an agent acts as ITSELF rather than as whoever
    # owns TOKEN_FILE. This must come BEFORE the global file — that file exists on
    # every operator laptop, so checking it first is exactly what masked the bug.
    slug = agent_context_slug()
    agent_pat = _slug_env_pat(slug) or _agent_env_pat()
    if agent_pat:
        return agent_pat
    if slug and agent_strict:
        raise AgentIdentityError(
            f"this is agent '{slug}'s session, but no PAT of its own resolved: "
            f"CANOPY_WEB_PAT is unset and ~/.{slug}/.env has no CANOPY_WEB_PAT. "
            f"Refusing to fall back to the operator's workbench-token ({TOKEN_FILE}) — "
            f"the write would be attributed to the operator and land in the operator's "
            f"workspaces. Set CANOPY_WEB_PAT to {slug}'s PAT, or materialize its env "
            f"(`op inject -i .env.tpl -o ~/.{slug}/.env`)."
        )
    if TOKEN_FILE.exists():
        stored = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if stored:
            # About to act as the operator. If this is an agent's session that is
            # an identity swap, and it must not happen quietly.
            if slug:
                _warn_borrowed_identity(slug)
            return stored
    raise RuntimeError(
        f"no canopy-web PAT — run /canopy:canopy-web-pat-mint to mint one, "
        f"or set CANOPY_WEB_PAT. Expected token at {TOKEN_FILE}."
    )


def urllib_transport(method: str, url: str, headers: dict, body: Optional[bytes]) -> "tuple[int, str]":
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def call(method: str, path: str, body=None, *,
         base_url: Optional[str] = None, token: Optional[str] = None,
         workspace: Optional[str] = None,
         transport: Optional[Transport] = None,
         headers: Optional[dict] = None) -> dict:
    base = resolve_base_url(base_url)
    tok = resolve_token(token)
    path = scoped_api_path(path, workspace)  # → /api/w/<ws>/… when a workspace is active
    transport = transport or urllib_transport
    # Provenance first, so a caller's own header (or the bearer) always wins.
    headers = {**provenance_headers(), **(headers or {}), "Authorization": f"Bearer {tok}",
               "Content-Type": "application/json"}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    status, text = transport(method, base + path, headers, data)
    if not (200 <= status < 300):
        raise CanopyError(f"{method} {path} -> {status}: {text[:400]}")
    return json.loads(text) if text.strip() else {}


def call_text(method: str, path: str, *,
              base_url: Optional[str] = None, token: Optional[str] = None,
              workspace: Optional[str] = None,
              transport: Optional[Transport] = None) -> str:
    """`call`, for a route whose body is NOT JSON (e.g. a turn's raw JSONL
    transcript at ``/api/harness/turns/<id>/transcript``). Same auth, base-url and
    error contract — a non-2xx raises `CanopyError` — but the body comes back as
    text, undecoded."""
    base = resolve_base_url(base_url)
    tok = resolve_token(token)
    path = scoped_api_path(path, workspace)
    transport = transport or urllib_transport
    status, text = transport(method, base + path,
                             {**provenance_headers(), "Authorization": f"Bearer {tok}"}, None)
    if not (200 <= status < 300):
        raise CanopyError(f"{method} {path} -> {status}: {text[:400]}")
    return text


# --- Writes that must land in a NAMED workspace -------------------------------
#
# A write with no workspace goes to the flat route, and canopy-web files it in the
# caller's default workspace — for a human in dimagi + connect that is `dimagi`,
# whatever the content is about. ace#2805 / canopy-web#1289: an ACE session posted
# the chlorine narrative (Connect's) into dimagi, as the operator, and nothing said
# so. These helpers make a write name its tenant up front and confirm afterwards
# where it actually landed.

class WorkspaceRequiredError(RuntimeError):
    """A canopy-web write refused because the caller named no workspace."""


class WorkspaceMismatchError(RuntimeError):
    """A write succeeded but the object is not readable in the workspace it was
    meant for — it landed somewhere else."""


def require_write_workspace(workspace: Optional[str], *,
                            how_to_set: str = "set CANOPY_WEB_WORKSPACE=<slug>") -> str:
    """The workspace a write goes INTO — always one the caller named.

    ``workspace`` is the caller's already-resolved choice (arg/env/config). Empty
    raises :class:`WorkspaceRequiredError`: a flat route lets the server pick its
    default, and guessing from memberships is the same guess one step removed.
    """
    ws = (workspace or "").strip()
    if ws:
        return ws
    raise WorkspaceRequiredError(
        f"no canopy-web workspace named for this write. Refusing to let the server "
        f"pick its default (that is how a Connect narrative landed in dimagi, "
        f"ace#2805) — {how_to_set}."
    )


def confirm_landed(app: str, obj_id: str, workspace: str, *, query: Optional[dict] = None,
                   base_url: Optional[str] = None, token: Optional[str] = None,
                   transport: Optional[Transport] = None) -> dict:
    """Read a just-written object back through the TENANT-PINNED list
    (``/api/w/<ws>/<app>/``) and return its row.

    The pinned list holds only that workspace's rows, so finding the id there is
    proof of where it landed (a by-id GET is not: a link-visibility object reads
    from any tenant). Prints the workspace it landed in. Raises
    :class:`WorkspaceMismatchError` when the id is absent; a failed READ (network,
    5xx) only warns — the write already happened and a flaky read must not hide it.
    """
    import urllib.parse

    path = f"/api/w/{workspace}/{app}/"
    if query:
        path += "?" + urllib.parse.urlencode(query)
    try:
        rows = call("GET", path, base_url=base_url, token=token, transport=transport) or []
    except (CanopyError, OSError, ValueError) as exc:
        print(f"[canopy] WARNING: wrote {app} {obj_id} but could not read it back from "
              f"workspace '{workspace}' to confirm where it landed: {exc}", file=sys.stderr)
        return {}
    if isinstance(rows, dict):
        rows = rows.get("items") or rows.get("results") or []
    for row in rows:
        if isinstance(row, dict) and str(row.get("id")) == str(obj_id):
            who = f", owner {row['owner_email']}" if row.get("owner_email") else ""
            print(f"[canopy] landed in workspace '{workspace}' ({app} {obj_id}{who})",
                  file=sys.stderr)
            return row
    raise WorkspaceMismatchError(
        f"wrote {app} {obj_id} but it is NOT in workspace '{workspace}' — it landed in "
        f"another workspace. Check which identity wrote it (CANOPY_WEB_PAT) and move or "
        f"delete it."
    )
