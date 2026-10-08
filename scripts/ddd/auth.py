"""Shared canopy-web auth + URL resolution for the DDD scripts.

Single source of truth for the conventions previously duplicated byte-for-byte
in ``scripts/ddd/review.py`` and ``scripts/ddd/upload.py``:

  - Base URL: env var ``CANOPY_WEB_API_URL``, default :data:`DEFAULT_API`.
  - PAT:      env var ``CANOPY_WEB_PAT``, then the agent's own ``~/.<slug>/.env``,
              then the on-disk :data:`TOKEN_FILE` — but NEVER the latter in an
              agent's session (``$CANOPY_AGENT``): DDD writes publish under a name,
              so an agent without its own PAT fails loudly instead of posting as
              the operator (ace#2805).
  - Workspace: every write names one (:func:`require_write_workspace`) and is
              read back from it (:func:`confirm_landed`).

Public API
----------
resolve_base_url(base_url: str | None) -> str
    Effective base URL, stripped of any trailing slash.
resolve_token(token: str | None) -> str
    Effective PAT; raises ``RuntimeError`` if none can be resolved (and
    ``AgentIdentityError`` in an agent session with no PAT of its own).
require_write_workspace(workspace=None) -> str
    The workspace a DDD write goes into; refuses when none is named.
confirm_landed(app, obj_id, workspace, *, query, base_url, token) -> dict
    Read a write back, tenant-pinned, and print the workspace it landed in.
"""
from __future__ import annotations

# Canonical single source of these conventions now lives in orchestrator.canopy_web;
# this module re-exports them so existing callers (scripts/ddd/upload.py, review.py) are
# untouched. Run under `uv run` from the repo root, where `orchestrator` is importable.
from pathlib import Path
from typing import Optional

from orchestrator import canopy_web as _cw
from orchestrator.canopy_web import (  # noqa: F401  (re-exported public API)
    DEFAULT_API,
    TOKEN_FILE,
    AgentIdentityError,
    WorkspaceMismatchError,
    WorkspaceRequiredError,
    resolve_base_url,
    resolve_workspace,
    scoped_api_path,
    scoped_app_path,
)

HOW_TO_SET_WORKSPACE = (
    "set CANOPY_WEB_WORKSPACE=<slug>, or commit `workspace: <slug>` in the target "
    "repo's .canopy/ddd/config.yaml"
)

def resolve_token(token: Optional[str]) -> str:
    """The PAT for DDD calls — strict about agent identity (see module docstring)."""
    return _cw.resolve_token(token, agent_strict=True)


def require_write_workspace(workspace: Optional[str] = None) -> str:
    """The workspace a DDD write goes INTO, resolved like
    :func:`resolve_ddd_workspace`; none named raises
    :class:`WorkspaceRequiredError` naming CANOPY_WEB_WORKSPACE and
    ``.canopy/ddd/config.yaml``."""
    return _cw.require_write_workspace(
        resolve_ddd_workspace(workspace), how_to_set=HOW_TO_SET_WORKSPACE
    )


def confirm_landed(
    app: str,
    obj_id: str,
    workspace: str,
    *,
    query: Optional[dict] = None,
    base_url: Optional[str] = None,
    token: Optional[str] = None,
) -> dict:
    """Read a DDD write back from ``workspace`` (tenant-pinned) and print where it
    landed; raises :class:`WorkspaceMismatchError` if it isn't there."""
    return _cw.confirm_landed(
        app, obj_id, workspace, query=query,
        base_url=resolve_base_url(base_url), token=resolve_token(token),
    )


def resolve_ddd_workspace(
    workspace: Optional[str] = None, *, ddd_dir: Optional[Path] = None
) -> Optional[str]:
    """The canopy-web workspace a DDD run targets, or None (→ flat routes → the
    org default, e.g. ``dimagi``). Writes never go flat: they pass through
    :func:`require_write_workspace`, which refuses on None.

    Precedence: explicit arg → env ``CANOPY_WEB_WORKSPACE`` → per-repo
    ``<repo>/.canopy/ddd/config.yaml`` (``workspace:`` key) → None. The per-repo
    file is how a repo pins its DDD artifacts to a workspace (e.g. the Connect
    repo commits ``workspace: connect``) without anyone remembering an env var.
    """
    ws = resolve_workspace(workspace)  # explicit arg → env → None
    if ws:
        return ws
    try:
        import yaml

        from scripts.ddd.runstate import _resolve_ddd_dir

        d = Path(ddd_dir) if ddd_dir is not None else _resolve_ddd_dir()
        cfg = d / "config.yaml"
        if cfg.exists():
            data = yaml.safe_load(cfg.read_text()) or {}
            val = str(data.get("workspace") or "").strip()
            return val or None
    except Exception:
        return None
    return None


__all__ = [
    "DEFAULT_API",
    "TOKEN_FILE",
    "HOW_TO_SET_WORKSPACE",
    "AgentIdentityError",
    "WorkspaceMismatchError",
    "WorkspaceRequiredError",
    "require_write_workspace",
    "confirm_landed",
    "resolve_base_url",
    "resolve_token",
    "resolve_workspace",
    "resolve_ddd_workspace",
    "scoped_api_path",
    "scoped_app_path",
]
