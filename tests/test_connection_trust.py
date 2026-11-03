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

from orchestrator import device_key, vault_keeper
from orchestrator import vault_zk as zk
from orchestrator.connections import trust, wire
from orchestrator.connections.trust import TrustError

CONN = "abcdef11-2222-4333-8444-555555555555"
ACCOUNT = "00D000000000001/005000000000001"
OWNER_ID = "7"
BINDING = wire.binding_id(CONN)


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
ENDORSER = zk.jwk_thumbprint(ENDORSER_JWK)
MY_BOX = zk.jwk_thumbprint(MY_BOX_JWK)
AGENT_BOX = zk.jwk_thumbprint(AGENT_BOX_JWK)
STRANGER = zk.jwk_thumbprint(STRANGER_JWK)
STRANGER_ROW = {"kind": "device", "target": STRANGER, "public_jwk": STRANGER_JWK, "runner_name": "evil"}


def _sig(priv, msg: bytes) -> str:
    r, s = decode_dss_signature(priv.sign(msg, ec.ECDSA(hashes.SHA256())))
    return zk.b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def _statement(**over) -> dict:
    return {"account_id": ACCOUNT, "agents": ["12", "40"], "connection": CONN,
            "endorsers": [ENDORSER], "my_computers": [MY_BOX], "v": 1, "version": 3, **over}


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
    return {"id": CONN, "binding": BINDING, "account_id": ACCOUNT, "statement": st,
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


def _allowed(material, res) -> dict:
    return trust.allowed(material, material["statement"], owner_jkt=O, res=res)


# ── the statement ────────────────────────────────────────────────────────────────────

def test_verifying_only_reads_the_pins_and_recording_is_explicit(material):
    pins: dict = {}
    st = trust.verified_statement(material, owner_jkt=O, pins=pins)
    assert st == material["statement"]
    assert pins == {}
    trust.record_pin(st, owner_jkt=O, pins=pins)
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
    trust.record_pin(trust.verified_statement(material, owner_jkt=O, pins=pins), owner_jkt=O, pins=pins)
    assert pins[CONN]["version"] == 3
    newer = trust.verified_statement(_material(_statement(version=9)), owner_jkt=O, pins=pins)
    assert pins[CONN]["version"] == 3
    trust.record_pin(newer, owner_jkt=O, pins=pins)
    assert pins[CONN]["version"] == 9


def test_recording_cannot_move_a_pin_backwards_or_to_another_owner(material):
    pins = {CONN: {"owner_jkt": O, "version": 5}}
    with pytest.raises(TrustError) as e:
        trust.record_pin(material["statement"], owner_jkt=O, pins=pins)
    assert e.value.code == "statement-rollback"
    with pytest.raises(TrustError) as e:
        trust.record_pin(_statement(version=9), owner_jkt="someone-else", pins=pins)
    assert e.value.code == "statement-owner-changed"
    assert pins == {CONN: {"owner_jkt": O, "version": 5}}


def test_a_statement_about_another_connection_cannot_govern_this_envelope(material):
    """A real statement of the owner's about connection Y, with the envelope of X: the id
    says Y so the statement passes as Y's, and only the binding gives it away."""
    other = "99999999-2222-4333-8444-555555555555"
    forged = {**_material(_statement(connection=other)), "id": other, "binding": BINDING}
    with pytest.raises(TrustError) as e:
        trust.verified_statement(forged, owner_jkt=O, pins={})
    assert e.value.code == "bad-binding"
    forged.pop("binding")
    with pytest.raises(TrustError) as e:
        trust.verified_statement(forged, owner_jkt=O, pins={})
    assert e.value.code == "bad-binding"


def test_an_upper_case_id_cannot_dodge_the_pin(material):
    material["id"] = CONN.upper()
    with pytest.raises(TrustError) as e:
        trust.verified_statement(material, owner_jkt=O, pins={CONN: {"owner_jkt": O, "version": 9}})
    assert e.value.code == "statement-rollback"


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
    assert ("device", AGENT_BOX) not in _allowed(material, res)


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
    assert ("device", AGENT_BOX) not in _allowed(material, res)


def test_an_invented_person_with_a_valid_signature_is_refused(material, res):
    """The hole the statement's ``endorsers`` closes: canopy-web makes up a person and a
    key, and has them endorse its own device for an agent the statement lists."""
    invented = _endorse(device=STRANGER, signer=ATTACKER_KEY, signer_jwk=ATTACKER_JWK, by="9999")
    assert zk.verify_endorsement(invented, "12", ATTACKER_JWK)
    material["recipients"].append(STRANGER_ROW)
    material["endorsements"].append(invented)
    got = _allowed(material, res)
    assert ("device", STRANGER) not in got
    with pytest.raises(TrustError) as e:
        trust.refuse_unallowed(material["recipients"], got)
    assert e.value.code == "zk_unendorsed_recipient"


def test_the_owner_vouches_without_being_listed_and_no_one_else_does(res):
    material = _material(_statement(endorsers=[]))
    assert ("device", AGENT_BOX) not in _allowed(material, res)  # the fixture's endorser, unlisted
    material["endorsements"] = [_endorse(signer=OWNER_KEY, signer_jwk=OWNER_JWK, by=OWNER_ID)]
    assert ("device", AGENT_BOX) in _allowed(material, res)


def test_an_endorsers_thumbprint_comes_from_the_key_that_verifies(material, res):
    # Claims to be the listed endorser, in by_jkt; the key and signature are someone else's.
    material["endorsements"] = [dict(_endorse(signer=ATTACKER_KEY, signer_jwk=ATTACKER_JWK), by_jkt=ENDORSER)]
    assert ("device", AGENT_BOX) not in _allowed(material, res)
    # Shows the listed endorser's key, over a signature someone else made.
    material["endorsements"] = [dict(_endorse(signer=ATTACKER_KEY, signer_jwk=ATTACKER_JWK),
                                     by_jkt=ENDORSER, by_public_jwk=ENDORSER_JWK)]
    assert ("device", AGENT_BOX) not in _allowed(material, res)
    # Extra fields do not make the listed key another key.
    material["endorsements"] = [dict(_endorse(), by_public_jwk={**ENDORSER_JWK, "kid": "x"})]
    assert ("device", AGENT_BOX) in _allowed(material, res)


def test_my_computers_admits_exactly_the_listed_thumbprints(material, res):
    _, other_jwk = _key()
    material["endorsements"] = []
    material["recipients"] = [_row(MY_BOX_JWK), _row(other_jwk)]
    got = _allowed(material, res)
    assert ("device", MY_BOX) in got and len(got) == 2  # the listed computer + the owner
    assert ("device", zk.jwk_thumbprint(other_jwk)) not in got


def test_a_listed_thumbprint_with_another_key_behind_it_is_refused(material, res):
    # canopy-web names the owner's computer but hands over its own public key.
    material["recipients"] = [_row(STRANGER_JWK, target=MY_BOX)]
    got = _allowed(material, res)
    assert ("device", MY_BOX) not in got
    with pytest.raises(TrustError) as e:
        trust.refuse_unallowed(material["recipients"], got)
    assert e.value.code == "zk_unendorsed_recipient"


def test_a_non_canonical_or_off_curve_key_is_not_the_listed_key(material, res):
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    last = alphabet.index(MY_BOX_JWK["x"][-1])  # a 32-byte value leaves its last character 2 spare bits
    same_point = {**MY_BOX_JWK, "x": MY_BOX_JWK["x"][:-1] + alphabet[last + 1]}
    off_curve = {**MY_BOX_JWK, "y": MY_BOX_JWK["x"]}
    material["recipients"] = [_row(same_point, target=MY_BOX), _row(same_point), _row(off_curve, target=MY_BOX),
                              _row(off_curve)]
    assert set(_allowed(material, res)) == {("member", OWNER_ID)}


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
                      dict(_endorse(), sig="!!"), dict(_endorse(), sig="é"), dict(_endorse(), sig=None),
                      dict(_endorse(), agent_id=["12"])]),
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


