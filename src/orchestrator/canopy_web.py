"""Shared canopy-web transport + auth — the one place PAT/base-url resolution
and HTTP live. stdlib urllib only (the canopy plugin has no `requests` dep)."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

DEFAULT_API = "https://labs.connect.dimagi.com/canopy"
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
# workspace is active; unscoped apps (insights, sessions, system, me, …) are
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
            return (json.loads(manifest.read_text(encoding="utf-8")) or {}).get("name") or ""
        except (OSError, ValueError):
            return ""
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
        f"[canopy] WARNING: in the '{slug}' agent repo, but ~/.{slug}/.env has no "
        f"CANOPY_WEB_PAT — falling back to the operator's workbench-token.\n"
        f"[canopy] Calls will be attributed to the OPERATOR, not to '{slug}', and "
        f"will see the operator's workspaces.\n"
        f"[canopy] If you are {slug}: materialize its env "
        f"(`op inject -i .env.tpl -o ~/.{slug}/.env`) or set CANOPY_WEB_PAT.",
        file=sys.stderr,
    )


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
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        manifest = d / ".claude-plugin" / "plugin.json"
        if not manifest.is_file():
            continue
        try:
            slug = (json.loads(manifest.read_text(encoding="utf-8")) or {}).get("name") or ""
        except (OSError, ValueError):
            return ""
        if not slug:
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
    return ""


def resolve_token(token: Optional[str]) -> str:
    if token:
        return token
    # Explicit env wins: it is how a runner pins the identity for a turn.
    from_env = os.environ.get("CANOPY_WEB_PAT", "").strip()
    if from_env:
        return from_env
    # Then the agent's own PAT, so an agent acts as ITSELF rather than as whoever
    # owns TOKEN_FILE. This must come BEFORE the global file — that file exists on
    # every operator laptop, so checking it first is exactly what masked the bug.
    agent_pat = _agent_env_pat()
    if agent_pat:
        return agent_pat
    if TOKEN_FILE.exists():
        stored = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if stored:
            # About to act as the operator. If we're standing in an agent's repo
            # that is an identity swap, and it must not happen quietly.
            slug = _agent_slug_for_cwd()
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
         transport: Optional[Transport] = None) -> dict:
    base = resolve_base_url(base_url)
    tok = resolve_token(token)
    path = scoped_api_path(path, workspace)  # → /api/w/<ws>/… when a workspace is active
    transport = transport or urllib_transport
    headers = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    status, text = transport(method, base + path, headers, data)
    if not (200 <= status < 300):
        raise CanopyError(f"{method} {path} -> {status}: {text[:400]}")
    return json.loads(text) if text.strip() else {}
