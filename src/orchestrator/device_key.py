"""This machine's device key — the thing a Canopy Vault resolve proves it holds (canopy#946).

canopy-web can require that an agent's credentials be read only from a machine it has
APPROVED (`Agent.require_device_binding`, canopy-web#1538). The proof is an RFC 9449 DPoP
JWS in a ``DPoP`` header on ``GET /credentials/resolve``, signed by a P-256 key that lives
on this machine and nowhere else:

* **secure-enclave** — on a Mac with a Secure Enclave, the key is made and used by the
  small Swift helper ``canopy-device-key`` (built by the runner installer into
  ``~/.canopy/bin/``; source in canopy-web ``runner/device-key/``). Only the key's
  ``dataRepresentation`` blob is on disk (``se.key``); it is useless off this Mac's
  enclave. It is created WITHOUT a user-presence requirement, because unattended turns
  must never prompt.
* **tpm** — on Windows with a TPM, a non-exportable P-256 key named ``canopy-device-key`` in
  the CNG "Microsoft Platform Crypto Provider", made and used through ctypes over
  ``ncrypt.dll`` (no native dependency), with no UI policy so it never prompts. Only a
  marker (``tpm.json``) is on disk. See :mod:`orchestrator.device_key_tpm`.
* **aws-iam** — on an EC2 cloud runner, no key at all: the box's instance ROLE is its
  identity (the HashiCorp Vault AWS-auth pattern, canopy-web#1549). A resolve carries an
  ``X-Canopy-Aws-Proof`` header — a SigV4-signed, unsent ``sts:GetCallerIdentity`` that
  canopy-web replays to STS. See :mod:`orchestrator.device_key_aws`. Marker: ``aws-iam.json``.
* **software** — anywhere else (no enclave, no TPM, not EC2): a P-256 key in a 0600 PEM
  (``sw.pem``), registered as ``kind=software`` so canopy-web can tell them apart.

Enrollment: ``POST /api/runner-device-keys/`` registers the public JWK for a runner and
answers ``{id, jkt, status: "pending", approve_url}``; a person approves it in canopy-web
with a passkey step-up, after which its status is ``active``. The answer is cached in
``status.json`` beside the key, so a resolve can decide whether to attach a proof without
a round trip.

**The ECDH key (zero-knowledge vault, canopy-web#1550).** Beside the signing key, each
machine holds a P-256 key-AGREEMENT key, which a ZK agent's vault key is wrapped to
(ECDH-ES, `orchestrator.vault_zk`). It is a separate key because the Secure Enclave's keys
are per-purpose: ``se-ecdh.key`` (the helper's ``ecdh-create`` / ``ecdh-public-jwk`` /
``ecdh-derive``), the TPM key ``canopy-device-ecdh`` (marker ``tpm-ecdh.json``), or for
``software`` and ``aws-iam`` a 0600 PEM (``ecdh.pem``). Whatever the kind, the private half
only ever answers ECDH: it hands back Z for a wrap's ephemeral point, and the library does
the rest. Enrollment sends its public JWK as ``ecdh_public_jwk``; an older helper or server
that knows nothing of it is not an error — the key is simply enrolled without it.

**An ECDH key behind a local broker (runner isolation units, spec §2).** On a box that runs
several isolation units, each unit's ECDH key lives in KMS, but a unit cannot call KMS (the
instance role is root-only). A root-owned broker answers for it on a Unix socket, checking
the caller's uid. ``$CANOPY_ECDH_BROKER=<socket path>`` selects it (kind ``kms-broker``):
the store is the broker, nothing is created or written here. Protocol — one newline-
delimited JSON request per connection, one JSON line back::

    {"op": "public_key"}                         → {"jwk": {"kty","crv","x","y"}}
    {"op": "derive", "peer_jwk": {...}}          → {"secret_b64": "<Z, 32 bytes, base64>"}
    any failure                                  → {"error": "<reason>"}

**Forcing the signing kind.** ``$CANOPY_DEVICE_KEY_KIND`` (e.g. ``software``) is what
``ensure()`` uses when no kind is passed — a unit on an EC2 box must make a software key,
although ``ensure()`` otherwise picks ``aws-iam`` wherever an instance role answers.

Nothing here ever prints or logs a credential; the key itself never leaves its file (the
enclave key never leaves the enclave).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit, urlunsplit

KIND_SE = "secure-enclave"
KIND_SOFTWARE = "software"
KIND_TPM = "tpm"
KIND_AWS_IAM = "aws-iam"
#: An ECDH key held in AWS KMS (an EC2 box's; never a signing kind).
KIND_AWS_KMS = "aws-kms"
#: An ECDH key a local broker holds for this unit (`$CANOPY_ECDH_BROKER`; never signing).
KIND_KMS_BROKER = "kms-broker"
#: The broker's Unix socket; set → the ECDH store IS the broker.
ECDH_BROKER_ENV = "CANOPY_ECDH_BROKER"
#: The signing kind `ensure()` uses when none is passed (a unit forcing `software`).
KIND_ENV = "CANOPY_DEVICE_KEY_KIND"
KINDS =(KIND_SE, KIND_TPM, KIND_AWS_IAM, KIND_SOFTWARE)

#: The helper's location after the runner installer built it; overridable for tests.
HELPER_ENV = "CANOPY_DEVICE_KEY_BIN"
#: The key directory; overridable for tests and for a smoke test in a temp HOME.
DIR_ENV = "CANOPY_DEVICE_KEY_DIR"

#: Statuses for which a resolve attaches a proof. `pending` is included on purpose: the
#: approval happens in a browser, so the local cache lags it; canopy-web honours a proof
#: only for an ACTIVE key and ignores the header otherwise, so sending it costs nothing.
PROOF_STATUSES = ("active", "pending")

REGISTER_PATH = "/api/runner-device-keys/"
#: A runner whose token was revoked asks here instead (canopy-web#1611): anonymous,
#: the owner's token in the body plus a proof of the key; lands pending only.
REQUEST_PATH = "/api/runner-device-key-requests/"

Runner = Callable[..., subprocess.CompletedProcess]


class DeviceKeyError(Exception):
    """No usable device key; the message says what to do."""


# ──────────────────────────────────────────────────────────────────────────────
# paths
# ──────────────────────────────────────────────────────────────────────────────

def key_dir() -> Path:
    override = os.environ.get(DIR_ENV, "").strip()
    return Path(override).expanduser() if override else Path.home() / ".canopy" / "device-key"


def se_path() -> Path:
    return key_dir() / "se.key"


def sw_path() -> Path:
    return key_dir() / "sw.pem"


def status_path() -> Path:
    return key_dir() / "status.json"


def tpm_path() -> Path:
    """Marker for a TPM key: the key itself lives in the platform provider, by name."""
    return key_dir() / "tpm.json"


def aws_path() -> Path:
    """Marker for an aws-iam identity: the instance role's ARN (not a secret)."""
    return key_dir() / "aws-iam.json"


