"""Shared fixtures for the DDD tests."""
import pytest


@pytest.fixture
def pinned_write_workspace(monkeypatch):
    """A DDD write with its workspace configured, and the post-write read-back
    stubbed — for tests about WHAT a write sends, not where it lands (that is
    tests/ddd/test_write_guards.py). Returns the workspace slug."""
    from scripts.ddd import auth

    monkeypatch.setenv("CANOPY_WEB_WORKSPACE", "connect")
    monkeypatch.setattr(auth._cw, "confirm_landed", lambda *a, **k: {})
    return "connect"
