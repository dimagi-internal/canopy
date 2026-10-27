"""This machine's ECDH key — what a zero-knowledge vault key is wrapped to (canopy-web#1550).

Every backend is driven to the SAME normative answer: the vector's device key is planted in
it (a software PEM, a fake Secure Enclave helper speaking the real helper's protocol, a
fake ``ncrypt`` standing in for the TPM), asked for Z against the vector's ephemeral point,
and must reproduce ``device_wrap_shared_secret_hex`` and unwrap ``device_wrap`` to the
vector's VK. None of these tests touches real hardware: the Secure Enclave is smoke-tested
by hand on a Mac (see canopy-web runner/device-key), and a real TPM needs Windows.
"""
from __future__ import annotations

import ctypes
import json
import os
import stat
import struct
import sys
import textwrap
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from orchestrator import device_key as dk
from orchestrator import device_key_tpm as tpm
from orchestrator import vault_zk as zk

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "vault_zk" /
                      "zk_v1_vectors.json").read_text(encoding="utf-8"))
I, O = VECTORS["inputs"], VECTORS["outputs"]
DEVICE = ec.derive_private_key(int(I["device_priv_scalar_hex"], 16), ec.SECP256R1())
EPK = zk.wrap_epk_point(O["device_wrap"])
Z = bytes.fromhex(O["device_wrap_shared_secret_hex"])
VK = bytes.fromhex(I["vk_hex"])
INFO = zk.device_info(I["agent_id"], I["kv"], I["device_jkt"])