def se_ecdh_path() -> Path:
    """The Secure Enclave ECDH key's dataRepresentation (written by the helper)."""
    return key_dir() / "se-ecdh.key"


def ecdh_pem_path() -> Path:
    """The software / aws-iam ECDH key (0600 PEM)."""
    return key_dir() / "ecdh.pem"


def tpm_ecdh_path() -> Path:
    """Marker for a TPM ECDH key (the key itself lives in the platform provider)."""
    return key_dir() / "tpm-ecdh.json"


def kms_ecdh_path() -> Path:
    """Marker for an EC2 box's KMS ECDH key: its ARN and region (the key lives in KMS)."""
    return key_dir() / "kms-ecdh.json"


def helper_path(which: Callable[[str], Optional[str]] = shutil.which) -> Optional[str]:
    """The Secure Enclave helper: $CANOPY_DEVICE_KEY_BIN, the installer's
    ~/.canopy/bin/canopy-device-key, else one on PATH."""
    override = os.environ.get(HELPER_ENV, "").strip()
    if override:
        return override if os.access(override, os.X_OK) else None
    installed = Path.home() / ".canopy" / "bin" / "canopy-device-key"
    if installed.is_file() and os.access(installed, os.X_OK):
        return str(installed)
    return which("canopy-device-key")


def _ensure_dir() -> Path:
    d = key_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def _write_private(path: Path, data: bytes) -> None:
    _ensure_dir()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ──────────────────────────────────────────────────────────────────────────────
# encoding
# ──────────────────────────────────────────────────────────────────────────────

def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _compact_json(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), sort_keys=False).encode("utf-8")


