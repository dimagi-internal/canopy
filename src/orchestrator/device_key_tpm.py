"""A Windows machine's device key in its TPM (canopy-web#1549, Phase 1b).

The key is an ECDSA P-256 key made by the CNG "Microsoft Platform Crypto Provider" — the
provider backed by the TPM 2.0 — and persisted there under the name ``canopy-device-key``.
Its private half never leaves the TPM (platform-provider keys are not exportable). It is
made with NO UI policy (``NCRYPT_UI_POLICY_PROPERTY`` is never set), so signing never
prompts: unattended turns must not wait on a dialog. Everything is ctypes over
``ncrypt.dll``, so there is no native dependency to install.

The calls, in the order a key's life uses them:

* ``NCryptOpenStorageProvider(&prov, "Microsoft Platform Crypto Provider", 0)``
* ``NCryptCreatePersistedKey(prov, &key, "ECDSA_P256", "canopy-device-key", 0, 0)`` then
  ``NCryptFinalizeKey(key, 0)`` — or ``NCryptOpenKey`` for an existing one
* ``NCryptExportKey(key, 0, "ECCPUBLICBLOB", NULL, …)`` → a ``BCRYPT_ECCKEY_BLOB``
  (``dwMagic`` = ``BCRYPT_ECDSA_PUBLIC_P256_MAGIC``, ``cbKey`` = 32, then X‖Y) → the JWK
* ``NCryptSignHash(key, NULL, sha256(data), 32, sig, 64, &n, 0)`` → raw r‖s (CNG's ECDSA
  signature format is already IEEE P1363, exactly what ES256 wants)
* ``NCryptDeleteKey`` on re-enroll; ``NCryptFreeObject`` for every handle.

Nothing here runs except on Windows; :func:`available` answers False everywhere else and
whenever the provider cannot be opened (no TPM, TPM disabled), and the caller falls back to
a software key. The ``lib`` seam lets the unit tests drive the wrapper with a fake
``ncrypt`` — they cannot exercise a real TPM, which needs Windows hardware.

**The ECDH key (zero-knowledge vault, canopy-web#1550).** A second persisted key,
``ECDH_P256`` named ``canopy-device-ecdh`` (signing and key-agreement keys are separate, as
on the Secure Enclave), made with key usage ``NCRYPT_ALLOW_KEY_AGREEMENT_FLAG`` and again no
UI policy. A vault-key wrap's ephemeral public point is imported as an
``ECCPUBLICBLOB`` (``BCRYPT_ECDH_PUBLIC_P256_MAGIC``), then
``NCryptSecretAgreement(key, peer, &secret)`` and
``NCryptDeriveKey(secret, BCRYPT_KDF_RAW_SECRET, NULL, out, 32, …)`` give the shared
x-coordinate — **little-endian**, so it is reversed to the big-endian Z every other ECDH
implementation (and the format) uses.
"""
from __future__ import annotations

import ctypes
import hashlib
import struct
import sys

PROVIDER = "Microsoft Platform Crypto Provider"
ALGORITHM = "ECDSA_P256"            # BCRYPT_ECDSA_P256_ALGORITHM
KEY_NAME = "canopy-device-key"
PUBLIC_BLOB = "ECCPUBLICBLOB"       # BCRYPT_ECCPUBLIC_BLOB
P256_PUBLIC_MAGIC = 0x31534345      # BCRYPT_ECDSA_PUBLIC_P256_MAGIC ("ECS1")
P256_BYTES = 32

ECDH_ALGORITHM = "ECDH_P256"        # BCRYPT_ECDH_P256_ALGORITHM
ECDH_KEY_NAME = "canopy-device-ecdh"
P256_ECDH_PUBLIC_MAGIC = 0x314B4345  # BCRYPT_ECDH_PUBLIC_P256_MAGIC ("ECK1")
KDF_RAW_SECRET = "TRUNCATE"         # BCRYPT_KDF_RAW_SECRET
KEY_USAGE_PROPERTY = "Key Usage"    # NCRYPT_KEY_USAGE_PROPERTY
ALLOW_KEY_AGREEMENT = 0x00000004    # NCRYPT_ALLOW_KEY_AGREEMENT_FLAG

ERROR_SUCCESS = 0
NTE_BAD_KEYSET = 0x80090016         # no key by that name
NTE_EXISTS = 0x8009000F             # a key by that name already exists

