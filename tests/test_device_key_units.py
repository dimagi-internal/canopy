"""What a runner isolation unit needs from the device-key CLI (canopy-web PR 6, spec §2).

* A unit on an EC2 box must make a SOFTWARE signing key: `ensure()` otherwise picks aws-iam
  wherever the instance role answers. `--kind software` and `$CANOPY_DEVICE_KEY_KIND=software`
  both force it.
* `--public-jwk` hands the box's pairing step the key's public half without enrolling, and
  `--registered` records the registration pair-by-role made, so proofs are sent.
* A unit's ECDH key lives behind a root-owned broker on a Unix socket
  (`$CANOPY_ECDH_BROKER`): newline-delimited JSON, `public_key` and `derive`. Driven here to
  the zero-knowledge vectors' answer, as every other backend is.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives.asymmetric import ec

from orchestrator import device_key as dk
from orchestrator import vault_zk as zk
from tests.test_device_key_ecdh import DEVICE, EPK, INFO, VK, Z, I, O


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in (dk.KIND_ENV, dk.ECDH_BROKER_ENV):
        monkeypatch.delenv(k, raising=False)


# ── forcing the software kind on an EC2 box ───────────────────────────────────

def _ec2(monkeypatch):
    made = []
    monkeypatch.setattr(dk, "_create_aws", lambda: made.append("aws") or dk._aws_key("arn:x"))
    return dict(system=lambda: "Linux", on_ec2=lambda: True), made


def test_ec2_without_a_kind_is_aws_iam(monkeypatch):
    kw, made = _ec2(monkeypatch)
    key, _ = dk.ensure(**kw)
    assert key.kind == dk.KIND_AWS_IAM and made == ["aws"]


def test_kind_software_wins_on_ec2(monkeypatch):
    kw, made = _ec2(monkeypatch)
    key, created = dk.ensure(kind="software", **kw)
    assert key.kind == "software" and created and made == []


def test_env_forces_software_on_ec2(monkeypatch):
    kw, made = _ec2(monkeypatch)
    monkeypatch.setenv(dk.KIND_ENV, "software")
    key, _ = dk.ensure(**kw)
    assert key.kind == "software" and made == []


def test_env_kind_is_validated(monkeypatch):
    monkeypatch.setenv(dk.KIND_ENV, "bogus")
    with pytest.raises(dk.DeviceKeyError, match="unknown device key kind"):
        dk.ensure(system=lambda: "Linux", on_ec2=lambda: False)


def test_public_jwk_cli_makes_a_software_key_without_enrolling(monkeypatch):
    from orchestrator.cli import main
    r = CliRunner().invoke(main, ["vault", "device", "--public-jwk", "--kind", "software"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output.strip().splitlines()[-1])
    assert out["kind"] == "software" and out["created"] is True
    assert set(out["jwk"]) == {"kty", "crv", "x", "y"} and out["jkt"] == dk.jwk_thumbprint(out["jwk"])
    assert dk.read_status() == {}                       # nothing registered by this
    again = json.loads(CliRunner().invoke(main, ["vault", "device", "--public-jwk", "--kind",
                                                 "software"]).output.strip().splitlines()[-1])
    assert again["jkt"] == out["jkt"] and again["created"] is False


def test_registered_records_the_pairing_so_proofs_are_sent(monkeypatch):
    from orchestrator.cli import main
    CliRunner().invoke(main, ["vault", "device", "--public-jwk", "--kind", "software"])
    assert dk.proof_headers("POST", "https://c.test/api/runner-sessions/") == {}
    r = CliRunner().invoke(main, ["vault", "device", "--registered", "41", "--runner", "r-7"])
    assert r.exit_code == 0, r.output
    st = dk.read_status()
    assert st["id"] == "41" and st["runner"] == "r-7" and st["status"] == "active"
    assert st["kind"] == "software" and st["jkt"] == dk.load().jkt
    assert "DPoP" in dk.proof_headers("POST", "https://c.test/api/runner-sessions/")


def test_registered_needs_a_runner_and_a_key(monkeypatch):
    from orchestrator.cli import main
    r = CliRunner().invoke(main, ["vault", "device", "--registered", "41"])
    assert r.exit_code != 0 and "--runner" in r.output
    r = CliRunner().invoke(main, ["vault", "device", "--registered", "41", "--runner", "r"])
    assert r.exit_code != 0 and "no device key" in r.output


# ── the ECDH broker ───────────────────────────────────────────────────────────

class FakeBroker:
    """A broker speaking the protocol, holding the vectors' device key."""

    def __init__(self, key=DEVICE, *, refuse=False, secret_encoding="std"):
        self.dir = tempfile.mkdtemp(prefix="ck-", dir="/tmp")   # AF_UNIX paths are short
        self.path = os.path.join(self.dir, "sock")
        self.key, self.refuse, self.enc = key, refuse, secret_encoding
        self.requests = []
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.path)
        self.srv.listen(8)
        self.t = threading.Thread(target=self._serve, daemon=True)
        self.t.start()

    def _jwk(self):
        n = self.key.public_key().public_numbers()
        return dk.public_jwk_from_point(n.x.to_bytes(32, "big"), n.y.to_bytes(32, "big"))

    def _serve(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            with conn:
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                req = json.loads(buf.split(b"\n")[0])
                self.requests.append(req)
                if self.refuse:
                    resp = {"error": "uid 1002 has no unit"}
                elif req.get("op") == "public_key":
                    resp = {"jwk": self._jwk()}
                elif req.get("op") == "derive":
                    j = req["peer_jwk"]
                    point = b"\x04" + dk.b64url_decode(j["x"]) + dk.b64url_decode(j["y"])
                    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
                    z = self.key.exchange(ec.ECDH(), peer)
                    enc = base64.b64encode(z) if self.enc == "std" else \
                        base64.urlsafe_b64encode(z).rstrip(b"=")
                    resp = {"secret_b64": enc.decode()}
                else:
                    resp = {"error": "unknown op"}
                conn.sendall(json.dumps(resp).encode() + b"\n")

    def close(self):
        self.srv.close()
        shutil.rmtree(self.dir, ignore_errors=True)


@pytest.fixture
def broker(monkeypatch):
    b = FakeBroker()
    monkeypatch.setenv(dk.ECDH_BROKER_ENV, b.path)
    yield b
    b.close()


def test_broker_ecdh_key_reproduces_the_vectors(broker):
    key = dk.load_ecdh()
    assert key.kind == dk.KIND_KMS_BROKER
    assert key.public_jwk == I["device_pub_jwk"] and key.jkt == I["device_jkt"]
    assert key.derive(EPK) == Z
    assert zk.unwrap_ecdh_es_with_z(O["device_wrap"], key.derive(EPK), key.point, INFO) == VK
    assert broker.requests[0] == {"op": "public_key"}
    assert broker.requests[1]["op"] == "derive"
    assert set(broker.requests[1]["peer_jwk"]) == {"kty", "crv", "x", "y"}


def test_broker_url_safe_secret_is_accepted(monkeypatch):
    b = FakeBroker(secret_encoding="urlsafe")
    monkeypatch.setenv(dk.ECDH_BROKER_ENV, b.path)
    try:
        assert dk.load_ecdh().derive(EPK) == Z
    finally:
        b.close()


def test_ensure_ecdh_with_a_broker_creates_nothing(broker):
    key, created = dk.ensure_ecdh("software")
    assert key.kind == dk.KIND_KMS_BROKER and created is False
    assert not dk.ecdh_pem_path().exists() and not dk.kms_ecdh_path().exists()


def test_broker_refusal_is_a_device_key_error(monkeypatch):
    b = FakeBroker(refuse=True)
    monkeypatch.setenv(dk.ECDH_BROKER_ENV, b.path)
    try:
        with pytest.raises(dk.DeviceKeyError, match="broker refused"):
            dk.load_ecdh()
    finally:
        b.close()


def test_broker_unreachable_is_a_device_key_error(monkeypatch):
    monkeypatch.setenv(dk.ECDH_BROKER_ENV, "/tmp/no-such-canopy-broker.sock")
    with pytest.raises(dk.DeviceKeyError, match="did not answer"):
        dk.load_ecdh()


def test_ecdh_jwk_cli_prints_the_brokers_public_key(broker):
    from orchestrator.cli import main
    r = CliRunner().invoke(main, ["vault", "device", "--ecdh-jwk", "--kind", "software"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output.strip().splitlines()[-1])
    assert out == {"kind": "kms-broker", "jkt": I["device_jkt"], "jwk": I["device_pub_jwk"],
                   "created": False}


def test_resolve_unwraps_through_the_broker(broker):
    from orchestrator import cred_resolve
    assert cred_resolve.unwrap_device_vk(O["device_wrap"], agent_id=I["agent_id"],
                                         kv=I["kv"]) == VK


def test_registered_records_the_brokers_ecdh_thumbprint(broker):
    from orchestrator.cli import main
    CliRunner().invoke(main, ["vault", "device", "--public-jwk", "--kind", "software"])
    CliRunner().invoke(main, ["vault", "device", "--registered", "9", "--runner", "r-1"])
    st = dk.read_status()
    assert st["ecdh_jkt"] == I["device_jkt"] and st["ecdh_kind"] == "kms-broker"
