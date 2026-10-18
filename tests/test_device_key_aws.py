"""An EC2 cloud runner's aws-iam device proof (canopy-web#1549): the SigV4-signed, unsent
STS GetCallerIdentity in X-Canopy-Aws-Proof, the instance-role credentials from IMDSv2,
enrollment with {runner, kind, role_arn, proof}, and the resolve attaching the proof.

No real AWS anywhere: the signature is pinned to a vector botocore's SigV4Auth produced
for the same inputs (canopy does not depend on botocore; canopy-web's cloud runner carries
the same signer and checks it against botocore live), and IMDS / STS are fake openers.
"""
from __future__ import annotations

import base64
import io
import json

import pytest

from orchestrator import device_key as dk
from orchestrator import device_key_aws as aws

CREDS = aws.Credentials("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
                        "SESSIONTOKEN/abc==")
HTU = "https://canopy.example/api/agents/eva/credentials/resolve"
ROLE = "arn:aws:iam::123456789012:role/canopy-runner-RunnerRole-ABC"


# ── SigV4 ─────────────────────────────────────────────────────────────────────

def test_signature_is_byte_identical_to_botocore():
    # botocore.auth.SigV4Auth(creds, "sts", "us-east-1").add_auth() on the same request
    # (same headers, body, X-Amz-Date 20261016T201243Z) gave exactly this Authorization.
    req = aws.signed_get_caller_identity(CREDS, htu=HTU, iat=1700000000, now=1792181563, jti="")
    h = req["headers"]
    assert h["X-Amz-Date"] == "20261016T201243Z"
    assert h["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20261016/us-east-1/sts/aws4_request, "
        "SignedHeaders=content-type;host;x-amz-date;x-amz-security-token;"
        "x-canopy-proof-htu;x-canopy-proof-iat, "
        "Signature=391ecfc04bb5eccd8b4e5c5e1939896c237c7a794d23335de7722d77befafad9")


def test_proof_header_shape_binds_htu_and_iat():
    raw = aws.proof(CREDS, HTU, now=1792181563.7)
    doc = json.loads(base64.b64decode(raw))                    # standard base64 of JSON
    assert set(doc) == {"method", "url", "headers", "body"}
    assert doc["method"] == "POST" and doc["url"] == "https://sts.amazonaws.com/"
    assert doc["body"] == "Action=GetCallerIdentity&Version=2011-06-15"
    h = doc["headers"]
    assert h["X-Canopy-Proof-Htu"] == HTU and h["X-Canopy-Proof-Iat"] == "1792181563"
    assert h["Host"] == "sts.amazonaws.com" and h["X-Amz-Security-Token"] == CREDS.token
    signed = h["Authorization"].split("SignedHeaders=")[1].split(",")[0].split(";")
    assert {"x-canopy-proof-htu", "x-canopy-proof-iat", "host", "x-amz-date"} <= set(signed)


def test_two_proofs_in_the_same_second_differ():
    # canopy-web keeps each proof's signature single-use; `resolve` and
    # `resolve?refs=` share an htu, so without a nonce two resolves in one second
    # made one proof and the second was refused as a replay (cloud-ec2-2, 2026-10-17).
    a = json.loads(base64.b64decode(aws.proof(CREDS, HTU, now=1792181563.1)))["headers"]
    b = json.loads(base64.b64decode(aws.proof(CREDS, HTU, now=1792181563.9)))["headers"]
    assert a["X-Canopy-Proof-Jti"] != b["X-Canopy-Proof-Jti"]
    assert a["Authorization"] != b["Authorization"]
    assert "x-canopy-proof-jti" in a["Authorization"].split("SignedHeaders=")[1].split(",")[0].split(";")


def test_changing_the_htu_changes_the_signature():
    a = aws.signed_get_caller_identity(CREDS, htu=HTU, now=1792181563)
    b = aws.signed_get_caller_identity(CREDS, htu=HTU.replace("eva", "ace"), now=1792181563)
    assert a["headers"]["Authorization"] != b["headers"]["Authorization"]


def test_no_secret_in_the_repr():
    assert CREDS.secret_key not in repr(CREDS) and CREDS.token not in repr(CREDS)


# ── IMDSv2 + STS ──────────────────────────────────────────────────────────────

class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def imds_opener(calls=None, role="canopy-runner-RunnerRole-ABC"):
    def opener(req, timeout):
        url, method = req.full_url, req.get_method()
        if calls is not None:
            calls.append((method, url, dict(req.header_items())))
        if url.endswith("/latest/api/token"):
            assert method == "PUT"
            return _Resp(b"IMDSTOKEN")
        assert req.get_header("X-aws-ec2-metadata-token") == "IMDSTOKEN"   # v2 only
        if url.endswith("/security-credentials/"):
            return _Resp(role.encode())
        if url.endswith(f"/security-credentials/{role}"):
            return _Resp(json.dumps({"Code": "Success", "AccessKeyId": "ASIA1",
                                     "SecretAccessKey": "s3cr3t", "Token": "tok",
                                     "Expiration": "2099-01-01T00:00:00Z"}).encode())
        raise AssertionError(url)
    return opener


def test_instance_credentials_from_imdsv2_and_cached():
    calls = []
    c = aws.instance_credentials(opener=imds_opener(calls))
    assert (c.access_key, c.secret_key, c.token) == ("ASIA1", "s3cr3t", "tok")
    assert len(calls) == 3
    assert aws.instance_credentials(opener=imds_opener(calls)) is c and len(calls) == 3


def test_not_ec2_is_none_not_an_error():
    assert aws.instance_credentials() is None          # conftest: IMDS is a closed port


