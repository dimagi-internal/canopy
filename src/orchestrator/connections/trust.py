"""Who a box will seal a connection to, and whose envelope it will read: the owner's signed
grant statement decides, never canopy-web's list (canopy-web plan Part 0 §0.5, spec §4.3).

canopy-web can leave someone out, so its list can only NARROW whom this box seals to: a key
it never names cannot be sealed to, and one it names counts only if it passes here.

* The statement counts when its signer's key has the thumbprint sealed in the ``refresh``
  envelope (``owner_key``). The signer is identified by that thumbprint, never by a whole
  key and never by a key canopy-web alone supplies (``verified_statement``).
* A box pins ``(owner_jkt, version)`` per connection (:data:`PINS_FILE`), and a pin only
  moves forward. It also stops a wholly forged set: an envelope naming an attacker as
  ``owner_key``, a statement they signed and a device of theirs as the writer agree with
  each other, and only the pin disagrees. So a changed owner refuses, however new the
  statement. canopy-web never changes a row's owner key (R10): a new owner is a new
  connection, with a new id and a new pin.
* Allowed are the owner's member key, the devices in ``my_computers``, and a device with a
  zk-v1 §7 endorsement of its exact ECDH key, for an agent the statement lists, signed by
  the owner's key or a key the statement lists in ``endorsers``.

The statement alone says who may vouch. The person pins the vault keeper uses
(``vault_keeper._trust_person``) play no part here: they trust a person on first sight,
and canopy-web could invent one. An endorser's thumbprint is computed from the key that
verifies the signature, and an endorsement's subject is the agent it was signed for, which
the statement must list.

Everything here fails closed: malformed input admits nobody and never crashes.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from orchestrator import vault_zk as zk
from orchestrator.connections import wire

if TYPE_CHECKING:
    from orchestrator.vault_keeper import KeepResult

#: ``{connection_id: {"owner_jkt", "version"}}``. ``vault_zk_cli.load_pins`` and
#: ``save_pins`` read and write it.
PINS_FILE = Path.home() / ".canopy" / "connection-pins.json"

UNENDORSED = "zk_unendorsed_recipient"


class TrustError(Exception):
    """This box refuses. ``code`` says which refusal: ``bad-owner-key``,
    ``bad-statement``, ``bad-binding``, ``bad-statement-sig``, ``bad-pin``,
    ``statement-rollback``, ``statement-owner-changed``, ``zk_unendorsed_recipient``.
    Never carries a secret."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _clean_jwk(jwk) -> dict | None:
    """The P-256 public key in ``jwk``, exactly ``{kty, crv, x, y}``; None if there is
    none. A fresh dict, so nothing else in ``jwk`` is ever handed on."""
    if not isinstance(jwk, dict):
        return None
    try:
        return wire._public_jwk({k: jwk.get(k) for k in wire._JWK_KEYS})
    except ValueError:
        return None


def _same_key(a, b) -> bool:
    """Do both name one P-256 key, by RFC 7638 thumbprint? False if either does not."""
    a, b = _clean_jwk(a), _clean_jwk(b)
    return a is not None and b is not None and zk.jwk_thumbprint(a) == zk.jwk_thumbprint(b)


def _owner_key(material: dict, owner_jkt: str) -> dict | None:
    """The key canopy-web shows as the owner's, if it is the one the envelope names."""
    owner = material.get("owner")
    jwk = _clean_jwk(owner.get("public_jwk")) if isinstance(owner, dict) else None
    return jwk if jwk is not None and zk.jwk_thumbprint(jwk) == owner_jkt else None


def _owner_id(material: dict) -> str:
    owner = material.get("owner")
    owner_id = str(owner.get("user_id") or "") if isinstance(owner, dict) else ""
    return owner_id if wire._DECIMAL_ID.fullmatch(owner_id) else ""


def _check_pin(pin, *, owner_jkt: str, version: int) -> None:
    if not isinstance(pin, dict) or not isinstance(pin.get("owner_jkt"), str) \
            or type(pin.get("version")) is not int:
        raise TrustError("bad-pin", f"this connection's entry in {PINS_FILE} is damaged — fix or "
                                    "remove it; refusing to guess what this box has seen")
    if pin["owner_jkt"] != owner_jkt:
        raise TrustError("statement-owner-changed",
                         "this connection's owner key is not the one this box pinned, and canopy-web "
                         "never changes it — treat this as forged and look into it")
    if version < pin["version"]:
        raise TrustError("statement-rollback",
                         f"this statement is version {version}; this box has seen version {pin['version']}")


def verified_statement(material: dict, *, owner_jkt: str, pins: dict) -> dict:
    """The statement in ``material``, once it is the owner's (``owner_jkt``, from the
    ``refresh`` plaintext), about this connection and account, and no older than what
    this box has seen. ``pins`` (:data:`PINS_FILE`'s content) learns it only then."""
    owner_key = _owner_key(material, owner_jkt)
    if owner_key is None:
        raise TrustError("bad-owner-key", "the owner key shown is not the one sealed in the envelope")
    statement = material.get("statement")
    try:
        wire.check_statement(statement, connection_id=material.get("id"),
                             account_id=material.get("account_id"))
    except wire.WireError as e:
        raise TrustError(e.code, e.detail) from None
    if not wire.verify_statement(statement, material.get("statement_sig"), owner_key):
        raise TrustError("bad-statement-sig", "the statement's signature does not verify against the owner's key")
    cid = statement["connection"]
    if cid in pins:
        _check_pin(pins[cid], owner_jkt=owner_jkt, version=statement["version"])
    pins[cid] = {"owner_jkt": owner_jkt, "version": statement["version"]}
    return statement


