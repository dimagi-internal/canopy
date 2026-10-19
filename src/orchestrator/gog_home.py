"""Per-agent gog homes: each agent's mail token in its own keyring, under its own password.

Runner isolation units, spec §6. On a box whose runner sets ``$CANOPY_GOG_SECRET_FILE`` (a
unit's 32 random bytes, ``/etc/canopy-runner/<unit>.gog-secret``, 0400, owned by the unit
user), every agent gets:

* its own gog home, ``~/.gog/<slug>/``, through ``GOG_HOME`` (gog's own knob); and
* its own ``file``-keyring password, derived — never stored:
  ``HKDF-SHA256(IKM = <secret file bytes>, salt = none (32 zero bytes),
  info = b"gog:" + slug, L = 32)``, lower-case hex (64 characters).

So there is no box-wide ``GOG_KEYRING_PASSWORD`` that opens every agent's mail: a confined
turn's mail capability carries exactly its own agent's pair (:func:`confined_mail_env`) and
never the secret file's path.

Without ``$CANOPY_GOG_SECRET_FILE`` (a laptop, a box on older runner code) nothing here does
anything: :func:`agent_gog_env` is ``{}`` and gog keeps today's single home and keyring.

The runner (canopy-web ``cloud_runner.py``, stdlib only) derives the same password itself;
:func:`derive_password` is plain RFC 5869 over ``hmac`` so the two stay byte-identical.

stdlib only.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
from pathlib import Path
from typing import Mapping, MutableMapping, Optional

SECRET_VAR = "CANOPY_GOG_SECRET_FILE"
#: What a gog call needs for one agent's own keyring.
GOG_VARS = ("GOG_HOME", "GOG_KEYRING_PASSWORD", "GOG_KEYRING_BACKEND")
#: The variables that name the turn's agent (strongest first).
AGENT_VARS = ("CANOPY_AGENT_SLUG", "CANOPY_AGENT")
INFO_PREFIX = b"gog:"
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$", re.I)


class GogHomeError(Exception):
    """The unit secret is named but cannot be used; the message says why."""


def gog_home(slug: str, home: Optional[Path] = None) -> Path:
    """``~/.gog/<slug>/`` — the agent's own gog home."""
    if not _SLUG_RE.match(slug or ""):
        raise GogHomeError(f"not an agent slug: {slug!r}")
    return (home or Path.home()) / ".gog" / slug


def hkdf_sha256(ikm: bytes, info: bytes, length: int = 32, salt: bytes = b"") -> bytes:
    """RFC 5869 HKDF-SHA256 (an empty salt is HashLen zero bytes)."""
    prk = hmac.new(salt or b"\0" * 32, ikm, hashlib.sha256).digest()
    out, block, i = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([i]), hashlib.sha256).digest()
        out += block
        i += 1
    return out[:length]


def derive_password(secret: bytes, slug: str) -> str:
    """The agent's gog keyring password: HKDF-SHA256(secret, info = "gog:" + slug), hex."""
    if not secret:
        raise GogHomeError("the unit's gog secret is empty")
    return hkdf_sha256(secret, INFO_PREFIX + slug.encode("utf-8")).hex()


def secret_path(environ: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    env = os.environ if environ is None else environ
    raw = (env.get(SECRET_VAR) or "").strip()
    return Path(raw).expanduser() if raw else None


def read_secret(environ: Optional[Mapping[str, str]] = None, *, strict: bool = False
                ) -> Optional[bytes]:
    """The unit secret's bytes, or None when ``$CANOPY_GOG_SECRET_FILE`` is unset — or set
    but unreadable/empty and not ``strict`` (then GogHomeError)."""
    path = secret_path(environ)
    if path is None:
        return None
    try:
        data = path.read_bytes()
    except OSError as e:
        if strict:
            raise GogHomeError(f"cannot read ${SECRET_VAR} ({path}): {e.strerror or e}")
        return None
    if not data:
        if strict:
            raise GogHomeError(f"${SECRET_VAR} ({path}) is empty")
        return None
    return data


def agent_gog_env(slug: str, environ: Optional[Mapping[str, str]] = None,
                  home: Optional[Path] = None, *, strict: bool = False) -> dict:
    """``{GOG_HOME, GOG_KEYRING_PASSWORD, GOG_KEYRING_BACKEND}`` for agent ``slug`` on a box
    with a unit secret; ``{}`` without one (today's behaviour)."""
    secret = read_secret(environ, strict=strict)
    if secret is None:
        return {}
    return {"GOG_HOME": str(gog_home(slug, home)),
            "GOG_KEYRING_PASSWORD": derive_password(secret, slug),
            "GOG_KEYRING_BACKEND": "file"}


def confined_mail_env(slug: str, environ: Optional[Mapping[str, str]] = None,
                      home: Optional[Path] = None) -> dict:
    """What a confined (caller) turn's mail capability may carry: THIS agent's gog home and
    password and nothing else — never another agent's, never the unit secret or its path.

    Without a unit secret (legacy) it is what the process has: the box's one keyring
    (``GOG_KEYRING_PASSWORD`` / ``GOG_KEYRING_BACKEND`` / ``GOG_HOME`` as set)."""
    env = os.environ if environ is None else environ
    if secret_path(env) is not None:
        own = agent_gog_env(slug, env, home)
        return own  # {} when the secret is unreadable here: no mail beats another's mail
    return {k: env[k] for k in GOG_VARS if env.get(k)}


def session_agent(environ: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if environ is None else environ
    for var in AGENT_VARS:
        slug = (env.get(var) or "").strip().lower()
        if slug and _SLUG_RE.match(slug):
            return slug
    return ""


def adopt_session_agent_env(environ: Optional[MutableMapping[str, str]] = None) -> dict:
    """On a box with a unit secret, point THIS process's gog calls at the session agent's
    own home and keyring — unless ``GOG_HOME`` is already set (the runner gave the turn its
    pair). Returns what it set (``{}`` when nothing applies). Every gog subprocess canopy
    starts inherits it."""
    env = os.environ if environ is None else environ
    if (env.get("GOG_HOME") or "").strip() or secret_path(env) is None:
        return {}
    slug = session_agent(env)
    if not slug:
        return {}
    own = agent_gog_env(slug, env)
    env.update(own)
    return own