_HANDLE = ctypes.c_size_t            # NCRYPT_HANDLE / NCRYPT_PROV_HANDLE / NCRYPT_KEY_HANDLE (ULONG_PTR)
_DWORD = ctypes.c_uint32


class TpmError(Exception):
    pass


def _status(code: int) -> int:
    return code & 0xFFFFFFFF


def _check(what: str, code: int) -> None:
    if _status(code) != ERROR_SUCCESS:
        raise TpmError(f"{what} failed: 0x{_status(code):08X}")


def load_ncrypt():
    """ncrypt.dll with its prototypes declared, or None off Windows."""
    if sys.platform != "win32":
        return None
    lib = ctypes.WinDLL("ncrypt.dll")  # type: ignore[attr-defined]
    p, w, d = ctypes.POINTER, ctypes.c_wchar_p, _DWORD
    proto = {
        "NCryptOpenStorageProvider": [p(_HANDLE), w, d],
        "NCryptCreatePersistedKey": [_HANDLE, p(_HANDLE), w, w, d, d],
        "NCryptOpenKey": [_HANDLE, p(_HANDLE), w, d, d],
        "NCryptFinalizeKey": [_HANDLE, d],
        "NCryptExportKey": [_HANDLE, _HANDLE, w, ctypes.c_void_p, ctypes.c_void_p, d, p(d), d],
        "NCryptSignHash": [_HANDLE, ctypes.c_void_p, ctypes.c_void_p, d, ctypes.c_void_p, d,
                           p(d), d],
        "NCryptDeleteKey": [_HANDLE, d],
        "NCryptFreeObject": [_HANDLE],
        # the ECDH key (canopy-web#1550)
        "NCryptSetProperty": [_HANDLE, w, ctypes.c_void_p, d, d],
        "NCryptImportKey": [_HANDLE, _HANDLE, w, ctypes.c_void_p, p(_HANDLE), ctypes.c_void_p,
                            d, d],
        "NCryptSecretAgreement": [_HANDLE, _HANDLE, p(_HANDLE), d],
        "NCryptDeriveKey": [_HANDLE, w, ctypes.c_void_p, ctypes.c_void_p, d, p(d), d],
    }
    for name, args in proto.items():
        fn = getattr(lib, name)
        fn.argtypes = args
        fn.restype = ctypes.c_long   # SECURITY_STATUS
    return lib


# ──────────────────────────────────────────────────────────────────────────────
# conversions (pure — what the unit tests pin)
# ──────────────────────────────────────────────────────────────────────────────

