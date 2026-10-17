"""Canopy Vault zero-knowledge v1 — the byte formats, client side (canopy-web#1550).

The NORMATIVE definition is canopy-web ``docs/vault/zk-v1/zk_v1_reference.py`` and its
vectors ``zk_v1_vectors.json``; ``tests/fixtures/vault_zk/`` holds verbatim copies of both,
and ``tests/test_vault_zk.py`` reproduces every output byte for byte (including the
passphrase + Secret Key "2SKD" keyset seal, §4b — opened here so a future
``canopy vault zk unlock --passphrase`` can act as a person, not only as a device). Do not change a
format here — change the reference first, then the vectors, then every implementation.

Primitives: P-256 ECDH, HKDF-SHA256 (L=32), AES-256-GCM (12-byte nonce, tag appended).
Encoding: base64url without padding. Points: X9.62 uncompressed (65 bytes).

* VK — 32 random bytes per agent and key version ``kv``.
* value   — ``"zk1:" + b64u(nonce12 || AES-GCM(VK, nonce, utf8(v),
  aad="canopy-vault/v1/value|{agent_id}|{name}|{kv}"))``
* wrap    — ECDH-ES of VK to a recipient P-256 key (a device's ECDH key, or a person's
  keyset key): ``{"alg":"ecdh-es","epk","n","ct"}``, KEK = HKDF(Z, salt=epk||Q, info).
* recovery — ``{"alg":"recovery","s","n","ct"}`` under HKDF(R, salt, info); R is shown
  once as a Crockford base32 code.

Hardware device keys never expose their private scalar, so the device unwrap takes Z
(the ECDH shared x-coordinate) from the key itself: :func:`unwrap_ecdh_es_with_z`.

The ``ephemeral``/``nonce``/``salt`` keyword arguments exist ONLY so the tests can
reproduce the vectors; production callers leave them out and get fresh randomness.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Optional, Union

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

FORMAT = "canopy-vault/zk/v1"
VALUE_PREFIX = "zk1:"
VK_BYTES = 32
NONCE_BYTES = 12
SALT_BYTES = 16
RECOVERY_BYTES = 32
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class ZkError(Exception):
    """A zero-knowledge value or wrap could not be produced or opened. Never carries a
    secret — only what was wrong with it."""


# ──────────────────────────────────────────────────────────────────────────────
# encoding
# ──────────────────────────────────────────────────────────────────────────────

def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def unb64u(s: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except (ValueError, TypeError) as e:
        raise ZkError(f"not base64url ({e})") from None


def hkdf(ikm: bytes, salt: bytes, info: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                info=info.encode("utf-8")).derive(ikm)


def point(pub: ec.EllipticCurvePublicKey) -> bytes:
    """X9.62 uncompressed point (65 bytes)."""
    return pub.public_bytes(serialization.Encoding.X962,
                            serialization.PublicFormat.UncompressedPoint)


def jwk(pub: ec.EllipticCurvePublicKey) -> dict:
    p = point(pub)
    return {"kty": "EC", "crv": "P-256", "x": b64u(p[1:33]), "y": b64u(p[33:])}


def jwk_thumbprint(public_jwk: dict) -> str:
    """RFC 7638 thumbprint (b64u of SHA-256 over the canonical required members)."""
    canon = json.dumps({k: public_jwk[k] for k in ("crv", "kty", "x", "y")},
                       separators=(",", ":"), sort_keys=True)
    return b64u(hashlib.sha256(canon.encode("utf-8")).digest())


def jkt(pub: ec.EllipticCurvePublicKey) -> str:
    return jwk_thumbprint(jwk(pub))


def public_key_from_jwk(public_jwk: dict) -> ec.EllipticCurvePublicKey:
    """A P-256 public JWK → key. Rejects anything that is not a point on P-256 (the
    library validates the point), so a wrap is never made to a bogus key."""
    if not isinstance(public_jwk, dict) or public_jwk.get("kty") != "EC" \
            or public_jwk.get("crv") != "P-256":
        raise ZkError("not a P-256 EC public JWK")
    x, y = unb64u(str(public_jwk.get("x") or "")), unb64u(str(public_jwk.get("y") or ""))
    if len(x) != 32 or len(y) != 32:
        raise ZkError("P-256 JWK coordinates must be 32 bytes each")
    return public_key_from_point(b"\x04" + x + y)


def public_key_from_point(raw: bytes) -> ec.EllipticCurvePublicKey:
    if len(raw) != 65 or raw[0] != 4:
        raise ZkError("not an uncompressed P-256 point")
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    except ValueError as e:
        raise ZkError(f"not a point on P-256 ({e})") from None


def point_from_jwk(public_jwk: dict) -> bytes:
    return point(public_key_from_jwk(public_jwk))


def random_vk() -> bytes:
    return os.urandom(VK_BYTES)


def _check_vk(vk: bytes) -> None:
    if not isinstance(vk, (bytes, bytearray)) or len(vk) != VK_BYTES:
        raise ZkError("a vault key is 32 bytes")


def _gcm_open(key: bytes, nonce: bytes, ct: bytes, aad: bytes, what: str) -> bytes:
    try:
        return AESGCM(key).decrypt(nonce, ct, aad)
    except InvalidTag:
        raise ZkError(f"{what} did not authenticate — wrong key, or it was altered "
                      "or bound to another agent/name/version") from None


# ──────────────────────────────────────────────────────────────────────────────
# 2. values
# ──────────────────────────────────────────────────────────────────────────────

def value_aad(agent_id, name, kv) -> bytes:
    return f"canopy-vault/v1/value|{agent_id}|{name}|{kv}".encode("utf-8")


def encrypt_value(vk: bytes, agent_id, name: str, kv, plaintext: str, *,
                  nonce: Optional[bytes] = None) -> str:
    _check_vk(vk)
    nonce = os.urandom(NONCE_BYTES) if nonce is None else nonce
    ct = AESGCM(vk).encrypt(nonce, plaintext.encode("utf-8"), value_aad(agent_id, name, kv))
    return VALUE_PREFIX + b64u(nonce + ct)


def is_zk_value(stored) -> bool:
    return isinstance(stored, str) and stored.startswith(VALUE_PREFIX)


def decrypt_value(vk: bytes, agent_id, name: str, kv, stored: str) -> str:
    _check_vk(vk)
    if not is_zk_value(stored):
        raise ZkError(f"{name}: not a zk1 value")
    blob = unb64u(stored[len(VALUE_PREFIX):])
    if len(blob) < NONCE_BYTES + 16:
        raise ZkError(f"{name}: zk1 value is too short")
    pt = _gcm_open(vk, blob[:NONCE_BYTES], blob[NONCE_BYTES:], value_aad(agent_id, name, kv),
                   f"value {name}")
    return pt.decode("utf-8")


# ──────────────────────────────────────────────────────────────────────────────
# 3. ECDH-ES wraps
# ──────────────────────────────────────────────────────────────────────────────

def device_info(agent_id, kv, device_jkt: str) -> str:
    return f"canopy-vault/v1/wrap-device|{agent_id}|{kv}|{device_jkt}"


def member_info(agent_id, kv, user_id) -> str:
    return f"canopy-vault/v1/wrap-member|{agent_id}|{kv}|{user_id}"


Recipient = Union[ec.EllipticCurvePublicKey, dict]


def _recipient_key(recipient: Recipient) -> ec.EllipticCurvePublicKey:
    if isinstance(recipient, ec.EllipticCurvePublicKey):
        return recipient
    return public_key_from_jwk(recipient)


def wrap_ecdh_es(vk: bytes, recipient: Recipient, info: str, *,
                 ephemeral: Optional[ec.EllipticCurvePrivateKey] = None,
                 nonce: Optional[bytes] = None) -> dict:
    """Wrap VK to a recipient's PUBLIC key (a JWK or a key object)."""
    _check_vk(vk)
    q = _recipient_key(recipient)
    e_priv = ephemeral if ephemeral is not None else ec.generate_private_key(ec.SECP256R1())
    nonce = os.urandom(NONCE_BYTES) if nonce is None else nonce
    z = e_priv.exchange(ec.ECDH(), q)
    epk = point(e_priv.public_key())
    kek = hkdf(z, epk + point(q), info)
    return {"alg": "ecdh-es", "epk": b64u(epk), "n": b64u(nonce),
            "ct": b64u(AESGCM(kek).encrypt(nonce, vk, info.encode("utf-8")))}


