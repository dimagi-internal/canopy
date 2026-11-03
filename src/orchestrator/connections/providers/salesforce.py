"""Salesforce: endpoints hang off the SEALED login host, and only a host the rules allow
(production, a sandbox, or a My Domain), because the refresh token and the client secret
are sent there. The token answer carries no ``expires_in`` (the app's
``access_token_seconds`` says), and a refresh may rotate the refresh token."""
from __future__ import annotations

import re

from orchestrator.connections.providers import base
from orchestrator.connections.providers.base import ProviderError

LOGIN_HOSTS = ("login.salesforce.com", "test.salesforce.com")
MY_DOMAIN_SUFFIX = ".my.salesforce.com"

#: One or more DNS labels: nothing that could smuggle a port, a userinfo or a path.
_LABELS = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_MAX_HOST = 253


def _my_domain(host: str) -> bool:
    return (host.endswith(MY_DOMAIN_SUFFIX) and len(host) <= _MAX_HOST
            and _LABELS.fullmatch(host[:-len(MY_DOMAIN_SUFFIX)]) is not None)


def login_host_ok(host) -> bool:
    """A bare host name: ``login``/``test.salesforce.com``, or ``<label>.my.salesforce.com``."""
    return isinstance(host, str) and (host in LOGIN_HOSTS or _my_domain(host))


def instance_ok(url) -> bool:
    """``https://<label>.my.salesforce.com`` and nothing more (an optional trailing ``/``)."""
    if not isinstance(url, str) or not url.startswith("https://"):
        return False
    return _my_domain(url[len("https://"):].removesuffix("/"))


class Provider(base.Provider):
    name = "salesforce"
    title = "Salesforce"
    required_scopes = ("api", "refresh_token")
    rotates_refresh_token = True
    default_access_token_seconds = 1800

    def check_hosts(self, plain: dict) -> None:
        if not login_host_ok(plain["login_host"]):
            raise ProviderError("bad-host", "the sealed login host is not a Salesforce login host")
        if not instance_ok(plain["instance_url"]):
            raise ProviderError("bad-host", "the sealed instance URL is not a Salesforce My Domain")

    def token_url(self, plain: dict) -> str:
        return f"https://{plain['login_host']}/services/oauth2/token"

    def revoke_url(self, plain: dict) -> str:
        return f"https://{plain['login_host']}/services/oauth2/revoke"