@pytest.mark.parametrize("value", [5, True, "x", {"a": 1}, None])
@pytest.mark.parametrize("field", ["recipients", "endorsements"])
def test_a_list_of_the_wrong_type_counts_as_empty(material, res, field, value):
    material[field] = value
    got = _allowed(material, res)
    assert ("device", AGENT_BOX) not in got and ("member", OWNER_ID) in got
    by = {"kind": "device", "target": AGENT_BOX, "public_jwk": AGENT_BOX_JWK}
    assert trust.writer_allowed(by, material, material["statement"], owner_jkt=O) is (field == "recipients")


@pytest.mark.parametrize("material", [None, 5, "x", [], True])
def test_an_answer_that_is_not_an_object_refuses(material, res):
    st = _statement()
    for call in (lambda: trust.verified_statement(material, owner_jkt=O, pins={}),
                 lambda: trust.allowed(material, st, owner_jkt=O, res=res)):
        with pytest.raises(TrustError) as e:
            call()
        assert e.value.code == "bad-material"
    by = {"kind": "member", "target": OWNER_ID, "public_jwk": OWNER_JWK}
    assert trust.writer_allowed(by, material, st, owner_jkt=O) is False


@pytest.mark.parametrize("statement", [None, 5, [], {}, {**_statement(), "extra": 1}])
def test_an_unshaped_statement_admits_nobody(material, res, statement):
    with pytest.raises(TrustError) as e:
        trust.allowed(material, statement, owner_jkt=O, res=res)
    assert e.value.code == "bad-statement"
    by = {"kind": "member", "target": OWNER_ID, "public_jwk": OWNER_JWK}
    assert trust.writer_allowed(by, material, statement, owner_jkt=O) is False


