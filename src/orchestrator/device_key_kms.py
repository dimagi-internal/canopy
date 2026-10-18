"""An EC2 cloud runner's zero-knowledge ECDH key, held in AWS KMS (canopy-web#1550).

A Mac keeps the key a zero-knowledge vault key is wrapped to in its Secure Enclave and a
Windows box in its TPM; an EC2 box has neither, so until now its ECDH key was a 0600 PEM
on disk. Anyone who could read that file — a disk snapshot, a stray backup, root — could
open every vault key wrapped to the box. Here the private half lives in KMS instead: an
``ECC_NIST_P256`` key with usage ``KEY_AGREEMENT``, created by the runner's CloudFormation
stack with a key policy that lets ONLY that runner's instance role call
``kms:DeriveSharedSecret`` and ``kms:GetPublicKey``. ``DeriveSharedSecret`` with
``KeyAgreementAlgorithm=ECDH`` returns exactly what the other backends return: Z, the
32-byte x-coordinate of ECDH(this key, the wrap's ephemeral point). The private key never
leaves KMS, never reaches the disk, and every use is in CloudTrail.

How the box finds its key: the stack names it ``alias/canopy-runner-ecdh/<role name>``,
and the box learns its role name from the instance metadata service — no configuration
to ship (UserData does not re-run on a live box; see runner.cfn.yaml's DEPLOY GAP). Once
found, its ARN is pinned in ``kms-ecdh.json`` beside the other key markers.

Stdlib only, like ``device_key_aws``: SigV4 comes from there, and credentials are the
instance role's, from IMDSv2, held in memory only.
"""
from __future__ import annotations

import base64
import datetime as _dt
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

from orchestrator import device_key_aws as aws

ALIAS_PREFIX = "alias/canopy-runner-ecdh/"
KEY_SPEC = "ECC_NIST_P256"
KEY_USAGE = "KEY_AGREEMENT"
#: Overrides for a box that is not where its key is (tests; a manual migration).
REGION_ENV = "CANOPY_KMS_REGION"
KEY_ENV = "CANOPY_KMS_ECDH_KEY"
#: KMS's own JSON endpoint; overridable so tests never leave the machine.
ENDPOINT_ENV = "CANOPY_KMS_ENDPOINT"
TIMEOUT = 15

Opener = Callable[..., object]


class KmsError(Exception):
    """KMS refused or could not be reached — never carries a credential."""


class KmsUnavailable(KmsError):
    """There is no usable KMS ECDH key for this box (not EC2, no alias, no permission):
    the caller falls back to the software PEM, exactly as before."""


@dataclass(frozen=True)
class KmsKey:
    key_arn: str
    region: str
    public_der: bytes


def _imds_text(path: str, opener: Opener) -> str:
    token = aws._imds("/latest/api/token", method="PUT", opener=opener,
                      headers={"X-aws-ec2-metadata-token-ttl-seconds": "300"})
    return aws._imds(path, token=token, opener=opener).strip()


def role_name(*, opener: Opener = urllib.request.urlopen) -> str:
    """The instance role's NAME (IMDS lists it under security-credentials/)."""
    try:
        names = _imds_text("/latest/meta-data/iam/security-credentials/", opener).splitlines()
    except Exception:  # noqa: BLE001 — not EC2 / IMDS off: no KMS key to look for
        raise KmsUnavailable("no instance role (not EC2, or IMDS unreachable)") from None
    if not names or not names[0].strip():
        raise KmsUnavailable("the instance has no role")
    return names[0].strip()


def region(*, opener: Opener = urllib.request.urlopen) -> str:
    env = os.environ.get(REGION_ENV, "").strip() or os.environ.get("AWS_REGION", "").strip()
    if env:
        return env
    try:
        return _imds_text("/latest/meta-data/placement/region", opener)
    except Exception:  # noqa: BLE001
        raise KmsUnavailable("the instance's region is unknown (IMDS unreachable)") from None


def _endpoint(reg: str) -> tuple[str, str]:
    """(url, host) of KMS in `reg`."""
    override = os.environ.get(ENDPOINT_ENV, "").strip().rstrip("/")
    if override:
        return override + "/", override.split("://", 1)[-1].split("/", 1)[0]
    host = f"kms.{reg}.amazonaws.com"
    return f"https://{host}/", host


