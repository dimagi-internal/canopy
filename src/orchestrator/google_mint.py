"""Finish an agent's Google sign-in on a runner — canopy-web never sees the token.

"Connect Google mailbox" on canopy-web starts a sign-in for an agent and hands the work to
one of the agent's runners (a box whose owner is an admin of the agent). The box makes
the PKCE verifier and keeps it; canopy-web builds the consent link from its challenge and
relays the single-use code Google sends back to it. A code is useless without the
verifier, so canopy-web, which only ever sees the code, cannot redeem it.

This module is the box's half once the code arrives (``canopy vault google-mint``):

1. read the OAuth client's secret from the agent's vault (the workspace's shared
   ``gog-oauth-client-web``, the same item gog refreshes the token with),
2. trade code + verifier + secret for a refresh token at Google,
3. refuse what would only look like success (no refresh token, no account, the wrong
   account),
4. build the token ``gog auth tokens import`` accepts, and
5. encrypt it for the agent: under the vault key (``zk1``) when this box holds a copy,
   else sealed (``zks1``) to the readers canopy-web listed.

It prints one JSON line — ``{name, kv, value_zk, account}`` or ``{error, detail}`` — and
the runner posts that to canopy-web. The plaintext token lives only in this process.

A PERSON's own account, delegated to the agent (canopy-web ``google_mint.start_delegated``;
the work says ``delegated`` and names the entry, ``google-delegated-<account>``): the token
is sealed (``zks1``) under that name to exactly the readers canopy-web listed — the
machines that may hold a credential of theirs and their own vault key — and NEVER under
the vault key, even when this box holds a copy (every vault member could read it then).
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Optional

from orchestrator import vault_zk as zk

TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOG_TOKEN = "gog-token"
#: gog's client name -> the shared vault item holding that OAuth client's id + secret
#: (agent_bootstrap.SHARED_CLIENT_ITEMS: the same items a box refreshes tokens with).
CLIENT_ITEMS = {"canopy-web": "gog-oauth-client-web", "canopy": "gog-oauth-client"}

# gog's short service names, keyed by the scope-path prefix that implies them.
_SERVICE_BY_SCOPE_PREFIX: tuple[tuple[str, str], ...] = (
    ("https://www.googleapis.com/auth/gmail", "gmail"),
    ("https://www.googleapis.com/auth/documents", "docs"),
    ("https://www.googleapis.com/auth/drive", "drive"),
    ("https://www.googleapis.com/auth/forms", "forms"),
    ("https://www.googleapis.com/auth/spreadsheets", "sheets"),
    ("https://www.googleapis.com/auth/presentations", "slides"),
    ("https://www.googleapis.com/auth/calendar", "calendar"),
)

Post = Callable[[str, dict], dict]


class MintError(Exception):
    """A sign-in that must not be stored. ``error`` is the outcome canopy-web shows
    (``exchange-failed``, ``no-refresh-token``, ``no-email``, ``wrong-account``,
    ``no-client``, ``zk-no-recipients``, ``bad-request``); ``detail`` says why."""

    def __init__(self, error: str, detail: str, *, account: str = ""):
        super().__init__(detail)
        self.error, self.detail, self.account = error, detail, account

    def out(self) -> dict:
        body = {"error": self.error, "detail": self.detail[:900]}
        if self.account:
            body["account"] = self.account
        return body


def services_for(scopes) -> list[str]:
    """gog's `services`, derived from the scopes Google actually GRANTED — a person can
    uncheck one on the consent screen, and a token claiming a service it lacks fails at
    call time instead of now."""
    out = set()
    for scope in scopes:
        for prefix, service in _SERVICE_BY_SCOPE_PREFIX:
            if scope == prefix or scope.startswith(prefix + "."):
                out.add(service)
    return sorted(out)


def build_gog_token(*, email: str, refresh_token: str, granted_scopes, client: str,
                    now: Optional[datetime] = None) -> dict:
    """The exact on-disk shape `gog auth tokens import` accepts."""
    scopes = sorted(set(granted_scopes))
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return {
        "email": email,
        "client": client,
        "services": services_for(scopes),
        "scopes": scopes,
        "created_at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "refresh_token": refresh_token,
    }


def email_from_id_token(id_token: str) -> str:
    """The account out of the id_token's payload. No signature check, safely: this JWT
    came back over TLS from Google's token endpoint in a request this process made."""
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return str(json.loads(base64.urlsafe_b64decode(payload)).get("email") or "")
    except (IndexError, ValueError):
        return ""