@pytest.mark.parametrize("owner_jkt", [None, 5, ["x"], {}])
def test_an_owner_key_that_is_no_thumbprint_refuses(material, res, owner_jkt):
    st = material["statement"]
    for call in (lambda: trust.verified_statement(material, owner_jkt=owner_jkt, pins={}),
                 lambda: trust.allowed(material, st, owner_jkt=owner_jkt, res=res),
                 lambda: trust.record_pin(st, owner_jkt=owner_jkt, pins={})):
        with pytest.raises(TrustError) as e:
            call()
        assert e.value.code == "bad-owner-key"
    by = {"kind": "member", "target": OWNER_ID, "public_jwk": OWNER_JWK}
    assert trust.writer_allowed(by, material, st, owner_jkt=owner_jkt) is False


@pytest.mark.parametrize("rows", [5, None, "x", {"kind": "device"}])
def test_recipients_that_are_not_a_list_refuse_the_seal(rows):
    with pytest.raises(TrustError) as e:
        trust.refuse_unallowed(rows, {})
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

def _by(kind, target, jwk) -> dict:
    return {"kind": kind, "target": target, "public_jwk": jwk}


def _writer(by, material) -> bool:
    return trust.writer_allowed(by, material, material["statement"], owner_jkt=O)


def test_writer_must_be_allowed_or_the_owner(material):
    assert _writer(_by("device", MY_BOX, MY_BOX_JWK), material)
    assert _writer(_by("device", AGENT_BOX, AGENT_BOX_JWK), material)
    assert _writer(_by("member", OWNER_ID, OWNER_JWK), material)
    # By thumbprint: extra fields in a key do not make it another key.
    assert _writer(_by("member", OWNER_ID, {**OWNER_JWK, "kid": "x"}), material)
    assert not _writer(_by("device", STRANGER, STRANGER_JWK), material)
    assert not _writer(_by("member", OWNER_ID, ATTACKER_JWK), material)
    assert not _writer(_by("device", MY_BOX, STRANGER_JWK), material)
    assert not _writer(_by("device", STRANGER, MY_BOX_JWK), material)
    assert not _writer(_by("member", "9", OWNER_JWK), material)
    assert not _writer(_by("owner", OWNER_ID, OWNER_JWK), material)


def test_a_writer_pruned_since_it_wrote_can_still_be_proved(material):
    """It left the recipients (its agent's lender stopped administering it, say), and
    readers must still validate it, or nobody could open the envelope to re-seal it."""
    material["recipients"] = [_row(OWNER_JWK, OWNER_ID, "member")]
    assert _writer(_by("device", AGENT_BOX, AGENT_BOX_JWK), material)
    assert _writer(_by("device", MY_BOX, MY_BOX_JWK), material)


