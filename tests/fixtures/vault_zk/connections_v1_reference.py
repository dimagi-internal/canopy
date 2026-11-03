"""Canopy connections zero-knowledge v1 — REFERENCE implementation + test-vector generator.

Normative definition of the bytes a connection's envelopes and grant statement
carry (spec docs/superpowers/specs/2026-10-31-connections-zero-knowledge-design.md;
plan Part 0). Every implementation (canopy-web's checks in apps/connections/wire.py,
the canopy CLI, the browser) must agree with `connections_v1_vectors.json`.
It builds on zk-v1 (zk_v1_reference.py, beside this file): same primitives, same
encodings (b64u = base64url WITHOUT padding; jkt = RFC 7638 thumbprint).

1. BINDING. A connection's binding id is "connection:{uuid}" (Connection.id, lower
   case, hyphenated). It takes the agent-id slot of every zk-v1 derivation.

2. ENVELOPES. Two zk-v1 §6 "zks1" values per connection, both at kv = 0:
     name "refresh" (stored as `sealed`)         plaintext = compact UTF-8 JSON
       {"v":1,"provider","refresh_token","client_id","login_host","instance_url",
        "account_id","owner_key","issued_at","scopes","statement_floor"}
       owner_key = jkt of the owner's member public key. Google: login_host and
       instance_url are "". statement_floor = integer >= 0: 0 from canopy-web's first
       seal, the new statement's version from the owner's browser at every share, and
       kept unchanged by machines on every re-seal and write-back. A reader refuses a
       statement whose version is below it.
     name "access"  (stored as `access_sealed`)  plaintext = compact UTF-8 JSON
       {"v":1,"access_token","expires_at","instance_url","account_id","scope","owner_key",
        "statement_floor"}
       A reader refuses an access whose account_id or owner_key differs from the refresh
       plaintext's. statement_floor = the refresh plaintext's value when the access was
       written. A reader refuses a statement whose version is below the floor in refresh
       OR the floor in access: independent checks against the statement, with no equality
       between the two, so an access left over from before a share stays readable.
   Recipients: kind "device" (target = the device's ECDH jkt) and kind "member"
   (target = the owner's user id; only the owner).
   sealed_digest = sha256_hex(utf8(stored value)), the compare-and-swap token.

3. GRANT STATEMENT, signed by the owner's person key (the same P-256 scalar as
   their vault keyset, as zk-v1 §7 endorsements are):
     st  = {"account_id","agents":[agent pks as decimal strings, sorted as strings,
            unique],"connection":uuid,"endorsers":[jkts of person public keys, sorted,
            unique, at most 64, may be empty],"my_computers":[ECDH jkts, sorted, unique],
            "v":1,"version":int >= 1,
            "canopy_web": OPTIONAL, when present exactly a 43-char b64u RFC 7638
            thumbprint (^[A-Za-z0-9_-]{43}$) of the canopy-web instance's ECDH key
            that may be served this connection's secrets; "", null or any other
            form is refused, and absent means no server holds a share}
           (ASCII; account_id matches ^[\\x21-\\x7e]{1,255}$ with no '"' or '\\')
     msg = "canopy-connections/v1/grant|" + json.dumps(st, sort_keys=True,
           separators=(",", ":"), ensure_ascii=True), UTF-8
     sig = ECDSA-P256-SHA256(msg), IEEE P1363 r||s (64 bytes), b64u
   ECDSA is randomized: the vector pins ONE valid (RFC 6979) signature every
   verifier must accept, not a signature every signer must reproduce.
   Who may vouch: a device outside my_computers is allowed only by a valid zk-v1 §7
   endorsement of its exact ECDH key (kind=device, target=key_jkt=its jkt) for an agent
   in st.agents, signed by a person key whose jkt is the owner's or is in st.endorsers,
   computed from the key that verifies the signature. Nobody is trusted on first sight.

4. WRITER MAC. Beside each envelope, a writer stores
     {"by":{"kind":"device"|"member","target","public_jwk":{"kty","crv","x","y"}},
      "expect": sealed_digest this write replaces, or "",
      "macs":[{"kind","target","mac"}, ... exactly one per recipient]}
   by.target = jkt(by.public_jwk) for a device; the owner's user id for a member.
   For each recipient entry e = {"kind","target","epk","ct"} of the zks1 body:
     entry_digest = sha256_hex(json.dumps(e, sort_keys=True, separators=(",", ":")))
     msg  = "canopy-connections/v1/envelope|{binding}|{name}|{expect}|{entry_digest}"
     Z    = ECDH(writer's static key, recipient's key)        (32-byte x-coordinate;
            the recipient derives the same Z from its own key and by.public_jwk)
     info = "canopy-connections/v1/envelope-mac|{binding}|{name}|{kind}:{target}"
     key  = HKDF-SHA256(ikm=Z, salt=b"" (empty), info, L=32)
     mac  = b64u(HMAC-SHA256(key, utf8(msg)))
   Each MAC covers one entry, so dropping a recipient (zk-v1 keep_only) leaves
   every other MAC valid. A reader recomputes its own MAC in constant time.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import re
import sys
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature


def _load_zk_v1():
    spec = importlib.util.spec_from_file_location("_zk_v1_reference", Path(__file__).with_name("zk_v1_reference.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


zk = _load_zk_v1()
b64u, unb64u, hkdf, jwk, jkt = zk.b64u, zk.unb64u, zk.hkdf, zk.jwk, zk.jkt

STATEMENT_PREFIX = "canopy-connections/v1/grant|"
KV = 0
STATEMENT_VERSION = 3  # the vector statement's version, and so its refresh plaintext's statement_floor


# --- 1. binding / 2. envelopes ---------------------------------------------------------------
def binding_id(connection_uuid: str) -> str:
    return f"connection:{connection_uuid.lower()}"


def compact(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))


def sealed_digest(stored: str) -> str:
    return hashlib.sha256(stored.encode()).hexdigest()


# --- 3. grant statement ----------------------------------------------------------------------
_CANOPY_WEB = re.compile(r"[A-Za-z0-9_-]{43}")


def canopy_web_ok(value) -> bool:
    """The optional statement member `canopy_web`: a 43-char b64u thumbprint, nothing else."""
    return isinstance(value, str) and _CANOPY_WEB.fullmatch(value) is not None


def statement_msg(st: dict) -> bytes:
    return (STATEMENT_PREFIX + json.dumps(st, sort_keys=True, separators=(",", ":"), ensure_ascii=True)).encode()


def sign_statement(st: dict, priv: ec.EllipticCurvePrivateKey) -> str:
    der = priv.sign(statement_msg(st), ec.ECDSA(hashes.SHA256(), deterministic_signing=True))
    r, s = decode_dss_signature(der)
    return b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def verify_statement(st: dict, sig: str, signer_pub: ec.EllipticCurvePublicKey) -> bool:
    raw = unb64u(sig)
    if len(raw) != 64:
        return False
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    try:
        signer_pub.verify(der, statement_msg(st), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return False
    return True


# --- 4. writer MAC ---------------------------------------------------------------------------
def entry_digest(entry: dict) -> str:
    return hashlib.sha256(json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def envelope_msg(binding: str, name: str, expect: str, entry: dict) -> str:
    return f"canopy-connections/v1/envelope|{binding}|{name}|{expect}|{entry_digest(entry)}"


def envelope_mac_info(binding: str, name: str, kind: str, target: str) -> str:
    return f"canopy-connections/v1/envelope-mac|{binding}|{name}|{kind}:{target}"


def _mac(z: bytes, binding: str, name: str, expect: str, entry: dict) -> str:
    key = hkdf(z, b"", envelope_mac_info(binding, name, entry["kind"], entry["target"]))
    return b64u(hmac.new(key, envelope_msg(binding, name, expect, entry).encode(), hashlib.sha256).digest())


def envelope_mac(binding, name, expect, entry, writer_priv, recipient_pub) -> str:
    """The writer's MAC for one recipient entry."""
    return _mac(writer_priv.exchange(ec.ECDH(), recipient_pub), binding, name, expect, entry)