def wrap_epk_point(wrap: dict) -> bytes:
    """The wrap's ephemeral public point (validated on P-256) — what a hardware key is
    asked to run ECDH against."""
    if not isinstance(wrap, dict) or wrap.get("alg") != "ecdh-es":
        raise ZkError("not an ecdh-es wrap")
    return point(public_key_from_point(unb64u(str(wrap.get("epk") or ""))))


def unwrap_ecdh_es_with_z(wrap: dict, z: bytes, recipient_point: bytes, info: str) -> bytes:
    """For keys that only expose ECDH: the caller computed Z = ECDH(own key, epk)."""
    epk = wrap_epk_point(wrap)
    if len(z) != 32:
        raise ZkError("an ECDH shared secret is 32 bytes")
    kek = hkdf(z, epk + recipient_point, info)
    vk = _gcm_open(kek, unb64u(str(wrap.get("n") or "")), unb64u(str(wrap.get("ct") or "")),
                   info.encode("utf-8"), "the vault-key wrap")
    _check_vk(vk)
    return vk


def unwrap_ecdh_es(wrap: dict, recipient_priv: ec.EllipticCurvePrivateKey, info: str) -> bytes:
    epk = public_key_from_point(wrap_epk_point(wrap))
    z = recipient_priv.exchange(ec.ECDH(), epk)
    return unwrap_ecdh_es_with_z(wrap, z, point(recipient_priv.public_key()), info)


