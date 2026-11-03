"""A connection's wire format, client side: grant statements and writer-authenticated
envelopes (canopy-web plan "Connections hold nothing canopy-web can use", Part 0).

The NORMATIVE definition is canopy-web ``docs/vault/zk-v1/connections_v1_reference.py``
and its vectors; ``tests/fixtures/vault_zk/`` holds verbatim copies and
``tests/test_connections_wire.py`` reproduces them byte for byte. Do not change a format
here — change the reference first, then the vectors, then every implementation.

* binding — ``connection:<uuid>`` (lower case, hyphenated) in the agent-id slot of every
  zk-v1 derivation.
* envelopes — zk-v1 §6 ``zks1`` values at kv 0, named ``refresh`` (stored ``sealed``) and
  ``access`` (stored ``access_sealed``); recipients ``device`` (ECDH jkt) and ``member``
  (the owner's user id).
* grant statement — the owner's ECDSA P-256 signature (P1363, b64u) over
  ``"canopy-connections/v1/grant|" + json.dumps(st, sort_keys=True, separators=(",", ":"))``.
* writer sig — ``{"by", "expect", "macs"}`` beside each envelope: one HMAC per recipient
  entry, keyed by HKDF over the ECDH secret of the writer's static key and the
  recipient's. A MAC rather than a signature because an aws-iam box has no signing key;
  each MAC covers one entry, so dropping a recipient leaves the others' valid.

The checks mirror canopy-web's ``apps/connections/wire.py`` so the CLI and the server
refuse the same things; every identifier is matched with ``fullmatch`` (no trailing
newline). A reader fails closed: anything malformed is a :class:`WireError`, never a
crash, and a MAC is compared in constant time.

Who a writer may be and who an envelope may be sealed to is the caller's to judge
against the statement (``open_envelope`` returns the writer for that).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re

from orchestrator import device_key
from orchestrator import vault_zk as zk

REFRESH_NAME = "refresh"
ACCESS_NAME = "access"
NAMES = (REFRESH_NAME, ACCESS_NAME)
KV = 0
STATEMENT_PREFIX = "canopy-connections/v1/grant|"
#: As many as one sealed value may carry (canopy-web ``zk_seal.MAX_RECIPIENTS``); a
#: statement's lists are bounded the same way.
MAX_LISTED = 64

#: Exactly the fields of each sealed plaintext (contract §0.2).
REFRESH_KEYS = frozenset({"v", "provider", "refresh_token", "client_id", "login_host", "instance_url",
                          "account_id", "owner_key", "issued_at", "scopes", "statement_floor"})
ACCESS_KEYS = frozenset({"v", "access_token", "expires_at", "instance_url", "account_id", "scope", "owner_key"})

_STATEMENT_KEYS = frozenset({"account_id", "agents", "connection", "endorsers", "my_computers", "v", "version"})
_SIG_KEYS = frozenset({"by", "expect", "macs"})
_BY_KEYS = frozenset({"kind", "target", "public_jwk"})
_JWK_KEYS = frozenset({"kty", "crv", "x", "y"})
_MAC_KEYS = frozenset({"kind", "target", "mac"})
_ENTRY_KEYS = frozenset({"kind", "target", "epk", "ct"})
_SEALED_KEYS = frozenset({"kv", "r"})

#: Printable ASCII without '"' or '\', so the signed message has one spelling everywhere.
_ACCOUNT_ID = re.compile(r'[\x21\x23-\x5b\x5d-\x7e]{1,255}')
#: An agent pk or a user id: a decimal string without leading zeros.
_DECIMAL_ID = re.compile(r"[1-9][0-9]*")
#: An RFC 7638 thumbprint, and a P-256 coordinate: b64u of 32 bytes.
_B64U_32 = re.compile(r"[A-Za-z0-9_-]{43}")
_B64U = re.compile(r"[A-Za-z0-9_-]*")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_CANONICAL_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_BINDING = re.compile(r"connection:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_MAC_BYTES = 32


class WireError(Exception):
    """A statement, envelope or writer sig refused. ``code`` says which refusal:
    ``unsigned``, ``bad-mac``, ``bad-sig``, ``bad-statement``, ``bad-envelope``,
    ``not-for-me``, ``cannot-open``, ``bad-plaintext``, ``bad-recipient``,
    ``bad-binding``, ``bad-name``, ``bad-expect``, ``bad-writer``, ``device-key``.
    Never carries a secret."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# ──────────────────────────────────────────────────────────────────────────────
