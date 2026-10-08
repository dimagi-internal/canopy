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
    monkeypatch.delenv("CANOPY_ALLOW_OPERATOR_IDENTITY", raising=False)


@pytest.fixture(autouse=True)
def _no_ambient_dispatched_turn(monkeypatch, tmp_path_factory):
    """A dispatched turn's caller envelope also marks the session as an agent's
    (canopy#813), and is found from env or — on a laptop — from the emdash task the
    cwd belongs to. A suite run inside a turn would inherit the real one, so point
    the envelope root at an empty dir and clear the env that names a turn."""
    from orchestrator import canopy_web, provenance

    for var in ("CANOPY_CALLER", "CANOPY_TURN_ID", "CANOPY_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(provenance, "CALLER_ROOT", tmp_path_factory.mktemp("caller"))
    canopy_web._reset_identity_announcements()


@pytest.fixture(autouse=True)
def _write_identity_readback_off_by_default(monkeypatch):
    """Every write with a resolved token first reads `/api/me/` back (canopy#813).
    Tests that fake the transport and count its calls are about their own route, so
    the read-back is off unless a test asks for it via `real_identity_readback`."""
    from orchestrator import canopy_web

    monkeypatch.setattr(canopy_web, "announce_identity", lambda base, tok, transport: "")


@pytest.fixture
def real_identity_readback(monkeypatch):
    """Opt a test back into the real `/api/me/` read-back on writes."""
    from orchestrator import canopy_web

    monkeypatch.setattr(canopy_web, "announce_identity", canopy_web._announce_identity_impl)