# ──────────────────────────────────────────────────────────────────────────────
# 4. user keyset seal (the browser makes these; here for the vectors and for parity)
# ──────────────────────────────────────────────────────────────────────────────

def prf_input(user_id) -> bytes:
    return hashlib.sha256(f"canopy-vault/v1/prf|{user_id}".encode("utf-8")).digest()


def user_key_info(user_id, cred: str) -> str:
    return f"canopy-vault/v1/user-key|{user_id}|{cred}"


def seal_user_key(scalar32: bytes, user_id, cred: str, prf_out: bytes, *,
                  salt: Optional[bytes] = None, nonce: Optional[bytes] = None) -> dict:
    info = user_key_info(user_id, cred)
    salt = os.urandom(SALT_BYTES) if salt is None else salt
    nonce = os.urandom(NONCE_BYTES) if nonce is None else nonce
    kek = hkdf(prf_out, salt, info)
    return {"alg": "prf", "cred": cred, "s": b64u(salt), "n": b64u(nonce),
            "ct": b64u(AESGCM(kek).encrypt(nonce, scalar32, info.encode("utf-8")))}


def open_user_key(seal: dict, user_id, prf_out: bytes) -> bytes:
    if not isinstance(seal, dict) or seal.get("alg") != "prf":
        raise ZkError("not a prf seal")
    info = user_key_info(user_id, seal["cred"])
    kek = hkdf(prf_out, unb64u(seal["s"]), info)
    return _gcm_open(kek, unb64u(seal["n"]), unb64u(seal["ct"]), info.encode("utf-8"),
                     "the keyset seal")


# ──────────────────────────────────────────────────────────────────────────────
# 4b. user keyset seal by passphrase + Secret Key (1Password-style 2SKD)
# ──────────────────────────────────────────────────────────────────────────────

PBKDF2_ITERATIONS = 650000
SECRET_KEY_BYTES = 16
SECRET_KEY_PREFIX = "CV1-"


def _two_secret_kek(passphrase: str, secret_key: bytes, salt: bytes, user_id) -> tuple[bytes, str]:
    import unicodedata

    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    if len(secret_key) != SECRET_KEY_BYTES:
        raise ZkError("a Secret Key is 16 bytes")
    k1 = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                    iterations=PBKDF2_ITERATIONS).derive(
        unicodedata.normalize("NFKC", passphrase).encode("utf-8"))
    k2 = hkdf(secret_key, salt, f"canopy-vault/v1/secret-key|{user_id}")
    info = f"canopy-vault/v1/user-key|{user_id}|passphrase"
    return hkdf(bytes(a ^ b for a, b in zip(k1, k2)), salt, info), info


def seal_user_key_2skd(scalar32: bytes, user_id, passphrase: str, secret_key: bytes, *,
                       salt: Optional[bytes] = None, nonce: Optional[bytes] = None) -> dict:
    salt = os.urandom(SALT_BYTES) if salt is None else salt
    nonce = os.urandom(NONCE_BYTES) if nonce is None else nonce
    kek, info = _two_secret_kek(passphrase, secret_key, salt, user_id)
    return {"alg": "2skd", "kdf": "pbkdf2-sha256", "i": PBKDF2_ITERATIONS, "s": b64u(salt),
            "n": b64u(nonce),
            "ct": b64u(AESGCM(kek).encrypt(nonce, scalar32, info.encode("utf-8")))}


def open_user_key_2skd(seal: dict, user_id, passphrase: str, secret_key: bytes) -> bytes:
    """The person's keyset private scalar, from a passphrase + Secret Key seal."""
    if not isinstance(seal, dict) or seal.get("alg") != "2skd" \
            or seal.get("kdf") != "pbkdf2-sha256" or seal.get("i") != PBKDF2_ITERATIONS:
        raise ZkError("not a supported 2skd seal")
    kek, info = _two_secret_kek(passphrase, secret_key, unb64u(seal["s"]), user_id)
    return _gcm_open(kek, unb64u(seal["n"]), unb64u(seal["ct"]), info.encode("utf-8"),
                     "the passphrase seal (wrong passphrase or Secret Key?)")