# names, bindings, digests
# ──────────────────────────────────────────────────────────────────────────────

def binding_id(connection_id: str) -> str:
    """``connection:<uuid>``, the uuid lower-cased; refuses anything but a hyphenated uuid."""
    if not isinstance(connection_id, str) or not _UUID.fullmatch(connection_id):
        raise WireError("bad-binding", "a connection id is a hyphenated uuid")
    return f"connection:{connection_id.lower()}"


def digest(value: str) -> str:
    """What a compare-and-swap ``expect`` names: hex SHA-256 of the stored text, "" for
    none (canopy-web ``vault_engine.value_digest``)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else ""


def _check_name(name) -> None:
    if name not in NAMES:
        raise WireError("bad-name", f"an envelope is named {' or '.join(NAMES)}")


def _check_binding(binding) -> None:
    if not isinstance(binding, str) or not _BINDING.fullmatch(binding):
        raise WireError("bad-binding", "a binding is connection:<lower-case uuid>")


def _b64u_strict(value, length: int) -> bytes:
    """Unpadded base64url of exactly ``length`` bytes; ValueError otherwise."""
    if not isinstance(value, str) or not _B64U.fullmatch(value) or len(value) % 4 == 1:
        raise ValueError("not unpadded base64url")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        raise ValueError("not unpadded base64url") from None
    if len(raw) != length:
        raise ValueError(f"not {length} bytes")
    return raw


def _public_jwk(jwk) -> dict:
    """``jwk`` if it is exactly a P-256 public JWK ``{kty, crv, x, y}`` naming a point on
    the curve; ValueError otherwise."""
    if not isinstance(jwk, dict) or set(jwk) != _JWK_KEYS or (jwk["kty"], jwk["crv"]) != ("EC", "P-256"):
        raise ValueError("not exactly a P-256 public JWK {kty, crv, x, y}")
    if not all(isinstance(jwk[c], str) and _B64U_32.fullmatch(jwk[c]) for c in ("x", "y")):
        raise ValueError("a P-256 coordinate is 32 bytes of unpadded base64url")
    try:
        zk.public_key_from_jwk(jwk)
    except zk.ZkError as e:
        raise ValueError(str(e)) from None
    return jwk


# ──────────────────────────────────────────────────────────────────────────────
# grant statements (§0.3)
# ──────────────────────────────────────────────────────────────────────────────

def statement_msg(st: dict) -> bytes:
    """The bytes the owner signs."""
    return (STATEMENT_PREFIX + json.dumps(st, sort_keys=True, separators=(",", ":"),
                                          ensure_ascii=True)).encode("utf-8")


def _bad_statement(detail: str) -> WireError:
    return WireError("bad-statement", detail)


def _sorted_unique(items, pattern: re.Pattern, what: str) -> None:
    if not isinstance(items, list) or len(items) > MAX_LISTED:
        raise _bad_statement(f"{what} must be a list of at most {MAX_LISTED}")
    if not all(isinstance(i, str) and pattern.fullmatch(i) for i in items):
        raise _bad_statement(f"{what} has an entry of the wrong form")
    if items != sorted(set(items)):
        raise _bad_statement(f"{what} must be sorted with no repeats")


def _statement_shape(st) -> dict:
    if not isinstance(st, dict) or set(st) != _STATEMENT_KEYS:
        raise _bad_statement(f"must be an object with exactly {sorted(_STATEMENT_KEYS)}")
    if type(st["v"]) is not int or st["v"] != 1:
        raise _bad_statement("v must be 1")
    if type(st["version"]) is not int or st["version"] < 1:
        raise _bad_statement("version must be a whole number of at least 1")
    if not isinstance(st["account_id"], str) or not _ACCOUNT_ID.fullmatch(st["account_id"]):
        raise _bad_statement("account_id must be 1-255 printable ASCII characters without "
                             "quotes or backslashes")
    if not isinstance(st["connection"], str) or not _CANONICAL_UUID.fullmatch(st["connection"]):
        raise _bad_statement("connection must be a lower-case hyphenated uuid")
    _sorted_unique(st["agents"], _DECIMAL_ID, "agents")
    _sorted_unique(st["my_computers"], _B64U_32, "my_computers")
    _sorted_unique(st["endorsers"], _B64U_32, "endorsers")
    return st


def check_statement(st: dict, *, connection_id: str, account_id: str) -> dict:
    """``st`` if it is a well-formed statement about THIS connection and account; raises
    ``WireError("bad-statement")`` otherwise. Its signature, version and agents are the
    caller's to judge."""
    _statement_shape(st)
    if st["connection"] != binding_id(connection_id).split(":", 1)[1]:
        raise _bad_statement("it names another connection")
    if st["account_id"] != account_id:
        raise _bad_statement("it names another account")
    return st