def client_secret(raw: Optional[str], client_id: str) -> str:
    """The secret out of a client file (Google's download — ``{"web": {...}}`` or
    ``{"installed": {...}}`` — or gog's flat ``{client_id, client_secret}``), checked to be
    the client canopy-web signed in with: a code is bound to that client."""
    try:
        data = json.loads(raw or "")
    except ValueError:
        data = None
    if isinstance(data, dict):
        for key in ("web", "installed"):
            if isinstance(data.get(key), dict):
                data = data[key]
                break
    if not isinstance(data, dict) or not data.get("client_secret"):
        raise MintError("no-client", "this box cannot read the Google OAuth client from the "
                        "agent's vault (the workspace's shared vault item "
                        f"'{CLIENT_ITEMS['canopy-web']}') — give the box a key to that vault")
    if client_id and data.get("client_id") and str(data["client_id"]) != client_id:
        raise MintError("no-client", "the Google OAuth client in the vault is not the one "
                        "canopy-web signed in with, so the code cannot be exchanged")
    return str(data["client_secret"])


def _post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=20) as res:  # noqa: S310 - fixed https endpoint
        return json.loads(res.read().decode())


def exchange_code(*, code: str, verifier: str, client_id: str, secret: str, redirect_uri: str,
                  post: Optional[Post] = None) -> dict:
    """Trade the code (and the verifier only this box holds) for Google's token answer."""
    try:
        return (post or _post_form)(TOKEN_ENDPOINT, {
            "code": code, "code_verifier": verifier, "client_id": client_id,
            "client_secret": secret, "redirect_uri": redirect_uri,
            "grant_type": "authorization_code"})
    except urllib.error.HTTPError as e:
        try:
            why = json.loads(e.read().decode()).get("error_description") or str(e)
        except Exception:  # noqa: BLE001
            why = str(e)
        raise MintError("exchange-failed", f"Google refused the code: {why}"[:600]) from None
    except Exception as e:  # noqa: BLE001 — the person retries; say what happened
        raise MintError("exchange-failed", f"could not reach Google's token endpoint: {e}"[:600]) from None


def _readers(rows) -> list:
    """canopy-web's recipient rows as ``(kind, target, jwk)``, each key checked to be a real
    P-256 key and a device's target to be its own thumbprint."""
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        kind, target, jwk = str(row.get("kind") or ""), str(row.get("target") or ""), row.get("public_jwk")
        if kind not in ("device", "member") or not target or not isinstance(jwk, dict):
            continue
        try:
            zk.public_key_from_jwk(jwk)
            if kind == "device" and zk.jwk_thumbprint(jwk) != target:
                continue
        except (zk.ZkError, KeyError, TypeError, ValueError):
            continue
        out.append((kind, target, {k: jwk[k] for k in ("kty", "crv", "x", "y")}))
    return out


def encrypt_for_agent(value: str, *, agent_id: str, vk: Optional[bytes], kv, recipients,
                      name: str = GOG_TOKEN) -> tuple:
    """``(kv, value_zk)``: under the vault key at its kv when this box holds it (``vk``),
    else sealed to the readers canopy-web listed (at canopy-web's kv), which a vault-key
    holder later settles under the key. A delegated value passes ``vk=None``: sealed only."""
    if vk is not None and kv:
        return kv, zk.encrypt_value(vk, agent_id, name, kv, value)
    readers = _readers(recipients)
    if not readers:
        raise MintError("zk-no-recipients", "nobody can read this agent's secrets yet — set up "
                        "its vault (a passkey, or an approved machine) first")
    return kv or 0, zk.seal_value(agent_id, name, kv or 0, value, readers)


DELEGATED_PREFIX = "google-delegated-"


def delegated_name(work: dict) -> str:
    """The entry a delegated sign-in is sealed under, checked to be the account's own
    (``google-delegated-<expected_account>``). Raises MintError."""
    name = str(work.get("name") or "")
    expected = str(work.get("expected_account") or "").strip().lower()
    if not expected or name != DELEGATED_PREFIX + expected:
        raise MintError("bad-request", "a delegated Google sign-in must name the account it is for "
                        "and its entry (google-delegated-<account>) — start it again")
    return name


