"""An EC2 box's zero-knowledge ECDH key held in AWS KMS (device_key_kms).

A fake IMDS + KMS stands in for AWS: the fake KMS holds the vector's device key and
answers GetPublicKey / DeriveSharedSecret the way KMS does (DER SubjectPublicKeyInfo in,
the raw ECDH x-coordinate out), checking every request is SigV4-signed for `kms` with the
right X-Amz-Target. The backend must reproduce the vector's Z and unwrap its device wrap
to the vector's VK — the same normative answer every other backend gives.
"""
from __future__ import annotations

import base64
import io
import json
import urllib.error

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from orchestrator import device_key as dk
from orchestrator import device_key_aws as aws
from orchestrator import device_key_kms as kms
from orchestrator import vault_zk as zk

from tests.test_device_key_ecdh import DEVICE, EPK, I, INFO, O, VK, Z, _pem

ROLE = "canopy-cloud-runner-2-RunnerRole-abc"
ARN = "arn:aws:kms:us-east-1:858923557655:key/1111-2222"


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeAws:
    """IMDSv2 + KMS's JSON protocol, for exactly the calls the backend makes."""

    def __init__(self, *, key=DEVICE, alias_exists=True, key_usage="KEY_AGREEMENT"):
        self.key, self.alias_exists, self.key_usage = key, alias_exists, key_usage
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        if url.startswith("http://imds.test"):
            path = url[len("http://imds.test"):]
            if path == "/latest/api/token":
                return _Resp(b"tok")
            if path == "/latest/meta-data/iam/security-credentials/":
                return _Resp(ROLE.encode())
            if path == f"/latest/meta-data/iam/security-credentials/{ROLE}":
                return _Resp(json.dumps({"Code": "Success", "AccessKeyId": "AKIATEST",
                                         "SecretAccessKey": "s", "Token": "t",
                                         "Expiration": "2099-01-01T00:00:00Z"}).encode())
            if path == "/latest/meta-data/placement/region":
                return _Resp(b"us-east-1")
            raise AssertionError(path)
        assert url == "http://kms.test/"
        headers = {k.lower(): v for k, v in req.header_items()}
        assert "/us-east-1/kms/aws4_request" in headers["authorization"]
        op = headers["x-amz-target"].split(".", 1)[1]
        body = json.loads(req.data)
        self.calls.append((op, body))
        if op == "GetPublicKey":
            if body["KeyId"] not in (ARN, kms.ALIAS_PREFIX + ROLE) or (
                    body["KeyId"] != ARN and not self.alias_exists):
                raise urllib.error.HTTPError(url, 400, "", {}, io.BytesIO(
                    b'{"__type":"NotFoundException"}'))
            der = self.key.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
            return _Resp(json.dumps({"KeyId": ARN, "KeySpec": "ECC_NIST_P256",
                                     "KeyUsage": self.key_usage,
                                     "PublicKey": base64.b64encode(der).decode()}).encode())
        if op == "DeriveSharedSecret":
            assert body["KeyId"] == ARN and body["KeyAgreementAlgorithm"] == "ECDH"
            peer = serialization.load_der_public_key(base64.b64decode(body["PublicKey"]))
            z = self.key.exchange(ec.ECDH(), peer)
            return _Resp(json.dumps({"KeyId": ARN,
                                     "SharedSecret": base64.b64encode(z).decode()}).encode())
        raise AssertionError(op)


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv("CANOPY_AWS_IMDS_URL", "http://imds.test")
    monkeypatch.setenv(kms.ENDPOINT_ENV, "http://kms.test")
    aws._reset_cache()
    yield FakeAws()
    aws._reset_cache()


def test_aws_iam_box_with_a_kms_key_uses_it_and_reproduces_the_vector(fake):
    key, created = dk.ensure_ecdh("aws-iam", kms_opener=fake)
    assert created and key.kind == dk.KIND_AWS_KMS
    assert key.public_jwk == I["device_pub_jwk"] and key.jkt == I["device_jkt"]
    assert key.derive(EPK) == Z
    assert zk.unwrap_ecdh_es_with_z(O["device_wrap"], key.derive(EPK), key.point, INFO) == VK
    assert not dk.ecdh_pem_path().exists()
    # The marker is public material only, and found by the alias the stack names.
    m = json.loads(dk.kms_ecdh_path().read_text())
    assert set(m) == {"provider", "key_arn", "region", "public_der"} and m["key_arn"] == ARN
    assert fake.calls[0] == ("GetPublicKey", {"KeyId": kms.ALIAS_PREFIX + ROLE})


def test_loading_costs_no_kms_call_only_deriving_does(fake):
    dk.ensure_ecdh("aws-iam", kms_opener=fake)
    fake.calls.clear()
    again, created = dk.ensure_ecdh("aws-iam", kms_opener=fake)
    loaded = dk.load_ecdh(kms_opener=fake)
    assert not created and again.jkt == loaded.jkt == I["device_jkt"] and fake.calls == []
    assert loaded.derive(EPK) == Z and [c[0] for c in fake.calls] == ["DeriveSharedSecret"]


def test_an_existing_pem_is_migrated_and_deleted(fake):
    old = ec.generate_private_key(ec.SECP256R1())
    dk._write_private(dk.ecdh_pem_path(), _pem(old))
    key, created = dk.ensure_ecdh("aws-iam", kms_opener=fake)
    assert created and key.kind == dk.KIND_AWS_KMS and key.jkt == I["device_jkt"]
    assert not dk.ecdh_pem_path().exists()


def test_no_kms_key_keeps_the_pem_exactly_as_before(fake):
    fake.alias_exists = False
    dk._write_private(dk.ecdh_pem_path(), _pem(DEVICE))
    key, created = dk.ensure_ecdh("aws-iam", kms_opener=fake)
    assert not created and key.kind == "software" and dk.ecdh_pem_path().exists()
    assert not dk.kms_ecdh_path().exists()


def test_a_kms_key_of_the_wrong_usage_is_not_adopted(fake):
    fake.key_usage = "SIGN_VERIFY"
    key, _ = dk.ensure_ecdh("aws-iam", kms_opener=fake)
    assert key.kind == "software"


def test_not_ec2_falls_back_to_a_pem():
    # conftest points IMDS at a refusing port: no role, so no KMS lookup at all.
    key, created = dk.ensure_ecdh("aws-iam")
    assert created and key.kind == "software"


def test_other_kinds_never_look_for_kms(fake):
    key, _ = dk.ensure_ecdh("software", kms_opener=fake)
    assert key.kind == "software" and fake.calls == []


def test_remove_local_drops_the_marker_not_the_kms_key(fake):
    dk.ensure_ecdh("aws-iam", kms_opener=fake)
    gone = dk.remove_local()
    assert str(dk.kms_ecdh_path()) in gone and not dk.kms_ecdh_path().exists()
    assert all(op != "ScheduleKeyDeletion" for op, _ in fake.calls)
