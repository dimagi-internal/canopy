"""Suite-wide isolation.

The macOS keyring check (`agent_email.session_keyring_problems`) reads launchd's
environment and the real home's agent .env files. Left live, every preflight-based
test would pass or fail by the developer's own Mac setup — so it is off by default here
and switched on, with every input faked, only by the tests that are about it.
"""
import pytest


@pytest.fixture(autouse=True)
def _keyring_check_off_by_default(monkeypatch):
    from orchestrator import agent_email

    monkeypatch.setattr(agent_email, "_PLATFORM", "linux")


@pytest.fixture(autouse=True)
def _canopy_web_session_source_off_by_default(monkeypatch):
    """The canopy-web session source joins the default set whenever a PAT resolves —
    which it does on any developer laptop. Keep it out unless a test opts in, so no
    test reaches the network or depends on the developer's token."""
    monkeypatch.setenv("CANOPY_SESSION_WEB", "0")


@pytest.fixture(autouse=True)
def _no_ambient_agent_identity(monkeypatch):
    """`$CANOPY_AGENT` / `$CANOPY_AGENT_SLUG` mark a session as an agent's, which
    changes which canopy-web PAT resolves (and makes DDD writes refuse the operator's
    token). A suite run inside an agent turn inherits them — so clear them, and let
    the tests about agent identity set them explicitly."""
    monkeypatch.delenv("CANOPY_AGENT", raising=False)
    monkeypatch.delenv("CANOPY_AGENT_SLUG", raising=False)