def _pem(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _assert_vector(key: dk.EcdhKey) -> None:
    assert key.public_jwk == I["device_pub_jwk"]
    assert key.jkt == I["device_jkt"]
    assert key.derive(EPK) == Z
    assert zk.unwrap_ecdh_es_with_z(O["device_wrap"], key.derive(EPK), key.point, INFO) == VK


# ── software / aws-iam: a 0600 PEM ────────────────────────────────────────────

def test_software_ecdh_key_is_private_idempotent_and_separate(tmp_path):
    key, created = dk.ensure_ecdh("software")
    assert created and key.kind == "software"
    assert stat.S_IMODE(os.stat(dk.ecdh_pem_path()).st_mode) == 0o600
    again, created2 = dk.ensure_ecdh("software")
    assert not created2 and again.jkt == key.jkt
    signing, _ = dk.ensure(allow_software=True, system=lambda: "Linux")
    assert signing.jkt != key.jkt                     # never the signing key


def test_aws_iam_ecdh_is_a_software_pem():
    key, _ = dk.ensure_ecdh("aws-iam")
    assert key.kind == "software" and dk.ecdh_pem_path().is_file()


def test_software_backend_reproduces_the_vector():
    dk._write_private(dk.ecdh_pem_path(), _pem(DEVICE))
    _assert_vector(dk.load_ecdh())


# ── Secure Enclave: the helper protocol ───────────────────────────────────────

FAKE_HELPER = textwrap.dedent('''\
    #!{python}
    """A stand-in for canopy-device-key: the ECDH half of its protocol, software inside."""
    import base64, json, os, sys
    from pathlib import Path
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    d = Path(os.environ["CANOPY_DEVICE_KEY_DIR"]); f = d / "se-ecdh.key"
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    unb64 = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    def load():
        return serialization.load_pem_private_key(f.read_bytes(), None)
    def jwk(k):
        n = k.public_key().public_numbers()
        return {{"kty": "EC", "crv": "P-256", "x": b64(n.x.to_bytes(32, "big")),
                 "y": b64(n.y.to_bytes(32, "big"))}}
    cmd = sys.argv[1]
    if not {ecdh} and cmd.startswith("ecdh"):
        sys.exit(2)                                   # a helper built before #1550
    if cmd == "info":
        print(json.dumps({{"secure_enclave_available": True, "key_present": True}}))
    elif cmd == "ecdh-create":
        created = not f.exists()
        if created:
            d.mkdir(parents=True, exist_ok=True)
            k = ec.generate_private_key(ec.SECP256R1())
            f.write_bytes(k.private_bytes(serialization.Encoding.PEM,
                          serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            os.chmod(f, 0o600)
        print(json.dumps({{"kind": "secure-enclave", "created": created, "jwk": jwk(load())}}))
    elif cmd == "ecdh-public-jwk":
        print(json.dumps(jwk(load())))
    elif cmd == "ecdh-derive":
        peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64(sys.argv[2]))
        print(b64(load().exchange(ec.ECDH(), peer)))
    else:
        sys.exit(2)
''')


@pytest.fixture
def se_helper(tmp_path, monkeypatch):
    def make(ecdh=True):
        p = tmp_path / "canopy-device-key"
        p.write_text(FAKE_HELPER.format(python=sys.executable, ecdh=ecdh))
        p.chmod(0o755)
        monkeypatch.setenv(dk.HELPER_ENV, str(p))
        return p
    return make


def test_secure_enclave_ecdh_through_the_helper(se_helper):
    se_helper()
    key, created = dk.ensure_ecdh("secure-enclave")
    assert created and key.kind == "secure-enclave" and dk.se_ecdh_path().is_file()
    assert dk.ensure_ecdh("secure-enclave")[1] is False


def test_secure_enclave_backend_reproduces_the_vector(se_helper):
    se_helper()
    dk._write_private(dk.se_ecdh_path(), _pem(DEVICE))
    _assert_vector(dk.load_ecdh())


def test_a_helper_from_before_ecdh_is_unsupported_not_fatal(se_helper):
    se_helper(ecdh=False)
    with pytest.raises(dk.EcdhUnsupported, match="too old"):
        dk.ensure_ecdh("secure-enclave")


# ── TPM: a fake ncrypt, little-endian raw secret ──────────────────────────────

class FakeNCryptEcdh:
    """ncrypt.dll's ECDH calls over software keys. Like the real BCRYPT_KDF_RAW_SECRET,
    NCryptDeriveKey hands the shared x-coordinate back LITTLE-endian."""

    def __init__(self, planted=None):
        self.planted = planted
        self.keys: dict = {}
        self.usage: dict = {}
        self.handles: dict = {}
        self.calls: list = []
        self.next = 500

    def _h(self, obj):
        self.next += 1
        self.handles[self.next] = obj
        return self.next

    @staticmethod
    def _v(h):
        return h.value if hasattr(h, "value") else int(h)

    def NCryptOpenStorageProvider(self, pprov, name, flags):
        pprov._obj.value = self._h(("prov",))
        return 0

    def NCryptCreatePersistedKey(self, prov, pkey, alg, name, legacy, flags):
        self.calls.append(f"Create:{alg}:{name}")
        assert alg == "ECDH_P256"
        if name in self.keys:
            return tpm.NTE_EXISTS - (1 << 32)
        pkey._obj.value = self._h(("pending", name))
        return 0

    def NCryptSetProperty(self, key, prop, pval, cb, flags):
        self.calls.append(f"SetProperty:{prop}")
        _, name = self.handles[self._v(key)]
        self.usage[name] = ctypes.cast(pval, ctypes.POINTER(ctypes.c_uint32)).contents.value
        return 0

    def NCryptFinalizeKey(self, key, flags):
        _, name = self.handles[self._v(key)]
        self.keys[name] = self.planted or ec.generate_private_key(ec.SECP256R1())
        return 0

    def NCryptOpenKey(self, prov, pkey, name, legacy, flags):
        if name not in self.keys:
            return tpm.NTE_BAD_KEYSET - (1 << 32)
        pkey._obj.value = self._h(("key", name))
        return 0

    def NCryptExportKey(self, key, export_key, blob_type, params, out, cb, pcb, flags):
        _, name = self.handles[self._v(key)]
        n = self.keys[name].public_key().public_numbers()
        blob = struct.pack("<II", tpm.P256_ECDH_PUBLIC_MAGIC, 32) + n.x.to_bytes(32, "big") + \
            n.y.to_bytes(32, "big")
        pcb._obj.value = len(blob)
        if out is not None:
            ctypes.memmove(out, blob, len(blob))
        return 0

    def NCryptImportKey(self, prov, import_key, blob_type, params, pkey, buf, cb, flags):
        self.calls.append(f"Import:{blob_type}")
        blob = buf.raw[:cb]
        magic, size = struct.unpack("<II", blob[:8])
        assert (magic, size) == (tpm.P256_ECDH_PUBLIC_MAGIC, 32)
        peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b"\x04" + blob[8:])
        pkey._obj.value = self._h(("peer", peer))
        return 0

    def NCryptSecretAgreement(self, key, peer, psecret, flags):
        self.calls.append("SecretAgreement")
        _, name = self.handles[self._v(key)]
        _, pub = self.handles[self._v(peer)]
        psecret._obj.value = self._h(("secret", self.keys[name].exchange(ec.ECDH(), pub)))
        return 0

    def NCryptDeriveKey(self, secret, kdf, params, out, cb, pcb, flags):
        self.calls.append(f"Derive:{kdf}")
        _, z = self.handles[self._v(secret)]
        raw = bytes(reversed(z))                      # BCRYPT_KDF_RAW_SECRET: little-endian
        ctypes.memmove(out, raw, len(raw))
        pcb._obj.value = len(raw)
        return 0

    def NCryptDeleteKey(self, key, flags):
        _, name = self.handles.pop(self._v(key))
        self.keys.pop(name, None)
        return 0

    def NCryptFreeObject(self, h):
        self.handles.pop(self._v(h), None)
        return 0