def _b64url(data: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def jwk_from_ecc_public_blob(blob: bytes, expect_magic: int = P256_PUBLIC_MAGIC) -> dict:
    """A ``BCRYPT_ECCKEY_BLOB`` (little-endian ``ULONG dwMagic, ULONG cbKey``, then X, Y
    big-endian, cbKey bytes each) → the public JWK."""
    if len(blob) < 8:
        raise TpmError(f"public key blob is {len(blob)} bytes — too short")
    magic, cb = struct.unpack("<II", blob[:8])
    if magic != expect_magic or cb != P256_BYTES:
        raise TpmError(f"not a P-256 public key blob (magic 0x{magic:08X}, cbKey {cb})")
    if len(blob) != 8 + 2 * cb:
        raise TpmError(f"public key blob is {len(blob)} bytes, expected {8 + 2 * cb}")
    x, y = blob[8:8 + cb], blob[8 + cb:8 + 2 * cb]
    return {"kty": "EC", "crv": "P-256", "x": _b64url(x), "y": _b64url(y)}


def ecdh_public_blob_from_point(point: bytes) -> bytes:
    """An X9.62 uncompressed P-256 point (0x04‖X‖Y) → the ``BCRYPT_ECCPUBLIC_BLOB``
    ``NCryptImportKey`` takes for an ECDH peer."""
    if len(point) != 1 + 2 * P256_BYTES or point[0] != 4:
        raise TpmError("not an uncompressed P-256 point")
    return struct.pack("<II", P256_ECDH_PUBLIC_MAGIC, P256_BYTES) + point[1:]


def z_from_raw_secret(raw: bytes) -> bytes:
    """``BCRYPT_KDF_RAW_SECRET`` hands back the shared x-coordinate LITTLE-endian; the
    format (and every other ECDH implementation) uses big-endian Z."""
    if len(raw) != P256_BYTES:
        raise TpmError(f"TPM returned a {len(raw)}-byte shared secret, not 32")
    return bytes(reversed(raw))


def raw_signature(sig: bytes) -> bytes:
    """CNG ECDSA signatures are r‖s already; this only checks the size (and left-pads
    nothing — CNG returns fixed-width 32-byte halves for P-256)."""
    if len(sig) != 2 * P256_BYTES:
        raise TpmError(f"TPM returned a {len(sig)}-byte signature, not 64")
    return sig


# ──────────────────────────────────────────────────────────────────────────────
# the key
# ──────────────────────────────────────────────────────────────────────────────

class TpmKey:
    """An open handle to the persisted key. Every call re-opens the provider and key —
    a runner lives for days, and a stale handle across a TPM reset is not worth caching."""

    ALGORITHM = ALGORITHM
    PUBLIC_MAGIC = P256_PUBLIC_MAGIC
    #: Key-usage flags set before finalize (None = the provider's default, as the signing
    #: key always has been).
    KEY_USAGE: "int | None" = None

    def __init__(self, name: str = KEY_NAME, lib=None):
        self.name = name
        self.lib = lib if lib is not None else load_ncrypt()
        if self.lib is None:
            raise TpmError("not Windows: there is no ncrypt.dll")

    # -- handles --
    def _provider(self) -> _HANDLE:
        prov = _HANDLE(0)
        _check("NCryptOpenStorageProvider",
               self.lib.NCryptOpenStorageProvider(ctypes.byref(prov), PROVIDER, 0))
        return prov

    def _free(self, h: _HANDLE) -> None:
        if h.value:
            self.lib.NCryptFreeObject(h)

    def _open(self, prov: _HANDLE) -> _HANDLE:
        key = _HANDLE(0)
        _check(f"NCryptOpenKey({self.name})",
               self.lib.NCryptOpenKey(prov, ctypes.byref(key), self.name, 0, 0))
        return key

    def _with_key(self, fn):
        prov = self._provider()
        key = _HANDLE(0)
        try:
            key = self._open(prov)
            return fn(key)
        finally:
            self._free(key)
            self._free(prov)

    # -- life cycle --
    def create(self) -> "TpmKey":
        """Make the persisted key (no UI policy, so never a prompt). An existing key by
        this name is kept — creating is idempotent."""
        prov = self._provider()
        key = _HANDLE(0)
        try:
            rc = _status(self.lib.NCryptCreatePersistedKey(prov, ctypes.byref(key),
                                                            self.ALGORITHM, self.name, 0, 0))
            if rc == NTE_EXISTS:
                return self
            _check("NCryptCreatePersistedKey", rc)
            if self.KEY_USAGE is not None:
                usage = _DWORD(self.KEY_USAGE)
                _check("NCryptSetProperty(Key Usage)",
                       self.lib.NCryptSetProperty(key, KEY_USAGE_PROPERTY, ctypes.byref(usage),
                                                  ctypes.sizeof(usage), 0))
            _check("NCryptFinalizeKey", self.lib.NCryptFinalizeKey(key, 0))
            return self
        finally:
            self._free(key)
            self._free(prov)

    def delete(self) -> None:
        prov = self._provider()
        key = _HANDLE(0)
        try:
            rc = _status(self.lib.NCryptOpenKey(prov, ctypes.byref(key), self.name, 0, 0))
            if rc == NTE_BAD_KEYSET:
                return
            _check(f"NCryptOpenKey({self.name})", rc)
            # NCryptDeleteKey frees the handle itself, success or not.
            rc = self.lib.NCryptDeleteKey(key, 0)
            key = _HANDLE(0)
            _check("NCryptDeleteKey", rc)
        finally:
            self._free(key)
            self._free(prov)

    # -- use --
    def public_blob(self) -> bytes:
        def export(key: _HANDLE) -> bytes:
            size = _DWORD(0)
            _check("NCryptExportKey(size)",
                   self.lib.NCryptExportKey(key, 0, PUBLIC_BLOB, None, None, 0,
                                            ctypes.byref(size), 0))
            buf = ctypes.create_string_buffer(size.value)
            _check("NCryptExportKey",
                   self.lib.NCryptExportKey(key, 0, PUBLIC_BLOB, None, buf, size.value,
                                            ctypes.byref(size), 0))
            return buf.raw[:size.value]
        return self._with_key(export)

    def public_jwk(self) -> dict:
        return jwk_from_ecc_public_blob(self.public_blob(), self.PUBLIC_MAGIC)

    def sign(self, data: bytes) -> bytes:
        """Raw ES256 signature (r‖s) over SHA-256(`data`), made inside the TPM."""
        digest = hashlib.sha256(data).digest()

        def sign_hash(key: _HANDLE) -> bytes:
            hbuf = ctypes.create_string_buffer(digest, len(digest))
            out = ctypes.create_string_buffer(2 * P256_BYTES)
            n = _DWORD(0)
            _check("NCryptSignHash",
                   self.lib.NCryptSignHash(key, None, hbuf, len(digest), out, len(out),
                                           ctypes.byref(n), 0))
            return raw_signature(out.raw[:n.value])
        return self._with_key(sign_hash)


class TpmEcdhKey(TpmKey):
    """The persisted ECDH P-256 key a zero-knowledge vault key is wrapped to (canopy-web
    #1550). It never signs; it answers ECDH against a wrap's ephemeral public point."""

    ALGORITHM = ECDH_ALGORITHM
    PUBLIC_MAGIC = P256_ECDH_PUBLIC_MAGIC
    KEY_USAGE = ALLOW_KEY_AGREEMENT

    def __init__(self, name: str = ECDH_KEY_NAME, lib=None):
        super().__init__(name, lib)

    def sign(self, data: bytes) -> bytes:  # pragma: no cover — a guard, not a path
        raise TpmError("the ECDH key does not sign")

    def derive(self, peer_point: bytes) -> bytes:
        """Z = the x-coordinate of ECDH(this key, peer), big-endian, 32 bytes."""
        blob = ecdh_public_blob_from_point(peer_point)
        prov = self._provider()
        key, peer, secret = _HANDLE(0), _HANDLE(0), _HANDLE(0)
        try:
            key = self._open(prov)
            bbuf = ctypes.create_string_buffer(blob, len(blob))
            _check("NCryptImportKey(peer)",
                   self.lib.NCryptImportKey(prov, 0, PUBLIC_BLOB, None, ctypes.byref(peer),
                                            bbuf, len(blob), 0))
            _check("NCryptSecretAgreement",
                   self.lib.NCryptSecretAgreement(key, peer, ctypes.byref(secret), 0))
            out = ctypes.create_string_buffer(P256_BYTES)
            n = _DWORD(0)
            _check("NCryptDeriveKey(BCRYPT_KDF_RAW_SECRET)",
                   self.lib.NCryptDeriveKey(secret, KDF_RAW_SECRET, None, out, len(out),
                                            ctypes.byref(n), 0))
            return z_from_raw_secret(out.raw[:n.value])
        finally:
            self._free(secret)
            self._free(peer)
            self._free(key)
            self._free(prov)


# ──────────────────────────────────────────────────────────────────────────────
# what device_key.py calls
# ──────────────────────────────────────────────────────────────────────────────

def available(lib=None) -> bool:
    """Whether the TPM-backed provider can be opened here."""
    try:
        lib = lib if lib is not None else load_ncrypt()
        if lib is None:
            return False
        prov = _HANDLE(0)
        if _status(lib.NCryptOpenStorageProvider(ctypes.byref(prov), PROVIDER, 0)) != 0:
            return False
        lib.NCryptFreeObject(prov)
        return True
    except Exception:  # noqa: BLE001 — anything odd means "use a software key"
        return False


def create_key(name: str = KEY_NAME, lib=None) -> TpmKey:
    return TpmKey(name, lib).create()


def open_key(name: str = KEY_NAME, lib=None) -> TpmKey:
    key = TpmKey(name, lib)
    key.public_blob()          # fail now, not at the first sign, if the key is gone
    return key


def delete_key(name: str = KEY_NAME, lib=None) -> None:
    TpmKey(name, lib).delete()


def create_ecdh_key(name: str = ECDH_KEY_NAME, lib=None) -> TpmEcdhKey:
    return TpmEcdhKey(name, lib).create()  # type: ignore[return-value]


def open_ecdh_key(name: str = ECDH_KEY_NAME, lib=None) -> TpmEcdhKey:
    key = TpmEcdhKey(name, lib)
    key.public_blob()
    return key


def delete_ecdh_key(name: str = ECDH_KEY_NAME, lib=None) -> None:
    TpmEcdhKey(name, lib).delete()
