"""An EC2 cloud runner's device proof: its instance ROLE (canopy-web#1549, Phase 1b).

An EC2 box has no Secure Enclave and no TPM; the strongest machine identity it has is the
IAM role its instance profile carries. So, as in HashiCorp Vault's AWS auth method, the box
proves it holds that role by SigV4-signing — but never sending — an
``sts:GetCallerIdentity`` request with the role's credentials, and hands canopy-web the
signed request. canopy-web replays it to ``https://sts.amazonaws.com/`` (an endpoint it
pins; it never takes one from the client), reads the caller ARN AWS answers with, and
accepts the proof if that role is an active ``aws-iam`` device key for the runner.

Header (``X-Canopy-Aws-Proof``): standard base64 of compact JSON::

    {"method": "POST", "url": "https://sts.amazonaws.com/",
     "headers": {"Content-Type": …, "Host": "sts.amazonaws.com", "X-Amz-Date": …,
                 "X-Amz-Security-Token": …, "X-Canopy-Proof-Htu": <the URL being called,
                 scheme+host+path, no query>, "X-Canopy-Proof-Iat": <unix seconds>,
                 "Authorization": "AWS4-HMAC-SHA256 Credential=…, SignedHeaders=…;
                 x-canopy-proof-htu;x-canopy-proof-iat, Signature=…"},
     "body": "Action=GetCallerIdentity&Version=2011-06-15"}

``X-Canopy-Proof-Htu`` and ``X-Canopy-Proof-Iat`` are among the SIGNED headers, so a
captured proof cannot be replayed against another endpoint or later: changing either
breaks the signature STS checks. A signed random ``X-Canopy-Proof-Jti`` makes every
proof distinct, so canopy-web's single-use check never mistakes two honest proofs made
in the same second for a replay (``JTI_HEADER``).

SigV4 is implemented here with the stdlib (hmac/hashlib) rather than botocore, because
neither consumer has botocore: the canopy CLI does not depend on it, and canopy-web's
``runner/ec2/cloud_runner.py`` (which carries the same signer) runs on the box's system
python and must stay stdlib-only. ``tests/test_device_key_aws.py`` pins the signature to a
vector botocore's ``SigV4Auth`` produced, so the two are byte-identical.

Credentials come from IMDSv2 (the instance role), are held in memory only, and are never
printed or logged. The proof is a credential-bearing artifact for ~15 minutes (SigV4's
window) — it goes only into the header of the request it was made for.
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

PROOF_HEADER = "X-Canopy-Aws-Proof"
HTU_HEADER = "X-Canopy-Proof-Htu"
IAT_HEADER = "X-Canopy-Proof-Iat"
#: A random nonce, signed with the rest. Without it a proof is a pure function of
#: (credentials, htu, second), and canopy-web keeps each proof's signature as
#: single-use — so two resolves of the same URL in the same second (`resolve` and
#: `resolve?refs=…` share an htu; the query is ignored) made the SAME proof and the
#: second was refused as replayed: a spurious 401 device_proof_required on an
#: active aws-iam key (seen on cloud-ec2-2, 2026-10-17).
JTI_HEADER = "X-Canopy-Proof-Jti"

STS_URL = "https://sts.amazonaws.com/"
STS_HOST = "sts.amazonaws.com"
STS_REGION = "us-east-1"            # the global STS endpoint signs as us-east-1
STS_BODY = "Action=GetCallerIdentity&Version=2011-06-15"
STS_CONTENT_TYPE = "application/x-www-form-urlencoded; charset=utf-8"

#: IMDS base; overridable so tests (and a non-EC2 machine's probe) never wait on it.
IMDS_ENV = "CANOPY_AWS_IMDS_URL"
IMDS_DEFAULT = "http://169.254.169.254"
IMDS_TIMEOUT = 2.0


class AwsProofError(Exception):
    pass


@dataclass(frozen=True)
class Credentials:
    access_key: str
    secret_key: str
    token: str = ""
    expires_at: float = 0.0          # unix seconds; 0 = unknown

    def __repr__(self) -> str:       # never let a secret reach a log line by accident
        return f"Credentials(access_key={self.access_key[:4]}…, expires_at={self.expires_at})"


# ──────────────────────────────────────────────────────────────────────────────
# SigV4 (stdlib) — https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv-create-signed-request.html
# ──────────────────────────────────────────────────────────────────────────────

def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sigv4_headers(creds: Credentials, *, method: str, url_path: str, host: str, region: str,
                  service: str, body: str, headers: dict, amz_date: str) -> dict:
    """`headers` plus X-Amz-Date, X-Amz-Security-Token (with a session token) and
    Authorization, signing EVERY header given (botocore's SigV4Auth does the same for
    these names). `amz_date` is ``YYYYMMDDTHHMMSSZ``."""
    out = dict(headers)
    out["X-Amz-Date"] = amz_date
    if creds.token:
        out["X-Amz-Security-Token"] = creds.token
    canon = {}
    for name, value in out.items():
        canon[name.lower().strip()] = " ".join(str(value).split())
    canon.setdefault("host", host)
    signed = ";".join(sorted(canon))
    canonical_headers = "".join(f"{k}:{canon[k]}\n" for k in sorted(canon))
    payload_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    canonical_request = "\n".join([method.upper(), url_path or "/", "", canonical_headers,
                                   signed, payload_hash])
    scope = f"{amz_date[:8]}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()])
    k = _hmac(("AWS4" + creds.secret_key).encode("utf-8"), amz_date[:8])
    for part in (region, service, "aws4_request"):
        k = _hmac(k, part)
    signature = hmac.new(k, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    out["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={creds.access_key}/{scope}, "
                            f"SignedHeaders={signed}, Signature={signature}")
    return out


def signed_get_caller_identity(creds: Credentials, *, htu: str = "", iat: Optional[int] = None,
                               now: Optional[float] = None, jti: Optional[str] = None) -> dict:
    """The unsent request as ``{method, url, headers, body}``. With `htu`, the canopy
    binding headers (X-Canopy-Proof-Htu, X-Canopy-Proof-Iat, and a fresh
    X-Canopy-Proof-Jti unless `jti` is given; ``""`` omits it) are added and signed."""
    t = time.time() if now is None else now
    headers = {"Content-Type": STS_CONTENT_TYPE, "Host": STS_HOST}
    if htu:
        headers[HTU_HEADER] = htu
        headers[IAT_HEADER] = str(int(t if iat is None else iat))
        nonce = secrets.token_hex(16) if jti is None else jti
        if nonce:
            headers[JTI_HEADER] = nonce
    amz_date = _dt.datetime.fromtimestamp(t, _dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    signed = sigv4_headers(creds, method="POST", url_path="/", host=STS_HOST, region=STS_REGION,
                           service="sts", body=STS_BODY, headers=headers, amz_date=amz_date)
    return {"method": "POST", "url": STS_URL, "headers": signed, "body": STS_BODY}


def proof(creds: Credentials, htu: str, *, now: Optional[float] = None) -> str:
    """The ``X-Canopy-Aws-Proof`` header value for a request to `htu` (scheme+host+path)."""
    req = signed_get_caller_identity(creds, htu=htu, now=now)
    return base64.b64encode(json.dumps(req, separators=(",", ":")).encode("utf-8")).decode("ascii")


# ──────────────────────────────────────────────────────────────────────────────
# the instance role (IMDSv2) and its ARN (STS)
# ──────────────────────────────────────────────────────────────────────────────

Opener = Callable[..., object]

_CACHE: dict = {}
_LOCK = threading.Lock()


def _imds_base() -> str:
    return (os.environ.get(IMDS_ENV, "").strip() or IMDS_DEFAULT).rstrip("/")


def _imds(path: str, *, token: str = "", method: str = "GET", headers: Optional[dict] = None,
          opener: Opener = urllib.request.urlopen) -> str:
    req = urllib.request.Request(_imds_base() + path, method=method, data=b"" if method == "PUT" else None)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if token:
        req.add_header("X-aws-ec2-metadata-token", token)
    with opener(req, timeout=IMDS_TIMEOUT) as resp:
        return resp.read().decode("utf-8")


def _parse_expiry(text: str) -> float:
    try:
        return _dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_dt.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0.0


def instance_credentials(*, opener: Opener = urllib.request.urlopen,
                         now: Optional[float] = None) -> Optional[Credentials]:
    """The instance role's temporary credentials from IMDSv2, or None when this is not
    an EC2 box with a role (any failure — a laptop answers None within IMDS_TIMEOUT).
    Cached in memory until five minutes before they expire."""
    t = time.time() if now is None else now
    with _LOCK:
        hit = _CACHE.get(_imds_base())
        if hit is not None and (not hit.expires_at or hit.expires_at - 300 > t):
            return hit
    try:
        token = _imds("/latest/api/token", method="PUT", opener=opener,
                      headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"})
        role = _imds("/latest/meta-data/iam/security-credentials/", token=token,
                     opener=opener).strip().splitlines()
        if not role or not role[0].strip():
            return None
        doc = json.loads(_imds(f"/latest/meta-data/iam/security-credentials/{role[0].strip()}",
                               token=token, opener=opener))
        if doc.get("Code", "Success") != "Success" or not doc.get("AccessKeyId"):
            return None
        creds = Credentials(str(doc["AccessKeyId"]), str(doc["SecretAccessKey"]),
                            str(doc.get("Token") or ""), _parse_expiry(doc.get("Expiration")))
    except Exception:  # noqa: BLE001 — not EC2, IMDS off, no role: all mean "no identity"
        return None
    with _LOCK:
        _CACHE[_imds_base()] = creds
    return creds


def normalize_role_arn(arn: str) -> str:
    """``arn:aws:sts::123:assumed-role/ROLE/i-0abc`` → ``arn:aws:iam::123:role/ROLE``
    (the same normalisation canopy-web applies to what STS answers). An IAM role ARN is
    returned as-is."""
    m = re.fullmatch(r"arn:([\w-]+):sts::(\d+):assumed-role/([^/]+)/.+", arn.strip())
    if m:
        return f"arn:{m.group(1)}:iam::{m.group(2)}:role/{m.group(3)}"
    return arn.strip()


def caller_role_arn(creds: Credentials, *, opener: Opener = urllib.request.urlopen) -> str:
    """SEND a GetCallerIdentity with the role's credentials and return the normalised
    role ARN. Needs no IAM permission. Used once, at enrollment, to learn what to register."""
    req_doc = signed_get_caller_identity(creds)
    req = urllib.request.Request(req_doc["url"], data=req_doc["body"].encode("utf-8"),
                                 method="POST")
    for k, v in req_doc["headers"].items():
        req.add_header(k, v)
    try:
        with opener(req, timeout=15) as resp:
            text = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise AwsProofError(f"STS answered {e.code}") from None
    except Exception as e:  # noqa: BLE001
        raise AwsProofError(f"STS unreachable ({e.__class__.__name__})") from None
    m = re.search(r"<Arn>([^<]+)</Arn>", text)
    if not m:
        raise AwsProofError("STS answered without an Arn")
    return normalize_role_arn(m.group(1))


def _reset_cache() -> None:
    with _LOCK:
        _CACHE.clear()