def test_a_writer_outside_the_recipients_that_cannot_be_proved_is_refused(material):
    material["recipients"] = []
    by = _by("device", AGENT_BOX, AGENT_BOX_JWK)
    for endorsements in ([],                                                            # none left
                         [_endorse(signer=ATTACKER_KEY, signer_jwk=ATTACKER_JWK)],      # an unlisted person
                         [_endorse(signed_for="99", tag="99")],                         # another agent
                         [_endorse(signed_for="99", tag="12")],                         # relabelled
                         [_endorse(device=STRANGER)]):                                  # another machine
        material["endorsements"] = endorsements
        assert not _writer(by, material)


def test_a_forged_set_cannot_prove_a_writer_to_a_box_that_knows_the_owner(material):
    invented = _endorse(device=STRANGER, signer=ATTACKER_KEY, signer_jwk=ATTACKER_JWK, by="9999")
    material["endorsements"].append(invented)
    material["recipients"].append(STRANGER_ROW)
    assert not _writer(_by("device", STRANGER, STRANGER_JWK), material)


@pytest.mark.parametrize("by", [None, "x", {}, {"kind": "device"},
                                {"kind": "device", "target": MY_BOX, "public_jwk": "x"},
                                {"kind": "device", "target": MY_BOX, "public_jwk": None},
                                {"kind": ["device"], "target": MY_BOX, "public_jwk": MY_BOX_JWK},
                                {"kind": "device", "target": [MY_BOX], "public_jwk": MY_BOX_JWK}])
def test_a_malformed_writer_is_no_writer(material, by):
    assert _writer(by, material) is False


def test_an_owner_with_no_id_is_no_writer(material):
    material["owner"]["user_id"] = ""
    assert not _writer(_by("member", "", OWNER_JWK), material)


# ── a whole read, as B4 and B5 make it ───────────────────────────────────────────────

def _ecdh_pair():
    priv = ec.generate_private_key(ec.SECP256R1())

    def derive(point: bytes) -> bytes:
        return priv.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point))
    return device_key.EcdhKey(device_key.KIND_SOFTWARE, zk.jwk(priv.public_key()), derive)


FRESH, EVIL, REAL_WRITER = _ecdh_pair(), _ecdh_pair(), _ecdh_pair()
ATTACKER = zk.jwk_thumbprint(ATTACKER_JWK)
INSTANCE = "https://acme.my.salesforce.com"


def _refresh_plain(**over) -> dict:
    return {"v": 1, "provider": "salesforce", "refresh_token": "REAL-REFRESH", "client_id": "c",
            "login_host": "login.salesforce.com", "instance_url": INSTANCE, "account_id": ACCOUNT,
            "owner_key": O, "issued_at": 1, "scopes": "api", "statement_floor": 3, **over}


def _access_plain(**over) -> dict:
    return {"v": 1, "access_token": "REAL-ACCESS", "expires_at": 9999999999, "instance_url": INSTANCE,
            "account_id": ACCOUNT, "scope": "api", "owner_key": O, "statement_floor": 3, **over}


def _seal(name, plain, *boxes, writer=REAL_WRITER):
    return wire.seal_envelope(BINDING, name, plain, [("device", b.jkt, b.public_jwk) for b in boxes],
                              writer=writer, writer_target=writer.jkt, expect="")


def _read(m, refresh, access, *, me=FRESH, pins):
    """The order the module docstring gives. Returns the statement and plaintexts."""
    plain, by = wire.open_envelope(*refresh, binding=BINDING, name="refresh", me_kind="device",
                                   me_target=me.jkt, ecdh=me)
    owner_jkt = plain["owner_key"]
    st = trust.verified_statement(m, owner_jkt=owner_jkt, pins=pins)
    if not trust.writer_allowed(by, m, st, owner_jkt=owner_jkt):
        raise TrustError("writer", "refresh")
    opened = None
    if access:
        opened, by = wire.open_envelope(*access, binding=BINDING, name="access", me_kind="device",
                                        me_target=me.jkt, ecdh=me)
        if not trust.writer_allowed(by, m, st, owner_jkt=owner_jkt):
            raise TrustError("writer", "access")
    trust.check_plain(plain, opened, st)
    trust.record_pin(st, owner_jkt=owner_jkt, pins=pins)
    return st, plain, opened


