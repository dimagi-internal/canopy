"""Who a box will seal a connection to (canopy-web plan Part 0 §0.5, Rulings R10-R11): the
owner's signed grant statement decides, and canopy-web's list can only narrow it.

The attacker in these tests IS canopy-web: every field of ``material`` except the signed
statement and the keys it can be checked against is the server's word."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from orchestrator import vault_keeper
from orchestrator import vault_zk as zk
from orchestrator.connections import trust, wire
from orchestrator.connections.trust import TrustError

CONN = "11111111-2222-4333-8444-555555555555"
ACCOUNT = "00D000000000001/005000000000001"
OWNER_ID = "7"


def _key():
    priv = ec.generate_private_key(ec.SECP256R1())
    return priv, zk.jwk(priv.public_key())


OWNER_KEY, OWNER_JWK = _key()
ENDORSER_KEY, ENDORSER_JWK = _key()
ATTACKER_KEY, ATTACKER_JWK = _key()
_, MY_BOX_JWK = _key()
_, AGENT_BOX_JWK = _key()
_, STRANGER_JWK = _key()
O = zk.jwk_thumbprint(OWNER_JWK)
MY_BOX = zk.jwk_thumbprint(MY_BOX_JWK)
AGENT_BOX = zk.jwk_thumbprint(AGENT_BOX_JWK)
STRANGER = zk.jwk_thumbprint(STRANGER_JWK)
STRANGER_ROW = {"kind": "device", "target": STRANGER, "public_jwk": STRANGER_JWK, "runner_name": "evil"}


def _sig(priv, msg: bytes) -> str:
    r, s = decode_dss_signature(priv.sign(msg, ec.ECDSA(hashes.SHA256())))
    return zk.b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def _statement(**over) -> dict:
    return {"account_id": ACCOUNT, "agents": ["12", "40"], "connection": CONN,
            "my_computers": [MY_BOX], "v": 1, "version": 3, **over}


def _endorse(device=AGENT_BOX, *, signed_for="12", tag="12", signer=ENDORSER_KEY, signer_jwk=ENDORSER_JWK,
             by="20", kind="device", target=None, key_jkt=None) -> dict:
    target, key_jkt = target or device, key_jkt or device
    e = {"kind": kind, "target": target, "key_jkt": key_jkt, "by": by, "by_email": f"u{by}@dimagi.com",
         "by_jkt": zk.jwk_thumbprint(signer_jwk), "by_public_jwk": signer_jwk,
         "sig": _sig(signer, zk.endorse_msg(signed_for, kind, target, key_jkt).encode())}
    if tag is not None:
        e["agent_id"] = tag
    return e


def _row(jwk, target=None, kind="device") -> dict:
    return {"kind": kind, "target": target or zk.jwk_thumbprint(jwk), "public_jwk": jwk, "runner_name": "box"}


def _material(statement=None, *, signer=OWNER_KEY) -> dict:
    st = statement or _statement()
    return {"id": CONN, "account_id": ACCOUNT, "statement": st,
            "statement_sig": _sig(signer, wire.statement_msg(st)),
            "owner": {"user_id": OWNER_ID, "public_jwk": OWNER_JWK},
            "endorsements": [_endorse()],
            "recipients": [_row(OWNER_JWK, OWNER_ID, "member"), _row(MY_BOX_JWK), _row(AGENT_BOX_JWK)]}


@pytest.fixture
def material() -> dict:
    return _material()


@pytest.fixture
def res() -> vault_keeper.KeepResult:
    return vault_keeper.KeepResult("conn")


def _allowed(material, res, pins=None) -> dict:
    return trust.allowed(material, material["statement"], owner_jkt=O,
                         pins={} if pins is None else pins, res=res)


# ── the statement ────────────────────────────────────────────────────────────────────

def test_a_good_statement_is_returned_and_pinned_where_the_contract_says(material):
    pins: dict = {}
    assert trust.verified_statement(material, owner_jkt=O, pins=pins) == material["statement"]
    assert pins == {CONN: {"owner_jkt": O, "version": 3}}
    assert trust.PINS_FILE == Path.home() / ".canopy" / "connection-pins.json"


def test_a_statement_signed_by_a_key_other_than_owner_key_is_refused(material):
    pins: dict = {}
    # The real owner's key is shown, but someone else signed.
    forged = _material(signer=ATTACKER_KEY)
    with pytest.raises(TrustError) as e:
        trust.verified_statement(forged, owner_jkt=O, pins=pins)
    assert e.value.code == "bad-statement-sig"
    # canopy-web swaps in a key of its own and signs with that: self-consistent, and the
    # thumbprint sealed in the envelope is what catches it.
    swapped = {**forged, "owner": {"user_id": OWNER_ID, "public_jwk": ATTACKER_JWK}}
    with pytest.raises(TrustError) as e:
        trust.verified_statement(swapped, owner_jkt=O, pins=pins)
    assert e.value.code == "bad-owner-key"
    assert pins == {}


def test_the_signer_is_identified_by_thumbprint_not_by_comparing_whole_keys(material):
    material["owner"]["public_jwk"] = {**OWNER_JWK, "kid": "whatever", "use": "sig"}
    assert trust.verified_statement(material, owner_jkt=O, pins={})["version"] == 3


@pytest.mark.parametrize("owner", [None, {}, {"user_id": OWNER_ID}, {"user_id": OWNER_ID, "public_jwk": "nope"},
                                   {"user_id": OWNER_ID, "public_jwk": {"kty": "EC"}}])
def test_a_malformed_owner_is_a_refusal_not_a_crash(material, owner):
    material["owner"] = owner
    with pytest.raises(TrustError) as e:
        trust.verified_statement(material, owner_jkt=O, pins={})
    assert e.value.code == "bad-owner-key"


def test_a_statement_about_another_connection_or_account_is_refused():
    for st in (_statement(connection="99999999-2222-4333-8444-555555555555"), _statement(account_id="00Dother")):
        pins: dict = {}
        with pytest.raises(TrustError) as e:
            trust.verified_statement(_material(st), owner_jkt=O, pins=pins)
        assert e.value.code == "bad-statement"
        assert pins == {}


def test_no_statement_is_a_refusal(material):
    material["statement"], material["statement_sig"] = None, None
    with pytest.raises(TrustError) as e:
        trust.verified_statement(material, owner_jkt=O, pins={})
    assert e.value.code == "bad-statement"


def test_an_older_statement_than_one_seen_is_refused(material):
    pins = {CONN: {"owner_jkt": O, "version": 4}}
    with pytest.raises(TrustError) as e:
        trust.verified_statement(material, owner_jkt=O, pins=pins)
    assert e.value.code == "statement-rollback"
    assert pins == {CONN: {"owner_jkt": O, "version": 4}}


def test_the_same_version_passes_and_a_newer_one_moves_the_pin(material):
    pins = {CONN: {"owner_jkt": O, "version": 3}}
    trust.verified_statement(material, owner_jkt=O, pins=pins)
    assert pins[CONN]["version"] == 3
    trust.verified_statement(_material(_statement(version=9)), owner_jkt=O, pins=pins)
    assert pins[CONN]["version"] == 9


def test_a_pinned_owner_is_never_silently_replaced():
    """A whole forged set is self-consistent: an envelope naming the attacker as
    ``owner_key``, a statement they signed, their device as the writer. Only the pin
    catches it, so a changed owner refuses, however new the statement is."""
    pinned = {CONN: {"owner_jkt": O, "version": 3}}
    forged = _material(_statement(version=99), signer=ATTACKER_KEY)
    forged["owner"]["public_jwk"] = ATTACKER_JWK
    with pytest.raises(TrustError) as e:
        trust.verified_statement(forged, owner_jkt=zk.jwk_thumbprint(ATTACKER_JWK), pins=pinned)
    assert e.value.code == "statement-owner-changed"
    assert pinned == {CONN: {"owner_jkt": O, "version": 3}}


@pytest.mark.parametrize("pin", ["nope", {}, {"owner_jkt": O}, {"owner_jkt": O, "version": "3"},
                                 {"owner_jkt": O, "version": True}])
def test_a_damaged_pin_refuses_rather_than_starts_over(material, pin):
    with pytest.raises(TrustError) as e:
        trust.verified_statement(material, owner_jkt=O, pins={CONN: pin})
    assert e.value.code == "bad-pin"


# ── who may be sealed to ─────────────────────────────────────────────────────────────

def test_everyone_the_statement_allows_is_allowed_with_the_key_that_was_checked(material, res):
    got = _allowed(material, res)
    assert got == {("member", OWNER_ID): OWNER_JWK, ("device", MY_BOX): MY_BOX_JWK,
                   ("device", AGENT_BOX): AGENT_BOX_JWK}
    assert trust.refuse_unallowed(material["recipients"], got) == [
        ("member", OWNER_ID, OWNER_JWK), ("device", MY_BOX, MY_BOX_JWK), ("device", AGENT_BOX, AGENT_BOX_JWK)]


def test_a_device_not_in_the_statement_is_refused_even_when_canopy_web_lists_it(material, res):
    with pytest.raises(TrustError) as e:
        trust.refuse_unallowed(material["recipients"] + [STRANGER_ROW], _allowed(material, res))
    assert e.value.code == "zk_unendorsed_recipient"
    # Listed in the material itself, as a hacked canopy-web would.
    material["recipients"].append(STRANGER_ROW)
    got = _allowed(material, res)
    assert ("device", STRANGER) not in got
    assert any(STRANGER[:8] in s for s in res.skipped)
    with pytest.raises(TrustError) as e:
        trust.refuse_unallowed(material["recipients"], got)
    assert e.value.code == "zk_unendorsed_recipient"


def test_canopy_web_can_narrow_by_leaving_someone_out(material, res):
    material["recipients"] = [_row(OWNER_JWK, OWNER_ID, "member")]
    assert set(_allowed(material, res)) == {("member", OWNER_ID)}


def test_an_endorsement_for_an_agent_not_in_the_statement_admits_nothing(material, res):
    material["endorsements"] = [_endorse(signed_for="99", tag="99")]
    pins: dict = {}
    assert ("device", AGENT_BOX) not in _allowed(material, res, pins)
    assert pins == {}  # and teaches this box nothing about who to trust


def test_an_endorsement_subject_is_checked_against_the_statement_not_the_payload(material, res):
    # Signed for agent 99, relabelled by canopy-web as one the statement lists.
    material["endorsements"] = [_endorse(signed_for="99", tag="12")]
    assert ("device", AGENT_BOX) not in _allowed(material, res)
    # No subject of its own, and the payload offers one: never taken from there.
    material["endorsements"] = [_endorse(signed_for="12", tag=None)]
    material["agent_id"] = "12"
    assert ("device", AGENT_BOX) not in _allowed(material, res)
    # The subject is every statement agent, not just the first.
    material["endorsements"] = [_endorse(signed_for="40", tag="40")]
    assert ("device", AGENT_BOX) in _allowed(material, res)


@pytest.mark.parametrize("over", [{"key_jkt": STRANGER}, {"target": STRANGER},
                                  {"kind": "member", "target": OWNER_ID}])
def test_an_endorsement_must_be_of_the_devices_exact_key(material, res, over):
    material["endorsements"] = [_endorse(**over)]
    assert ("device", AGENT_BOX) not in _allowed(material, res)


def test_an_endorsement_of_another_machine_or_with_a_forged_signature_admits_nothing(material, res):
    forged = dict(_endorse(), sig=_endorse(signer=ATTACKER_KEY, signer_jwk=ATTACKER_JWK)["sig"])
    material["endorsements"] = [_endorse(device=STRANGER), forged]
    pins: dict = {}
    assert ("device", AGENT_BOX) not in _allowed(material, res, pins)
    assert pins == {}


def test_an_endorser_is_pinned_on_first_sight_and_a_changed_key_is_not_trust(material, res):
    pins: dict = {}
    assert ("device", AGENT_BOX) in _allowed(material, res, pins)
    assert pins["20"]["jkt"] == zk.jwk_thumbprint(ENDORSER_JWK)
    # The same person, later, with another key: this box waits for an admin.
    material["endorsements"] = [_endorse(signer=ATTACKER_KEY, signer_jwk=ATTACKER_JWK)]
    res2 = vault_keeper.KeepResult("conn")
    assert ("device", AGENT_BOX) not in _allowed(material, res2, pins)
    assert any("key changed" in s for s in res2.skipped)


def test_my_computers_admits_exactly_the_listed_thumbprints(material, res):
    _, other_jwk = _key()
    material["endorsements"] = []
    material["recipients"] = [_row(MY_BOX_JWK), _row(other_jwk)]
    pins: dict = {}
    got = _allowed(material, res, pins)
    assert ("device", MY_BOX) in got and len(got) == 2  # the listed computer + the owner
    assert ("device", zk.jwk_thumbprint(other_jwk)) not in got
    assert pins == {}


def test_a_listed_thumbprint_with_another_key_behind_it_is_refused(material, res):
    # canopy-web names the owner's computer but hands over its own public key.
    material["recipients"] = [_row(STRANGER_JWK, target=MY_BOX)]
    got = _allowed(material, res)
    assert ("device", MY_BOX) not in got
    with pytest.raises(TrustError) as e:
        trust.refuse_unallowed(material["recipients"], got)
    assert e.value.code == "zk_unendorsed_recipient"


def test_a_row_cannot_swap_the_key_of_someone_allowed(material, res):
    got = _allowed(material, res)
    for row in (_row(STRANGER_JWK, target=MY_BOX), _row(STRANGER_JWK, OWNER_ID, "member")):
        with pytest.raises(TrustError) as e:
            trust.refuse_unallowed([row], got)
        assert e.value.code == "zk_unendorsed_recipient"


def test_the_key_handed_on_is_exactly_the_public_key(material, res):
    material["recipients"] = [_row({**MY_BOX_JWK, "d": "private-looking", "kid": "x"}, target=MY_BOX)]
    assert _allowed(material, res)[("device", MY_BOX)] == MY_BOX_JWK


def test_a_member_other_than_the_owner_is_never_allowed(material, res):
    _, member_jwk = _key()
    member = zk.jwk_thumbprint(member_jwk)
    material["recipients"].append(_row(member_jwk, "9", "member"))
    material["endorsements"].append(_endorse(device="9", kind="member", target="9", key_jkt=member))
    got = _allowed(material, res)
    assert ("member", "9") not in got
    assert any("no member but its owner" in s for s in res.skipped)
    with pytest.raises(TrustError) as e:
        trust.refuse_unallowed(material["recipients"], got)
    assert e.value.code == "zk_unendorsed_recipient"


def test_the_owner_counts_only_by_the_key_the_envelope_names(material, res):
    material["owner"]["public_jwk"] = ATTACKER_JWK
    assert ("member", OWNER_ID) not in _allowed(material, res)


@pytest.mark.parametrize("field,junk", [
    ("recipients", ["x", None, {}, {"kind": "device"}, {"kind": "device", "target": MY_BOX, "public_jwk": "x"},
                    {"kind": "device", "target": 5, "public_jwk": MY_BOX_JWK},
                    {"kind": ["device"], "target": MY_BOX, "public_jwk": MY_BOX_JWK},
                    {"kind": "member", "target": [OWNER_ID], "public_jwk": OWNER_JWK},
                    {"kind": "device", "target": [MY_BOX], "public_jwk": MY_BOX_JWK}]),
    ("endorsements", ["x", None, {}, {"kind": "device", "target": AGENT_BOX, "key_jkt": AGENT_BOX},
                      dict(_endorse(), by_public_jwk="x"), dict(_endorse(), by_public_jwk=None),
                      dict(_endorse(), sig="!!"), dict(_endorse(), agent_id=["12"])]),
])
def test_anything_malformed_admits_nothing_and_never_crashes(material, res, field, junk):
    material[field] = junk
    got = _allowed(material, res)
    assert ("device", AGENT_BOX) not in got
    assert ("member", OWNER_ID) in got
    if field == "recipients":
        for row in junk:
            with pytest.raises(TrustError) as e:
                trust.refuse_unallowed([row], got)
            assert e.value.code == "zk_unendorsed_recipient"


def test_a_missing_list_or_owner_is_empty_not_a_crash(material, res):
    material.pop("recipients")
    material.pop("endorsements")
    material["owner"] = None
    assert _allowed(material, res) == {}


def test_nothing_is_mutated_in_the_material(material, res):
    before = copy.deepcopy(material)
    trust.verified_statement(material, owner_jkt=O, pins={})
    got = _allowed(material, res)
    got[("device", MY_BOX)]["kty"] = "changed"
    assert material == before


# ── who may write ────────────────────────────────────────────────────────────────────

def test_writer_must_be_allowed_or_the_owner(material, res):
    got = _allowed(material, res)

    def by(kind, target, jwk):
        return {"kind": kind, "target": target, "public_jwk": jwk}

    assert trust.writer_allowed(by("device", MY_BOX, MY_BOX_JWK), got, OWNER_ID)
    assert trust.writer_allowed(by("device", AGENT_BOX, AGENT_BOX_JWK), got, OWNER_ID)
    assert trust.writer_allowed(by("member", OWNER_ID, OWNER_JWK), got, OWNER_ID)
    # By thumbprint: extra fields in a key do not make it another key.
    assert trust.writer_allowed(by("member", OWNER_ID, {**OWNER_JWK, "kid": "x"}), got, OWNER_ID)
    assert not trust.writer_allowed(by("device", STRANGER, STRANGER_JWK), got, OWNER_ID)
    assert not trust.writer_allowed(by("member", OWNER_ID, ATTACKER_JWK), got, OWNER_ID)
    assert not trust.writer_allowed(by("device", MY_BOX, STRANGER_JWK), got, OWNER_ID)
    assert not trust.writer_allowed(by("member", "9", OWNER_JWK), got, OWNER_ID)
    # A member entry that somehow is not the owner's still does not write.
    assert not trust.writer_allowed(by("member", "9", OWNER_JWK), {("member", "9"): OWNER_JWK}, OWNER_ID)


@pytest.mark.parametrize("by", [None, "x", {}, {"kind": "device"},
                                {"kind": "device", "target": MY_BOX, "public_jwk": "x"},
                                {"kind": "device", "target": MY_BOX, "public_jwk": None},
                                {"kind": ["device"], "target": MY_BOX, "public_jwk": MY_BOX_JWK}])
def test_a_malformed_writer_is_no_writer(material, res, by):
    assert trust.writer_allowed(by, _allowed(material, res), OWNER_ID) is False
