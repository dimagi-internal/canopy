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
* **software** — anywhere else (no enclave, Linux, Windows): a P-256 key in a 0600 PEM
  (``sw.pem``), registered as ``kind=software`` so canopy-web can tell the two apart.

Enrollment: ``POST /api/runner-device-keys/`` registers the public JWK for a runner and
answers ``{id, jkt, status: "pending", approve_url}``; a person approves it in canopy-web
with a passkey step-up, after which its status is ``active``. The answer is cached in
``status.json`` beside the key, so a resolve can decide whether to attach a proof without
a round trip.

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

#: The helper's location after the runner installer built it; overridable for tests.
HELPER_ENV = "CANOPY_DEVICE_KEY_BIN"
#: The key directory; overridable for tests and for a smoke test in a temp HOME.
DIR_ENV = "CANOPY_DEVICE_KEY_DIR"

#: Statuses for which a resolve attaches a proof. `pending` is included on purpose: the
#: approval happens in a browser, so the local cache lags it; canopy-web honours a proof
#: only for an ACTIVE key and ignores the header otherwise, so sending it costs nothing.
PROOF_STATUSES = ("active", "pending")

REGISTER_PATH = "/api/runner-device-keys/"

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

    @property
    def jkt(self) -> str:
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


def load(*, runner: Runner = subprocess.run,
         which: Callable[[str], Optional[str]] = shutil.which) -> Optional[DeviceKey]:
    """The existing device key, or None when this machine has none. Never creates."""
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


def ensure(*, allow_software: Optional[bool] = None, runner: Runner = subprocess.run,
           which: Callable[[str], Optional[str]] = shutil.which,
           system: Callable[[], str] = platform.system) -> tuple[DeviceKey, bool]:
    """(the device key, whether it was created now). Idempotent.

    On a Mac the Secure Enclave is used whenever the helper says it is available. A Mac
    WITHOUT the helper is an error, not a silent software key: a software key made there
    would later sit beside the enclave key the installer meant to make. `allow_software`
    forces the software kind (or, False, forbids it); the default allows it only where
    there is no enclave to prefer."""
    existing = load(runner=runner, which=which)
    if existing is not None:
        return existing, False
    is_mac = system() == "Darwin"
    helper = helper_path(which) if is_mac else None
    if helper and allow_software is not True:
        info = helper_info(helper, runner=runner)
        if info.get("secure_enclave_available"):
            _ensure_dir()
            p = _run_helper(helper, "create", runner=runner)
            if p.returncode != 0:
                raise _helper_fail("create", p)
            return _se_key(helper, runner), True
    if allow_software is False:
        raise DeviceKeyError("no Secure Enclave key could be made here and software keys "
                             "were not allowed")
    if is_mac and not helper and allow_software is None:
        raise DeviceKeyError(
            "this Mac has no canopy-device-key helper to make a Secure Enclave key — re-run "
            "the runner installer (it builds ~/.canopy/bin/canopy-device-key), or pass "
            "--software to register a software key instead")
    return _create_software(), True


def remove_local() -> list[str]:
    """Delete the local key files and the status cache; returns what was removed."""
    gone = []
    for p in (se_path(), sw_path(), status_path()):
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
    """``{"DPoP": proof}`` when this machine holds a REGISTERED device key, else ``{}``.

    Never raises: a resolve must behave exactly as before when there is no key, the
    helper is missing, or anything else goes wrong — the server decides whether a proof
    is required, and says so with a 401 the caller can explain."""
    try:
        st = read_status()
        if st.get("status") not in PROOF_STATUSES:
            return {}
        key = load(runner=runner, which=which)
        if key is None or (st.get("jkt") and st["jkt"] != key.jkt):
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
           runner: Runner = subprocess.run,
           which: Callable[[str], Optional[str]] = shutil.which) -> dict:
    """Ensure a key exists and is registered for `runner_id`; returns the cached status
    (`id`, `jkt`, `status`, `approve_url`, `kind`, `runner`, `created`).

    `call(method, path, body)` is canopy-web, already authenticated. Re-posting the same
    jkt is idempotent on the server, so this is safe on every runner start."""
    if not runner_id:
        raise DeviceKeyError("no runner id — pair this machine as a runner first "
                             "(~/.canopy/runner.json has none), or pass --runner")
    key, created = ensure(allow_software=allow_software, runner=runner, which=which)
    body = call("POST", REGISTER_PATH, {"runner": runner_id, "public_jwk": key.public_jwk,
                                         "kind": key.kind, "label": label or default_label()})
    jkt = str(body.get("jkt") or key.jkt)
    if jkt != key.jkt:
        raise DeviceKeyError(f"canopy-web registered thumbprint {jkt}, but this key's is "
                             f"{key.jkt} — refusing to cache a mismatch")
    status = {"id": body.get("id"), "jkt": jkt, "status": str(body.get("status") or "pending"),
              "approve_url": str(body.get("approve_url") or ""), "kind": key.kind,
              "runner": runner_id, "checked_at": time.time()}
    write_status(status)
    return {**status, "created": created}


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