def _real_material(statement=None) -> dict:
    return _material(statement or _statement(my_computers=sorted([FRESH.jkt, REAL_WRITER.jkt])))


def test_a_real_read_passes_and_records_the_pin():
    pins: dict = {}
    _, plain, access = _read(_real_material(), _seal("refresh", _refresh_plain(), FRESH),
                             _seal("access", _access_plain(), FRESH), pins=pins)
    assert (plain["refresh_token"], access["access_token"]) == ("REAL-REFRESH", "REAL-ACCESS")
    assert pins == {CONN: {"owner_jkt": O, "version": 3}}


def _forged_material() -> dict:
    """Everything canopy-web can make: a statement under its own person key, listing its
    own device and this box, with that key shown as the owner's."""
    forged = _material(_statement(version=99, my_computers=sorted([EVIL.jkt, FRESH.jkt, REAL_WRITER.jkt])),
                       signer=ATTACKER_KEY)
    forged["owner"] = {"user_id": "9999", "public_jwk": ATTACKER_JWK}
    forged["recipients"] = [_row(FRESH.public_jwk), _row(EVIL.public_jwk)]
    return forged


def test_a_forged_refresh_beside_the_real_access_is_refused_on_a_fresh_box():
    """The review's attack. Every earlier check passes on a box with no pin; only the
    ``owner_key`` the real ``access`` carries stops a re-seal handing it to EVIL."""
    forged = _forged_material()
    refresh = _seal("refresh", _refresh_plain(refresh_token="ATTACKER", owner_key=ATTACKER), FRESH, writer=EVIL)
    access = _seal("access", _access_plain(), FRESH)
    pins: dict = {}
    with pytest.raises(TrustError) as e:
        _read(forged, refresh, access, pins=pins)
    assert e.value.code == "access-mismatch"
    assert pins == {}
    # That is the only check standing: the rest of the forged set does hang together.
    st = trust.verified_statement(forged, owner_jkt=ATTACKER, pins={})
    got = trust.allowed(forged, st, owner_jkt=ATTACKER, res=vault_keeper.KeepResult("c"))
    assert ("device", EVIL.jkt) in got


def test_a_forged_refresh_on_a_box_that_has_read_the_real_one_stops_at_the_pin():
    pins = {CONN: {"owner_jkt": O, "version": 3}}
    refresh = _seal("refresh", _refresh_plain(owner_key=ATTACKER), FRESH, writer=EVIL)
    with pytest.raises(TrustError) as e:
        _read(_forged_material(), refresh, None, pins=pins)
    assert e.value.code == "statement-owner-changed"


def test_an_old_signed_statement_replayed_at_a_fresh_box_cannot_readmit_a_removed_device():
    removed = sorted([EVIL.jkt, FRESH.jkt, REAL_WRITER.jkt])
    old = _material(_statement(version=1, my_computers=removed))
    pins: dict = {}
    with pytest.raises(TrustError) as e:
        _read(old, _seal("refresh", _refresh_plain(statement_floor=3), FRESH), None, pins=pins)
    assert e.value.code == "statement-below-floor"
    assert pins == {}


def test_a_removed_device_cannot_lower_the_floor_with_a_refresh_it_forged():
    """It knows the owner's public key, so it can seal a ``refresh`` naming the real owner
    with floor 0, beside an old statement that lists it and so proves it a writer. The real
    ``access`` carries its own floor, which it cannot change, and a re-seal never happens."""
    removed, keeper = _ecdh_pair(), _ecdh_pair()
    old = _material(_statement(version=1, my_computers=sorted([removed.jkt, keeper.jkt, FRESH.jkt, REAL_WRITER.jkt])))
    forged = _seal("refresh", _refresh_plain(refresh_token="ATTACKER", statement_floor=0), keeper, writer=removed)
    real_access = _seal("access", _access_plain(), keeper)
    pins: dict = {}
    with pytest.raises(TrustError) as e:
        _read(old, forged, real_access, me=keeper, pins=pins)
    assert e.value.code == "statement-below-floor"
    assert pins == {}


