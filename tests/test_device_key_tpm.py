"""A Windows machine's TPM device key (canopy-web#1549): ctypes over ncrypt.dll.

These tests cannot touch a real TPM — that needs Windows with TPM 2.0. They drive the
wrapper with a FAKE ncrypt that speaks the same calling convention (byref handles, caller
buffers, SECURITY_STATUS returns) with a software P-256 key inside, which pins:

* the BCRYPT_ECCPUBLIC_BLOB → JWK conversion (magic, cbKey, X‖Y);
* that NCryptSignHash is given SHA-256(data) and its r‖s comes back as the ES256 signature,
  verifiable with the exported JWK — end to end through a DPoP proof;
* the call sequence (no UI policy property is ever set; create is idempotent on NTE_EXISTS);
* provider selection: Windows with the provider → kind=tpm, without → software.
"""
from __future__ import annotations

import ctypes
import json
import struct

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (Prehashed, decode_dss_signature,
                                                              encode_dss_signature)

from orchestrator import device_key as dk
from orchestrator import device_key_tpm as tpm


class FakeNCrypt:
    """ncrypt.dll's eight functions, over a dict of named software keys."""

    def __init__(self, provider_ok=True):
        self.provider_ok = provider_ok
        self.keys: dict[str, ec.EllipticCurvePrivateKey] = {}
        self.handles: dict[int, object] = {}
        self.calls: list[str] = []
        self.next = 100

    def _h(self, obj) -> int:
        self.next += 1
        self.handles[self.next] = obj
        return self.next

    @staticmethod
    def _val(h):
        return h.value if hasattr(h, "value") else int(h)

    def NCryptOpenStorageProvider(self, pprov, name, flags):
        self.calls.append("OpenStorageProvider")
        if not self.provider_ok or name != tpm.PROVIDER:
            return 0x80090030 - (1 << 32)              # NTE_DEVICE_NOT_READY, as a signed LONG
        pprov._obj.value = self._h(("prov",))
        return 0

    def NCryptCreatePersistedKey(self, prov, pkey, alg, name, legacy, flags):
        self.calls.append(f"CreatePersistedKey:{alg}:{name}:{flags}")
        assert alg == "ECDSA_P256"
        if name in self.keys:
            return tpm.NTE_EXISTS - (1 << 32)
        pkey._obj.value = self._h(("pending", name))
        return 0

    def NCryptFinalizeKey(self, key, flags):
        self.calls.append("FinalizeKey")
        kind, name = self.handles[self._val(key)]
        self.keys[name] = ec.generate_private_key(ec.SECP256R1())
        return 0

    def NCryptOpenKey(self, prov, pkey, name, legacy, flags):
        self.calls.append(f"OpenKey:{name}")
        if name not in self.keys:
            return tpm.NTE_BAD_KEYSET - (1 << 32)
        pkey._obj.value = self._h(("key", name))
        return 0

    def NCryptExportKey(self, key, export_key, blob_type, params, out, cb, pcb, flags):
        assert blob_type == "ECCPUBLICBLOB"
        _, name = self.handles[self._val(key)]
        n = self.keys[name].public_key().public_numbers()
        blob = struct.pack("<II", tpm.P256_PUBLIC_MAGIC, 32) + n.x.to_bytes(32, "big") + \
            n.y.to_bytes(32, "big")
        pcb._obj.value = len(blob)
        if out is not None:
            ctypes.memmove(out, blob, len(blob))
        return 0

    def NCryptSignHash(self, key, padding, hbuf, cbhash, out, cbsig, pcb, flags):
        assert padding is None and cbhash == 32
        _, name = self.handles[self._val(key)]
        digest = hbuf.raw[:cbhash]
        r, s = decode_dss_signature(self.keys[name].sign(digest, ec.ECDSA(Prehashed(hashes.SHA256()))))
        sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        ctypes.memmove(out, sig, 64)
        pcb._obj.value = 64
        return 0

    def NCryptDeleteKey(self, key, flags):
        self.calls.append("DeleteKey")
        _, name = self.handles.pop(self._val(key))
        self.keys.pop(name, None)
        return 0

    def NCryptFreeObject(self, h):
        self.handles.pop(self._val(h), None)
        return 0


def _verify(jwk: dict, data: bytes, sig: bytes) -> None:
    pub = ec.EllipticCurvePublicNumbers(int.from_bytes(dk.b64url_decode(jwk["x"]), "big"),
                                        int.from_bytes(dk.b64url_decode(jwk["y"]), "big"),
                                        ec.SECP256R1()).public_key()
    der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
    pub.verify(der, data, ec.ECDSA(hashes.SHA256()))


# ── conversions ───────────────────────────────────────────────────────────────

def test_ecc_public_blob_to_jwk():
    x, y = bytes(range(32)), bytes(range(32, 64))
    blob = struct.pack("<II", 0x31534345, 32) + x + y
    assert tpm.jwk_from_ecc_public_blob(blob) == {
        "kty": "EC", "crv": "P-256", "x": dk.b64url(x), "y": dk.b64url(y)}


