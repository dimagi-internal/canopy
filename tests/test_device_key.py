"""The device key behind a Canopy Vault resolve (canopy#946): key kinds, the RFC 9449
DPoP proof, enrollment, and the rule that a proof is an ADDITION — never a new failure.

The Secure Enclave path is exercised through a FAKE `canopy-device-key` on disk that
speaks the real helper's protocol (create / public-jwk / sign / info) with a software key
underneath, so the wrapper's contract with the Swift helper is pinned without an enclave.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
import uuid

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from orchestrator import device_key as dk


@pytest.fixture
def keydir(tmp_path, monkeypatch):
    d = tmp_path / "dk"
    monkeypatch.setenv(dk.DIR_ENV, str(d))
    monkeypatch.setenv(dk.HELPER_ENV, "/nonexistent")
    return d


FAKE_HELPER = textwrap.dedent('''\
    #!{python}
    """A stand-in for the Swift helper: same argv, same output, a software key inside."""
    import base64, json, os, sys
    from pathlib import Path
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    d = Path(os.environ["CANOPY_DEVICE_KEY_DIR"]); f = d / "se.key"
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    def load():
        return serialization.load_pem_private_key(f.read_bytes(), None)
    def jwk(k):
        n = k.public_key().public_numbers()
        return {{"kty": "EC", "crv": "P-256", "x": b64(n.x.to_bytes(32, "big")),
                 "y": b64(n.y.to_bytes(32, "big"))}}
    cmd = sys.argv[1]
    if cmd == "info":
        print(json.dumps({{"secure_enclave_available": {available}, "key_present": f.exists()}}))
    elif cmd == "create":
        if not f.exists():
            d.mkdir(parents=True, exist_ok=True)
            k = ec.generate_private_key(ec.SECP256R1())
            f.write_bytes(k.private_bytes(serialization.Encoding.PEM,
                          serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            os.chmod(f, 0o600)
        print(json.dumps({{"kind": "secure-enclave", "jwk": jwk(load())}}))
    elif cmd == "public-jwk":
        print(json.dumps(jwk(load())))
    elif cmd == "sign":
        data = base64.urlsafe_b64decode(sys.argv[2] + "=" * (-len(sys.argv[2]) % 4))
        r, s = decode_dss_signature(load().sign(data, ec.ECDSA(hashes.SHA256())))
        print(b64(r.to_bytes(32, "big") + s.to_bytes(32, "big")))
    else:
        sys.exit(2)
''')


@pytest.fixture
def helper(tmp_path, monkeypatch, keydir):
    def make(available=True):
        p = tmp_path / "canopy-device-key"
        p.write_text(FAKE_HELPER.format(python=sys.executable, available=available))
        p.chmod(0o755)
        monkeypatch.setenv(dk.HELPER_ENV, str(p))
        return p
    return make


def _verify(jwk: dict, signing_input: bytes, sig: bytes) -> None:
    pub = ec.EllipticCurvePublicNumbers(int.from_bytes(dk.b64url_decode(jwk["x"]), "big"),
                                        int.from_bytes(dk.b64url_decode(jwk["y"]), "big"),
                                        ec.SECP256R1()).public_key()
    der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
    pub.verify(der, signing_input, ec.ECDSA(hashes.SHA256()))


# ── keys ──────────────────────────────────────────────────────────────────────

def test_software_key_is_private_and_stable(keydir):
    key, created = dk.ensure(allow_software=True, system=lambda: "Linux")
    assert created and key.kind == dk.KIND_SOFTWARE
    assert stat.S_IMODE(os.stat(dk.sw_path()).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(keydir).st_mode) == 0o700
    again, created2 = dk.ensure(system=lambda: "Linux")
    assert not created2 and again.jkt == key.jkt


def test_linux_falls_back_to_software_without_asking(keydir):
    key, _ = dk.ensure(system=lambda: "Linux")
    assert key.kind == dk.KIND_SOFTWARE


def test_a_mac_without_the_helper_does_not_silently_make_a_software_key(keydir):
    with pytest.raises(dk.DeviceKeyError, match="canopy-device-key"):
        dk.ensure(system=lambda: "Darwin", which=lambda _: None)
    assert not dk.sw_path().exists()


def test_secure_enclave_through_the_helper_protocol(helper):
    helper(available=True)
    key, created = dk.ensure(system=lambda: "Darwin")
    assert created and key.kind == dk.KIND_SE and dk.se_path().exists()
    sig = key.sign(b"hello")
    assert len(sig) == 64
    _verify(key.public_jwk, b"hello", sig)
    assert dk.load().jkt == key.jkt          # idempotent: loads, never re-creates


def test_a_mac_whose_enclave_is_unavailable_uses_software(helper):
    helper(available=False)
    key, _ = dk.ensure(system=lambda: "Darwin")
    assert key.kind == dk.KIND_SOFTWARE


def test_thumbprint_is_rfc7638():
    jwk = {"kty": "EC", "crv": "P-256", "x": "AAA", "y": "BBB", "kid": "ignored"}
    canonical = b'{"crv":"P-256","kty":"EC","x":"AAA","y":"BBB"}'
    import hashlib
    assert dk.jwk_thumbprint(jwk) == dk.b64url(hashlib.sha256(canonical).digest())


# ── DPoP ──────────────────────────────────────────────────────────────────────

def test_dpop_proof_matches_the_contract(keydir):
    key, _ = dk.ensure(allow_software=True, system=lambda: "Linux")
    proof = dk.dpop_proof(key, "get", "https://canopy.example/api/agents/eva/credentials/"
                                      "resolve?refs=A,B#frag", now=1700000000.9)
    h, c, s = proof.split(".")
    header, claims = json.loads(dk.b64url_decode(h)), json.loads(dk.b64url_decode(c))
    assert header == {"typ": "dpop+jwt", "alg": "ES256", "jwk": key.public_jwk}
    assert claims["htm"] == "GET"
    assert claims["htu"] == "https://canopy.example/api/agents/eva/credentials/resolve"
    assert claims["iat"] == 1700000000
    uuid.UUID(claims["jti"])
    _verify(key.public_jwk, f"{h}.{c}".encode(), dk.b64url_decode(s))
    other = dk.dpop_proof(key, "GET", "https://canopy.example/x")
    assert json.loads(dk.b64url_decode(other.split(".")[1]))["jti"] != claims["jti"]


def test_a_tampered_proof_does_not_verify(keydir):
    key, _ = dk.ensure(allow_software=True, system=lambda: "Linux")
    h, c, s = dk.dpop_proof(key, "GET", "https://x/y").split(".")
    with pytest.raises(InvalidSignature):
        _verify(key.public_jwk, f"{h}.{c}x".encode(), dk.b64url_decode(s))


def test_proof_header_only_for_a_registered_key(keydir):
    assert dk.proof_headers("GET", "https://x/y") == {}           # no key at all
    key, _ = dk.ensure(allow_software=True, system=lambda: "Linux")
    assert dk.proof_headers("GET", "https://x/y") == {}           # made, never registered
    for status, sent in (("pending", True), ("active", True), ("revoked", False)):
        dk.write_status({"jkt": key.jkt, "status": status})
        assert ("DPoP" in dk.proof_headers("GET", "https://x/y")) is sent
    dk.write_status({"jkt": "someone-elses", "status": "active"})
    assert dk.proof_headers("GET", "https://x/y") == {}           # cache names another key


def test_a_broken_helper_never_breaks_a_resolve(helper, monkeypatch):
    helper(available=True)
    key, _ = dk.ensure(system=lambda: "Darwin")
    dk.write_status({"jkt": key.jkt, "status": "active"})
    monkeypatch.setenv(dk.HELPER_ENV, "/nonexistent")             # helper uninstalled
    assert dk.proof_headers("GET", "https://x/y") == {}


# ── enrollment ────────────────────────────────────────────────────────────────

def test_enroll_registers_and_caches_status(keydir):
    calls = []

    def call(method, path, body):
        calls.append((method, path, body))
        return {"id": 7, "jkt": dk.jwk_thumbprint(body["public_jwk"]), "status": "pending",
                "approve_url": "https://canopy.example/approve/7"}

    st = dk.enroll(runner_id="r-1", call=call, allow_software=True)
    method, path, body = calls[0]
    assert (method, path) == ("POST", "/api/runner-device-keys/")
    assert body["runner"] == "r-1" and body["kind"] == "software"
    assert set(body["public_jwk"]) == {"kty", "crv", "x", "y"}     # public members only
    assert st["status"] == "pending" and st["approve_url"].endswith("/7") and st["created"]
    assert dk.read_status()["jkt"] == st["jkt"]
    again = dk.enroll(runner_id="r-1", call=call)                  # every runner start
    assert not again["created"] and again["jkt"] == st["jkt"]


def test_enroll_refuses_a_thumbprint_mismatch(keydir):
    with pytest.raises(dk.DeviceKeyError, match="mismatch"):
        dk.enroll(runner_id="r-1", allow_software=True,
                  call=lambda *a: {"id": 1, "jkt": "not-mine", "status": "pending"})
    assert dk.read_status() == {}


def _refused(*_a):
    raise RuntimeError("canopy-web refused POST /api/runner-device-keys/: -> 401: Authentication required")


def test_a_revoked_token_asks_through_the_stranded_door(keydir):
    """canopy-web#1611: a box whose PAT was revoked before it had a key still gets a
    PENDING key — the token rides in the body, the key proves itself with DPoP."""
    seen = {}

    def transport(method, url, headers, data):
        body = json.loads(data)
        seen.update(url=url, headers=headers, body=body)
        return 201, json.dumps({"id": 9, "jkt": dk.jwk_thumbprint(body["public_jwk"]),
                                "status": "pending", "approve_url": "https://c.test/approve/9"})

    st = dk.enroll(runner_id="r-1", call=_refused, allow_software=True, base_url="https://c.test",
                   owner_token="old-pat", transport=transport)
    assert seen["url"] == "https://c.test/api/runner-device-key-requests/"
    assert "Authorization" not in seen["headers"] and seen["headers"]["DPoP"].count(".") == 2
    assert seen["body"]["owner_token"] == "old-pat" and seen["body"]["runner"] == "r-1"
    assert st["status"] == "pending" and st["approve_url"].endswith("/9")


def test_without_a_token_a_refusal_stands(keydir):
    with pytest.raises(RuntimeError, match="401"):
        dk.enroll(runner_id="r-1", call=_refused, allow_software=True)


def test_enroll_needs_a_runner(keydir):
    with pytest.raises(dk.DeviceKeyError, match="runner"):
        dk.enroll(runner_id="", call=lambda *a: {}, allow_software=True)


def test_refresh_picks_up_an_approval(keydir):
    key, _ = dk.ensure(allow_software=True, system=lambda: "Linux")
    dk.write_status({"id": 7, "jkt": key.jkt, "status": "pending"})
    st = dk.refresh(runner_id="r-1", call=lambda m, p, b: [
        {"id": 7, "jkt": key.jkt, "status": "active"}])
    assert st["status"] == "active" and dk.read_status()["status"] == "active"
    st = dk.refresh(runner_id="r-1", call=lambda m, p, b: [])
    assert st["status"] == "unregistered"


def test_runner_id_comes_from_the_pairing_config(tmp_path):
    cfg = tmp_path / "runner.json"
    cfg.write_text(json.dumps({"runner_id": "abc", "token": "SECRET"}))
    assert dk.runner_id_from_config(cfg) == "abc"
    assert dk.runner_id_from_config(tmp_path / "missing.json") == ""