@pytest.mark.parametrize("refresh_floor,access_floor,version,ok", [
    (5, 3, 5, True),    # an access left over from before a share still reads
    (3, 5, 5, True),    # no equality between the envelopes, either way
    (5, 3, 4, False),   # below the refresh's floor alone
    (3, 5, 4, False),   # below the access's floor alone
    (3, 3, 3, True),
    (0, 0, 1, True),
])
def test_each_floor_is_checked_against_the_statement_on_its_own(refresh_floor, access_floor, version, ok):
    st = _statement(version=version)
    refresh, access = _refresh_plain(statement_floor=refresh_floor), _access_plain(statement_floor=access_floor)
    if ok:
        trust.check_plain(refresh, access, st)
    else:
        with pytest.raises(TrustError) as e:
            trust.check_plain(refresh, access, st)
        assert e.value.code == "statement-below-floor"


def test_a_statement_at_the_floor_passes_and_canopy_webs_first_seal_is_zero():
    pins: dict = {}
    _read(_real_material(), _seal("refresh", _refresh_plain(statement_floor=3), FRESH), None, pins=pins)
    _read(_real_material(), _seal("refresh", _refresh_plain(statement_floor=0), FRESH), None, pins=pins)
    assert pins[CONN]["version"] == 3


def test_a_read_aborted_by_the_writer_check_leaves_the_pins_untouched():
    newer = _real_material(_statement(version=9, my_computers=sorted([FRESH.jkt, REAL_WRITER.jkt])))
    pins = {CONN: {"owner_jkt": O, "version": 3}}
    with pytest.raises(TrustError) as e:
        _read(newer, _seal("refresh", _refresh_plain(), FRESH, writer=EVIL), None, pins=pins)
    assert e.value.detail == "refresh"
    with pytest.raises(TrustError) as e:
        _read(newer, _seal("refresh", _refresh_plain(), FRESH), _seal("access", _access_plain(), FRESH, writer=EVIL),
              pins=pins)
    assert e.value.detail == "access"
    assert pins == {CONN: {"owner_jkt": O, "version": 3}}


def test_a_read_aborted_by_the_plaintext_check_leaves_the_pins_untouched():
    newer = _real_material(_statement(version=9, my_computers=sorted([FRESH.jkt, REAL_WRITER.jkt])))
    pins = {CONN: {"owner_jkt": O, "version": 3}}
    with pytest.raises(TrustError) as e:
        _read(newer, _seal("refresh", _refresh_plain(), FRESH),
              _seal("access", _access_plain(account_id="00Dother"), FRESH), pins=pins)
    assert e.value.code == "access-mismatch"
    assert pins == {CONN: {"owner_jkt": O, "version": 3}}


ST = _statement()


@pytest.mark.parametrize("over,code", [
    ({"account_id": "00Dother"}, "account-mismatch"),
    ({"statement_floor": 4}, "statement-below-floor"),
    ({"statement_floor": "3"}, "bad-plaintext"),
    ({"statement_floor": True}, "bad-plaintext"),
    ({"statement_floor": -1}, "bad-plaintext"),
    ({"statement_floor": None}, "bad-plaintext"),
    ({"owner_key": 5}, "bad-plaintext"),
    ({"owner_key": "short"}, "bad-plaintext"),
    ({"provider": ["salesforce"]}, "bad-plaintext"),
    ({"provider": "other"}, "unknown-provider"),
    ({"extra": 1}, "bad-plaintext"),
    ({"login_host": "evil.example.com"}, "bad-host"),
    ({"instance_url": "https://evil.example.com"}, "bad-host"),
])
def test_a_refresh_plaintext_is_checked(over, code):
    with pytest.raises(TrustError) as e:
        trust.check_plain(_refresh_plain(**over), _access_plain(), ST)
    assert e.value.code == code


@pytest.mark.parametrize("refresh", [None, 5, "x", [], {}, {k: v for k, v in _refresh_plain().items()
                                                            if k != "statement_floor"}])
def test_a_refresh_plaintext_of_the_wrong_shape_refuses(refresh):
    with pytest.raises(TrustError) as e:
        trust.check_plain(refresh, None, ST)
    assert e.value.code == "bad-plaintext"


