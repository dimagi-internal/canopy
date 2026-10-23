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
def _no_ambient_device_key(monkeypatch, tmp_path_factory):
    """A resolve attaches a DPoP proof when THIS machine holds a registered device key
    (canopy#946). The suite must not sign with — or create — the real one in
    ~/.canopy/device-key, so point the key dir at an empty temp dir and the Secure
    Enclave helper at nothing."""
    monkeypatch.setenv("CANOPY_DEVICE_KEY_DIR", str(tmp_path_factory.mktemp("device-key")))
    monkeypatch.setenv("CANOPY_DEVICE_KEY_BIN", "/nonexistent/canopy-device-key")
    # Nor probe a real EC2 instance metadata service (aws-iam, canopy-web#1549): a closed
    # local port refuses at once, so "not EC2" costs nothing and a CI VM's IMDS is never hit.
    monkeypatch.setenv("CANOPY_AWS_IMDS_URL", "http://127.0.0.1:9")
    from orchestrator import device_key_aws
    device_key_aws._reset_cache()
    # Nor reuse (or delete) a real runner token this machine cached (canopy-web#1604).
    from orchestrator import runner_session
    monkeypatch.setattr(runner_session, "CACHE", tmp_path_factory.mktemp("runner-session") / "t.json")
    # Nor a turn-scoped agent login cached by a real turn (turn_agent_token).
    from orchestrator import turn_agent_token
    monkeypatch.setattr(turn_agent_token, "CACHE_DIR", tmp_path_factory.mktemp("agent-token"))


_LOOPBACK = {"127.0.0.1", "localhost", "::1", ""}


@pytest.fixture(autouse=True)
def _no_real_network(request, monkeypatch):
    """No test reaches a real host. The suite hides the device key (above) but not the
    developer's token, so a test that falls through to the real transport asks PRODUCTION
    canopy-web as that person with no device proof — and a device-bound agent's owner
    gets a "Resolve refused: bad or missing device proof" alert for it (eva, 2026-10-22:
    test_agent_bootstrap's vault read of the gog clients, four alerts in one evening). A
    non-loopback urlopen fails here the way an unreachable host does, so fail-open code
    takes its offline path. Mark a test ``real_network`` to let it out."""
    if request.node.get_closest_marker("real_network"):
        return
    import urllib.error
    import urllib.parse
    import urllib.request

    real = urllib.request.urlopen

    def guarded(url, *a, **k):
        full = url.full_url if isinstance(url, urllib.request.Request) else str(url)
        host = urllib.parse.urlsplit(full).hostname or ""
        if host not in _LOOPBACK:
            raise urllib.error.URLError(f"tests do not reach {host} (tests/conftest.py)")
        return real(url, *a, **k)

    monkeypatch.setattr(urllib.request, "urlopen", guarded)


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


@pytest.fixture(autouse=True)
def _stub_narrative_source_push(request, monkeypatch):
    """Every narrative post / video upload pins its iteration sources to
    canopy-web, and every pull restores them (scripts/ddd/sources.py). Tests
    about something else must not reach the network for that, so the push is a
    recorder here (calls land in ``request.node.pushed_sources``) and the list a
    pull reads is empty. Mark a test ``real_sources`` to exercise
    the real push with its own injected transport."""
    if request.node.get_closest_marker("real_sources"):
        return
    try:
        from scripts.ddd import sources
    except Exception:  # noqa: BLE001 — a suite that cannot import the DDD scripts
        return
    calls: list = []
    request.node.pushed_sources = calls

    def _record(slug, review_id, files, **_kw):
        calls.append((slug, review_id, dict(files)))
        return {role: {"reused": False} for role in files}

    monkeypatch.setattr(sources, "push_sources", _record)
    monkeypatch.setattr(sources, "list_sources", lambda *a, **k: [])
