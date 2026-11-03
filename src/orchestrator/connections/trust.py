"""Who a box will seal a connection to, and whose envelope it will read: the owner's signed
grant statement decides, never canopy-web's list (canopy-web plan Part 0 §0.5, spec §4.3).

canopy-web can leave someone out, so its list can only NARROW what this module allows: a
key it never names cannot be sealed to, and one it names counts only if it passes here.

* The statement counts when its signer's key has the thumbprint sealed in the ``refresh``
  envelope (``owner_key``). The signer is identified by that thumbprint, never by a whole
  key and never by a key canopy-web alone supplies (``verified_statement``).
* A box pins ``(owner_jkt, version)`` per connection (:data:`PINS_FILE`), and a pin only
  moves forward. It also stops a wholly forged set: an envelope naming an attacker as
  ``owner_key``, a statement they signed and a device of theirs as the writer agree with
  each other, and only the pin disagrees. So a changed owner refuses, however new the
  statement, until a person removes that connection from the pin file.
* Allowed are the owner's member key, the devices in ``my_computers``, and a device with a
  zk-v1 §7 endorsement of its exact ECDH key, for an agent the STATEMENT lists, by a person
  this box trusts (``vault_keeper._endorsed``). The endorsement's subject is checked
  against the statement: an endorsement that names no agent of its own admits nothing,
  rather than taking one from canopy-web's payload (``allowed``).

Two pin stores meet here, and their keys cannot collide. ``verified_statement`` reads and
writes connection pins, keyed by connection uuid (:data:`PINS_FILE`). ``allowed`` reads and
writes person pins, keyed by user id (``vault_zk_cli.pins_path``), as the keeper does.
``vault_zk_cli.load_pins`` and ``save_pins`` read and write either file.

Everything here fails closed: malformed input admits nobody and never crashes.
"""
from __future__ import annotations

from pathlib import Path

from orchestrator import vault_keeper
from orchestrator import vault_zk as zk
from orchestrator.connections import wire
from orchestrator.vault_keeper import KeepResult
from orchestrator.vault_zk_cli import Target

#: ``{connection_id: {"owner_jkt", "version"}}``
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


def _check_pin(pin, *, owner_jkt: str, version: int) -> None:
    if not isinstance(pin, dict) or not isinstance(pin.get("owner_jkt"), str) \
            or type(pin.get("version")) is not int:
        raise TrustError("bad-pin", f"this connection's entry in {PINS_FILE} is damaged — fix or "
                                    "remove it; refusing to guess what this box has seen")
    if pin["owner_jkt"] != owner_jkt:
        raise TrustError("statement-owner-changed",
                         "this connection's owner key is not the one this box pinned — confirm with "
                         f"its owner, then remove the connection from {PINS_FILE}")
    if version < pin["version"]:
        raise TrustError("statement-rollback",
                         f"this statement is version {version}; this box has seen version {pin['version']}")


def verified_statement(material: dict, *, owner_jkt: str, pins: dict) -> dict:
    """The statement in ``material``, once it is the owner's (``owner_jkt``, from the
    ``refresh`` plaintext), about this connection and account, and no older than what
    this box has seen. ``pins`` (connection pins) learns it only then."""
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


def _who(row: dict) -> str:
    target = str(row.get("target") or "?")[:16]
    return f"{row.get('runner_name') or row.get('kind') or 'recipient'} ({target}…)"


def _device_endorsed(row: dict, jwk: dict, jkt: str, statement: dict, endorsements: list, *,
                     pins: dict, res: KeepResult) -> bool:
    """A valid endorsement of this exact key, for an agent the statement lists, by a
    person this box trusts. Only endorsements naming such an agent reach ``_endorsed``:
    it would otherwise take a missing subject from the payload, and it pins an endorser
    on first sight, which an endorsement for another agent must not teach this box."""
    t = Target("device", jkt, jwk, str(row.get("runner_name") or ""), jkt)
    for agent in statement["agents"]:
        named = [e for e in endorsements if str(e.get("agent_id") or "") == agent]
        if named and vault_keeper._endorsed(t, jkt, named, agent, pins, res):
            return True
    return False


def allowed(material: dict, statement: dict, *, owner_jkt: str, pins: dict,
            res: KeepResult) -> dict[tuple[str, str], dict]:
    """``{(kind, target): public_jwk}``: everyone Part 0.5 lets this box seal to, among
    those canopy-web names. ``statement`` is what :func:`verified_statement` returned.
    ``pins`` (person pins) gains each endorser trusted on first sight; why a listed key
    was left out goes in ``res.skipped``."""
    out: dict[tuple[str, str], dict] = {}
    owner = material.get("owner")
    owner_id = str(owner.get("user_id") or "") if isinstance(owner, dict) else ""
    owner_key = _owner_key(material, owner_jkt)
    if owner_key is not None and wire._DECIMAL_ID.fullmatch(owner_id):
        out[("member", owner_id)] = owner_key

    computers = set(statement["my_computers"])
    endorsements = [e for e in material.get("endorsements") or [] if isinstance(e, dict)]
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
        elif target in computers or _device_endorsed(row, jwk, target, statement, endorsements,
                                                     pins=pins, res=res):
            out[("device", target)] = jwk
        else:
            res.skipped.append(f"{_who(row)}: neither in the owner's statement nor endorsed for an "
                               "agent it lists by a person this box trusts")
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


def writer_allowed(by: dict, allowed: dict, owner_user_id: str) -> bool:
    """May ``by`` (what ``wire.open_envelope`` returned) have written this envelope: a
    device ``allowed`` holds or the owner's member key, by thumbprint. Never raises."""
    if not isinstance(by, dict):
        return False
    kind, target = by.get("kind"), by.get("target")
    if not isinstance(target, str) or kind not in ("device", "member") \
            or (kind == "member" and target != owner_user_id):
        return False
    return (kind, target) in allowed and _same_key(by.get("public_jwk"), allowed[(kind, target)])
