"""Connection providers, CLI side (canopy-web plan Task B2): refresh and revoke on the
machine that holds a connection.

Every endpoint comes from the provider's hard-coded rules and the SEALED ``login_host``
— never from canopy-web's app options or anything else the server sends — and a sealed
plaintext naming a host outside the rules is refused before any call. A client id other
than the one sealed in the plaintext is refused too (Ruling R9). No token ever appears in
an exception or a repr.
"""
from __future__ import annotations

import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

import pytest

from orchestrator.connections import providers
from orchestrator.connections.providers import base, google, salesforce
from orchestrator.connections.providers.base import ProviderError, Tokens

# canopy-web tests/test_connections_shared_names.py pins this same dict.
SHARED = {
    "salesforce": {"required_scopes": ("api", "refresh_token"), "rotates_refresh_token": True},
    "google": {"required_scopes": ("openid", "https://www.googleapis.com/auth/userinfo.email"),
               "rotates_refresh_token": False},
}

REFRESH = "5Aep861-SECRET-REFRESH"
ACCESS = "00D-SECRET-ACCESS"
SECRET = "app-client-SECRET"
NOW = 1_793_500_000

PLAIN = {"v": 1, "provider": "salesforce", "refresh_token": REFRESH, "client_id": "3MVG-client",
         "login_host": "login.salesforce.com", "instance_url": "https://acme.my.salesforce.com",
         "account_id": "00Dxx/005xx", "owner_key": "k" * 43, "issued_at": NOW - 100,
         "scopes": "api refresh_token id"}
APP = {"client_id": "3MVG-client", "client_secret": SECRET, "options": {}}

GPLAIN = {**PLAIN, "provider": "google", "login_host": "", "instance_url": "",
          "client_id": "123.apps.googleusercontent.com", "account_id": "1170000",
          "scopes": "openid https://www.googleapis.com/auth/userinfo.email"}
GAPP = {"client_id": "123.apps.googleusercontent.com", "client_secret": SECRET, "options": {}}


@dataclass
class Call:
    url: str
    data: dict
    timeout: float


@dataclass
class FakeHttp:
    """Stands in for ``base.post_form``: records each call, answers from a queue."""
    answers: list = field(default_factory=list)
    calls: list = field(default_factory=list)

    def answer(self, status: int, body):
        self.answers.append(base.HttpResponse(status, body))

    def __call__(self, url, data, *, timeout):
        self.calls.append(Call(url, dict(data), timeout))
        if not self.answers:
            return base.HttpResponse(200, {"access_token": ACCESS, "scope": "api refresh_token"})
        a = self.answers.pop(0)
        if isinstance(a.body, Exception):
            raise a.body
        return a


@pytest.fixture
def http(monkeypatch):
    fake = FakeHttp()
    monkeypatch.setattr(base, "post_form", fake)
    return fake


# ── host rules ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("host,ok", [
    ("login.salesforce.com", True), ("test.salesforce.com", True),
    ("acme.my.salesforce.com", True), ("acme.sandbox.my.salesforce.com", True),
    ("acme--uat.sandbox.my.salesforce.com", True),
    ("my.salesforce.com", False), (".my.salesforce.com", False), ("evil.com", False),
    ("login.salesforce.com.evil.com", False), ("acme.my.salesforce.com.evil.com", False),
    ("", False), ("LOGIN.salesforce.com", False), ("acme.my.salesforce.com:8443", False),
    ("evil.com/x.my.salesforce.com", False), ("evil.com@acme.my.salesforce.com", False),
    ("a..my.salesforce.com", False), ("-acme.my.salesforce.com", False),
    ("acme.my.salesforce.com.", False), ("na1.salesforce.com", False), (None, False), (7, False),
])
def test_login_host_rule(host, ok):
    assert salesforce.login_host_ok(host) is ok


@pytest.mark.parametrize("url,ok", [
    ("https://acme.my.salesforce.com", True), ("https://acme.my.salesforce.com/", True),
    ("https://acme.sandbox.my.salesforce.com", True),
    ("http://acme.my.salesforce.com", False), ("https://na1.salesforce.com", False),
    ("https://acme.my.salesforce.com.evil.com", False), ("https://login.salesforce.com", False),
    ("https://my.salesforce.com", False), ("https://acme.my.salesforce.com:444", False),
    ("https://u:p@acme.my.salesforce.com", False), ("https://acme.my.salesforce.com/path", False),
    ("https://acme.my.salesforce.com?x=1", False), ("https://ACME.my.salesforce.com", False),
    ("", False), (None, False),
])
def test_instance_url_rule(url, ok):
    assert salesforce.instance_ok(url) is ok


# ── Salesforce refresh ──────────────────────────────────────────────────────────

