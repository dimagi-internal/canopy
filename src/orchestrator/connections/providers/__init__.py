"""Connection providers, CLI side: refresh and revoke on the machine that holds a
connection, every endpoint derived from hard-coded rules (see :mod:`.base`). canopy-web's
``apps/connections/providers`` does the rest of OAuth; a test on each side pins the names
they share."""
from __future__ import annotations

from orchestrator.connections.providers import google, salesforce
from orchestrator.connections.providers.base import Provider, ProviderError, Tokens

_CLASSES = {cls.name: cls for cls in (salesforce.Provider, google.Provider)}
NAMES = tuple(_CLASSES)

__all__ = ["NAMES", "Provider", "ProviderError", "Tokens", "get"]


def get(name: str) -> Provider:
    """The provider called ``name``, or ``ProviderError("unknown-provider")``."""
    cls = _CLASSES.get(name)
    if cls is None:
        raise ProviderError("unknown-provider", "this CLI has no such connection provider")
    return cls()
