"""This machine's runner token — what a runner's credential reads fall back to when
no person's PAT will do.

A paired runner buys an hour-long runner token with its approved device key
(`canopy vault runner-session`, canopy-web#1594). canopy-web lets that token stage
exactly the agents its own box serves (`/credentials/resolve`, `/credentials/access`,
`/runtime` — canopy-web#1604). So a box whose owner's PAT was revoked, or a turn whose
only token is the agent's own (which `/credentials/resolve` always refuses), still
gets its secrets — as the box, never as a person.

Why: on 2026-10-17 the laptop runner's owner PAT was revoked in a token cleanup. The
runner itself carried on (it signs in with its key), but every `canopy agent prepare`
and `canopy cred` on it was refused, so Hal's and Eva's turns there would have had
no secrets.

`canopy_web.call` uses this for credential reads only, and only after the token it
was given is refused. The token is cached in ``~/.canopy/runner-session.json`` (0600)
until shortly before it runs out. Opt out with ``CANOPY_DEVICE_KEY=0``.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Optional

CACHE = Path.home() / ".canopy" / "runner-session.json"
#: Buy the next token this long before the cached one runs out.
RENEW_SECONDS = 600

# The reads a runner token may make to stage an agent (canopy-web runner_auth).
_CREDENTIAL_READ = re.compile(
    r"/api/(?:w/[^/?]+/)?agents/[a-z0-9][a-z0-9_-]*/?"
    r"(?:runtime/?|credentials/(?:resolve|access)/?)?(?:\?.*)?")


def is_credential_read(method: str, path: str) -> bool:
    """Is this a read a runner token may make (and so worth retrying with one)?"""
    return method.upper() == "GET" and bool(_CREDENTIAL_READ.fullmatch(path or ""))


def _expires(value) -> Optional[dt.datetime]:
    try:
        when = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)


def _cached(runner_id: str, base_url: str) -> str:
    try:
        body = json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(body, dict) or body.get("runner") != runner_id or body.get("base_url") != base_url:
        return ""
    when = _expires(body.get("expires_at"))
    now = dt.datetime.now(dt.timezone.utc)
    if when is None or (when - now).total_seconds() < RENEW_SECONDS:
        return ""
    return str(body.get("access_token") or "")


def _save(body: dict, base_url: str) -> None:
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"access_token": body.get("access_token"), "expires_at": body.get("expires_at"),
                       "runner": str(body.get("runner") or ""), "base_url": base_url}, fh)
        os.replace(tmp, CACHE)
    except OSError:
        pass  # a cache is a saving, never a new failure


def forget() -> None:
    """Drop the cached token (canopy-web refused it: expired early, or its key was revoked)."""
    try:
        CACHE.unlink()
    except OSError:
        pass


def token(base_url: str, transport=None) -> str:
    """This machine's runner token for ``base_url``, or "" when it is no runner, has no
    approved device key, or opted out. Never raises.

    The runner id is a laptop's ``~/.canopy/runner.json``, else the runner its device key
    is registered for — a cloud box keeps no runner.json, only the key's status."""
    if os.environ.get("CANOPY_DEVICE_KEY", "1").strip() == "0":
        return ""
    from orchestrator import device_key

    runner_id = (device_key.runner_id_from_config()
                 or str(device_key.read_status().get("runner") or "").strip())
    if not runner_id:
        return ""
    cached = _cached(runner_id, base_url)
    if cached:
        return cached
    try:
        from orchestrator.vault_cli import run_runner_session

        body = run_runner_session(runner_id, base_url, transport=transport)
    except Exception:  # noqa: BLE001 — no token is an answer: the caller's own error stands
        return ""
    raw = str(body.get("access_token") or "")
    if raw:
        _save({**body, "runner": runner_id}, base_url)
    return raw