def test_refresh_endpoint_comes_from_the_sealed_login_host_never_the_server(http):
    salesforce.Provider().refresh(
        PLAIN, {**APP, "options": {"login_url": "https://evil.example", "token_url": "https://evil/"}})
    assert [c.url for c in http.calls] == ["https://login.salesforce.com/services/oauth2/token"]
    assert http.calls[0].timeout == 15
    assert http.calls[0].data == {"grant_type": "refresh_token", "refresh_token": REFRESH,
                                  "client_id": "3MVG-client", "client_secret": SECRET}


def test_a_my_domain_login_host_is_its_own_endpoint(http):
    salesforce.Provider().refresh({**PLAIN, "login_host": "acme.my.salesforce.com"}, APP)
    salesforce.Provider().revoke({**PLAIN, "login_host": "test.salesforce.com"}, APP)
    assert [c.url for c in http.calls] == ["https://acme.my.salesforce.com/services/oauth2/token",
                                           "https://test.salesforce.com/services/oauth2/revoke"]


@pytest.mark.parametrize("bad", [{"login_host": "evil.com"}, {"login_host": "my.salesforce.com"},
                                 {"instance_url": "https://evil.com"},
                                 {"instance_url": "http://acme.my.salesforce.com"}])
def test_a_plaintext_naming_a_bad_host_is_refused_before_any_call(http, bad):
    for call in (salesforce.Provider().refresh, salesforce.Provider().revoke):
        with pytest.raises(ProviderError) as e:
            call({**PLAIN, **bad}, APP)
        assert e.value.code == "bad-host"
    assert http.calls == []


def test_a_client_id_other_than_the_sealed_one_is_refused(http):
    for call in (salesforce.Provider().refresh, salesforce.Provider().revoke):
        with pytest.raises(ProviderError) as e:
            call(PLAIN, {**APP, "client_id": "3MVG-another"})
        assert e.value.code == "client-mismatch"
    assert http.calls == []


@pytest.mark.parametrize("plain", [{**PLAIN, "provider": "google"}, {**PLAIN, "refresh_token": ""},
                                   {**PLAIN, "v": 2},
                                   {**PLAIN, "client_id": ""}, {k: v for k, v in PLAIN.items()
                                                                if k != "login_host"}, "not a dict"])
def test_a_malformed_plaintext_is_refused_before_any_call(http, plain):
    with pytest.raises(ProviderError) as e:
        salesforce.Provider().refresh(plain, APP)
    assert e.value.code in ("bad-plaintext", "bad-host")
    assert http.calls == []


@pytest.mark.parametrize("app", [{**APP, "client_secret": ""}, {"client_id": "3MVG-client"}, None])
def test_an_app_without_its_client_pair_is_refused(http, app):
    with pytest.raises(ProviderError) as e:
        salesforce.Provider().refresh(PLAIN, app)
    assert e.value.code == "bad-app"
    assert http.calls == []


def test_salesforce_without_expires_in_lives_access_token_seconds(http):
    t = salesforce.Provider().refresh(PLAIN, {**APP, "options": {"access_token_seconds": 900}}, now=NOW)
    assert t.expires_at == NOW + 900
    t = salesforce.Provider().refresh(PLAIN, APP, now=NOW)
    assert t.expires_at == NOW + 1800  # the default when the app does not say


@pytest.mark.parametrize("seconds,want", [(10, 300), (10**9, 86400), ("x", 1800), (True, 1800)])
def test_an_unreasonable_access_token_seconds_is_bounded(http, seconds, want):
    t = salesforce.Provider().refresh(PLAIN, {**APP, "options": {"access_token_seconds": seconds}}, now=NOW)
    assert t.expires_at == NOW + want


def test_a_rotated_refresh_token_is_returned(http):
    http.answer(200, {"access_token": ACCESS, "refresh_token": "ROTATED", "scope": "api refresh_token",
                      "instance_url": "https://acme.my.salesforce.com"})
    t = salesforce.Provider().refresh(PLAIN, APP, now=NOW)
    assert (t.access_token, t.refresh_token, t.scope) == (ACCESS, "ROTATED", "api refresh_token")


def test_no_returned_refresh_token_is_empty_and_scope_falls_back_to_the_sealed_one(http):
    http.answer(200, {"access_token": ACCESS})
    t = salesforce.Provider().refresh(PLAIN, APP, now=NOW)
    assert t.refresh_token == "" and t.scope == "api refresh_token id"


def test_tokens_never_appear_in_repr():
    t = Tokens(access_token=ACCESS, refresh_token=REFRESH, expires_at=NOW, scope="api")
    assert ACCESS not in repr(t) and REFRESH not in repr(t)


# ── provider answers ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status,body,code,provider_code", [
    (400, {"error": "invalid_grant", "error_description": "expired access/refresh token"},
     "rejected", "invalid_grant"),
    (200, {"error": "invalid_grant"}, "rejected", "invalid_grant"),
    (401, None, "unavailable", ""),
    (302, None, "unavailable", ""),
    (503, {"error": "busy"}, "unavailable", ""),
    (200, None, "unavailable", ""),
    (200, {"token_type": "Bearer"}, "no-access-token", ""),
])
def test_a_refusal_is_typed_and_carries_no_token(http, status, body, code, provider_code):
    http.answer(status, body)
    with pytest.raises(ProviderError) as e:
        salesforce.Provider().refresh(PLAIN, APP)
    assert (e.value.code, e.value.provider_code) == (code, provider_code)
    for secret in (REFRESH, SECRET, ACCESS):
        assert secret not in str(e.value) and secret not in e.value.detail


