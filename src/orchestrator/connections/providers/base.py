"""The provider contract, CLI side: refresh and revoke a connection on the machine that
holds it (canopy-web plan "Connections hold nothing canopy-web can use", Task B2).

canopy-web keeps the browser legs and the code exchange (its ``apps/connections/
providers``); the machines that hold a connection's sealed refresh token refresh and
revoke it here. The inputs are the opened ``refresh`` plaintext (Part 0.2 — ``provider``,
``refresh_token``, ``client_id``, ``login_host``, ``instance_url``, ``scopes`` …) and the
app block canopy-web sends with the material (``{client_id, client_secret, options}``).

The rules that make a lying server harmless:

* **Endpoints come from hard-coded provider rules** and the SEALED ``login_host`` — never
  from the app's ``options`` or anything else the server sends. ``check_plain`` refuses a
  plaintext whose hosts break the rules (``bad-host``) before any call.
* **The app's client id must be the sealed one** (Ruling R9): otherwise ``client-mismatch``,
  so a server cannot swap in a client of its own.
* Only ``options.access_token_seconds`` is read from the app, and it is bounded.
* The default transport is https only and follows no redirect, so a token form goes to
  exactly the derived URL.
* No token or client secret appears in any exception or repr; a provider's own error text
  is scrubbed of them before it is kept.

The HTTP call is injectable: :func:`post_form` is looked up at call time, so a test (or a
caller) replaces it; it takes ``(url, form, *, timeout)`` and answers an
:class:`HttpResponse`. Every call has a 15 s timeout (canopy-web's ``HTTP_TIMEOUT``).
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, ClassVar, Optional

HTTP_TIMEOUT = 15
#: How long a provider's error text may be once kept.
_DETAIL_MAX = 300
#: The fields a ``refresh`` plaintext must carry for refresh or revoke.
_PLAIN_STR_FIELDS = ("provider", "refresh_token", "client_id", "login_host", "instance_url")


class ProviderError(Exception):
    """A refresh or revoke refused, here or by the provider. ``code`` says which:

    * ``bad-host`` — a sealed host breaks the provider's rules (no call made).
    * ``client-mismatch`` — the app's client id is not the sealed one (no call made).
    * ``bad-plaintext`` / ``bad-app`` — a field missing or of the wrong kind (no call made).
    * ``unknown-provider`` — no provider by that name.
    * ``rejected`` — the provider refused (4xx, or 200 carrying ``error``);
      ``provider_code`` is its own error string (``invalid_grant``: reconnect).
    * ``unavailable`` — no sensible answer (transport failure, 5xx, a redirect, a body that
      is not JSON): says nothing about the grant, so never "reconnect".
    * ``no-access-token`` — a 200 without an access token.

    ``detail`` is safe to log: it never carries a token or the client secret."""

    def __init__(self, code: str, detail: str = "", *, provider_code: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
        self.provider_code = provider_code


@dataclass(frozen=True)
class Tokens:
    """A refresh's answer. ``refresh_token`` is a NEW refresh token when the provider
    rotated it, else ``""`` (keep the sealed one). ``expires_at`` is epoch seconds."""

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: int
    scope: str


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: Any = field(default=None, repr=False)  # the JSON answer, or None when it was not JSON


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A token form goes to the derived URL and nowhere else: a redirect is an answer."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _json_or_none(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8")) if raw else None
    except (ValueError, UnicodeDecodeError):
        return None


def post_form(url: str, data: dict, *, timeout: float) -> HttpResponse:
    """POST ``data`` form-encoded to ``url`` (https only, no redirects) and answer its
    status and JSON body. A transport failure is ``ProviderError("unavailable")`` naming
    only the exception type, never the form."""
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ProviderError("bad-host", "provider endpoints are https only")
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST",
                                 headers={"Accept": "application/json",
                                          "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:  # noqa: S310 - https, derived URL
            return HttpResponse(resp.status, _json_or_none(resp.read()))
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
        except Exception:  # noqa: BLE001
            raw = b""
        return HttpResponse(e.code, _json_or_none(raw))
    except Exception as e:  # noqa: BLE001 - every transport failure is "unavailable"
        raise ProviderError("unavailable", f"could not reach the provider ({type(e).__name__})") from None


def _scrub(text: str, secrets: tuple[str, ...]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "[redacted]").replace(urllib.parse.quote(s, safe=""), "[redacted]")
    text = re.sub(r"[^\x20-\x7e]", " ", text)
    return text[:_DETAIL_MAX]


class Provider:
    """One OAuth 2.0 provider's refresh and revoke. Stateless: every call takes the
    opened ``refresh`` plaintext and the app block."""

    name: ClassVar[str]
    title: ClassVar[str]
    required_scopes: ClassVar[tuple[str, ...]] = ()
    #: Whether a refresh may hand back a NEW refresh token replacing the sealed one.
    rotates_refresh_token: ClassVar[bool] = False
    #: Whether a grant refused after the exchange is revoked (Google: no — revoking there
    #: ends every token of the account on that OAuth client). canopy-web's flag; the
    #: keeper revokes a disconnect only when told ``revoke_at_provider``.
    revokes_refused_grant: ClassVar[bool] = True
    default_access_token_seconds: ClassVar[int] = 3600
    #: Bounds on the app's ``access_token_seconds`` (canopy-web's Salesforce bounds).
    min_access_token_seconds: ClassVar[int] = 300
    max_access_token_seconds: ClassVar[int] = 86400

    # ---- what a provider module fills in ------------------------------------

    def check_hosts(self, plain: dict) -> None:
        """Raise ``ProviderError("bad-host")`` unless the plaintext's hosts obey the rules."""
        raise NotImplementedError

    def token_url(self, plain: dict) -> str:
        raise NotImplementedError

    def revoke_url(self, plain: dict) -> str:
        raise NotImplementedError

    # ---- the contract -------------------------------------------------------

    def check_plain(self, plain: dict) -> None:
        """Refuse a ``refresh`` plaintext this provider must not act on: the wrong
        provider or a missing field (``bad-plaintext``), a host outside the rules
        (``bad-host``)."""
        if not isinstance(plain, dict):
            raise ProviderError("bad-plaintext", "the sealed value is not an object")
        if plain.get("v") != 1:
            raise ProviderError("bad-plaintext", "the sealed value is not a version this CLI reads")
        for k in _PLAIN_STR_FIELDS:
            if not isinstance(plain.get(k), str):
                raise ProviderError("bad-plaintext", f"the sealed value has no {k}")
        if plain["provider"] != self.name:
            raise ProviderError("bad-plaintext", f"the sealed value is not a {self.title} connection")
        if not plain["refresh_token"] or not plain["client_id"]:
            raise ProviderError("bad-plaintext", "the sealed value has no refresh token or client id")
        self.check_hosts(plain)

    def refresh(self, plain: dict, app: dict, *, now: Optional[float] = None, http=None) -> Tokens:
        """Refresh at the provider. A returned refresh token (rotation) comes back in
        ``Tokens.refresh_token``; the caller writes it back before relying on it. ``http``
        replaces :func:`post_form` for this call."""
        client_id, secret = self._checked(plain, app)
        form = {"grant_type": "refresh_token", "refresh_token": plain["refresh_token"],
                "client_id": client_id, "client_secret": secret}
        secrets = (plain["refresh_token"], secret)
        now = time.time() if now is None else now
        resp = self._post(self.token_url(plain), form, "refresh", http)
        body = resp.body
        if resp.status != 200 or (isinstance(body, dict) and "error" in body):
            raise self._error(resp, "refresh", secrets)
        if not isinstance(body, dict):
            raise ProviderError("unavailable", f"{self.title} refresh answered {resp.status} without JSON")
        access = body.get("access_token")
        if not isinstance(access, str) or not access:
            raise ProviderError("no-access-token", f"{self.title} refresh returned no access token")
        rotated = body.get("refresh_token")
        scope = body.get("scope")
        return Tokens(
            access_token=access,
            refresh_token=rotated if isinstance(rotated, str) else "",
            expires_at=int(now) + self._lifetime(body, app),
            scope=scope if isinstance(scope, str) and scope else str(plain.get("scopes") or ""),
        )

    def revoke(self, plain: dict, app: dict) -> None:
        """Revoke the sealed refresh token at the provider (RFC 7009), with the client's
        authentication. Raises on any answer but 200. Whether to revoke at all is the
        caller's (a disconnect only when told ``revoke_at_provider``)."""
        client_id, secret = self._checked(plain, app)
        form = {"token": plain["refresh_token"], "token_type_hint": "refresh_token",
                "client_id": client_id, "client_secret": secret}
        secrets = (plain["refresh_token"], secret)
        resp = self._post(self.revoke_url(plain), form, "revoke")
        if resp.status != 200:
            raise self._error(resp, "revoke", secrets)

    # ---- plumbing -----------------------------------------------------------

    def _checked(self, plain: dict, app: dict) -> tuple[str, str]:
        """The plaintext checked, then the app's (client_id, client_secret) — the sealed
        client id, or ``client-mismatch``."""
        self.check_plain(plain)
        if not isinstance(app, dict):
            raise ProviderError("bad-app", "no app client was sent with the connection")
        client_id, secret = app.get("client_id"), app.get("client_secret")
        if not isinstance(client_id, str) or not client_id or not isinstance(secret, str) or not secret:
            raise ProviderError("bad-app", f"the workspace's {self.title} app has no client id and secret")
        if client_id != plain["client_id"]:
            raise ProviderError("client-mismatch", f"the workspace's {self.title} app is not the client "
                                                   "this connection was made with — reconnect it")
        return client_id, secret

    def _lifetime(self, body: dict, app: dict) -> int:
        """Seconds the access token lives: the answer's ``expires_in``, else the app's
        ``access_token_seconds`` (bounded), else the provider default."""
        given = body.get("expires_in")
        if isinstance(given, (int, str)) and not isinstance(given, bool):
            try:
                seconds = int(given)
                if seconds > 0:
                    return seconds
            except ValueError:
                pass
        options = app.get("options") if isinstance(app.get("options"), dict) else {}
        seconds = options.get("access_token_seconds")
        if not isinstance(seconds, int) or isinstance(seconds, bool):
            return self.default_access_token_seconds
        return max(self.min_access_token_seconds, min(self.max_access_token_seconds, seconds))

    def _post(self, url: str, form: dict, what: str, http=None) -> HttpResponse:
        try:
            return (http or post_form)(url, form, timeout=HTTP_TIMEOUT)
        except ProviderError:
            raise
        except Exception as e:  # noqa: BLE001 - an injected transport's failure, typed
            raise ProviderError("unavailable", f"{self.title} {what} failed ({type(e).__name__})") from None

    def _error(self, resp: HttpResponse, what: str, secrets: tuple[str, ...]) -> ProviderError:
        """A refused answer as an error: a 5xx, a redirect or a body that is not JSON is the
        provider unavailable; anything else a rejection carrying the provider's own code."""
        body = resp.body
        if not isinstance(body, dict) or resp.status >= 500 or 300 <= resp.status < 400:
            return ProviderError("unavailable", f"{self.title} {what} failed ({resp.status})")
        code = _scrub(str(body.get("error") or ""), secrets)
        why = _scrub(str(body.get("error_description") or code), secrets)
        detail = f"{self.title} {what} refused ({resp.status})" + (f": {why}" if why else "")
        return ProviderError("rejected", detail, provider_code=code)