def test_raw_secret_is_reversed_and_points_become_ecdh_blobs():
    assert tpm.z_from_raw_secret(bytes(range(32))) == bytes(reversed(range(32)))
    with pytest.raises(tpm.TpmError):
        tpm.z_from_raw_secret(b"\x00" * 31)
    blob = tpm.ecdh_public_blob_from_point(EPK)
    assert blob[:8] == struct.pack("<II", 0x314B4345, 32) and blob[8:] == EPK[1:]
    with pytest.raises(tpm.TpmError):
        tpm.ecdh_public_blob_from_point(EPK[:-1])


def test_tpm_ecdh_key_reproduces_the_vector_and_frees_every_handle():
    lib = FakeNCryptEcdh(planted=DEVICE)
    key = tpm.create_ecdh_key(lib=lib)
    assert "Create:ECDH_P256:canopy-device-ecdh" in lib.calls
    assert lib.usage["canopy-device-ecdh"] == tpm.ALLOW_KEY_AGREEMENT
    assert key.public_jwk() == I["device_pub_jwk"]
    assert key.derive(EPK) == Z                       # the LE→BE reversal is right
    assert "Derive:TRUNCATE" in lib.calls and "Import:ECCPUBLICBLOB" in lib.calls
    assert lib.handles == {}
    assert tpm.create_ecdh_key(lib=lib).public_jwk() == I["device_pub_jwk"]   # idempotent


def test_device_key_drives_the_tpm_ecdh_key(monkeypatch):
    lib = FakeNCryptEcdh(planted=DEVICE)
    monkeypatch.setattr(tpm, "load_ncrypt", lambda: lib)
    key, created = dk.ensure_ecdh("tpm")
    assert created and key.kind == "tpm"
    assert json.loads(dk.tpm_ecdh_path().read_text())["name"] == "canopy-device-ecdh"
    _assert_vector(dk.load_ecdh())
    dk.remove_local()
    assert lib.keys == {} and not dk.tpm_ecdh_path().exists()


def test_a_tpm_that_will_not_make_an_ecdh_key_falls_back_to_software(monkeypatch):
    # Raising here used to enroll the TPM signing key with no ECDH key at all, so no
    # vault key could be wrapped to the device (DIM-SI-W021, 2026-10-26).
    class Refuses:
        def __getattr__(self, name):
            raise OSError("NTE_NOT_SUPPORTED")
    monkeypatch.setattr(tpm, "load_ncrypt", lambda: Refuses())
    key, created = dk.ensure_ecdh("tpm")
    assert created and key.kind == "software" and "TPM would not make" in key.note
    assert dk.ecdh_pem_path().is_file() and not dk.tpm_ecdh_path().exists()
    again, created2 = dk.ensure_ecdh("tpm")
    assert not created2 and again.jkt == key.jkt


# ── enrollment sends ecdh_public_jwk ──────────────────────────────────────────

def _server(seen, **extra):
    def call(method, path, body):
        seen.append(body)
        return {"id": 9, "jkt": dk.jwk_thumbprint(body["public_jwk"]), "status": "pending",
                "approve_url": "https://c/approve/9", **extra}
    return call


def test_enroll_sends_the_ecdh_public_jwk_and_caches_its_thumbprint():
    seen = []
    st = dk.enroll(runner_id="r-1", call=_server(seen), allow_software=True)
    body = seen[0]
    assert set(body["ecdh_public_jwk"]) == {"kty", "crv", "x", "y"}
    ecdh = dk.load_ecdh()
    assert body["ecdh_public_jwk"] == ecdh.public_jwk
    assert body["ecdh_public_jwk"] != body["public_jwk"]
    assert st["ecdh_jkt"] == ecdh.jkt == dk.read_status()["ecdh_jkt"]
    dk.enroll(runner_id="r-1", call=_server(seen))
    assert seen[1]["ecdh_public_jwk"] == body["ecdh_public_jwk"]     # stable across starts


def test_enroll_refuses_an_ecdh_thumbprint_mismatch():
    with pytest.raises(dk.DeviceKeyError, match="ECDH thumbprint"):
        dk.enroll(runner_id="r-1", call=_server([], ecdh_jkt="not-mine"), allow_software=True)


def test_a_helper_without_ecdh_still_enrolls_the_signing_key(se_helper, monkeypatch):
    se_helper(ecdh=False)
    key, _ = dk.ensure(allow_software=True, system=lambda: "Linux")
    dk.write_status({})
    monkeypatch.setattr(dk, "_ecdh_kind_for", lambda kind: "secure-enclave")
    seen = []
    st = dk.enroll(runner_id="r-1", call=_server(seen))
    assert "ecdh_public_jwk" not in seen[0]
    assert "too old" in st["ecdh_note"] and "ecdh_jkt" not in st
