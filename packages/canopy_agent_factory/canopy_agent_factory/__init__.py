"""canopy-agent-factory — stamp out a new Claude Code agent from the canopy operating model.

Zero-runtime-dependency, published separately from the `canopy` distribution so a second
consumer (e.g. a Django web app) can scaffold an agent repo without inheriting canopy's
video-tooling dependency footprint (Pillow, NumPy). See `_factory.py` for the implementation;
this module re-exports only the public surface.
"""
from __future__ import annotations

from importlib import metadata as _metadata

from ._factory import (
    AgentFactoryError,
    AgentSpec,
    create_agent,
    gating_config,
    normalize_slug,
    templates,
)

__all__ = [
    "AgentFactoryError",
    "AgentSpec",
    "create_agent",
    "gating_config",
    "normalize_slug",
    "templates",
]

# Single-sourced from pyproject.toml's `[project] version` — the installed distribution's
# metadata, or that file itself in a source tree nobody installed — so the two can never
# drift (1.2.0's release failed on a hand-kept copy that still said 1.1.0).
def _version() -> str:
    try:
        return _metadata.version("canopy-agent-factory")
    except _metadata.PackageNotFoundError:
        import tomllib
        from pathlib import Path

        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        try:
            return tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
        except (OSError, KeyError, ValueError):
            return "0+unknown"


__version__ = _version()
