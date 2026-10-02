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