def _vouched_for(jkt: str, material: dict, statement: dict, owner_jkt: str) -> bool:
    """A valid endorsement of exactly this device key, for an agent the statement lists,
    signed by the owner's key or a key the statement names as an endorser."""
    vouchers = {owner_jkt, *statement["endorsers"]}
    for e in material.get("endorsements") or []:
        if not isinstance(e, dict) or (e.get("kind"), e.get("target"), e.get("key_jkt")) != ("device", jkt, jkt):
            continue
        agent = str(e.get("agent_id") or "")
        by = _clean_jwk(e.get("by_public_jwk"))
        if agent not in statement["agents"] or by is None:
            continue
        key = zk.public_key_from_jwk(by)
        if zk.jkt(key) in vouchers and zk.verify_endorsement(e, agent, key):
            return True
    return False


def _device_allowed(jkt: str, material: dict, statement: dict, owner_jkt: str) -> bool:
    return jkt in statement["my_computers"] or _vouched_for(jkt, material, statement, owner_jkt)


def _who(row: dict) -> str:
    target = str(row.get("target") or "?")[:16]
    return f"{row.get('runner_name') or row.get('kind') or 'recipient'} ({target}…)"


def allowed(material: dict, statement: dict, *, owner_jkt: str, res: KeepResult) -> dict[tuple[str, str], dict]:
    """``{(kind, target): public_jwk}``: everyone Part 0.5 lets this box seal to, among
    those canopy-web names. ``statement`` is what :func:`verified_statement` returned.
    Why a listed key was left out goes in ``res.skipped``."""
    out: dict[tuple[str, str], dict] = {}
    owner_id = _owner_id(material)
    owner_key = _owner_key(material, owner_jkt)
    if owner_key is not None and owner_id:
        out[("member", owner_id)] = owner_key

    for row in material.get("recipients") or []:
        if not isinstance(row, dict):
            res.skipped.append("canopy-web sent a malformed recipient")
            continue
        kind, target = row.get("kind"), row.get("target")
        if kind == "member":
            if target != owner_id or owner_key is None or not _same_key(row.get("public_jwk"), owner_key):
                res.skipped.append(f"{_who(row)}: a connection is sealed to no member but its owner")
            continue
        jwk = _clean_jwk(row.get("public_jwk"))
        if kind != "device" or not isinstance(target, str) or jwk is None or zk.jwk_thumbprint(jwk) != target:
            res.skipped.append(f"{_who(row)}: canopy-web sent no key with that thumbprint")
        elif _device_allowed(target, material, statement, owner_jkt):
            out[("device", target)] = jwk
        else:
            res.skipped.append(f"{_who(row)}: neither in the owner's statement nor endorsed for an "
                               "agent it lists by someone it names")
    return out


def refuse_unallowed(rows: list[dict], allowed: dict) -> list[tuple[str, str, dict]]:
    """``(kind, target, public_jwk)`` for each of ``rows``, ready to seal to, each once. Any
    row ``allowed`` does not hold, by that target AND that key, refuses the whole seal
    with ``zk_unendorsed_recipient``. The key handed back is the one that was checked."""
    out: dict[tuple[str, str], tuple[str, str, dict]] = {}
    for row in rows:
        kind, target = (row.get("kind"), row.get("target")) if isinstance(row, dict) else (None, None)
        want = allowed.get((kind, target)) if isinstance(kind, str) and isinstance(target, str) else None
        if want is None or not _same_key(row.get("public_jwk"), want):
            raise TrustError(UNENDORSED, f"{_who(row) if isinstance(row, dict) else 'a recipient'} is not "
                                         "one the owner's statement allows")
        out[(kind, target)] = (kind, target, want)
    return list(out.values())


def writer_allowed(by: dict, material: dict, statement: dict, *, owner_jkt: str) -> bool:
    """May ``by`` (what ``wire.open_envelope`` returned) have written this envelope: the
    owner's member key, or a device the statement allows. Judged from ``by``'s own key
    and the statement, not from today's recipients: a writer pruned since it wrote must
    still be provable, or nobody could open the envelope to re-seal it. Never raises."""
    if not isinstance(by, dict):
        return False
    kind, target = by.get("kind"), by.get("target")
    key = _clean_jwk(by.get("public_jwk"))
    if key is None or not isinstance(target, str):
        return False
    jkt = zk.jwk_thumbprint(key)
    if kind == "member":
        return bool(target) and target == _owner_id(material) and jkt == owner_jkt
    return kind == "device" and jkt == target and _device_allowed(jkt, material, statement, owner_jkt)