def verify_envelope_mac(binding, name, expect, entry, mac, recipient_priv, writer_pub) -> bool:
    """The recipient's check of its own entry's MAC, against the writer's public key."""
    want = _mac(recipient_priv.exchange(ec.ECDH(), writer_pub), binding, name, expect, entry)
    return hmac.compare_digest(want.encode(), mac.encode())


def sealed_entries(stored: str) -> list[dict]:
    if not stored.startswith("zks1:"):
        raise ValueError("not a zks1 value")
    return json.loads(unb64u(stored[5:]))["r"]


def writer_sig(binding, name, expect, stored, writer_priv, by_kind, by_target, recipient_pubs) -> dict:
    """The full sig stored beside an envelope; `recipient_pubs`: {(kind, target): public key}."""
    macs = [{"kind": e["kind"], "target": e["target"],
             "mac": envelope_mac(binding, name, expect, e, writer_priv, recipient_pubs[(e["kind"], e["target"])])}
            for e in sealed_entries(stored)]
    return {"by": {"kind": by_kind, "target": by_target, "public_jwk": jwk(writer_priv.public_key())},
            "expect": expect, "macs": macs}


# --- vectors ---------------------------------------------------------------------------------
def _priv(scalar_hex: str) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int(scalar_hex, 16), ec.SECP256R1())


