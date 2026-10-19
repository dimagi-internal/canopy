"""Where an agent's resolved env lives: ``$CANOPY_AGENT_ENV_FILE`` first, else ``~/.<slug>/.env``.

A runner that keeps secrets off disk (canopy-web runner isolation units, spec §4) writes
each turn's values to a tmpfs file (``/run/canopy/<uid>/<turn>/env``, 0600) that is gone
when the turn ends, and hands the turn its path as ``CANOPY_AGENT_ENV_FILE``. Every reader
of an agent's env asks :func:`agent_env_path` so it finds that file first. It has to stay a
FILE (not just process env): Claude Code strips secret variables before an MCP
``headersHelper`` runs, so the helper reads the agent's PAT from disk.

The turn file belongs to ONE agent: the turn's (``$CANOPY_AGENT_SLUG`` / ``$CANOPY_AGENT``).
A reader asking for a different slug never gets it — it gets that slug's legacy path, as
before. A named file that does not exist (yet) also falls back to the legacy path, so a
runner on older code, or a turn whose file was never written, behaves exactly as today.

Writers (``canopy cred env`` without ``--out``, ``cred sync``, the SessionStart hook) never
write the turn file: the runner owns it.

stdlib only.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional

ENV_VAR = "CANOPY_AGENT_ENV_FILE"
#: The variables that name the turn's agent (strongest first), as cred_cli / gating_guard.
AGENT_VARS = ("CANOPY_AGENT_SLUG", "CANOPY_AGENT")


def legacy_env_path(slug: str, home: Optional[Path] = None) -> Path:
    """``~/.<slug>/.env`` — where every agent's env lived before per-turn files."""
    return (home or Path.home()) / f".{slug}" / ".env"


def turn_env_file(slug: str, environ: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    """``$CANOPY_AGENT_ENV_FILE`` when it is set, names an existing file, and this session's
    agent is ``slug`` (or the session names no agent); else None."""
    env = os.environ if environ is None else environ
    raw = (env.get(ENV_VAR) or "").strip()
    if not raw or not slug:
        return None
    for var in AGENT_VARS:
        owner = (env.get(var) or "").strip().lower()
        if owner:
            if owner != slug.lower():
                return None
            break
    path = Path(raw).expanduser()
    return path if path.is_file() else None


def agent_env_path(slug: str, home: Optional[Path] = None,
                   environ: Optional[Mapping[str, str]] = None) -> Path:
    """The env file to READ for agent ``slug``: the turn's file, else the legacy home."""
    return turn_env_file(slug, environ) or legacy_env_path(slug, home)


def read_env_value(slug: str, key: str, home: Optional[Path] = None,
                   environ: Optional[Mapping[str, str]] = None) -> str:
    """``key`` out of the agent's env file (see :func:`agent_env_path`), or ""."""
    try:
        text = agent_env_path(slug, home, environ).read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if line.startswith(f"{key}="):
            return line.partition("=")[2].strip().strip('"').strip("'")
    return ""