def _crockford(b: bytes) -> str:
    n, v, out = len(b) * 8, int.from_bytes(b, "big"), []
    v <<= (-n) % 5
    for i in range((n + 4) // 5 - 1, -1, -1):
        out.append(CROCKFORD[(v >> (5 * i)) & 31])
    s = "".join(out)
    return "-".join(s[i:i + 4] for i in range(0, len(s), 4))


def _uncrockford(s: str, nbytes: int, what: str) -> bytes:
    s = s.replace("-", "").replace(" ", "").upper().replace("O", "0").replace("I", "1") \
        .replace("L", "1")
    want = (nbytes * 8 + 4) // 5
    if len(s) != want:
        raise ZkError(f"a {what} has {want} characters")
    v = 0
    for ch in s:
        idx = CROCKFORD.find(ch)
        if idx < 0:
            raise ZkError(f"a {what} uses Crockford base32 characters only")
        v = (v << 5) | idx
    v >>= (5 * len(s)) - nbytes * 8
    return v.to_bytes(nbytes, "big")


def secret_key_code(sk: bytes) -> str:
    """16 bytes → "CV1-" + 26 Crockford chars in groups of 4."""
    return SECRET_KEY_PREFIX + _crockford(sk)


def parse_secret_key_code(code: str) -> bytes:
    s = code.replace("-", "").replace(" ", "").upper()
    if len(s) == 29 and s.startswith("CV1"):          # the prefix, however it was typed
        s = s[3:]
    return _uncrockford(s, SECRET_KEY_BYTES, "Secret Key")


# ──────────────────────────────────────────────────────────────────────────────
# 5. recovery
# ──────────────────────────────────────────────────────────────────────────────

def recovery_info(agent_id, kv) -> str:
    return f"canopy-vault/v1/wrap-recovery|{agent_id}|{kv}"


def random_recovery_secret() -> bytes:
    return os.urandom(RECOVERY_BYTES)


def wrap_recovery(vk: bytes, agent_id, kv, r: bytes, *, salt: Optional[bytes] = None,
                  nonce: Optional[bytes] = None) -> dict:
    _check_vk(vk)
    if len(r) != RECOVERY_BYTES:
        raise ZkError("a recovery secret is 32 bytes")
    info = recovery_info(agent_id, kv)
    salt = os.urandom(SALT_BYTES) if salt is None else salt
    nonce = os.urandom(NONCE_BYTES) if nonce is None else nonce
    kek = hkdf(r, salt, info)
    return {"alg": "recovery", "s": b64u(salt), "n": b64u(nonce),
            "ct": b64u(AESGCM(kek).encrypt(nonce, vk, info.encode("utf-8")))}


def unwrap_recovery(wrap: dict, agent_id, kv, r: bytes) -> bytes:
    if not isinstance(wrap, dict) or wrap.get("alg") != "recovery":
        raise ZkError("not a recovery wrap")
    info = recovery_info(agent_id, kv)
    kek = hkdf(r, unb64u(wrap["s"]), info)
    vk = _gcm_open(kek, unb64u(wrap["n"]), unb64u(wrap["ct"]), info.encode("utf-8"),
                   "the recovery wrap")
    _check_vk(vk)
    return vk


def recovery_code(r: bytes) -> str:
    """256 bits → 52 Crockford chars (the last carries 1 bit, low bits zero), in groups
    of 4 joined by '-'."""
    return _crockford(r)


def parse_recovery_code(code: str) -> bytes:
    return _uncrockford(code, RECOVERY_BYTES, "recovery code")


# ──────────────────────────────────────────────────────────────────────────────
# 6. sealed values — "zks1:" (a value for named recipients, no vault key needed)
# ──────────────────────────────────────────────────────────────────────────────
# canopy-web mints some secrets itself (Connect Google mailbox's gog-token, a rotated
# canopy-pat). On a zero-knowledge agent it holds no vault key, so it seals the new value
# to each recipient's PUBLIC key instead:
#
#   "zks1:" + b64u(JSON {"kv": kv, "r": [{"kind": "device"|"member",
#                                          "target": <ecdh jkt | user id>,
#                                          "epk": <P-256 JWK>,
#                                          "ct": b64u(nonce12 || AES-GCM ct+tag)}]})
#   key  = HKDF-SHA256(ECDH(recipient, epk), salt=b"",
#                      info="canopy-vault/v1/seal-value|{agent_id}|{name}|{kv}|{kind}:{target}")
#   aad  = value_aad(agent_id, name, kv)
#
# A device opens its own entry (kind "device", target = its ECDH jkt). A holder of the
# vault key later SETTLES the value — re-encrypts it as zk1 under VK — so it reaches every
# member, not just the recipients the server knew at mint time.

SEALED_PREFIX = "zks1:"


def is_sealed_value(stored) -> bool:
    return isinstance(stored, str) and stored.startswith(SEALED_PREFIX)


def seal_info(agent_id, name, kv, kind: str, target) -> str:
    return f"canopy-vault/v1/seal-value|{agent_id}|{name}|{kv}|{kind}:{target}"


def seal_value(agent_id, name: str, kv, plaintext: str, recipients: list, *,
               ephemerals: Optional[list] = None, nonces: Optional[list] = None) -> str:
    """Seal one value to ``recipients`` — ``(kind, target, public key or JWK)`` each.
    canopy-web does this server side; here for parity and the tests."""
    if not recipients:
        raise ZkError(f"{name}: a sealed value needs at least one recipient")
    entries = []
    for i, (kind, target, pub) in enumerate(recipients):
        if kind not in ("device", "member"):
            raise ZkError(f"{name}: recipient kind must be device or member, not {kind!r}")
        q = _recipient_key(pub)
        e_priv = ephemerals[i] if ephemerals else ec.generate_private_key(ec.SECP256R1())
        nonce = nonces[i] if nonces else os.urandom(NONCE_BYTES)
        key = hkdf(e_priv.exchange(ec.ECDH(), q), b"", seal_info(agent_id, name, kv, kind, target))
        ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), value_aad(agent_id, name, kv))
        entries.append({"kind": kind, "target": target, "epk": jwk(e_priv.public_key()),
                        "ct": b64u(nonce + ct)})
    body = json.dumps({"kv": kv, "r": entries}, separators=(",", ":"))
    return SEALED_PREFIX + b64u(body.encode("utf-8"))


