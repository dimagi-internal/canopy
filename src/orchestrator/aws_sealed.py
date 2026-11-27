"""Sealed AWS credentials: what an agent's machine opens itself (canopy-web `apps/agents/aws_sealed.py`).

canopy-web carries each labs role's own AWS key, and the sessions made with it, only as `zks1`
values sealed to the machines that run the agent (or to the people who approve). This is the
machine's half, behind `canopy aws credentials`:

* `kind: session` — a person approved a sealed grant; their browser asked STS for the agent's
  session and sealed it to this machine. Open it with this machine's key; that is the keys.
* `kind: key` — the agent stands for the role; the role's key is sealed to this machine. Open it
  and ask STS here (`assume_role`, Signature V4 with the stdlib), stamped with the agent exactly
  as canopy-web says (`sts`).

`fetch` answers None when canopy-web has no sealed source for the role (or predates them), and
the caller falls back to canopy-web's own `aws-credentials`.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Callable, Optional

import click

STS_HOST = "sts.amazonaws.com"
CONTENT_TYPE = "application/x-www-form-urlencoded; charset=utf-8"
PROCESS_KEYS = ("AccessKeyId", "SecretAccessKey", "SessionToken", "Expiration")


def _enc(value: str) -> str:
    return urllib.parse.quote(str(value), safe="-_.~")


def assume_role_body(sts: dict, seconds: int) -> str:
    params = [("Action", "AssumeRole"), ("Version", "2011-06-15"), ("RoleArn", sts["role_arn"]),
              ("RoleSessionName", sts["role_session_name"]), ("DurationSeconds", str(seconds)),
              ("SourceIdentity", sts["source_identity"])]
    for i, tag in enumerate(sts.get("tags") or [], start=1):
        params += [(f"Tags.member.{i}.Key", tag["Key"]), (f"Tags.member.{i}.Value", tag["Value"])]
    return "&".join(f"{_enc(k)}={_enc(v)}" for k, v in params)


def amz_date(when: dt.datetime) -> str:
    return when.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def signed_headers(key: dict, body: str, when: dt.datetime, region: str = "us-east-1") -> dict:
    """The headers for a POST of `body` to STS, signed with `key` (SigV4, service `sts`)."""
    date = amz_date(when)
    day = date[:8]
    headers = {"content-type": CONTENT_TYPE, "host": STS_HOST, "x-amz-date": date}
    if key.get("SessionToken"):
        headers["x-amz-security-token"] = key["SessionToken"]
    names = sorted(headers)
    canonical = "\n".join(["POST", "/", "", *[f"{n}:{headers[n]}" for n in names], "", ";".join(names),
                           hashlib.sha256(body.encode()).hexdigest()])
    scope = f"{day}/{region}/sts/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    k = hmac.new(f"AWS4{key['SecretAccessKey']}".encode(), day.encode(), hashlib.sha256).digest()
    for part in (region, "sts", "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = (f"AWS4-HMAC-SHA256 Credential={key['AccessKeyId']}/{scope}, "
                                f"SignedHeaders={';'.join(names)}, Signature={signature}")
    return headers


class StsError(click.ClickException):
    def __init__(self, code: str, message: str):
        super().__init__(f"AWS refused: {code}" + (f" — {message}" if message else ""))
        self.code = code


def _find(root: ET.Element, tag: str) -> str:
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == tag:
            return (el.text or "").strip()
    return ""


def parse_assume_role(xml: str) -> dict:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise StsError("BadResponse", f"STS answered with something that is not XML ({exc})") from None
    code = _find(root, "Code")
    if code:
        raise StsError(code, _find(root, "Message"))
    out = {k: _find(root, k) for k in PROCESS_KEYS}
    if not all(out.values()):
        raise StsError("BadResponse", "STS answered without a whole session")
    return {"Version": 1, **out}


def _post(body: str, headers: dict, timeout: float = 20.0) -> str:
    req = urllib.request.Request(f"https://{STS_HOST}/", data=body.encode(), method="POST",
                                 headers={k: v for k, v in headers.items() if k != "host"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — a fixed https host
            return resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.read().decode(errors="replace")


def assume_role(key: dict, sts: dict, *, post: Callable[[str, dict], str] = _post,
                now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc)) -> dict:
    """`AssumeRole` with the role's own key, as canopy-web's `sts` says; an hour when the role
    allows less than asked."""
    def ask(seconds: int) -> dict:
        body = assume_role_body(sts, seconds)
        return parse_assume_role(post(body, signed_headers(key, body, now(), sts.get("region", "us-east-1"))))
    asked = int(sts.get("duration_seconds") or 3600)
    try:
        return ask(asked)
    except StsError as exc:
        if exc.code == "ValidationError" and "DurationSeconds" in exc.message and asked > 3600:
            return ask(3600)
        raise


def _open_on_this_machine(value_zk: str, binding: str, name: str) -> str:
    from orchestrator import cred_resolve

    return cred_resolve.open_sealed_for_device(value_zk, agent_id=binding, name=name)


def fetch(role: str, agent: str, call: Callable, *, opener: Callable[[str, str, str], str] = _open_on_this_machine,
          sts_assume: Callable[[dict, dict], dict] = assume_role) -> Optional[dict]:
    """credential_process JSON from this agent's sealed source for `role`, or None when there is none."""
    from orchestrator import canopy_web

    try:
        doc = call("POST", f"/api/agents/{agent}/aws-credentials/sealed", {"role": role})
    except canopy_web.CanopyError as exc:
        text = str(exc)
        if "no_sealed_source" in text or "-> 404" in text or "-> 405" in text:
            return None   # nothing sealed for this role, or a canopy-web that predates sealed roles
        if "session_pending" in text:
            raise click.ClickException(
                f"{agent}'s grant for {role} is approved, but the approver's browser has not handed over its keys "
                "yet. They do as they approve; ask them to open the request's page ('Make new keys').") from None
        raise click.ClickException(f"canopy-web refused sealed AWS credentials for {role}: {exc}") from None
    if not isinstance(doc, dict) or doc.get("kind") not in ("key", "session"):
        return None
    from orchestrator import cred_resolve

    try:
        opened = json.loads(opener(doc["value_zk"], doc["binding"], doc["name"]))
    except cred_resolve.ZkUnlockError as exc:
        raise click.ClickException(
            f"this machine cannot open the sealed AWS {doc['name'].lower().replace('_', ' ')}: {exc}. It was sealed "
            "to the machines that ran the agent then; ask the approver to make new keys from the request's page.") from None
    if doc["kind"] == "session":
        return {"Version": 1, **{k: opened[k] for k in PROCESS_KEYS}}
    return sts_assume({"AccessKeyId": opened["AccessKeyId"], "SecretAccessKey": opened["SecretAccessKey"]},
                      doc["sts"])