@pytest.mark.parametrize("over,code", [
    ({"owner_key": ATTACKER}, "access-mismatch"),
    ({"owner_key": None}, "access-mismatch"),
    ({"account_id": "00Dother"}, "access-mismatch"),
    ({"statement_floor": "3"}, "bad-plaintext"),
    ({"statement_floor": True}, "bad-plaintext"),
    ({"statement_floor": -1}, "bad-plaintext"),
    ({"statement_floor": None}, "bad-plaintext"),
    ({"statement_floor": 3.0}, "bad-plaintext"),
    ({"statement_floor": 4}, "statement-below-floor"),
    ({"instance_url": "https://evil.example.com"}, "bad-host"),
    ({"instance_url": 5}, "bad-plaintext"),
    ({"access_token": ""}, "bad-plaintext"),
    ({"access_token": 5}, "bad-plaintext"),
    ({"expires_at": "soon"}, "bad-plaintext"),
    ({"expires_at": True}, "bad-plaintext"),
    ({"v": 2}, "bad-plaintext"),
    ({"extra": 1}, "bad-plaintext"),
])
def test_an_access_plaintext_is_checked_against_the_refresh(over, code):
    with pytest.raises(TrustError) as e:
        trust.check_plain(_refresh_plain(), _access_plain(**over), ST)
    assert e.value.code == code


@pytest.mark.parametrize("access", [5, "x", [], {}, {k: v for k, v in _access_plain().items() if k != "owner_key"}])
def test_an_access_plaintext_of_the_wrong_shape_refuses(access):
    with pytest.raises(TrustError) as e:
        trust.check_plain(_refresh_plain(), access, ST)
    assert e.value.code == "bad-plaintext"


def test_a_google_access_names_no_host():
    google = _refresh_plain(provider="google", login_host="", instance_url="")
    trust.check_plain(google, _access_plain(instance_url=""), ST)
    with pytest.raises(TrustError) as e:
        trust.check_plain(google, _access_plain(), ST)
    assert e.value.code == "bad-host"


@pytest.mark.parametrize("statement", [None, 5, {}, {**ST, "extra": 1}])
def test_the_plaintext_check_needs_a_shaped_statement(statement):
    with pytest.raises(TrustError) as e:
        trust.check_plain(_refresh_plain(), None, statement)
    assert e.value.code == "bad-statement"


# ── canopy_web: an optional recipient the owner may name ─────────────────────────────

_, CW_JWK = _key()
CW = zk.jwk_thumbprint(CW_JWK)


def _cw_material(statement) -> dict:
    m = _material(statement)
    m["recipients"].append(_row(CW_JWK))
    return m


def test_writer_allowed_accepts_named_canopy_web():
    m = _cw_material(_statement(canopy_web=CW))
    assert trust.writer_allowed(_by("device", CW, CW_JWK), m, m["statement"], owner_jkt=O)


def test_writer_allowed_refuses_canopy_web_when_statement_omits_it():
    m = _cw_material(_statement())
    assert not trust.writer_allowed(_by("device", CW, CW_JWK), m, m["statement"], owner_jkt=O)
    # Naming another key admits nothing for this one.
    m = _cw_material(_statement(canopy_web=MY_BOX))
    assert not trust.writer_allowed(_by("device", CW, CW_JWK), m, m["statement"], owner_jkt=O)


def test_allowed_keeps_canopy_web_recipient_when_named(res):
    m = _cw_material(_statement(canopy_web=CW))
    got = _allowed(m, res)
    assert got[("device", CW)] == CW_JWK
    assert ("device", CW, CW_JWK) in trust.refuse_unallowed(m["recipients"], got)


def test_allowed_skips_canopy_web_row_when_not_named(res):
    m = _cw_material(_statement())
    got = _allowed(m, res)
    assert ("device", CW) not in got
    assert any(CW[:8] in s for s in res.skipped)


@pytest.mark.parametrize("bad", ["", None, "short", CW + "="])
def test_a_malformed_canopy_web_admits_nobody(bad, res):
    st = _statement(canopy_web=bad)
    m = _cw_material(st)
    with pytest.raises(TrustError):
        _allowed(m, res)
    assert not trust.writer_allowed(_by("device", CW, CW_JWK), m, st, owner_jkt=O)