def jwk_thumbprint(jwk: dict) -> str:
    """RFC 7638 thumbprint of an EC public JWK: SHA-256 over the required members in
    lexicographic order, no whitespace — the `jkt` canopy-web keys device keys by."""
    required = {k: jwk[k] for k in ("crv", "kty", "x", "y")}
    canonical = json.dumps(required, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return b64url(hashlib.sha256(canonical).digest())


def public_jwk_from_point(x: bytes, y: bytes) -> dict:
    return {"kty": "EC", "crv": "P-256", "x": b64url(x), "y": b64url(y)}


# ──────────────────────────────────────────────────────────────────────────────
# the two kinds of key
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class DeviceKey:
    kind: str
    public_jwk: dict
    _sign: Callable[[bytes], bytes]
    #: aws-iam only: the instance role this box proves it is (no JWK, no signing key).
    role_arn: str = ""

    @property
    def jkt(self) -> str:
        if self.kind == KIND_AWS_IAM:
            # canopy-web keys an aws-iam row by the sha256 of its role ARN. It is the
            # server's answer that is cached; this is only for showing an unregistered one.
            return hashlib.sha256(self.role_arn.encode("utf-8")).hexdigest()
        return jwk_thumbprint(self.public_jwk)

    def sign(self, data: bytes) -> bytes:
        """Raw ES256 signature (r‖s, 64 bytes) over `data`."""
        sig = self._sign(data)
        if len(sig) != 64:
            raise DeviceKeyError(f"{self.kind} key returned a {len(sig)}-byte signature, not 64")
        return sig


def _run_helper(helper: str, *args: str, runner: Runner = subprocess.run
                ) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env[DIR_ENV] = str(key_dir())
    return runner([helper, *args], capture_output=True, text=True, timeout=30, env=env)


def _helper_fail(what: str, p: subprocess.CompletedProcess) -> DeviceKeyError:
    err = ((p.stderr or "").strip().splitlines() or [f"exit {p.returncode}"])[-1][:300]
    return DeviceKeyError(f"canopy-device-key {what} failed: {err}")


def helper_info(helper: str, *, runner: Runner = subprocess.run) -> dict:
    p = _run_helper(helper, "info", runner=runner)
    if p.returncode != 0:
        raise _helper_fail("info", p)
    try:
        return json.loads(p.stdout or "{}")
    except ValueError as e:
        raise DeviceKeyError(f"canopy-device-key info printed no JSON ({e})")


def _se_key(helper: str, runner: Runner) -> DeviceKey:
    p = _run_helper(helper, "public-jwk", runner=runner)
    if p.returncode != 0:
        raise _helper_fail("public-jwk", p)
    try:
        jwk = json.loads(p.stdout)
    except ValueError as e:
        raise DeviceKeyError(f"canopy-device-key public-jwk printed no JSON ({e})")

    def sign(data: bytes) -> bytes:
        s = _run_helper(helper, "sign", b64url(data), runner=runner)
        if s.returncode != 0:
            raise _helper_fail("sign", s)
        return b64url_decode((s.stdout or "").strip())

    return DeviceKey(KIND_SE, {k: jwk[k] for k in ("kty", "crv", "x", "y")}, sign)


def _sw_key(pem: bytes) -> DeviceKey:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        raise DeviceKeyError(f"{sw_path()} is not a P-256 private key")
    nums = key.public_key().public_numbers()
    jwk = public_jwk_from_point(nums.x.to_bytes(32, "big"), nums.y.to_bytes(32, "big"))

    def sign(data: bytes) -> bytes:
        r, s = decode_dss_signature(key.sign(data, ec.ECDSA(hashes.SHA256())))
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")

    return DeviceKey(KIND_SOFTWARE, jwk, sign)


def _create_software() -> DeviceKey:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    _write_private(sw_path(), pem)
    return _sw_key(pem)


def _read_marker(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _tpm_key(marker: dict) -> DeviceKey:
    from orchestrator import device_key_tpm as tpm
    key = tpm.open_key(str(marker.get("name") or tpm.KEY_NAME))
    return DeviceKey(KIND_TPM, key.public_jwk(), key.sign)


def _aws_key(role_arn: str) -> DeviceKey:
    def no_sign(_data: bytes) -> bytes:
        raise DeviceKeyError("an aws-iam identity signs no JWS — it sends X-Canopy-Aws-Proof")
    return DeviceKey(KIND_AWS_IAM, {}, no_sign, role_arn=role_arn)


def load(*, runner: Runner = subprocess.run,
         which: Callable[[str], Optional[str]] = shutil.which) -> Optional[DeviceKey]:
    """The existing device key, or None when this machine has none. Never creates."""
    if aws_path().is_file():
        arn = str(_read_marker(aws_path()).get("role_arn") or "")
        if not arn:
            raise DeviceKeyError(f"{aws_path()} names no role ARN — re-enroll with "
                                 "`canopy vault device --reenroll`")
        return _aws_key(arn)
    if tpm_path().is_file():
        try:
            return _tpm_key(_read_marker(tpm_path()))
        except Exception as e:  # noqa: BLE001 — name it as a device-key problem
            raise DeviceKeyError(f"the TPM key named in {tpm_path()} could not be opened ({e})")
    if se_path().is_file():
        helper = helper_path(which)
        if not helper:
            raise DeviceKeyError(
                f"{se_path()} is a Secure Enclave key but canopy-device-key is not installed "
                "— re-run the runner installer (it builds ~/.canopy/bin/canopy-device-key)")
        return _se_key(helper, runner)
    if sw_path().is_file():
        return _sw_key(sw_path().read_bytes())
    return None


def _create_tpm() -> DeviceKey:
    from orchestrator import device_key_tpm as tpm
    key = tpm.create_key(tpm.KEY_NAME)
    _write_private(tpm_path(), json.dumps({"provider": tpm.PROVIDER, "name": tpm.KEY_NAME},
                                          indent=1).encode("utf-8"))
    return DeviceKey(KIND_TPM, key.public_jwk(), key.sign)


def _create_aws() -> DeviceKey:
    from orchestrator import device_key_aws as aws
    creds = aws.instance_credentials()
    if creds is None:
        raise DeviceKeyError("no EC2 instance role here (the instance metadata service did not "
                             "answer with role credentials) — aws-iam is for cloud runners")
    try:
        arn = aws.caller_role_arn(creds)
    except aws.AwsProofError as e:
        raise DeviceKeyError(f"could not learn this box's role ARN from STS ({e})")
    _write_private(aws_path(), json.dumps({"role_arn": arn}, indent=1).encode("utf-8"))
    return _aws_key(arn)


def _tpm_available() -> bool:
    try:
        from orchestrator import device_key_tpm as tpm
        return tpm.available()
    except Exception:  # noqa: BLE001 — no TPM is the common answer, not an error
        return False


def _on_ec2() -> bool:
    try:
        from orchestrator import device_key_aws as aws
        return aws.instance_credentials() is not None
    except Exception:  # noqa: BLE001
        return False


def ensure(*, allow_software: Optional[bool] = None, kind: Optional[str] = None,
           runner: Runner = subprocess.run,
           which: Callable[[str], Optional[str]] = shutil.which,
           system: Callable[[], str] = platform.system,
           tpm_available: Callable[[], bool] = _tpm_available,
           on_ec2: Callable[[], bool] = _on_ec2) -> tuple[DeviceKey, bool]:
    """(the device key, whether it was created now). Idempotent.

    `kind` asks for one kind and fails loudly when this machine cannot make it. Without
    it the strongest kind this machine has is used:

    * a Mac: the Secure Enclave whenever the helper says it is available. A Mac WITHOUT
      the helper is an error, not a silent software key: a software key made there would
      later sit beside the enclave key the installer meant to make;
    * Windows: the TPM (Microsoft Platform Crypto Provider), else software;
    * an EC2 box with an instance role: aws-iam, else software.

    `allow_software` forces the software kind (or, False, forbids it); the default allows
    it only where there is nothing stronger to prefer. An existing key always wins — a
    different kind needs `--reenroll`."""
    if kind is None and allow_software is None:
        kind = os.environ.get(KIND_ENV, "").strip() or None
    if kind is not None and kind not in KINDS:
        raise DeviceKeyError(f"unknown device key kind {kind!r} (one of {', '.join(KINDS)})")
    if allow_software is True and kind is None:
        kind = KIND_SOFTWARE
    existing = load(runner=runner, which=which)
    if existing is not None:
        if kind is not None and existing.kind != kind:
            raise DeviceKeyError(f"this machine already has a {existing.kind} device key — "
                                 f"`canopy vault device --reenroll --kind {kind}` replaces it")
        return existing, False
    os_name = system()
    if kind == KIND_SOFTWARE:
        return _create_software(), True
    if kind == KIND_TPM or (kind is None and os_name == "Windows"):
        if tpm_available():
            return _create_tpm(), True
        if kind == KIND_TPM:
            raise DeviceKeyError("no TPM here: the Microsoft Platform Crypto Provider is not "
                                 "available (no TPM 2.0, or not Windows)")
    if kind == KIND_AWS_IAM or (kind is None and os_name == "Linux"):
        if kind == KIND_AWS_IAM or on_ec2():
            return _create_aws(), True
    if kind == KIND_SE or (kind is None and os_name == "Darwin"):
        helper = helper_path(which) if os_name == "Darwin" else None
        if helper:
            info = helper_info(helper, runner=runner)
            if info.get("secure_enclave_available"):
                _ensure_dir()
                p = _run_helper(helper, "create", runner=runner)
                if p.returncode != 0:
                    raise _helper_fail("create", p)
                return _se_key(helper, runner), True
        if kind == KIND_SE:
            raise DeviceKeyError("no Secure Enclave key could be made here (not a Mac with "
                                 "the canopy-device-key helper and an enclave)")
        if not helper and allow_software is None:
            raise DeviceKeyError(
                "this Mac has no canopy-device-key helper to make a Secure Enclave key — re-run "
                "the runner installer (it builds ~/.canopy/bin/canopy-device-key), or pass "
                "--software to register a software key instead")
    if allow_software is False:
        raise DeviceKeyError("no hardware-bound key could be made here and software keys "
                             "were not allowed")
    return _create_software(), True


# ──────────────────────────────────────────────────────────────────────────────
# the ECDH key (zero-knowledge vault, canopy-web#1550)
# ──────────────────────────────────────────────────────────────────────────────

class EcdhUnsupported(DeviceKeyError):
    """This machine's key backend cannot make an ECDH key (e.g. a helper built before
    canopy-web#1550) — enrollment proceeds without one."""


@dataclass
class EcdhKey:
    kind: str
    public_jwk: dict
    _derive: Callable[[bytes], bytes]

    @property
    def jkt(self) -> str:
        """RFC 7638 thumbprint of the ECDH public JWK — the `ecdh_jkt` a device wrap's
        HKDF info binds."""
        return jwk_thumbprint(self.public_jwk)

    @property
    def point(self) -> bytes:
        return b"\x04" + b64url_decode(self.public_jwk["x"]) + b64url_decode(self.public_jwk["y"])

    def derive(self, peer_point: bytes) -> bytes:
        """Z: the 32-byte big-endian x-coordinate of ECDH(this key, peer)."""
        z = self._derive(peer_point)
        if len(z) != 32:
            raise DeviceKeyError(f"{self.kind} ECDH key returned a {len(z)}-byte secret, not 32")
        return z


def _ecdh_kind_for(signing_kind: str) -> str:
    """The ECDH backend for a signing key's kind: the same hardware where there is
    some; aws-iam has no key of its own, so its ECDH key is a software PEM."""
    return signing_kind if signing_kind in (KIND_SE, KIND_TPM) else KIND_SOFTWARE


def _sw_ecdh_key(pem: bytes) -> EcdhKey:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        raise DeviceKeyError(f"{ecdh_pem_path()} is not a P-256 private key")
    nums = key.public_key().public_numbers()
    jwk = public_jwk_from_point(nums.x.to_bytes(32, "big"), nums.y.to_bytes(32, "big"))

    def derive(peer_point: bytes) -> bytes:
        peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), peer_point)
        return key.exchange(ec.ECDH(), peer)

    return EcdhKey(KIND_SOFTWARE, jwk, derive)


