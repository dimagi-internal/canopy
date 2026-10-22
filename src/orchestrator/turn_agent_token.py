"""An agent's runner turn acts with a login canopy-web issued for THAT turn.

canopy-web hands the runner holding a turn a short-lived login as the turn's agent
(`POST /api/harness/runners/{rid}/turns/{tid}/agent-token`, revoked when the turn ends;
canopy-web docs/superpowers/specs/2026-10-21-turn-scoped-agent-token-design.md). It
replaces the agent's stored, never-expiring `canopy-pat` (`CANOPY_WEB_PAT` in
``~/.<slug>/.env``) for runner turns.

The cloud runner puts the token in the turn's env itself. A laptop runner cannot — emdash
starts the session, not the runner — so `canopy_web.resolve_token` asks here on demand:
this machine's runner token (`runner_session.token`, bought with its approved device key)
plus the turn this session runs for (`provenance.resolve_parent`: env, the caller
envelope, or the emdash task's by-task record). Each CLI call is a fresh process, so a
token revoked at turn end is never reused past it.

**A laptop session outlives its turn.** emdash finishes the turn as soon as the session
exists, and the person keeps driving it; so this asks for a SESSION-scoped login first
(``?scope=session``: tied to the session the turn opened, held while that session stays
live on this runner, and still attributed to the turn), and only then for a turn login.
A runner that is not a laptop has no live session to name and gets the turn login as
before.

Phase 1 is additive: any miss — no runner on this machine, no turn, an older canopy-web,
a finished turn, a token for some other agent — returns "" and the caller falls back to
the stored PAT. Answers are cached per turn (0600), refusals for `REFUSED_TTL` so a
session whose turn has ended does not ask on every call. Opt out:
``CANOPY_TURN_AGENT_TOKEN=0``.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
from pathlib import Path
from typing import Optional

#: Where answers are cached, one file per turn. Read at call time (tests move it).
CACHE_DIR = Path.home() / ".canopy" / "agent-token" / "cli"
#: Ask again this long before a cached token runs out.
RENEW_SECONDS = 600
#: How long a refusal for a turn is remembered.
REFUSED_TTL = 900
#: How long a TURN-scoped login is reused before asking again (it may be revoked soon).
TURN_LOGIN_CACHE_SECONDS = 120
_ID = re.compile(r"^[0-9a-fA-F-]{8,64}$")


def _expires(value) -> float:
    try:
        when = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return when.timestamp()


def _cache_path(turn_id: str) -> Path:
    return CACHE_DIR / f"{turn_id}.json"


def _read_cache(turn_id: str, slug: str, base: str, now: float) -> Optional[str]:
    """The cached answer: a token, "" for a remembered refusal, None for nothing usable."""
    try:
        doc = json.loads(_cache_path(turn_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("agent") != slug or doc.get("base_url") != base:
        return None
    if doc.get("token"):
        return str(doc["token"]) if float(doc.get("exp") or 0) - now > RENEW_SECONDS else None
    return "" if now < float(doc.get("refused_until") or 0) else None


def _write_cache(turn_id: str, doc: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(CACHE_DIR, 0o700)
        tmp = _cache_path(turn_id).with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        os.replace(tmp, _cache_path(turn_id))
    except OSError:
        pass  # a cache is a saving, never a new failure


def resolve(slug: str, *, base_url: Optional[str] = None, transport=None,
            now=time.time, session_only: bool = False) -> str:
    """A login as agent ``slug`` for the session (else the turn) this runs in, or "".
    ``session_only``: never a turn login — for a caller that outlives the turn (the MCP
    headers helper connects once per session).

    Never raises: "" means "use what you used before" (the stored PAT)."""
    if not slug or os.environ.get("CANOPY_TURN_AGENT_TOKEN", "1").strip() == "0":
        return ""
    try:
        return _resolve(slug, base_url=base_url, transport=transport, now=now(),
                        session_only=session_only)
    except Exception:  # noqa: BLE001 — identity lookup must never crash a call
        return ""


def _ask(transport, base: str, runner_id: str, turn_id: str, rtok: str, scope: str):
    from orchestrator import provenance

    q = "?scope=session" if scope == "session" else ""
    status, text = transport(
        "POST", f"{base}/api/harness/runners/{runner_id}/turns/{turn_id}/agent-token{q}",
        {**provenance.provenance_headers(), "Authorization": f"Bearer {rtok}",
         "Content-Type": "application/json"}, None)
    body = {}
    if 200 <= status < 300:
        try:
            body = json.loads(text) if text.strip() else {}
        except ValueError:
            body = {}
    return status, body if isinstance(body, dict) else {}


def _resolve(slug: str, *, base_url, transport, now: float, session_only: bool = False) -> str:
    from orchestrator import canopy_web, device_key, provenance, runner_session

    turn_id = provenance.resolve_parent().get("turn_id") or ""
    if not _ID.match(turn_id):
        return ""
    runner_id = (device_key.runner_id_from_config()
                 or str(device_key.read_status().get("runner") or "").strip())
    if not runner_id:
        return ""  # no runner on this machine: nothing holds the turn here
    base = canopy_web.resolve_base_url(base_url)
    key = f"{turn_id}.session" if session_only else turn_id
    cached = _read_cache(key, slug, base, now)
    if cached is not None:
        return cached
    transport = transport or canopy_web.urllib_transport
    rtok = runner_session.token(base, transport=transport)
    if not rtok:
        return ""  # no approved device key: not remembered, it may be approved soon
    status, body = _ask(transport, base, runner_id, turn_id, rtok, "session")
    if not body.get("token") and not session_only and status != 401:
        status, body = _ask(transport, base, runner_id, turn_id, rtok, "turn")
    token = str(body.get("token") or "")
    if status == 401:
        runner_session.forget()
    if not token or body.get("agent_slug") != slug:
        # Refused (a finished turn, another box's, a caller's, an older canopy-web) or
        # a login for some other agent: remembered for a while, then asked again.
        _write_cache(key, {"agent": slug, "base_url": base,
                           "refused_until": now + REFUSED_TTL, "status": status})
        return ""
    exp = _expires(body.get("expires_at"))
    if not body.get("session_id"):
        # A TURN login dies when the turn ends, which on a laptop is minutes away: keep it
        # only briefly, so the next call asks again and gets the session's login.
        exp = min(exp, now + RENEW_SECONDS + TURN_LOGIN_CACHE_SECONDS)
    _write_cache(key, {"agent": slug, "base_url": base, "token": token, "exp": exp})
    return token