def finish(work: dict, *, client_raw: Optional[str], vk: Optional[bytes], vk_kv=None,
           post: Optional[Post] = None, now: Optional[datetime] = None) -> dict:
    """The box's half of the sign-in, given what canopy-web handed it (``work``: code,
    redirect_uri, client_id, client, expected_account, agent_id, kv, recipients — plus the
    ``code_verifier`` the runner kept), the client file from the vault, and the vault key if
    this box holds it. Returns what to post to canopy-web. Raises MintError."""
    delegated = bool(work.get("delegated"))
    name = delegated_name(work) if delegated else GOG_TOKEN
    code, verifier = str(work.get("code") or ""), str(work.get("code_verifier") or "")
    client_id, redirect = str(work.get("client_id") or ""), str(work.get("redirect_uri") or "")
    agent_id = str(work.get("agent_id") or "")
    if not (code and verifier and client_id and redirect and agent_id):
        raise MintError("bad-request", "the sign-in is missing its code, verifier, client, "
                        "redirect or agent — start it again")
    secret = client_secret(client_raw, client_id)
    tokens = exchange_code(code=code, verifier=verifier, client_id=client_id, secret=secret,
                           redirect_uri=redirect, post=post)
    refresh = str(tokens.get("refresh_token") or "")
    if not refresh:
        # Google withholds it when the account already consented and `prompt` was not
        # forced; a token without one imports cleanly and can never refresh.
        raise MintError("no-refresh-token", "Google returned no refresh token")
    email = email_from_id_token(str(tokens.get("id_token") or "")).strip()
    if not email:
        raise MintError("no-email", "Google did not say which account consented")
    expected = str(work.get("expected_account") or "").strip().lower()
    if expected and email.lower() != expected:
        raise MintError("wrong-account", f"Google signed in as {email}, not {expected}", account=email)
    token = build_gog_token(email=email, refresh_token=refresh,
                            granted_scopes=str(tokens.get("scope") or "").split(),
                            client=str(work.get("client") or "canopy-web"), now=now)
    plain = json.dumps(token)
    if delegated:
        # A person's own account: sealed to the readers canopy-web listed, never under
        # the vault key this box may hold — every member of the vault could open that.
        kv, value_zk = encrypt_for_agent(plain, agent_id=agent_id, vk=None, kv=work.get("kv"),
                                         recipients=work.get("recipients"), name=name)
    else:
        kv, value_zk = encrypt_for_agent(plain, agent_id=agent_id, vk=vk,
                                         kv=vk_kv if vk is not None else work.get("kv"),
                                         recipients=work.get("recipients"))
    plain = refresh = ""  # noqa: F841 — drop our references
    return {"name": name, "kv": kv, "value_zk": value_zk, "account": email}


def run(slug: str, work: dict, *, token: Optional[str] = None, base_url: Optional[str] = None,
        transport=None, ecdh=None, post: Optional[Post] = None) -> dict:
    """``finish``, reading the client file and the vault key with this box's own key from
    the agent's resolve (``/credentials/resolve``, the read a box prepares the agent with)."""
    from orchestrator import canopy_web, cred_resolve

    item = CLIENT_ITEMS.get(str(work.get("client") or "canopy-web"), CLIENT_ITEMS["canopy-web"])
    try:
        body = cred_resolve.resolve(slug, token=token, refs=[item], base_url=base_url,
                                    transport=transport, raw=True, fallback=False,
                                    purpose=cred_resolve.RUNNER_REFRESH)
    except canopy_web.CanopyError as e:
        raise MintError("no-client", f"this box could not read {slug}'s vault: {str(e)[:400]}") from None
    vk = vk_kv = None
    try:
        layered = cred_resolve.merge_vault_layers(slug, body, ecdh=ecdh)
        client_raw = (layered.get("values") or {}).get(item)
        if zk.is_zk_value(client_raw) or zk.is_sealed_value(client_raw):
            # The agent's own copy of the client (rare): open it like any value.
            opened = cred_resolve.open_zk(slug, {**body, "vaults": []},
                                          agent_id=str(body.get("agent_id") or work.get("agent_id")),
                                          ecdh=ecdh)
            client_raw = (opened.get("values") or {}).get(item)
        wrap, kv = body.get("wrap"), body.get("kv")
        if isinstance(wrap, dict) and kv:
            vk = cred_resolve.unwrap_device_vk(wrap, agent_id=str(work.get("agent_id")), kv=kv, ecdh=ecdh)
            vk_kv = kv
    except cred_resolve.ZkUnlockError as e:
        raise MintError("no-client", f"this box could not open {slug}'s vault: {str(e)[:400]}") from None
    try:
        return finish(work, client_raw=client_raw, vk=vk, vk_kv=vk_kv, post=post)
    finally:
        vk = None  # noqa: F841