def _se_ecdh_key(helper: str, runner: Runner) -> EcdhKey:
    p = _run_helper(helper, "ecdh-public-jwk", runner=runner)
    if p.returncode == 2:
        raise EcdhUnsupported("canopy-device-key predates ECDH (canopy-web#1550) — re-run the "
                              "runner installer to rebuild it")
    if p.returncode != 0:
        raise _helper_fail("ecdh-public-jwk", p)
    try:
        jwk = json.loads(p.stdout)
    except ValueError as e:
        raise DeviceKeyError(f"canopy-device-key ecdh-public-jwk printed no JSON ({e})")

    def derive(peer_point: bytes) -> bytes:
        d = _run_helper(helper, "ecdh-derive", b64url(peer_point), runner=runner)
        if d.returncode != 0:
            raise _helper_fail("ecdh-derive", d)
        return b64url_decode((d.stdout or "").strip())

    return EcdhKey(KIND_SE, {k: jwk[k] for k in ("kty", "crv", "x", "y")}, derive)


def _tpm_ecdh_key(marker: dict) -> EcdhKey:
    from orchestrator import device_key_tpm as tpm
    key = tpm.open_ecdh_key(str(marker.get("name") or tpm.ECDH_KEY_NAME))
    return EcdhKey(KIND_TPM, key.public_jwk(), key.derive)


