"""`canopy aws credentials` from a sealed source (`orchestrator/aws_sealed.py`).

Pinned, with a real P-256 machine key: a session an approver sealed to this machine opens with its
key and is the keys; a standing agent's role key sealed to this machine opens and signs STS here —
exactly as botocore signs (the same vector the browser's signer is pinned to); a value sealed to
another machine opens nothing; with no sealed source the custodial route is asked; a grant still
waiting for its approver's browser says so.
"""
from __future__ import annotations

import datetime as dt
import json

import click
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from orchestrator import aws_cli, aws_sealed, canopy_web, cred_resolve, device_key, vault_zk

KEY = {"AccessKeyId": "AKIAIOSFODNN7EXAMPLE", "SecretAccessKey": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"}
WHEN = dt.datetime(2026, 11, 27, 15, 48, 32, tzinfo=dt.timezone.utc)
STS = {"role_arn": "arn:aws:iam::858923557655:role/labs-jj-agent-readwrite", "role_session_name": "canopy-ace",
       "source_identity": "canopy-agent-ace", "duration_seconds": 3600, "region": "us-east-1",
       "tags": [{"Key": "agent", "Value": "ace"}, {"Key": "caller", "Value": "user-7"},
                {"Key": "delivery", "Value": "sealed"}]}
OK = ("<AssumeRoleResponse xmlns=\"https://sts.amazonaws.com/doc/2011-06-15/\"><AssumeRoleResult><Credentials>"
      "<AccessKeyId>ASIAMINTED</AccessKeyId><SecretAccessKey>s</SecretAccessKey><SessionToken>t</SessionToken>"
      "<Expiration>2099-01-01T00:00:00Z</Expiration></Credentials></AssumeRoleResult></AssumeRoleResponse>")


def _machine():
    priv = ec.generate_private_key(ec.SECP256R1())
    pem = priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())
    return device_key._sw_ecdh_key(pem)


def _opener(ecdh):
    return lambda value, binding, name: cred_resolve.open_sealed_for_device(value, agent_id=binding, name=name,
                                                                            ecdh=ecdh)


def _sealed(ecdh, plaintext: dict, binding: str, name: str) -> str:
    return vault_zk.seal_value(binding, name, 0, json.dumps(plaintext), [("device", ecdh.jkt, ecdh.public_jwk)])


def test_signs_sts_exactly_as_botocore_does():
    body = aws_sealed.assume_role_body(STS, 3600)
    assert body.startswith("Action=AssumeRole&Version=2011-06-15&RoleArn=arn%3Aaws%3Aiam%3A%3A858923557655%3Arole%2F")
    h = aws_sealed.signed_headers(KEY, body, WHEN)
    assert h["authorization"].endswith(
        "SignedHeaders=content-type;host;x-amz-date, "
        "Signature=b985c3aa5e11f886b88661a9d5e2c71c01981df3f658fcffa960bfe65229c055")
    h2 = aws_sealed.signed_headers({**KEY, "SessionToken": "FwoGZXIvYXdzEXAMPLETOKEN"}, body, WHEN)
    assert h2["authorization"].endswith(
        "Signature=4514a3a9b6d616027ad6a4b96e4e498aedede3833a1505e77ab97db1310c0dc5")


def test_a_session_sealed_to_this_machine_is_the_keys():
    ecdh = _machine()
    session = {"Version": 1, "AccessKeyId": "ASIASESSION", "SecretAccessKey": "s", "SessionToken": "t",
               "Expiration": "2099-01-01T00:00:00Z"}
    doc = {"kind": "session", "binding": "aws-grant:5", "name": "AWS_SESSION",
           "value_zk": _sealed(ecdh, session, "aws-grant:5", "AWS_SESSION")}
    out = aws_sealed.fetch("labs-readwrite", "ace", lambda *a: doc, opener=_opener(ecdh))
    assert out == session


