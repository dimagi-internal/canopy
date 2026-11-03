"""Google: fixed endpoints, so a plaintext naming any host is refused. A refresh omits
``refresh_token`` (no rotation). Revoking ends every token of the account on that OAuth
client, so a refused grant is never revoked and a disconnect only when told
``revoke_at_provider``: that decision is the caller's."""
from __future__ import annotations

from orchestrator.connections.providers import base
from orchestrator.connections.providers.base import ProviderError

TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_EMAIL = "https://www.googleapis.com/auth/userinfo.email"


class Provider(base.Provider):
    name = "google"
    title = "Google"
    required_scopes = ("openid", USERINFO_EMAIL)
    rotates_refresh_token = False
    revokes_refused_grant = False

    def check_hosts(self, plain: dict) -> None:
        if plain["login_host"] != "" or plain["instance_url"] != "":
            raise ProviderError("bad-host", "a Google connection names no host of its own")

    def token_url(self, plain: dict) -> str:
        return TOKEN_URL

    def revoke_url(self, plain: dict) -> str:
        return REVOKE_URL