def call(op: str, body: dict, *, reg: str, creds: Optional[aws.Credentials] = None,
         opener: Opener = urllib.request.urlopen, now: Optional[float] = None) -> dict:
    """One KMS JSON-protocol call (``TrentService.<op>``), SigV4-signed with the
    instance role. Raises KmsUnavailable for a missing key / missing permission, KmsError
    otherwise."""
    creds = creds or aws.instance_credentials(opener=opener)
    if creds is None:
        raise KmsUnavailable("the instance role's credentials are not available")
    url, host = _endpoint(reg)
    data = json.dumps(body, separators=(",", ":"))
    t = time.time() if now is None else now
    amz_date = _dt.datetime.fromtimestamp(t, _dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    headers = aws.sigv4_headers(creds, method="POST", url_path="/", host=host, region=reg,
                                service="kms", body=data, amz_date=amz_date,
                                headers={"Content-Type": "application/x-amz-json-1.1",
                                         "Host": host, "X-Amz-Target": f"TrentService.{op}"})
    req = urllib.request.Request(url, data=data.encode("utf-8"), method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with opener(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            kind = str(json.loads(e.read().decode("utf-8") or "{}").get("__type") or "")
        except Exception:  # noqa: BLE001
            kind = ""
        kind = kind.rsplit("#", 1)[-1]
        if kind in ("NotFoundException", "AccessDeniedException", "DisabledException",
                    "KMSInvalidStateException"):
            raise KmsUnavailable(f"KMS {op}: {kind}") from None
        raise KmsError(f"KMS {op} answered {e.code} {kind}".strip()) from None
    except KmsError:
        raise
    except Exception as e:  # noqa: BLE001
        raise KmsError(f"KMS unreachable ({e.__class__.__name__})") from None


def find(*, opener: Opener = urllib.request.urlopen) -> KmsKey:
    """This box's KMS ECDH key — by ``CANOPY_KMS_ECDH_KEY`` if set, else by the alias its
    stack gives it. Checks it really is a P-256 key-agreement key. Raises KmsUnavailable
    when there is none."""
    reg = region(opener=opener)
    key_id = os.environ.get(KEY_ENV, "").strip() or ALIAS_PREFIX + role_name(opener=opener)
    return describe(key_id, reg=reg, opener=opener)


def describe(key_id: str, *, reg: str, opener: Opener = urllib.request.urlopen) -> KmsKey:
    out = call("GetPublicKey", {"KeyId": key_id}, reg=reg, opener=opener)
    if out.get("KeySpec") != KEY_SPEC or out.get("KeyUsage") != KEY_USAGE:
        raise KmsUnavailable(f"{key_id} is {out.get('KeySpec')}/{out.get('KeyUsage')}, not "
                             f"{KEY_SPEC}/{KEY_USAGE}")
    try:
        der = base64.b64decode(str(out["PublicKey"]))
        arn = str(out["KeyId"])
    except (KeyError, ValueError):
        raise KmsError("KMS GetPublicKey answered without a public key") from None
    return KmsKey(arn, reg, der)


def derive(key: KmsKey, peer_der: bytes, *, opener: Opener = urllib.request.urlopen) -> bytes:
    """Z for the peer public key (DER SubjectPublicKeyInfo). The private key stays in KMS."""
    out = call("DeriveSharedSecret", {"KeyId": key.key_arn, "KeyAgreementAlgorithm": "ECDH",
                                      "PublicKey": base64.b64encode(peer_der).decode("ascii")},
               reg=key.region, opener=opener)
    try:
        return base64.b64decode(str(out["SharedSecret"]))
    except (KeyError, ValueError):
        raise KmsError("KMS DeriveSharedSecret answered without a secret") from None


def marker(key: KmsKey) -> dict:
    """What ``kms-ecdh.json`` holds — nothing secret: the key's ARN, region, PUBLIC key."""
    return {"provider": "aws-kms", "key_arn": key.key_arn, "region": key.region,
            "public_der": base64.b64encode(key.public_der).decode("ascii")}


def from_marker(m: dict) -> KmsKey:
    return KmsKey(str(m["key_arn"]), str(m["region"]),
                  base64.b64decode(str(m["public_der"]), validate=True))
