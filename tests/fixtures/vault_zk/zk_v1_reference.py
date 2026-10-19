"""Canopy Vault zero-knowledge v1 — REFERENCE implementation + test-vector generator.

Normative definition of every byte format in Phase 2 (canopy-web#1550). Every
implementation (browser WebCrypto, the Python client, any server validation) must
reproduce `zk_v1_vectors.json` byte for byte.

Primitives: P-256 ECDH, HKDF-SHA256 (L=32), AES-256-GCM (12-byte nonce, 16-byte tag
appended). Encoding: base64url WITHOUT padding ("b64u"). Points: X9.62 uncompressed
(65 bytes). Strings in info/aad are UTF-8. Identifiers bound into derivations:
  agent_id — canopy-web Agent primary key as a string;  name — credential name;
  kv — vault-key version (decimal);  user_id — canopy-web User pk as a string;
  cred — WebAuthn credential id (b64u);  jkt — RFC 7638 thumbprint of the device's
  ECDH public JWK (b64u, as canopy-web already computes for signing keys).

MODEL (1Password-style): every principal that may read a vault holds a P-256 ECDH
key pair whose private half the server never sees.
  * a DEVICE (runner): Secure Enclave / TPM / 0600-file key, non-exportable where
    the hardware allows;
  * a PERSON: a "user keyset" — a P-256 key pair generated in the browser, whose
    private scalar is sealed under a key derived from the person's passkey via the
    WebAuthn PRF extension (one seal per PRF-capable passkey).
The per-agent vault key VK is wrapped (ECDH-ES) to each principal's public key.
Granting access = an existing member unwraps VK and wraps it to the newcomer's
PUBLIC key — no secret of the newcomer is needed. Removing access = rotate VK.

1. VK: 32 random bytes, generated client-side.

2. Value:   aad  = "canopy-vault/v1/value|{agent_id}|{name}|{kv}"
            store = "zk1:" + b64u(nonce12 || AES-GCM(VK, nonce, utf8(plaintext), aad))

3. ECDH-ES wrap of VK to a recipient public key Q (device OR person):
            E    = fresh ephemeral P-256 key pair
            Z    = ECDH(E_priv, Q)                         (32-byte x-coordinate)
            info = device: "canopy-vault/v1/wrap-device|{agent_id}|{kv}|{jkt}"
                   person: "canopy-vault/v1/wrap-member|{agent_id}|{kv}|{user_id}"
            KEK  = HKDF-SHA256(ikm=Z, salt=E_pub(65)||Q(65), info)
            wrap = {"alg":"ecdh-es","epk":b64u(E_pub),"n":b64u(nonce12),
                    "ct":b64u(AES-GCM(KEK, nonce, VK, aad=info))}

4. User keyset seal (protects a person's private scalar s, 32 bytes big-endian):
            prf_input = SHA-256("canopy-vault/v1/prf|{user_id}")   -> prf.eval.first
            P    = PRF output (32 bytes)
            info = "canopy-vault/v1/user-key|{user_id}|{cred}"
            KEK  = HKDF-SHA256(ikm=P, salt=salt16 (random, stored), info)
            seal = {"alg":"prf","cred":cred,"s":b64u(salt),"n":b64u(nonce12),
                    "ct":b64u(AES-GCM(KEK, nonce, s, aad=info))}

4b. User keyset seal by PASSPHRASE + SECRET KEY (1Password-style 2SKD) — the
   alternative for people whose passkey/browser lacks PRF, or who prefer it.
   The Secret Key (SK) is 16 random bytes generated in the browser, shown once as
   "CV1-" + Crockford base32 (groups of 4), kept on the person's devices / printed;
   the server never receives it, so a stolen database cannot brute-force a weak
   passphrase.
            K1   = PBKDF2-HMAC-SHA256(NFKC(passphrase), salt16, iterations=650000, L=32)
            K2   = HKDF-SHA256(ikm=SK, salt=salt16, info="canopy-vault/v1/secret-key|{user_id}")
            info = "canopy-vault/v1/user-key|{user_id}|passphrase"
            KEK  = HKDF-SHA256(ikm=K1 XOR K2, salt=salt16, info)
            seal = {"alg":"2skd","kdf":"pbkdf2-sha256","i":650000,"s":b64u(salt),
                    "n":b64u(nonce12),"ct":b64u(AES-GCM(KEK, nonce, s, aad=info))}

4c. User keyset seal by the person's RECOVERY CODE — ONE per person, opening
   their key and so every agent they hold (the per-agent §5 recovery wrap is
   then optional; existing ones stay valid). R = 32 random bytes, shown once in
   the §5 recovery-code format.
            info = "canopy-vault/v1/user-key|{user_id}|recovery"
            KEK  = HKDF-SHA256(ikm=R, salt=salt16, info)
            seal = {"alg":"recovery","s":b64u(salt),"n":b64u(nonce12),
                    "ct":b64u(AES-GCM(KEK, nonce, s, aad=info))}

5. Recovery wrap of VK:   R = 32 random bytes, shown once as Crockford base32 in
   groups of 4 ("recovery code");
            info = "canopy-vault/v1/wrap-recovery|{agent_id}|{kv}"
            KEK  = HKDF-SHA256(ikm=R, salt=salt16, info)
            wrap = {"alg":"recovery","s":b64u(salt),"n":b64u(nonce12),
                    "ct":b64u(AES-GCM(KEK, nonce, VK, aad=info))}

6. Value SEALED TO RECIPIENTS ("zks1") — for a value canopy-web mints itself
   (Gmail token, rotated canopy-pat) or that is written before a vault key
   exists (kv 0: an agent born zero-knowledge). Sealed to every principal that
   may read the agent, at the agent's current kv; no vault key involved.
   For each recipient Q (kind "device": target = the device's ECDH jkt;
                         kind "member": target = the user_id):
            E    = fresh ephemeral P-256 key pair (one per recipient)
            Z    = ECDH(E_priv, Q)
            info = "canopy-vault/v1/seal-value|{agent_id}|{name}|{kv}|{kind}:{target}"
            K    = HKDF-SHA256(ikm=Z, salt=b"" (empty), info)
            r    = {"kind":kind,"target":target,"epk":JWK(E_pub) {"kty","crv","x","y"},
                    "ct":b64u(nonce12 || AES-GCM(K, nonce, utf8(plaintext), aad=value aad §2))}
            store = "zks1:" + b64u(utf8(JSON {"kv":kv,"r":[r, ...]}))
   (JSON compact, keys in that order; readers must parse, not byte-compare.)
   A reader finds its own entry by (kind, target), derives K the same way, and
   opens ct. A vault-key holder "settles" a zks1 value by re-encrypting the
   plaintext under VK as §2 zk1 at the current kv (enable/rotate require it).

7. ENDORSEMENT of a recipient — a person's signed "this key may read this agent",
   made in their browser when they wrap VK to it (or confirm it). The publicly
   checkable twin of a wrap: a writer who holds no VK seals a §6 value only to
   recipients endorsed by a key it already trusts, so a server cannot add its own.
            msg  = "canopy-vault/v1/endorse|{agent_id}|{kind}|{target}|{key_jkt}"
                   (kind/target as in §6; key_jkt = jkt of the recipient's public JWK —
                    for a device, equal to target)
            sig  = ECDSA-P256-SHA256(utf8(msg)) by the endorser's person key (the
                   same P-256 scalar as their §4 keyset: the fixed prefix keeps the
                   signed message out of every ECDH derivation), IEEE P1363 r||s
                   (64 bytes) — WebCrypto's native encoding
            endorsement = {"kind","target","key_jkt","by":endorser user_id,
                           "by_jkt":jkt(endorser public key),"sig":b64u(sig)}
   ECDSA is randomized: the vector pins the message and ONE valid signature
   (RFC 6979 deterministic) that every verifier must accept, and a tampered field
   it must refuse — not a signature every signer must reproduce.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sys

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hkdf(ikm: bytes, salt: bytes, info: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=info.encode()).derive(ikm)


def point(pub: ec.EllipticCurvePublicKey) -> bytes:
    return pub.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def jwk(pub: ec.EllipticCurvePublicKey) -> dict:
    p = point(pub)
    return {"kty": "EC", "crv": "P-256", "x": b64u(p[1:33]), "y": b64u(p[33:])}


def jkt(pub: ec.EllipticCurvePublicKey) -> str:
    j = jwk(pub)
    canon = json.dumps({"crv": j["crv"], "kty": j["kty"], "x": j["x"], "y": j["y"]},
                       separators=(",", ":"), sort_keys=True)
    return b64u(hashlib.sha256(canon.encode()).digest())


# --- 2. values ---------------------------------------------------------------------
def value_aad(agent_id, name, kv) -> bytes:
    return f"canopy-vault/v1/value|{agent_id}|{name}|{kv}".encode()


def encrypt_value(vk, agent_id, name, kv, plaintext, nonce) -> str:
    return "zk1:" + b64u(nonce + AESGCM(vk).encrypt(nonce, plaintext.encode(), value_aad(agent_id, name, kv)))


def decrypt_value(vk, agent_id, name, kv, stored) -> str:
    if not stored.startswith("zk1:"):
        raise ValueError("not a zk1 value")
    blob = unb64u(stored[4:])
    return AESGCM(vk).decrypt(blob[:12], blob[12:], value_aad(agent_id, name, kv)).decode()


# --- 3. ECDH-ES wraps ----------------------------------------------------------------
def device_info(agent_id, kv, device_jkt) -> str:
    return f"canopy-vault/v1/wrap-device|{agent_id}|{kv}|{device_jkt}"


def member_info(agent_id, kv, user_id) -> str:
    return f"canopy-vault/v1/wrap-member|{agent_id}|{kv}|{user_id}"


def wrap_ecdh_es(vk, recipient_pub, info, e_priv, nonce) -> dict:
    z = e_priv.exchange(ec.ECDH(), recipient_pub)
    epk = point(e_priv.public_key())
    kek = hkdf(z, epk + point(recipient_pub), info)
    return {"alg": "ecdh-es", "epk": b64u(epk), "n": b64u(nonce),
            "ct": b64u(AESGCM(kek).encrypt(nonce, vk, info.encode()))}


def unwrap_ecdh_es_with_z(wrap, z, recipient_point, info) -> bytes:
    """For hardware keys that only expose ECDH(Z): the caller computes Z itself."""
    kek = hkdf(z, unb64u(wrap["epk"]) + recipient_point, info)
    return AESGCM(kek).decrypt(unb64u(wrap["n"]), unb64u(wrap["ct"]), info.encode())


def unwrap_ecdh_es(wrap, recipient_priv, info) -> bytes:
    epk = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64u(wrap["epk"]))
    z = recipient_priv.exchange(ec.ECDH(), epk)
    return unwrap_ecdh_es_with_z(wrap, z, point(recipient_priv.public_key()), info)


# --- 4. user keyset seal ---------------------------------------------------------------
def prf_input(user_id) -> bytes:
    return hashlib.sha256(f"canopy-vault/v1/prf|{user_id}".encode()).digest()


def user_key_info(user_id, cred) -> str:
    return f"canopy-vault/v1/user-key|{user_id}|{cred}"


def seal_user_key(scalar32, user_id, cred, prf_out, salt, nonce) -> dict:
    info = user_key_info(user_id, cred)
    kek = hkdf(prf_out, salt, info)
    return {"alg": "prf", "cred": cred, "s": b64u(salt), "n": b64u(nonce),
            "ct": b64u(AESGCM(kek).encrypt(nonce, scalar32, info.encode()))}


def open_user_key(seal, user_id, prf_out) -> bytes:
    info = user_key_info(user_id, seal["cred"])
    kek = hkdf(prf_out, unb64u(seal["s"]), info)
    return AESGCM(kek).decrypt(unb64u(seal["n"]), unb64u(seal["ct"]), info.encode())


# --- 4b. passphrase + secret key (2SKD) seal -------------------------------------------
PBKDF2_ITERATIONS = 650000


def _two_secret_kek(passphrase: str, secret_key: bytes, salt: bytes, user_id) -> tuple[bytes, str]:
    import unicodedata

    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    k1 = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                    iterations=PBKDF2_ITERATIONS).derive(unicodedata.normalize("NFKC", passphrase).encode())
    k2 = hkdf(secret_key, salt, f"canopy-vault/v1/secret-key|{user_id}")
    info = f"canopy-vault/v1/user-key|{user_id}|passphrase"
    return hkdf(bytes(a ^ b for a, b in zip(k1, k2)), salt, info), info


def seal_user_key_2skd(scalar32, user_id, passphrase, secret_key, salt, nonce) -> dict:
    kek, info = _two_secret_kek(passphrase, secret_key, salt, user_id)
    return {"alg": "2skd", "kdf": "pbkdf2-sha256", "i": PBKDF2_ITERATIONS, "s": b64u(salt),
            "n": b64u(nonce), "ct": b64u(AESGCM(kek).encrypt(nonce, scalar32, info.encode()))}


def open_user_key_2skd(seal, user_id, passphrase, secret_key) -> bytes:
    if seal.get("alg") != "2skd" or seal.get("i") != PBKDF2_ITERATIONS:
        raise ValueError("unsupported seal")
    kek, info = _two_secret_kek(passphrase, secret_key, unb64u(seal["s"]), user_id)
    return AESGCM(kek).decrypt(unb64u(seal["n"]), unb64u(seal["ct"]), info.encode())


def secret_key_code(sk: bytes) -> str:
    """16 bytes -> 'CV1-' + 26 Crockford chars in groups of 4."""
    n, v, out = len(sk) * 8, int.from_bytes(sk, "big"), []
    v <<= (-n) % 5
    for i in range((n + 4) // 5 - 1, -1, -1):
        out.append(CROCKFORD[(v >> (5 * i)) & 31])
    s = "".join(out)
    return "CV1-" + "-".join(s[i:i + 4] for i in range(0, len(s), 4))


def parse_secret_key_code(code: str) -> bytes:
    s = code.upper().removeprefix("CV1-").replace("-", "").replace(" ", "")
    s = s.replace("O", "0").replace("I", "1").replace("L", "1")
    v = 0
    for ch in s:
        v = (v << 5) | CROCKFORD.index(ch)
    v >>= (5 * len(s)) - 128
    return v.to_bytes(16, "big")


# --- 4c. person recovery-code seal -----------------------------------------------------
def user_recovery_info(user_id) -> str:
    return f"canopy-vault/v1/user-key|{user_id}|recovery"


def seal_user_key_recovery(scalar32, user_id, r, salt, nonce) -> dict:
    info = user_recovery_info(user_id)
    kek = hkdf(r, salt, info)
    return {"alg": "recovery", "s": b64u(salt), "n": b64u(nonce),
            "ct": b64u(AESGCM(kek).encrypt(nonce, scalar32, info.encode()))}


def open_user_key_recovery(seal, user_id, r) -> bytes:
    if seal.get("alg") != "recovery":
        raise ValueError("unsupported seal")
    info = user_recovery_info(user_id)
    kek = hkdf(r, unb64u(seal["s"]), info)
    return AESGCM(kek).decrypt(unb64u(seal["n"]), unb64u(seal["ct"]), info.encode())


# --- 5. recovery ---------------------------------------------------------------------------
def recovery_info(agent_id, kv) -> str:
    return f"canopy-vault/v1/wrap-recovery|{agent_id}|{kv}"


def wrap_recovery(vk, agent_id, kv, r, salt, nonce) -> dict:
    info = recovery_info(agent_id, kv)
    kek = hkdf(r, salt, info)
    return {"alg": "recovery", "s": b64u(salt), "n": b64u(nonce),
            "ct": b64u(AESGCM(kek).encrypt(nonce, vk, info.encode()))}


def unwrap_recovery(wrap, agent_id, kv, r) -> bytes:
    info = recovery_info(agent_id, kv)
    kek = hkdf(r, unb64u(wrap["s"]), info)
    return AESGCM(kek).decrypt(unb64u(wrap["n"]), unb64u(wrap["ct"]), info.encode())


CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def recovery_code(r: bytes) -> str:
    """256 bits -> 52 Crockford chars (last char carries 1 bit, low bits zero), groups of 4."""
    n, v, out = len(r) * 8, int.from_bytes(r, "big"), []
    v <<= (-n) % 5
    for i in range((n + 4) // 5 - 1, -1, -1):
        out.append(CROCKFORD[(v >> (5 * i)) & 31])
    s = "".join(out)
    return "-".join(s[i:i + 4] for i in range(0, len(s), 4))


def parse_recovery_code(code: str) -> bytes:
    s = code.replace("-", "").replace(" ", "").upper().replace("O", "0").replace("I", "1").replace("L", "1")
    v = 0
    for ch in s:
        v = (v << 5) | CROCKFORD.index(ch)
    v >>= (5 * len(s)) - 256
    return v.to_bytes(32, "big")


# --- 6. sealed to recipients ----------------------------------------------------------------
def seal_info(agent_id, name, kv, kind, target) -> str:
    return f"canopy-vault/v1/seal-value|{agent_id}|{name}|{kv}|{kind}:{target}"


def seal_value(plaintext, agent_id, name, kv, recipients, ephemerals, nonces) -> str:
    """`recipients`: [(kind, target, public key)]; one ephemeral + nonce each."""
    out = []
    for (kind, target, q), e, nonce in zip(recipients, ephemerals, nonces):
        k = hkdf(e.exchange(ec.ECDH(), q), b"", seal_info(agent_id, name, kv, kind, target))
        out.append({"kind": kind, "target": target, "epk": jwk(e.public_key()),
                    "ct": b64u(nonce + AESGCM(k).encrypt(nonce, plaintext.encode(),
                                                         value_aad(agent_id, name, kv)))})
    return "zks1:" + b64u(json.dumps({"kv": kv, "r": out}, separators=(",", ":")).encode())


def open_sealed_value(stored, agent_id, name, kind, target, recipient_priv) -> str:
    if not stored.startswith("zks1:"):
        raise ValueError("not a zks1 value")
    body = json.loads(unb64u(stored[5:]))
    r = next(x for x in body["r"] if x["kind"] == kind and x["target"] == target)
    epk = b"\x04" + unb64u(r["epk"]["x"]) + unb64u(r["epk"]["y"])
    z = recipient_priv.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), epk))
    k = hkdf(z, b"", seal_info(agent_id, name, body["kv"], kind, target))
    blob = unb64u(r["ct"])
    return AESGCM(k).decrypt(blob[:12], blob[12:], value_aad(agent_id, name, body["kv"])).decode()


# --- 7. endorsements ------------------------------------------------------------------------
def endorse_msg(agent_id, kind, target, key_jkt) -> str:
    return f"canopy-vault/v1/endorse|{agent_id}|{kind}|{target}|{key_jkt}"


def endorse(agent_id, kind, target, recipient_pub, endorser_priv, endorser_user_id) -> dict:
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    key_jkt = jkt(recipient_pub)
    der = endorser_priv.sign(endorse_msg(agent_id, kind, target, key_jkt).encode(),
                             ec.ECDSA(hashes.SHA256(), deterministic_signing=True))
    r, s = decode_dss_signature(der)
    return {"kind": kind, "target": target, "key_jkt": key_jkt, "by": endorser_user_id,
            "by_jkt": jkt(endorser_priv.public_key()),
            "sig": b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))}


def verify_endorsement(e, agent_id, endorser_pub) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    raw = unb64u(e["sig"])
    if len(raw) != 64 or e["by_jkt"] != jkt(endorser_pub):
        return False
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    try:
        endorser_pub.verify(der, endorse_msg(agent_id, e["kind"], e["target"], e["key_jkt"]).encode(),
                            ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return False
    return True


# --- vectors ----------------------------------------------------------------------------
def _priv(scalar_hex: str) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int(scalar_hex, 16), ec.SECP256R1())


def vectors() -> dict:
    agent_id, kv, user_id = "42", 1, "7"
    vk = bytes(range(32))
    nonce = bytes.fromhex("000102030405060708090a0b")
    salt = bytes.fromhex("a0a1a2a3a4a5a6a7a8a9aaabacadaeaf")
    device, user, e1, e2 = _priv("1f" * 32), _priv("3c" * 32), _priv("2e" * 32), _priv("4d" * 32)
    cred = b64u(b"test-credential-id")
    prf_out = hashlib.sha256(b"test-prf-output").digest()
    r = hashlib.sha256(b"test-recovery-secret").digest()
    djkt = jkt(device.public_key())
    user_scalar = user.private_numbers().private_value.to_bytes(32, "big")

    value = encrypt_value(vk, agent_id, "kindora-service-key", kv, "s3cr3t-value", nonce)
    dwrap = wrap_ecdh_es(vk, device.public_key(), device_info(agent_id, kv, djkt), e1, nonce)
    mwrap = wrap_ecdh_es(vk, user.public_key(), member_info(agent_id, kv, user_id), e2, nonce)
    seal = seal_user_key(user_scalar, user_id, cred, prf_out, salt, nonce)
    rwrap = wrap_recovery(vk, agent_id, kv, r, salt, nonce)
    code = recovery_code(r)
    # "ﬁ" (U+FB01) NFKC-normalizes to "fi": the vector pins normalization.
    passphrase = "correct horse battery staple ﬁ"
    sk = hashlib.sha256(b"test-secret-key").digest()[:16]
    pseal = seal_user_key_2skd(user_scalar, user_id, passphrase, sk, salt, nonce)
    sk_code = secret_key_code(sk)
    assert parse_secret_key_code(sk_code) == sk
    assert open_user_key_2skd(pseal, user_id, "correct horse battery staple fi", sk) == user_scalar
    for bad in (lambda: open_user_key_2skd(pseal, user_id, "wrong passphrase", sk),
                lambda: open_user_key_2skd(pseal, user_id, passphrase, bytes(16)),
                lambda: open_user_key_2skd(pseal, "8", passphrase, sk)):
        try:
            bad()
        except Exception:
            continue
        raise AssertionError("2skd binding not enforced")

    urseal = seal_user_key_recovery(user_scalar, user_id, r, salt, nonce)
    assert open_user_key_recovery(urseal, user_id, parse_recovery_code(code)) == user_scalar
    for bad in (lambda: open_user_key_recovery(urseal, "8", r),
                lambda: open_user_key_recovery(urseal, user_id, bytes(32))):
        try:
            bad()
        except Exception:
            continue
        raise AssertionError("recovery seal binding not enforced")

    e3, e4 = _priv("5e" * 32), _priv("6f" * 32)
    nonce2 = bytes.fromhex("0c0d0e0f1011121314151617")
    sealed = seal_value("minted-token", agent_id, "gog-token", kv,
                        [("device", djkt, device.public_key()), ("member", user_id, user.public_key())],
                        [e3, e4], [nonce, nonce2])
    assert open_sealed_value(sealed, agent_id, "gog-token", "device", djkt, device) == "minted-token"
    assert open_sealed_value(sealed, agent_id, "gog-token", "member", user_id, user) == "minted-token"
    for bad in (lambda: open_sealed_value(sealed, "43", "gog-token", "device", djkt, device),
                lambda: open_sealed_value(sealed, agent_id, "other", "member", user_id, user),
                lambda: open_sealed_value(sealed, agent_id, "gog-token", "member", user_id, device)):
        try:
            bad()
        except Exception:
            continue
        raise AssertionError("seal binding not enforced")

    # §7: the person (user) endorses the device for agent 42.
    endorsement = endorse(agent_id, "device", djkt, device.public_key(), user, user_id)
    assert verify_endorsement(endorsement, agent_id, user.public_key())
    for bad in ({**endorsement, "key_jkt": jkt(user.public_key())},
                {**endorsement, "target": "someone-else"},
                {**endorsement, "kind": "member"}):
        assert not verify_endorsement(bad, agent_id, user.public_key()), "endorsement binding not enforced"
    assert not verify_endorsement(endorsement, "43", user.public_key())
    assert not verify_endorsement(endorsement, agent_id, device.public_key())

    # Self-checks: every round trip, the person path end to end, and binding.
    assert decrypt_value(vk, agent_id, "kindora-service-key", kv, value) == "s3cr3t-value"
    assert unwrap_ecdh_es(dwrap, device, device_info(agent_id, kv, djkt)) == vk
    opened = _priv(open_user_key(seal, user_id, prf_out).hex())
    assert unwrap_ecdh_es(mwrap, opened, member_info(agent_id, kv, user_id)) == vk
    assert unwrap_recovery(rwrap, agent_id, kv, parse_recovery_code(code)) == vk
    for bad in (lambda: decrypt_value(vk, "43", "kindora-service-key", kv, value),
                lambda: decrypt_value(vk, agent_id, "other-name", kv, value),
                lambda: decrypt_value(vk, agent_id, "kindora-service-key", 2, value),
                lambda: unwrap_ecdh_es(dwrap, device, device_info(agent_id, 2, djkt)),
                lambda: unwrap_ecdh_es(mwrap, opened, member_info(agent_id, kv, "8")),
                lambda: open_user_key(seal, "8", prf_out)):
        try:
            bad()
        except Exception:
            continue
        raise AssertionError("binding not enforced")

    return {
        "format": "canopy-vault/zk/v1",
        "inputs": {
            "agent_id": agent_id, "kv": kv, "user_id": user_id,
            "vk_hex": vk.hex(), "nonce_hex": nonce.hex(), "salt_hex": salt.hex(),
            "value_name": "kindora-service-key", "value_plaintext": "s3cr3t-value",
            "device_priv_scalar_hex": "1f" * 32, "device_pub_jwk": jwk(device.public_key()),
            "device_jkt": djkt,
            "user_priv_scalar_hex": "3c" * 32, "user_pub_jwk": jwk(user.public_key()),
            "ephemeral_device_wrap_scalar_hex": "2e" * 32,
            "ephemeral_member_wrap_scalar_hex": "4d" * 32,
            "credential_id_b64u": cred, "prf_output_hex": prf_out.hex(),
            "recovery_secret_hex": r.hex(),
            "passphrase": passphrase, "secret_key_hex": sk.hex(),
            "sealed_name": "gog-token", "sealed_plaintext": "minted-token",
            "ephemeral_seal_device_scalar_hex": "5e" * 32,
            "ephemeral_seal_member_scalar_hex": "6f" * 32,
            "seal_member_nonce_hex": nonce2.hex(),
        },
        "outputs": {
            "value": value,
            "device_wrap": dwrap,
            "device_wrap_shared_secret_hex": device.exchange(ec.ECDH(), e1.public_key()).hex(),
            "member_wrap": mwrap,
            "prf_input_b64u": b64u(prf_input(user_id)),
            "user_key_seal": seal,
            "recovery_wrap": rwrap,
            "recovery_code": code,
            "user_key_seal_2skd": pseal,
            "secret_key_code": sk_code,
            "sealed_value": sealed,
            "user_key_seal_recovery": urseal,
            "endorse_msg": endorse_msg(agent_id, "device", djkt, djkt),
            "endorsement": endorsement,
        },
    }


if __name__ == "__main__":
    json.dump(vectors(), sys.stdout, indent=2)
    print()