def test_a_standing_key_sealed_to_this_machine_asks_sts_here_as_the_agent():
    ecdh = _machine()
    doc = {"kind": "key", "binding": "aws-role:labs-read@hal", "name": "AWS_ACCESS_KEY", "sts": STS,
           "value_zk": _sealed(ecdh, KEY, "aws-role:labs-read@hal", "AWS_ACCESS_KEY")}
    posted = []

    def post(body, headers):
        posted.append((body, headers))
        return OK

    out = aws_sealed.fetch("labs-read", "hal", lambda *a: doc, opener=_opener(ecdh),
                           sts_assume=lambda key, sts: aws_sealed.assume_role(key, sts, post=post, now=lambda: WHEN))
    assert out == {"Version": 1, "AccessKeyId": "ASIAMINTED", "SecretAccessKey": "s", "SessionToken": "t",
                   "Expiration": "2099-01-01T00:00:00Z"}
    body, headers = posted[0]
    assert "SourceIdentity=canopy-agent-ace" in body
    assert headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20261127/")


def test_a_value_sealed_to_another_machine_opens_nothing():
    mine, theirs = _machine(), _machine()
    doc = {"kind": "session", "binding": "aws-grant:5", "name": "AWS_SESSION",
           "value_zk": _sealed(theirs, {"AccessKeyId": "x"}, "aws-grant:5", "AWS_SESSION")}
    with pytest.raises(click.ClickException, match="cannot open"):
        aws_sealed.fetch("labs-readwrite", "ace", lambda *a: doc, opener=_opener(mine))


def test_no_sealed_source_falls_back_to_canopy_web(tmp_path):
    asked = []

    def call(method, path, body):
        asked.append(path)
        if path.endswith("/sealed"):
            raise canopy_web.CanopyError(f'{method} {path} -> 404: {{"error": "no_sealed_source"}}')
        return {"Version": 1, "AccessKeyId": "ASIACUSTODIAL", "SecretAccessKey": "s", "SessionToken": "t",
                "Expiration": "2099-01-01T00:00:00Z"}

    out = aws_cli.aws_credentials("labs-readwrite", "ace", cache_dir=tmp_path, call=call)
    assert out["AccessKeyId"] == "ASIACUSTODIAL"
    assert asked == ["/api/agents/ace/aws-credentials/sealed", "/api/agents/ace/aws-credentials"]


def test_a_grant_waiting_for_its_approver_says_so():
    def call(method, path, body):
        raise canopy_web.CanopyError(f'{method} {path} -> 409: {{"error": "session_pending", "grant": 5}}')

    with pytest.raises(click.ClickException, match="approver's browser"):
        aws_sealed.fetch("labs-readwrite", "ace", call)


def test_sts_refusals_and_a_shorter_role_maximum():
    with pytest.raises(aws_sealed.StsError, match="AccessDenied"):
        aws_sealed.parse_assume_role("<ErrorResponse><Error><Code>AccessDenied</Code><Message>no</Message>"
                                     "</Error></ErrorResponse>")
    asked = []

    def post(body, headers):
        asked.append(body)
        if "DurationSeconds=7200" in body:
            return ("<ErrorResponse><Error><Code>ValidationError</Code><Message>The requested DurationSeconds "
                    "exceeds the MaxSessionDuration set for this role.</Message></Error></ErrorResponse>")
        return OK

    out = aws_sealed.assume_role(KEY, {**STS, "duration_seconds": 7200}, post=post, now=lambda: WHEN)
    assert out["AccessKeyId"] == "ASIAMINTED" and len(asked) == 2


def test_request_asks_sealed_first_and_falls_back_when_the_role_has_no_sealed_key():
    sent = []

    def call(method, path, body):
        sent.append(body.get("delivery", "custodial"))
        if body.get("delivery") == "sealed":
            raise canopy_web.CanopyError(f"{method} {path} -> 422: labs-readwrite has no sealed key yet")
        return {"id": 1, "status": "pending"}

    aws_cli.request_role("labs-readwrite", "why", 60, "ace", call=call)
    assert sent == ["sealed", "custodial"]