def _kms_ecdh_key(kkey, opener=None) -> EcdhKey:
    """An ECDH key whose private half is in AWS KMS (`device_key_kms`)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from orchestrator import device_key_kms as kms

    pub = serialization.load_der_public_key(kkey.public_der)
    if not isinstance(pub, ec.EllipticCurvePublicKey) or pub.curve.name != "secp256r1":
        raise DeviceKeyError(f"KMS key {kkey.key_arn} is not a P-256 key")
    nums = pub.public_numbers()
    jwk = public_jwk_from_point(nums.x.to_bytes(32, "big"), nums.y.to_bytes(32, "big"))
    kw = {"opener": opener} if opener is not None else {}

    def derive(peer_point: bytes) -> bytes:
        peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), peer_point)
        der = peer.public_bytes(serialization.Encoding.DER,
                                serialization.PublicFormat.SubjectPublicKeyInfo)
        try:
            return kms.derive(kkey, der, **kw)
        except kms.KmsError as e:
            raise DeviceKeyError(f"the KMS ECDH key could not derive ({e})") from None

    return EcdhKey(KIND_AWS_KMS, jwk, derive)


def _load_kms_marker(opener=None) -> EcdhKey:
    from orchestrator import device_key_kms as kms

    # The marker carries the PUBLIC key too, so loading costs no KMS call — only a
    # derive does (one per resolve of a zero-knowledge agent).
    try:
        kkey = kms.from_marker(_read_marker(kms_ecdh_path()))
    except (KeyError, ValueError) as e:
        raise DeviceKeyError(f"{kms_ecdh_path()} is not a KMS key marker ({e})") from None
    return _kms_ecdh_key(kkey, opener)


def ecdh_broker_path() -> str:
    """``$CANOPY_ECDH_BROKER`` — the unit's ECDH broker socket — or ""."""
    return os.environ.get(ECDH_BROKER_ENV, "").strip()