def verify_statement(st: dict, sig: str, signer_jwk: dict) -> bool:
    """Is ``st`` a well-formed statement and ``sig`` the signer's ECDSA P-256 signature
    (P1363 r‖s, b64u) over it? Never raises."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    try:
        _statement_shape(st)
        raw = _b64u_strict(sig, 64)
        if not isinstance(signer_jwk, dict):
            return False
        pub = zk.public_key_from_jwk(signer_jwk)
        der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        pub.verify(der, statement_msg(st), ec.ECDSA(hashes.SHA256()))
    except (WireError, InvalidSignature, zk.ZkError, ValueError, TypeError, KeyError):
        return False
    return True


# ──────────────────────────────────────────────────────────────────────────────
# writer MACs (§0.4)
# ──────────────────────────────────────────────────────────────────────────────

def entry_digest(entry: dict) -> str:
    return hashlib.sha256(json.dumps(entry, sort_keys=True, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()


def envelope_msg(binding: str, name: str, expect: str, entry: dict) -> str:
    return f"canopy-connections/v1/envelope|{binding}|{name}|{expect}|{entry_digest(entry)}"


def envelope_mac_info(binding: str, name: str, kind: str, target: str) -> str:
    return f"canopy-connections/v1/envelope-mac|{binding}|{name}|{kind}:{target}"


def envelope_mac(own: device_key.EcdhKey, peer_jwk: dict, binding: str, name: str,
                 expect: str, entry: dict) -> str:
    """The MAC over one recipient entry, keyed by ECDH(own static key, peer's): the
    writer computes it against the recipient's key, the recipient against ``by``'s."""
    z = own.derive(zk.point_from_jwk(peer_jwk))
    key = zk.hkdf(z, b"", envelope_mac_info(binding, name, entry["kind"], entry["target"]))
    return zk.b64u(hmac.new(key, envelope_msg(binding, name, expect, entry).encode("utf-8"),
                            hashlib.sha256).digest())


def _check_expect(expect) -> None:
    if not isinstance(expect, str) or not (expect == "" or _DIGEST.fullmatch(expect)):
        raise WireError("bad-expect", "expect is a sealed digest (64 lower-case hex) or empty")


def _sealed_entries(value) -> list[dict]:
    """The recipient entries of a well-formed ``zks1`` value at kv 0, each recipient
    once; ``WireError("bad-envelope")`` otherwise."""
    bad = WireError("bad-envelope", "not a zks1 value at kv 0 with well-formed recipients")
    if not isinstance(value, str) or not value.startswith(zk.SEALED_PREFIX):
        raise bad
    payload = value[len(zk.SEALED_PREFIX):]
    if not _B64U.fullmatch(payload) or len(payload) % 4 == 1:
        raise bad
    try:
        body = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise bad from None
    if not isinstance(body, dict) or set(body) != _SEALED_KEYS or type(body["kv"]) is not int \
            or body["kv"] != KV or not isinstance(body["r"], list) \
            or not 1 <= len(body["r"]) <= MAX_LISTED:
        raise bad
    seen = set()
    for e in body["r"]:
        if not isinstance(e, dict) or set(e) != _ENTRY_KEYS \
                or not isinstance(e["kind"], str) or not isinstance(e["target"], str):
            raise bad
        seen.add((e["kind"], e["target"]))
    if len(seen) != len(body["r"]):
        raise bad
    return body["r"]


def _bad_sig(detail: str) -> WireError:
    return WireError("bad-sig", detail)


def _check_by(by) -> dict:
    if not isinstance(by, dict) or set(by) != _BY_KEYS:
        raise _bad_sig("by must be an object with exactly ['kind', 'public_jwk', 'target']")
    try:
        _public_jwk(by["public_jwk"])
    except ValueError as e:
        raise _bad_sig(f"by.public_jwk: {e}") from None
    if by["kind"] == "device":
        if not isinstance(by["target"], str) or by["target"] != zk.jwk_thumbprint(by["public_jwk"]):
            raise _bad_sig("a device writer's target must be the thumbprint of its key")
    elif by["kind"] == "member":
        if not isinstance(by["target"], str) or not _DECIMAL_ID.fullmatch(by["target"]):
            raise _bad_sig("a member writer's target must be a user id")
    else:
        raise _bad_sig("by.kind must be device or member")
    return by


def _check_sig(sig, targets: set) -> dict:
    """``sig``'s MACs by recipient, if its structure fits an envelope sealed to
    ``targets``: a writer named by its key, one well-formed MAC per recipient, no more."""
    if not isinstance(sig, dict) or set(sig) != _SIG_KEYS:
        raise _bad_sig("must be an object with exactly ['by', 'expect', 'macs']")
    _check_by(sig["by"])
    try:
        _check_expect(sig["expect"])
    except WireError:
        raise _bad_sig("expect must be a sealed digest (64 lower-case hex) or empty") from None
    if not isinstance(sig["macs"], list):
        raise _bad_sig("macs must be a list")
    macs = {}
    for i, m in enumerate(sig["macs"]):
        if not isinstance(m, dict) or set(m) != _MAC_KEYS \
                or not isinstance(m["kind"], str) or not isinstance(m["target"], str):
            raise _bad_sig(f"macs[{i}] must be an object with exactly string kind, target, mac")
        try:
            _b64u_strict(m["mac"], _MAC_BYTES)
        except ValueError:
            raise _bad_sig(f"macs[{i}].mac must be {_MAC_BYTES} bytes of unpadded base64url") from None
        macs[(m["kind"], m["target"])] = m["mac"]
    if len(macs) != len(sig["macs"]) or set(macs) != targets:
        raise _bad_sig("macs must cover exactly the envelope's recipients, once each")
    return macs


def seal_envelope(binding: str, name: str, plaintext: dict,
                  recipients: list[tuple[str, str, dict]], *, writer: device_key.EcdhKey,
                  writer_target: str, expect: str,
                  ephemerals: list | None = None,
                  nonces: list | None = None) -> tuple[str, dict]:
    """``(value, sig)``: ``plaintext`` sealed to ``recipients`` (``(kind, target,
    public_jwk)`` each) and this box's writer sig over every entry. ``expect`` is the
    digest of the envelope this write replaces, or "". Whether a recipient is allowed is
    the caller's to judge; this refuses only what no reader would accept.

    ``ephemerals``/``nonces`` exist ONLY so the tests can reproduce the vectors."""
    _check_binding(binding)
    _check_name(name)
    _check_expect(expect)
    if not isinstance(writer_target, str) or writer_target != writer.jkt:
        raise WireError("bad-writer", "a device writer's target is the thumbprint of its ECDH key")
    if not isinstance(plaintext, dict):
        raise WireError("bad-plaintext", "an envelope's plaintext is a JSON object")
    if not recipients or len(recipients) > MAX_LISTED:
        raise WireError("bad-recipient", f"an envelope has 1-{MAX_LISTED} recipients")
    by_target: dict[tuple[str, str], dict] = {}
    for r in recipients:
        try:
            kind, target, jwk = r
            _public_jwk({k: jwk.get(k) for k in _JWK_KEYS} if isinstance(jwk, dict) else jwk)
        except (ValueError, TypeError):
            raise WireError("bad-recipient", "a recipient is (kind, target, P-256 public JWK)") from None
        if kind == "device":
            ok = isinstance(target, str) and target == zk.jwk_thumbprint(jwk)
        elif kind == "member":
            ok = isinstance(target, str) and bool(_DECIMAL_ID.fullmatch(target))
        else:
            ok = False
        if not ok or (kind, target) in by_target:
            raise WireError("bad-recipient", "a device is named by its key's thumbprint, a "
                                             "member by a user id, each once")
        by_target[(kind, target)] = jwk
    text = json.dumps(plaintext, separators=(",", ":"))
    try:
        value = zk.seal_value(binding, name, KV, text, [(k, t, j) for (k, t), j in by_target.items()],
                              ephemerals=ephemerals, nonces=nonces)
        macs = [{"kind": e["kind"], "target": e["target"],
                 "mac": envelope_mac(writer, by_target[(e["kind"], e["target"])], binding, name,
                                     expect, e)}
                for e in zk.parse_sealed(value, name)["r"]]
    except zk.ZkError as e:
        raise WireError("bad-recipient", str(e)) from None
    except device_key.DeviceKeyError as e:
        raise WireError("device-key", str(e)) from None
    by = {"kind": "device", "target": writer_target,
          "public_jwk": {k: writer.public_jwk[k] for k in ("kty", "crv", "x", "y")}}
    return value, {"by": by, "expect": expect, "macs": macs}


def open_envelope(value: str, sig: dict | None, *, binding: str, name: str, me_kind: str,
                  me_target: str, ecdh: device_key.EcdhKey) -> tuple[dict, dict]:
    """``(plaintext, by)``: this recipient's entry of ``value``, once the writer's MAC over
    it checks against ``by``'s key. The caller judges whether ``by`` may write (a device
    the statement allows, or the owner's member key).

    Refuses an unsigned envelope (``unsigned``: only the owner's browser accepts
    canopy-web's first seal), a MAC that does not check (``bad-mac``), and anything
    malformed — always as a :class:`WireError`."""
    _check_binding(binding)
    _check_name(name)
    if sig is None:
        raise WireError("unsigned", "a machine accepts only an envelope a writer authenticated")
    entries = _sealed_entries(value)
    macs = _check_sig(sig, {(e["kind"], e["target"]) for e in entries})
    by = sig["by"]
    if me_kind == "device" and me_target != ecdh.jkt:
        raise WireError("not-for-me", "a device opens the entry named by its own ECDH key")
    mine = (me_kind, me_target)
    entry = next((e for e in entries if (e["kind"], e["target"]) == mine), None)
    if entry is None:
        raise WireError("not-for-me", f"sealed for {len(entries)} recipient(s), not this one")
    try:
        want = envelope_mac(ecdh, by["public_jwk"], binding, name, sig["expect"], entry)
    except (device_key.DeviceKeyError, zk.ZkError, ValueError) as e:
        raise WireError("device-key", str(e)) from None
    if not hmac.compare_digest(want.encode("ascii"), macs[mine].encode("ascii")):
        raise WireError("bad-mac", "the writer's MAC over this entry does not check")
    try:
        z = ecdh.derive(zk.sealed_epk_point(entry))
        text = zk.open_sealed_with_z(value, binding, name, me_kind, me_target, z)
    except (zk.ZkError, device_key.DeviceKeyError, ValueError) as e:
        raise WireError("cannot-open", str(e)) from None
    try:
        plain = json.loads(text)
    except ValueError:
        raise WireError("bad-plaintext", "the plaintext is not JSON") from None
    if not isinstance(plain, dict) or type(plain.get("v")) is not int or plain["v"] != 1:
        raise WireError("bad-plaintext", "the plaintext is not a v1 object")
    return plain, {"kind": by["kind"], "target": by["target"], "public_jwk": dict(by["public_jwk"])}
