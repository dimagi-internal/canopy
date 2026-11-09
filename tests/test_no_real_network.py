"""tests/conftest.py keeps the suite off real hosts.

A test that falls through to the real transport would ask PRODUCTION canopy-web, as the
developer, with the device key hidden — so every suite run would raise a "Resolve refused: bad
or missing device proof" alert to a device-bound agent's owner."""
import urllib.error
import urllib.request

import pytest

from orchestrator import cred_resolve


def test_a_real_host_is_refused_like_an_unreachable_one():
    with pytest.raises(urllib.error.URLError, match="tests do not reach canopy.dimagi.com"):
        urllib.request.urlopen("https://canopy.dimagi.com/api/me/")


def test_a_resolve_never_leaves_the_machine(monkeypatch):
    monkeypatch.setenv("CANOPY_WEB_PAT", "cpat_not-a-real-token")
    with pytest.raises(urllib.error.URLError):
        cred_resolve.resolve("eva", refs=["gog-oauth-client"], base_url="https://canopy.dimagi.com")