@pytest.mark.parametrize("blob", [
    b"\x00" * 4,                                                       # too short
    struct.pack("<II", 0x32534345, 32) + b"\x00" * 64,                 # a PRIVATE-key magic
    struct.pack("<II", 0x31534345, 48) + b"\x00" * 96,                 # P-384
    struct.pack("<II", 0x31534345, 32) + b"\x00" * 63,                 # truncated
])
def test_a_wrong_blob_is_refused(blob):
    with pytest.raises(tpm.TpmError):
        tpm.jwk_from_ecc_public_blob(blob)


def test_a_signature_must_be_64_bytes():
    assert tpm.raw_signature(b"\x01" * 64) == b"\x01" * 64
    with pytest.raises(tpm.TpmError, match="72-byte"):
        tpm.raw_signature(b"\x30" * 72)                                # a DER signature


# ── the key through a fake ncrypt ─────────────────────────────────────────────

def test_create_sign_verify_through_ncrypt():
    lib = FakeNCrypt()
    key = tpm.create_key(lib=lib)
    assert "CreatePersistedKey:ECDSA_P256:canopy-device-key:0" in lib.calls
    assert "FinalizeKey" in lib.calls
    assert not any("Property" in c for c in lib.calls)                # no UI policy, ever
    jwk = key.public_jwk()
    sig = key.sign(b"payload")
    assert len(sig) == 64
    _verify(jwk, b"payload", sig)
    assert lib.handles == {}                                           # every handle freed


def test_create_is_idempotent():
    lib = FakeNCrypt()
    a = tpm.create_key(lib=lib).public_jwk()
    b = tpm.create_key(lib=lib).public_jwk()
    assert a == b and lib.calls.count("FinalizeKey") == 1


def test_open_a_missing_key_fails_loudly():
    with pytest.raises(tpm.TpmError, match="NCryptOpenKey"):
        tpm.open_key(lib=FakeNCrypt())


def test_delete_then_absent():
    lib = FakeNCrypt()
    tpm.create_key(lib=lib)
    tpm.delete_key(lib=lib)
    assert lib.keys == {} and "DeleteKey" in lib.calls
    tpm.delete_key(lib=lib)                                            # already gone: fine


def test_available_only_when_the_provider_opens():
    assert tpm.available(lib=FakeNCrypt()) is True
    assert tpm.available(lib=FakeNCrypt(provider_ok=False)) is False
    assert tpm.available() is False                                    # not Windows (CI, macOS)


# ── provider selection in device_key ──────────────────────────────────────────

@pytest.fixture
def windows(monkeypatch):
    lib = FakeNCrypt()
    monkeypatch.setattr(tpm, "load_ncrypt", lambda: lib)
    return lib


def test_windows_with_a_tpm_makes_a_tpm_key(windows):
    key, created = dk.ensure(system=lambda: "Windows")
    assert created and key.kind == dk.KIND_TPM
    assert json.loads(dk.tpm_path().read_text())["name"] == "canopy-device-key"
    again = dk.load()
    assert again.kind == dk.KIND_TPM and again.jkt == key.jkt
    proof = dk.dpop_proof(again, "GET", "https://canopy.example/x")
    h, c, s = proof.split(".")
    _verify(json.loads(dk.b64url_decode(h))["jwk"], f"{h}.{c}".encode(), dk.b64url_decode(s))


def test_windows_without_a_tpm_falls_back_to_software(monkeypatch):
    monkeypatch.setattr(tpm, "load_ncrypt", lambda: FakeNCrypt(provider_ok=False))
    key, _ = dk.ensure(system=lambda: "Windows")
    assert key.kind == dk.KIND_SOFTWARE


def test_asking_for_tpm_without_one_says_so():
    with pytest.raises(dk.DeviceKeyError, match="TPM"):
        dk.ensure(kind="tpm", system=lambda: "Windows")


def test_tpm_enrolls_as_kind_tpm_with_its_jwk(windows):
    seen = []

    def call(method, path, body):
        seen.append(body)
        return {"id": 3, "jkt": dk.jwk_thumbprint(body["public_jwk"]), "status": "pending",
                "approve_url": "https://c/approve/3"}

    st = dk.enroll(runner_id="r-win", call=call, system=lambda: "Windows")
    assert seen[0]["kind"] == "tpm" and set(seen[0]["public_jwk"]) == {"kty", "crv", "x", "y"}
    assert st["kind"] == "tpm" and st["status"] == "pending"
    assert "DPoP" in dk.proof_headers("GET", "https://c/api/agents/x/credentials/resolve")


def test_reenroll_deletes_the_tpm_key(windows):
    dk.ensure(system=lambda: "Windows")
    dk.remove_local()
    assert windows.keys == {} and not dk.tpm_path().exists()