def test_a_transport_failure_is_unavailable_without_the_form(http):
    http.answers.append(base.HttpResponse(0, OSError(f"boom {REFRESH}")))
    with pytest.raises(ProviderError) as e:
        salesforce.Provider().refresh(PLAIN, APP)
    assert e.value.code == "unavailable" and REFRESH not in str(e.value)


def test_a_refusal_detail_echoing_a_secret_is_scrubbed(http):
    http.answer(400, {"error": "invalid_client",
                      "error_description": f"bad {SECRET} for {REFRESH}"})
    with pytest.raises(ProviderError) as e:
        salesforce.Provider().refresh(PLAIN, APP)
    assert SECRET not in str(e.value) and REFRESH not in str(e.value)
    assert e.value.provider_code == "invalid_client"


def test_a_form_encoded_secret_is_scrubbed_too():
    token = "5Aep/861!token"
    assert base._scrub(f"x {quote(token, safe='')} y", (token,)) == "x [redacted] y"


def test_salesforce_revoke_posts_the_refresh_token_to_the_sealed_host(http):
    http.answer(200, None)
    salesforce.Provider().revoke(PLAIN, APP)
    assert http.calls[0].url == "https://login.salesforce.com/services/oauth2/revoke"
    assert http.calls[0].data == {"token": REFRESH, "token_type_hint": "refresh_token",
                                  "client_id": "3MVG-client", "client_secret": SECRET}


def test_a_refused_revoke_raises(http):
    http.answer(400, {"error": "unsupported_token_type"})
    with pytest.raises(ProviderError) as e:
        salesforce.Provider().revoke(PLAIN, APP)
    assert (e.value.code, e.value.provider_code) == ("rejected", "unsupported_token_type")


# ── Google ──────────────────────────────────────────────────────────────────────

def test_google_endpoints_are_constants(http):
    assert google.TOKEN_URL == "https://oauth2.googleapis.com/token"
    assert google.REVOKE_URL == "https://oauth2.googleapis.com/revoke"
    http.answer(200, {"access_token": ACCESS, "expires_in": 3599, "scope": "openid"})
    t = google.Provider().refresh(GPLAIN, {**GAPP, "options": {"token_url": "https://evil/"}}, now=NOW)
    http.answer(200, {})
    google.Provider().revoke(GPLAIN, {**GAPP, "options": {"revoke_url": "https://evil/"}})
    assert [c.url for c in http.calls] == [google.TOKEN_URL, google.REVOKE_URL]
    assert t.expires_at == NOW + 3599 and t.refresh_token == ""
    assert http.calls[1].data["token"] == REFRESH


@pytest.mark.parametrize("bad", [{"login_host": "accounts.google.com"},
                                 {"instance_url": "https://evil.com"}])
def test_google_refuses_a_plaintext_naming_any_host(http, bad):
    with pytest.raises(ProviderError) as e:
        google.Provider().refresh({**GPLAIN, **bad}, GAPP)
    assert e.value.code == "bad-host" and http.calls == []


def test_google_client_mismatch(http):
    with pytest.raises(ProviderError) as e:
        google.Provider().revoke(GPLAIN, {**GAPP, "client_id": "999.apps.googleusercontent.com"})
    assert e.value.code == "client-mismatch" and http.calls == []


def test_google_does_not_revoke_a_refused_grant_salesforce_does():
    assert google.Provider.revokes_refused_grant is False
    assert salesforce.Provider.revokes_refused_grant is True


# ── registry and shared names ───────────────────────────────────────────────────

def test_shared_names_match_canopy_web():
    assert set(SHARED) == set(providers.NAMES)
    for name, want in SHARED.items():
        p = providers.get(name)
        assert p.name == name
        assert (p.required_scopes, p.rotates_refresh_token) == (
            want["required_scopes"], want["rotates_refresh_token"])


def test_an_unknown_provider_is_a_provider_error():
    with pytest.raises(ProviderError) as e:
        providers.get("intacct")
    assert e.value.code == "unknown-provider"


def test_default_transport_refuses_redirects_and_non_https():
    with pytest.raises(ProviderError) as e:
        base.post_form("http://login.salesforce.com/services/oauth2/token", {}, timeout=15)
    assert e.value.code == "bad-host"
    assert base._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil/") is None


def test_the_real_opener_does_not_follow_a_redirect():
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/stolen")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/token", data=b"x=1", method="POST")
        with pytest.raises(urllib.error.HTTPError) as e:
            base._OPENER.open(req, timeout=5)
        assert e.value.code == 302 and hits == ["/token"]
    finally:
        server.shutdown()
        server.server_close()
