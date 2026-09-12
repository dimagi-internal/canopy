"""canopy-agent-factory — stamp out a new Claude Code agent from the canopy operating model.

Zero-runtime-dependency, published separately from the `canopy` distribution so a second
consumer (e.g. a Django web app) can scaffold an agent repo without inheriting canopy's
video-tooling dependency footprint (Pillow, NumPy). See `_factory.py` for the implementation;
this module re-exports only the public surface.
"""
from __future__ import annotations

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

__version__ = "1.0.0"