def _broker_call(sock_path: str, request: dict, *, timeout: float = 10.0) -> dict:
    """One request to the ECDH broker: a JSON line out, a JSON line back."""
    import socket as _socket
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path)
        s.sendall(json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 1 << 20:
                raise DeviceKeyError("the ECDH broker's answer is too long")
    except OSError as e:
        raise DeviceKeyError(f"the ECDH broker at {sock_path} did not answer "
                             f"({e.strerror or e})") from None
    finally:
        s.close()
    line = buf.split(b"\n", 1)[0].strip()
    try:
        body = json.loads(line)
    except ValueError:
        raise DeviceKeyError(f"the ECDH broker at {sock_path} answered no JSON") from None
    if not isinstance(body, dict):
        raise DeviceKeyError(f"the ECDH broker at {sock_path} answered no JSON object")
    if body.get("error"):
        raise DeviceKeyError(f"the ECDH broker refused: {str(body['error'])[:300]}")
    return body


def _b64_any(text: str) -> bytes:
    """Standard or url-safe base64, padded or not."""
    t = (text or "").strip().replace("-", "+").replace("_", "/")
    return base64.b64decode(t + "=" * (-len(t) % 4), validate=True)


def _broker_ecdh_key(sock_path: str) -> EcdhKey:
    """The unit's ECDH key behind the broker: the public half asked once, every derive a
    call. Nothing is cached on disk."""
    jwk = _broker_call(sock_path, {"op": "public_key"}).get("jwk")
    if not (isinstance(jwk, dict) and jwk.get("kty") == "EC" and jwk.get("crv") == "P-256"
            and jwk.get("x") and jwk.get("y")):
        raise DeviceKeyError(f"the ECDH broker at {sock_path} returned no P-256 public JWK")
    pub = {k: jwk[k] for k in ("kty", "crv", "x", "y")}

    def derive(peer_point: bytes) -> bytes:
        if len(peer_point) != 65 or peer_point[:1] != b"\x04":
            raise DeviceKeyError("not an uncompressed P-256 point")
        peer = public_jwk_from_point(peer_point[1:33], peer_point[33:])
        body = _broker_call(sock_path, {"op": "derive", "peer_jwk": peer})
        try:
            return _b64_any(str(body.get("secret_b64") or ""))
        except (ValueError, TypeError):
            raise DeviceKeyError("the ECDH broker's secret_b64 is not base64") from None

    return EcdhKey(KIND_KMS_BROKER, pub, derive)


def load_ecdh(*, runner: Runner = subprocess.run,
              which: Callable[[str], Optional[str]] = shutil.which,
              kms_opener=None) -> Optional[EcdhKey]:
    """This machine's ECDH key, or None when it has none. Never creates.

    ``$CANOPY_ECDH_BROKER`` set → the broker's key, and nothing on disk is consulted."""
    if ecdh_broker_path():
        return _broker_ecdh_key(ecdh_broker_path())
    if kms_ecdh_path().is_file():
        return _load_kms_marker(kms_opener)
    if tpm_ecdh_path().is_file():
        try:
            return _tpm_ecdh_key(_read_marker(tpm_ecdh_path()))
        except Exception as e:  # noqa: BLE001 — name it as a device-key problem
            raise DeviceKeyError(f"the TPM ECDH key named in {tpm_ecdh_path()} could not be "
                                 f"opened ({e})")
    if se_ecdh_path().is_file():
        helper = helper_path(which)
        if not helper:
            raise DeviceKeyError(
                f"{se_ecdh_path()} is a Secure Enclave key but canopy-device-key is not "
                "installed — re-run the runner installer")
        return _se_ecdh_key(helper, runner)
    if ecdh_pem_path().is_file():
        return _sw_ecdh_key(ecdh_pem_path().read_bytes())
    return None


def _adopt_kms(opener=None) -> Optional[EcdhKey]:
    """Switch this box to its KMS ECDH key if it has one: write the marker, delete any
    software PEM. None (changing nothing) when there is no usable KMS key."""
    from orchestrator import device_key_kms as kms

    try:
        kkey = kms.find(**({"opener": opener} if opener is not None else {}))
        key = _kms_ecdh_key(kkey, opener)
    except (kms.KmsError, DeviceKeyError):
        return None
    _ensure_dir()
    _write_private(kms_ecdh_path(), json.dumps(kms.marker(kkey), indent=1).encode("utf-8"))
    if ecdh_pem_path().exists():
        ecdh_pem_path().unlink()
    return key


def ensure_ecdh(signing_kind: str, *, runner: Runner = subprocess.run,
                which: Callable[[str], Optional[str]] = shutil.which,
                tpm_lib=None, kms_opener=None) -> tuple[EcdhKey, bool]:
    """(this machine's ECDH key, whether it was created now), on the backend that matches
    the signing key's kind. Idempotent. Raises EcdhUnsupported where the backend cannot
    make one (the caller enrolls without it).

    An aws-iam box whose stack gives it a KMS key (`device_key_kms`) uses that, and an
    existing software PEM is MIGRATED to it — the PEM is deleted, and the caller's next
    enrollment posts the new public key, which sends the device key back to `pending`
    until a person approves (and re-grants) it. Without a KMS key, it is the PEM as
    before."""
    if ecdh_broker_path():
        # A unit's ECDH key is the broker's: never a PEM here, never a KMS adoption.
        return _broker_ecdh_key(ecdh_broker_path()), False
    existing = load_ecdh(runner=runner, which=which, kms_opener=kms_opener)
    if signing_kind == KIND_AWS_IAM and (existing is None or existing.kind == KIND_SOFTWARE):
        moved = _adopt_kms(kms_opener)
        if moved is not None:
            return moved, True
    if existing is not None:
        return existing, False
    kind = _ecdh_kind_for(signing_kind)
    if kind == KIND_SE:
        helper = helper_path(which)
        if not helper:
            raise EcdhUnsupported("no canopy-device-key helper for a Secure Enclave ECDH key")
        _ensure_dir()
        p = _run_helper(helper, "ecdh-create", runner=runner)
        if p.returncode == 2:
            raise EcdhUnsupported("canopy-device-key predates ECDH (canopy-web#1550) — re-run "
                                  "the runner installer to rebuild it")
        if p.returncode != 0:
            raise _helper_fail("ecdh-create", p)
        return _se_ecdh_key(helper, runner), True
    if kind == KIND_TPM:
        from orchestrator import device_key_tpm as tpm
        try:
            key = tpm.create_ecdh_key(tpm.ECDH_KEY_NAME, tpm_lib)
            jwk = key.public_jwk()
        except Exception as e:  # noqa: BLE001 — a TPM that will not make one
            raise EcdhUnsupported(f"the TPM would not make an ECDH key ({e})")
        _write_private(tpm_ecdh_path(), json.dumps({"provider": tpm.PROVIDER,
                                                    "name": tpm.ECDH_KEY_NAME},
                                                   indent=1).encode("utf-8"))
        return EcdhKey(KIND_TPM, jwk, key.derive), True
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    _write_private(ecdh_pem_path(), pem)
    return _sw_ecdh_key(pem), True


def remove_local() -> list[str]:
    """Delete the local key files and the status cache; returns what was removed. A TPM
    key is also deleted from its provider (best effort — it is revoked server-side)."""
    gone = []
    if tpm_path().is_file():
        try:
            from orchestrator import device_key_tpm as tpm
            tpm.delete_key(str(_read_marker(tpm_path()).get("name") or tpm.KEY_NAME))
            gone.append("tpm:" + tpm.KEY_NAME)
        except Exception:  # noqa: BLE001 — the marker still goes; a re-create overwrites
            pass
    if tpm_ecdh_path().is_file():
        try:
            from orchestrator import device_key_tpm as tpm
            tpm.delete_ecdh_key(str(_read_marker(tpm_ecdh_path()).get("name")
                                    or tpm.ECDH_KEY_NAME))
            gone.append("tpm:" + tpm.ECDH_KEY_NAME)
        except Exception:  # noqa: BLE001
            pass
    for p in (se_path(), sw_path(), tpm_path(), aws_path(), status_path(),
              se_ecdh_path(), ecdh_pem_path(), tpm_ecdh_path(), kms_ecdh_path()):
        if p.exists():
            p.unlink()
            gone.append(str(p))
    return gone


# ──────────────────────────────────────────────────────────────────────────────
# DPoP proof (RFC 9449)
# ──────────────────────────────────────────────────────────────────────────────

def normalize_htu(url: str) -> str:
    """scheme://host/path — no query, no fragment (the contract's `htu`)."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def dpop_proof(key: DeviceKey, method: str, url: str, *, now: Optional[float] = None,
               jti: Optional[str] = None) -> str:
    header = {"typ": "dpop+jwt", "alg": "ES256", "jwk": key.public_jwk}
    claims = {"htm": method.upper(), "htu": normalize_htu(url),
              "iat": int(time.time() if now is None else now),
              "jti": jti or str(uuid.uuid4())}
    signing_input = f"{b64url(_compact_json(header))}.{b64url(_compact_json(claims))}"
    return f"{signing_input}.{b64url(key.sign(signing_input.encode('ascii')))}"


def proof_headers(method: str, url: str, *, runner: Runner = subprocess.run,
                  which: Callable[[str], Optional[str]] = shutil.which) -> dict:
    """The proof header for a request, when this machine holds a REGISTERED device key:
    ``{"DPoP": jws}`` for a key that signs (secure-enclave, tpm, software), or
    ``{"X-Canopy-Aws-Proof": …}`` for an EC2 box's aws-iam identity. Else ``{}``.

    Never raises: a resolve must behave exactly as before when there is no key, the
    helper is missing, or anything else goes wrong — the server decides whether a proof
    is required, and says so with a 401 the caller can explain."""
    try:
        st = read_status()
        if st.get("status") not in PROOF_STATUSES:
            return {}
        key = load(runner=runner, which=which)
        if key is None:
            return {}
        if key.kind == KIND_AWS_IAM:
            if st.get("kind") != KIND_AWS_IAM or (st.get("role_arn") and
                                                  st["role_arn"] != key.role_arn):
                return {}
            from orchestrator import device_key_aws as aws
            creds = aws.instance_credentials()
            if creds is None:
                return {}
            return {aws.PROOF_HEADER: aws.proof(creds, normalize_htu(url))}
        if st.get("jkt") and st["jkt"] != key.jkt:
            return {}
        return {"DPoP": dpop_proof(key, method, url)}
    except Exception:  # noqa: BLE001 — a proof is an addition, never a new failure
        return {}


# ──────────────────────────────────────────────────────────────────────────────
# enrollment status (cached beside the key — never a secret)
# ──────────────────────────────────────────────────────────────────────────────

def read_status() -> dict:
    try:
        data = json.loads(status_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_status(data: dict) -> None:
    _write_private(status_path(), json.dumps(data, indent=1, sort_keys=True).encode("utf-8"))


def runner_id_from_config(path: Optional[Path] = None) -> str:
    """The runner id this machine paired as (`~/.canopy/runner.json`), or ""."""
    path = path or Path.home() / ".canopy" / "runner.json"
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("runner_id") or "").strip()
    except (OSError, ValueError, AttributeError):
        return ""


def default_label() -> str:
    try:
        host = socket.gethostname().split(".")[0]
    except OSError:
        host = "this machine"
    return f"{host} ({platform.system() or 'unknown'})"


Call = Callable[..., dict]


def enroll(*, runner_id: str, call: Call, label: str = "", allow_software: Optional[bool] = None,
           kind: Optional[str] = None, base_url: Optional[str] = None,
           owner_token: str = "", transport=None,
           runner: Runner = subprocess.run,
           which: Callable[[str], Optional[str]] = shutil.which, **ensure_kw) -> dict:
    """Ensure a key exists and is registered for `runner_id`; returns the cached status
    (`id`, `jkt`, `status`, `approve_url`, `kind`, `runner`, `created`; `role_arn` for
    aws-iam).

    `call(method, path, body)` is canopy-web, already authenticated. Re-posting the same
    key is idempotent on the server, so this is safe on every runner start. An aws-iam
    identity registers ``{runner, kind, role_arn, proof}`` instead of a JWK: canopy-web
    replays the proof to STS and creates the pending key only if the ARN matches.

    ``owner_token``: when canopy-web refuses the token (401 — revoked in a cleanup
    before this box ever had a key), ask through ``REQUEST_PATH`` instead with that same
    token and a proof of the key: canopy-web makes a PENDING key and emails its owner the
    approve link (canopy-web#1611)."""
    if not runner_id:
        raise DeviceKeyError("no runner id — pair this machine as a runner first "
                             "(~/.canopy/runner.json has none), or pass --runner")
    key, created = ensure(allow_software=allow_software, kind=kind, runner=runner,
                          which=which, **ensure_kw)
    label = label or default_label()
    # The ECDH key a zero-knowledge vault key is wrapped to (canopy-web#1550). Best
    # effort: a backend that cannot make one enrolls the signing key exactly as before.
    ecdh: Optional[EcdhKey] = None
    ecdh_note = ""
    try:
        ecdh, _ = ensure_ecdh(key.kind, runner=runner, which=which)
    except DeviceKeyError as e:
        if ecdh_broker_path():
            # A unit whose ECDH key is the broker's must not enroll without it: its vault
            # wraps would then go nowhere, silently.
            raise DeviceKeyError(f"the ECDH broker ($CANOPY_ECDH_BROKER) could not give this "
                                 f"unit its ECDH key, so it is not enrolled: {e}") from None
        ecdh_note = str(e)
    extra = {"ecdh_public_jwk": ecdh.public_jwk} if ecdh is not None else {}
    from orchestrator import canopy_web
    base = canopy_web.resolve_base_url(base_url)
    if key.kind == KIND_AWS_IAM:
        from orchestrator import device_key_aws as aws
        creds = aws.instance_credentials()
        if creds is None:
            raise DeviceKeyError("the instance role's credentials are not available from the "
                                 "instance metadata service — cannot prove the role")

        def payload(path: str) -> dict:
            return {"runner": runner_id, "kind": key.kind, "role_arn": key.role_arn,
                    "label": label, "proof": aws.proof(creds, normalize_htu(base + path)), **extra}
    else:
        def payload(path: str) -> dict:
            return {"runner": runner_id, "public_jwk": key.public_jwk, "kind": key.kind,
                    "label": label, **extra}
    try:
        body = call("POST", REGISTER_PATH, payload(REGISTER_PATH))
    except Exception as e:  # noqa: BLE001 — only a refused token is retried below
        if not (owner_token and "-> 401" in str(e)):
            raise
        body = _request_as_stranded(key, payload(REQUEST_PATH), owner_token, base, transport)
    jkt = str(body.get("jkt") or key.jkt)    # aws-iam: the server's sha256 of the role ARN
    if key.kind != KIND_AWS_IAM:
        if jkt != key.jkt:
            raise DeviceKeyError(f"canopy-web registered thumbprint {jkt}, but this key's is "
                                 f"{key.jkt} — refusing to cache a mismatch")
    if ecdh is not None and body.get("ecdh_jkt") and body["ecdh_jkt"] != ecdh.jkt:
        raise DeviceKeyError(f"canopy-web registered ECDH thumbprint {body['ecdh_jkt']}, but "
                             f"this key's is {ecdh.jkt} — refusing to cache a mismatch")
    status = {"id": body.get("id"), "jkt": jkt, "status": str(body.get("status") or "pending"),
              "approve_url": str(body.get("approve_url") or ""), "kind": key.kind,
              "runner": runner_id, "checked_at": time.time()}
    if key.role_arn:
        status["role_arn"] = key.role_arn
    if ecdh is not None:
        status.update(ecdh_jkt=ecdh.jkt, ecdh_kind=ecdh.kind)
    elif ecdh_note:
        status["ecdh_note"] = ecdh_note[:300]
    write_status(status)
    return {**status, "created": created}


def _request_as_stranded(key: DeviceKey, body: dict, owner_token: str, base: str,
                         transport=None) -> dict:
    """POST ``REQUEST_PATH`` with no Authorization: the owner's (revoked) token rides in
    the body, and a key that signs proves itself with a DPoP proof for this URL."""
    from orchestrator import canopy_web
    url = base + REQUEST_PATH
    headers = {**canopy_web.provenance_headers(), "Content-Type": "application/json"}
    if key.kind != KIND_AWS_IAM:
        headers["DPoP"] = dpop_proof(key, "POST", url)
    status, text = (transport or canopy_web.urllib_transport)(
        "POST", url, headers, json.dumps({**body, "owner_token": owner_token}).encode("utf-8"))
    if status not in (200, 201):
        raise DeviceKeyError(f"canopy-web refused this runner's device key request ({status}): "
                             f"{text[:300]}")
    return json.loads(text)


def refresh(*, runner_id: str, call: Call) -> dict:
    """Re-read this key's status from canopy-web (`GET ?runner=`) into the cache."""
    st = read_status()
    if not st.get("jkt"):
        return st
    rows = call("GET", f"{REGISTER_PATH}?runner={runner_id}", None)
    if isinstance(rows, dict):
        rows = rows.get("items") or rows.get("results") or []
    for row in rows or []:
        if isinstance(row, dict) and row.get("jkt") == st["jkt"]:
            st.update({"id": row.get("id", st.get("id")),
                       "status": str(row.get("status") or st.get("status") or ""),
                       "approve_url": str(row.get("approve_url") or st.get("approve_url") or "")})
            break
    else:
        st["status"] = "unregistered"
    st["checked_at"] = time.time()
    write_status(st)
    return st