def test_caller_role_arn_normalises_the_assumed_role():
    sent = []

    def sts(req, timeout):
        sent.append(req)
        return _Resp(b"<GetCallerIdentityResponse><GetCallerIdentityResult>"
                     b"<Arn>arn:aws:sts::123456789012:assumed-role/canopy-runner-RunnerRole-ABC/"
                     b"i-0abc</Arn></GetCallerIdentityResult></GetCallerIdentityResponse>")

    assert aws.caller_role_arn(CREDS, opener=sts) == ROLE
    assert sent[0].full_url == "https://sts.amazonaws.com/" and sent[0].get_method() == "POST"


@pytest.mark.parametrize("arn,want", [
    ("arn:aws:sts::1:assumed-role/R/i-1", "arn:aws:iam::1:role/R"),
    ("arn:aws-us-gov:sts::1:assumed-role/R/s", "arn:aws-us-gov:iam::1:role/R"),
    ("arn:aws:iam::1:role/R", "arn:aws:iam::1:role/R"),
])
def test_normalize_role_arn(arn, want):
    assert aws.normalize_role_arn(arn) == want


# ── the device key kind ───────────────────────────────────────────────────────

@pytest.fixture
def on_ec2(monkeypatch):
    monkeypatch.setattr(aws, "instance_credentials", lambda **k: CREDS)
    monkeypatch.setattr(aws, "caller_role_arn", lambda creds, **k: ROLE)


def test_an_ec2_box_enrolls_its_role(on_ec2, monkeypatch):
    monkeypatch.setenv("CANOPY_WEB_API_URL", "https://canopy.example")
    calls = []

    def call(method, path, body):
        calls.append((method, path, body))
        return {"id": "k1", "jkt": "f" * 64, "status": "pending",
                "approve_url": "https://canopy.example/approve/k1"}

    st = dk.enroll(runner_id="r-ec2", call=call, system=lambda: "Linux",
                   on_ec2=lambda: True)
    method, path, body = calls[0]
    assert (method, path) == ("POST", "/api/runner-device-keys/")
    assert set(body) == {"runner", "kind", "role_arn", "label", "proof", "ecdh_public_jwk"}
    assert body["kind"] == "aws-iam" and body["role_arn"] == ROLE and body["runner"] == "r-ec2"
    # aws-iam has no key of its own: its ECDH key (canopy-web#1550) is a 0600 software PEM
    assert dk.ecdh_pem_path().stat().st_mode & 0o777 == 0o600
    assert body["ecdh_public_jwk"] == dk.load_ecdh().public_jwk
    assert dk.read_status()["ecdh_kind"] == "software"
    proof = json.loads(base64.b64decode(body["proof"]))
    assert proof["headers"]["X-Canopy-Proof-Htu"] == "https://canopy.example/api/runner-device-keys/"
    assert st["kind"] == "aws-iam" and st["role_arn"] == ROLE and st["jkt"] == "f" * 64
    assert json.loads(dk.aws_path().read_text())["role_arn"] == ROLE
    assert dk.load().kind == "aws-iam"


def test_resolve_carries_the_aws_proof_only_when_registered(on_ec2):
    dk.ensure(kind="aws-iam")
    assert dk.proof_headers("GET", HTU + "?refs=A") == {}            # never registered
    dk.write_status({"jkt": "f" * 64, "status": "active", "kind": "aws-iam", "role_arn": ROLE})
    h = dk.proof_headers("GET", HTU + "?refs=A")
    assert set(h) == {"X-Canopy-Aws-Proof"} and "DPoP" not in h
    doc = json.loads(base64.b64decode(h["X-Canopy-Aws-Proof"]))
    assert doc["headers"]["X-Canopy-Proof-Htu"] == HTU                 # no query
    dk.write_status({"jkt": "f" * 64, "status": "revoked", "kind": "aws-iam"})
    assert dk.proof_headers("GET", HTU) == {}


def test_an_aws_proof_never_breaks_a_resolve_when_imds_goes_away(on_ec2, monkeypatch):
    dk.ensure(kind="aws-iam")
    dk.write_status({"jkt": "f" * 64, "status": "active", "kind": "aws-iam", "role_arn": ROLE})
    monkeypatch.setattr(aws, "instance_credentials", lambda **k: None)
    assert dk.proof_headers("GET", HTU) == {}


def test_linux_off_ec2_still_makes_a_software_key():
    key, _ = dk.ensure(system=lambda: "Linux")
    assert key.kind == "software"


def test_asking_for_aws_iam_off_ec2_says_so():
    with pytest.raises(dk.DeviceKeyError, match="instance role"):
        dk.ensure(kind="aws-iam")


def test_reenroll_removes_the_marker(on_ec2):
    dk.ensure(kind="aws-iam")
    assert any("aws-iam.json" in g for g in dk.remove_local())
    assert dk.load() is None


def test_the_one_resolve_client_sends_the_aws_proof(on_ec2):
    from orchestrator import cred_resolve
    dk.ensure(kind="aws-iam")
    dk.write_status({"jkt": "f" * 64, "status": "active", "kind": "aws-iam", "role_arn": ROLE})
    seen = []

    def server(method, url, headers, data):
        seen.append(dict(headers))
        return 200, json.dumps({"values": {"A": "x"}})

    cred_resolve.resolve("eva", token="pat", base_url="https://canopy.example", refs=["A"],
                         turn_id="t-1", transport=server)
    h = {k.lower(): v for k, v in seen[0].items()}
    doc = json.loads(base64.b64decode(h["x-canopy-aws-proof"]))
    assert doc["headers"]["X-Canopy-Proof-Htu"] == HTU and "dpop" not in h