def _must_fail(*checks) -> None:
    for check in checks:
        try:
            ok = check()
        except Exception:
            continue
        if ok is False:
            continue
        raise AssertionError("binding not enforced")


def vectors() -> dict:
    connection = "0f8e3c2a-5b1d-4e7f-9a60-3c2b1d0e9f87"
    binding = binding_id(connection)
    owner_user_id = "7"
    account_id = "00D000000000001AAA/005000000000001AAA"
    owner_hex, writer_hex, other_hex = "7a" * 32, "8b" * 32, "9c" * 32
    owner, writer, other = _priv(owner_hex), _priv(writer_hex), _priv(other_hex)
    endorsers = sorted(jkt(_priv(h * 32).public_key()) for h in ("ad", "be"))
    owner_jkt, writer_jkt, other_jkt = jkt(owner.public_key()), jkt(writer.public_key()), jkt(other.public_key())

    # 2. the refresh envelope, sealed to the writing box, a second box and the owner.
    plaintext = compact({
        "v": 1, "provider": "salesforce", "refresh_token": "5Aep861-test-refresh-token",
        "client_id": "3MVG9-test-client-id", "login_host": "login.salesforce.com",
        "instance_url": "https://acme.my.salesforce.com", "account_id": account_id,
        "owner_key": owner_jkt, "issued_at": 1793500000, "scopes": "api refresh_token id",
        "statement_floor": STATEMENT_VERSION})
    access_plaintext = compact({
        "v": 1, "access_token": "00D-test-access-token", "expires_at": 1793501800,
        "instance_url": "https://acme.my.salesforce.com", "account_id": account_id, "scope": "api refresh_token id",
        "owner_key": owner_jkt, "statement_floor": STATEMENT_VERSION})
    recipients = [("device", writer_jkt, writer), ("device", other_jkt, other), ("member", owner_user_id, owner)]
    ephemerals = [_priv("a1" * 32), _priv("b2" * 32), _priv("c3" * 32)]
    nonces = [bytes.fromhex("101112131415161718191a1b"), bytes.fromhex("202122232425262728292a2b"),
              bytes.fromhex("303132333435363738393a3b")]
    sealed = zk.seal_value(plaintext, binding, "refresh", KV, [(k, t, p.public_key()) for k, t, p in recipients],
                           ephemerals, nonces)
    for kind, target, priv in recipients:
        assert zk.open_sealed_value(sealed, binding, "refresh", kind, target, priv) == plaintext
    _must_fail(lambda: zk.open_sealed_value(sealed, binding_id("00000000-0000-4000-8000-000000000000"),
                                            "refresh", "device", writer_jkt, writer),
               lambda: zk.open_sealed_value(sealed, binding, "access", "device", writer_jkt, writer))

    # 4. writer MACs: a box's write-back (replacing an earlier envelope) and the
    # owner's browser at share time (replacing canopy-web's first seal).
    pubs = {(k, t): p.public_key() for k, t, p in recipients}
    device_expect = sealed_digest("zks1:the-envelope-this-write-replaces")
    member_expect = sealed_digest("zks1:canopy-web-first-seal")
    device_sig = writer_sig(binding, "refresh", device_expect, sealed, writer, "device", writer_jkt, pubs)
    member_sig = writer_sig(binding, "refresh", member_expect, sealed, owner, "member", owner_user_id, pubs)
    entries = sealed_entries(sealed)
    for sig, writer_key in ((device_sig, writer), (member_sig, owner)):
        for e, m in zip(entries, sig["macs"]):
            priv = next(p for k, t, p in recipients if (k, t) == (e["kind"], e["target"]))
            assert verify_envelope_mac(binding, "refresh", sig["expect"], e, m["mac"], priv, writer_key.public_key())
            _must_fail(
                lambda: verify_envelope_mac(binding, "refresh", "", e, m["mac"], priv, writer_key.public_key()),
                lambda: verify_envelope_mac(binding, "access", sig["expect"], e, m["mac"], priv,
                                            writer_key.public_key()),
                lambda: verify_envelope_mac(binding, "refresh", sig["expect"], {**e, "target": "x"}, m["mac"],
                                            priv, writer_key.public_key()),
                lambda: verify_envelope_mac(binding, "refresh", sig["expect"], e, m["mac"], priv,
                                            _priv("d4" * 32).public_key()))

    # 3. the grant statement, signed by the owner.
    statement = {"account_id": account_id, "agents": ["12", "40"], "connection": connection,
                 "endorsers": endorsers, "my_computers": [other_jkt], "v": 1, "version": STATEMENT_VERSION}
    statement_sig = sign_statement(statement, owner)
    assert verify_statement(statement, statement_sig, owner.public_key())
    for bad in ({**statement, "agents": ["12"]}, {**statement, "version": 4},
                {**statement, "my_computers": []}, {**statement, "connection": "x"},
                {**statement, "endorsers": []}):
        assert not verify_statement(bad, statement_sig, owner.public_key()), "statement binding not enforced"
    assert not verify_statement(statement, statement_sig, writer.public_key())

    # 3b. the same statement naming a canopy-web instance (optional `canopy_web`). The
    # statement above stays byte-identical; this is a second vector beside it.
    cw_jkt = jkt(_priv("cd" * 32).public_key())
    cw_statement = {**statement, "canopy_web": cw_jkt}
    cw_sig = sign_statement(cw_statement, owner)
    assert verify_statement(cw_statement, cw_sig, owner.public_key())
    assert not verify_statement(cw_statement, statement_sig, owner.public_key()), "canopy_web not signed"
    assert not verify_statement(statement, cw_sig, owner.public_key())
    assert canopy_web_ok(cw_jkt)
    cw_must_fail = {"empty": {**statement, "canopy_web": ""},
                    "malformed": {**statement, "canopy_web": cw_jkt[:-1] + "="},
                    "null": {**statement, "canopy_web": None}}
    assert not any(canopy_web_ok(b["canopy_web"]) for b in cw_must_fail.values())

    return {
        "format": "canopy-connections/v1",
        "statement": {
            "statement": statement,
            "msg": statement_msg(statement).decode(),
            "sig": statement_sig,
            "signer_priv_scalar_hex": owner_hex,
            "signer_jwk": jwk(owner.public_key()),
            "signer_jkt": owner_jkt,
        },
        "envelope": {
            "binding": binding,
            "name": "refresh",
            "kv": KV,
            "plaintext": plaintext,
            "access_plaintext": access_plaintext,
            "ephemeral_scalars_hex": ["a1" * 32, "b2" * 32, "c3" * 32],
            "nonces_hex": [n.hex() for n in nonces],
            "sealed": sealed,
            "sealed_digest": sealed_digest(sealed),
            "recipients": [{"kind": k, "target": t, "priv_scalar_hex": h, "public_jwk": jwk(p.public_key())}
                           for (k, t, p), h in zip(recipients, (writer_hex, other_hex, owner_hex))],
            "entries": [{"kind": e["kind"], "target": e["target"], "entry_digest": entry_digest(e),
                         "device_writer_msg": envelope_msg(binding, "refresh", device_expect, e),
                         "mac_info": envelope_mac_info(binding, "refresh", e["kind"], e["target"])}
                        for e in entries],
            "device_writer": {"priv_scalar_hex": writer_hex, "sig": device_sig},
            "member_writer": {"priv_scalar_hex": owner_hex, "sig": member_sig},
        },
        "statement_canopy_web": {
            "statement": cw_statement,
            "msg": statement_msg(cw_statement).decode(),
            "sig": cw_sig,
            "must_fail": cw_must_fail,
        },
    }


if __name__ == "__main__":
    json.dump(vectors(), sys.stdout, indent=2)
    print()