def parse_sealed(stored: str, name: str = "value") -> dict:
    """The sealed JSON (``kv`` and ``r``), checked for shape. Holds no secret."""
    if not is_sealed_value(stored):
        raise ZkError(f"{name}: not a zks1 value")
    try:
        body = json.loads(unb64u(stored[len(SEALED_PREFIX):]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ZkError(f"{name}: zks1 value is not valid JSON") from None
    if not isinstance(body, dict) or body.get("kv") in (None, "") \
            or not isinstance(body.get("r"), list):
        raise ZkError(f"{name}: zks1 value has no kv or recipient list")
    return body


def sealed_entry(stored: str, name: str, kind: str, target) -> tuple[dict, object]:
    """(this recipient's entry, the value's kv). Raises when there is none."""
    body = parse_sealed(stored, name)
    for e in body["r"]:
        if isinstance(e, dict) and e.get("kind") == kind and str(e.get("target")) == str(target):
            return e, body["kv"]
    raise ZkError(f"{name}: sealed for {len(body['r'])} recipient(s), none of them "
                  f"{kind} {str(target)[:16]}")


def sealed_epk_point(entry: dict) -> bytes:
    """The entry's ephemeral public point, validated on P-256 — what a hardware key runs
    ECDH against."""
    return point_from_jwk(entry.get("epk") or {})


def open_sealed_with_z(stored: str, agent_id, name: str, kind: str, target, z: bytes) -> str:
    """For keys that only expose ECDH: ``z`` = ECDH(own key, the entry's epk)."""
    entry, kv = sealed_entry(stored, name, kind, target)
    if len(z) != 32:
        raise ZkError("an ECDH shared secret is 32 bytes")
    blob = unb64u(str(entry.get("ct") or ""))
    if len(blob) < NONCE_BYTES + 16:
        raise ZkError(f"{name}: sealed entry is too short")
    key = hkdf(z, b"", seal_info(agent_id, name, kv, kind, target))
    return _gcm_open(key, blob[:NONCE_BYTES], blob[NONCE_BYTES:],
                     value_aad(agent_id, name, kv), f"sealed value {name}").decode("utf-8")


def open_sealed(stored: str, agent_id, name: str, kind: str, target,
                recipient_priv: ec.EllipticCurvePrivateKey) -> str:
    entry, _ = sealed_entry(stored, name, kind, target)
    z = recipient_priv.exchange(ec.ECDH(), public_key_from_point(sealed_epk_point(entry)))
    return open_sealed_with_z(stored, agent_id, name, kind, target, z)
