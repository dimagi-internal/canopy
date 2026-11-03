"""The connections wire format, client side (canopy-web plan Part 0 §0.1-0.4): grant
statements and writer-authenticated envelopes.

The NORMATIVE definition is canopy-web ``docs/vault/zk-v1/connections_v1_reference.py``
with its vectors; ``fixtures/vault_zk/`` holds verbatim copies of both (the reference
loads ``zk_v1_reference.py`` beside it). Every byte the CLI makes is compared with the
vectors, and every check mirrors canopy-web's ``apps/connections/wire.py`` so the CLI and
the server refuse the same things — and the CLI refuses by ``WireError``, never a crash.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from orchestrator import device_key
from orchestrator import vault_zk as zk
from orchestrator.connections import wire

FIXTURES = Path(__file__).parent / "fixtures" / "vault_zk"
VECTORS = FIXTURES / "connections_v1_vectors.json"


def _load_reference():
    spec = importlib.util.spec_from_file_location("_connections_v1_reference",
                                                  FIXTURES / "connections_v1_reference.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ref = _load_reference()
V = json.loads(VECTORS.read_text(encoding="utf-8"))
ST, ENV = V["statement"], V["envelope"]
GOOD = ST["statement"]
B = ENV["binding"]
SEALED = ENV["sealed"]
DEVICE_SIG = ENV["device_writer"]["sig"]
MEMBER_SIG = ENV["member_writer"]["sig"]


def _priv(scalar_hex: str) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int(scalar_hex, 16), ec.SECP256R1())


def _ecdh(priv: ec.EllipticCurvePrivateKey) -> device_key.EcdhKey:
    def derive(peer_point: bytes) -> bytes:
        return priv.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), peer_point))
    return device_key.EcdhKey(device_key.KIND_SOFTWARE, zk.jwk(priv.public_key()), derive)


WRITER = _ecdh(_priv(ENV["device_writer"]["priv_scalar_hex"]))
RECIPIENTS = [(r["kind"], r["target"], r["public_jwk"]) for r in ENV["recipients"]]
KEYS = {(r["kind"], r["target"]): _ecdh(_priv(r["priv_scalar_hex"])) for r in ENV["recipients"]}
OTHER_DEVICE = ENV["recipients"][1]["target"]
PLAIN = json.loads(ENV["plaintext"])


@pytest.fixture
def ecdh_pair():
    return _ecdh(ec.generate_private_key(ec.SECP256R1())), _ecdh(ec.generate_private_key(ec.SECP256R1()))


def _entries(value: str) -> list[dict]:
    return zk.parse_sealed(value)["r"]


def _rebuild(value: str, entries: list[dict]) -> str:
    body = {**zk.parse_sealed(value), "r": entries}
    return zk.SEALED_PREFIX + zk.b64u(json.dumps(body, separators=(",", ":")).encode())


def _open(value, sig, kind="device", target=None, key=None, name="refresh"):
    key = key or WRITER
    return wire.open_envelope(value, sig, binding=B, name=name, me_kind=kind,
                              me_target=target or key.jkt, ecdh=key)


# ---- the reference and its vectors --------------------------------------------------

def test_vectors_match_the_canopy_web_reference():
    assert json.loads(VECTORS.read_text()) == ref.vectors()


def test_names_and_binding_id():
    assert (wire.REFRESH_NAME, wire.ACCESS_NAME) == ("refresh", "access")
    assert wire.binding_id(GOOD["connection"]) == B
    assert wire.binding_id(GOOD["connection"].upper()) == B


@pytest.mark.parametrize("bad", ["", "x", "connection:" + GOOD["connection"], None, 7,
                                 GOOD["connection"] + "\n"])
def test_binding_id_refuses_what_is_not_a_uuid(bad):
    with pytest.raises(wire.WireError):
        wire.binding_id(bad)


def test_digest_is_canopy_web_value_digest():
    assert wire.digest(SEALED) == ENV["sealed_digest"]
    assert wire.digest("") == ""


# ---- grant statements -------------------------------------------------------------

def test_statement_msg_and_signature_match_vectors():
    assert wire.statement_msg(GOOD) == ST["msg"].encode()
    assert wire.statement_msg(dict(reversed(list(GOOD.items())))) == ST["msg"].encode()
    assert wire.verify_statement(GOOD, ST["sig"], ST["signer_jwk"])
    assert zk.jwk_thumbprint(ST["signer_jwk"]) == ST["signer_jkt"] == PLAIN["owner_key"]


def test_a_randomized_signature_like_the_browser_makes_verifies():
    signer = _priv(ST["signer_priv_scalar_hex"])
    r, s = decode_dss_signature(signer.sign(ST["msg"].encode(), ec.ECDSA(hashes.SHA256())))
    assert wire.verify_statement(GOOD, zk.b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big")),
                                 ST["signer_jwk"])


@pytest.mark.parametrize("changed", [{"agents": ["12"]}, {"version": 4}, {"my_computers": []},
                                     {"connection": "0f8e3c2a-5b1d-4e7f-9a60-3c2b1d0e9f88"}])
def test_a_changed_statement_does_not_verify(changed):
    assert not wire.verify_statement({**GOOD, **changed}, ST["sig"], ST["signer_jwk"])


@pytest.mark.parametrize("sig,jwk", [
    (ST["sig"], ENV["recipients"][0]["public_jwk"]),
    (ST["sig"][:-4], ST["signer_jwk"]),
    ("not base64!", ST["signer_jwk"]),
    (ST["sig"] + "\n", ST["signer_jwk"]),
    (ST["sig"] + "==", ST["signer_jwk"]),
    (None, ST["signer_jwk"]),
    (64, ST["signer_jwk"]),
    (ST["sig"], {"kty": "EC", "x": "AA", "y": "AA"}),
    (ST["sig"], None),
    (ST["sig"], "jwk"),
])
def test_a_wrong_or_malformed_signature_fails_closed(sig, jwk):
    assert wire.verify_statement(GOOD, sig, jwk) is False


@pytest.mark.parametrize("bad", [None, [], "st", {**GOOD, "extra": 1}, {**GOOD, "v": 2},
                                 {**GOOD, "agents": ["40", "12"]}, {"account_id": GOOD["account_id"]}])
def test_verify_statement_fails_closed_on_a_malformed_statement(bad):
    assert wire.verify_statement(bad, ST["sig"], ST["signer_jwk"]) is False


def test_good_statement_is_accepted():
    assert wire.check_statement(dict(GOOD), connection_id=GOOD["connection"],
                                account_id=GOOD["account_id"]) == GOOD
    st = {**GOOD, "my_computers": []}
    assert wire.check_statement(st, connection_id=GOOD["connection"].upper(),
                                account_id=GOOD["account_id"]) == st


def test_a_statement_of_64_agents_is_accepted_and_65_is_not():
    agents = sorted(str(i) for i in range(1, 65))
    wire.check_statement({**GOOD, "agents": agents}, connection_id=GOOD["connection"],
                         account_id=GOOD["account_id"])
    with pytest.raises(wire.WireError):
        wire.check_statement({**GOOD, "agents": sorted(str(i) for i in range(1, 66))},
                             connection_id=GOOD["connection"], account_id=GOOD["account_id"])


@pytest.mark.parametrize("bad", [
    {"agents": ["40", "12"]}, {"agents": ["12", "12"]}, {"agents": ["12", "4a"]}, {"agents": ["012"]},
    {"agents": ["0"]}, {"agents": [12]}, {"agents": "12"}, {"agents": ["12\n"]},
    {"my_computers": ["b", "a"]}, {"my_computers": ["short"]},
    {"my_computers": [GOOD["my_computers"][0]] * 2}, {"my_computers": [GOOD["my_computers"][0] + "\n"]},
    {"version": 0}, {"version": True}, {"version": "3"}, {"version": -1}, {"version": 3.0},
    {"v": 2}, {"v": True}, {"extra": 1},
    {"account_id": 'a"b'}, {"account_id": "a\\b"}, {"account_id": "a b"}, {"account_id": ""},
    {"account_id": "é"}, {"account_id": "x" * 256}, {"account_id": GOOD["account_id"] + "\n"},
    {"account_id": "00D000000000002AAA/005000000000001AAA"},
    {"connection": "0f8e3c2a-5b1d-4e7f-9a60-3c2b1d0e9f88"},
    {"connection": GOOD["connection"].upper()}, {"connection": 7},
])
def test_malformed_statement_is_refused(bad):
    with pytest.raises(wire.WireError) as e:
        wire.check_statement({**GOOD, **bad}, connection_id=GOOD["connection"],
                             account_id=GOOD["account_id"])
    assert e.value.code == "bad-statement"


@pytest.mark.parametrize("bad", [None, [], "st", {k: v for k, v in GOOD.items() if k != "v"}])
def test_a_statement_that_is_not_an_object_of_the_keys_is_refused(bad):
    with pytest.raises(wire.WireError):
        wire.check_statement(bad, connection_id=GOOD["connection"], account_id=GOOD["account_id"])


# ---- envelopes: the vectors -------------------------------------------------------

def test_seal_reproduces_the_vector_bytes_and_device_writer_sig():
    value, sig = wire.seal_envelope(
        B, "refresh", PLAIN, RECIPIENTS, writer=WRITER, writer_target=WRITER.jkt,
        expect=DEVICE_SIG["expect"],
        ephemerals=[_priv(h) for h in ENV["ephemeral_scalars_hex"]],
        nonces=[bytes.fromhex(n) for n in ENV["nonces_hex"]])
    assert value == SEALED
    assert sig == DEVICE_SIG


def test_the_plaintext_is_compact_json_like_the_reference():
    assert ref.compact(PLAIN) == ENV["plaintext"]


@pytest.mark.parametrize("who", list(KEYS))
def test_every_vector_recipient_opens_both_writer_sigs(who):
    kind, target = who
    for sig, writer in ((DEVICE_SIG, ("device", WRITER.jkt)), (MEMBER_SIG, ("member", "7"))):
        plain, by = _open(SEALED, sig, kind=kind, target=target, key=KEYS[who])
        assert plain == PLAIN
        assert (by["kind"], by["target"]) == writer


def test_vector_entry_digests_and_messages():
    for e, want in zip(_entries(SEALED), ENV["entries"]):
        assert wire.entry_digest(e) == want["entry_digest"]


# ---- envelopes: behaviour ---------------------------------------------------------

def test_seal_then_open_round_trips_and_names_the_writer(ecdh_pair):
    me, _ = ecdh_pair
    value, sig = wire.seal_envelope(B, "refresh", PLAIN, [("device", me.jkt, me.public_jwk)],
                                    writer=me, writer_target=me.jkt, expect="")
    plain, by = wire.open_envelope(value, sig, binding=B, name="refresh", me_kind="device",
                                   me_target=me.jkt, ecdh=me)
    assert plain == PLAIN and by["target"] == me.jkt
    assert by == {"kind": "device", "target": me.jkt, "public_jwk": me.public_jwk}
    assert sig["expect"] == "" and [(m["kind"], m["target"]) for m in sig["macs"]] == [("device", me.jkt)]


def test_another_box_opens_what_one_box_wrote(ecdh_pair):
    me, other = ecdh_pair
    expect = wire.digest(SEALED)
    value, sig = wire.seal_envelope(B, "access", {"v": 1, "access_token": "t"},
                                    [("device", me.jkt, me.public_jwk),
                                     ("device", other.jkt, other.public_jwk)],
                                    writer=me, writer_target=me.jkt, expect=expect)
    plain, by = wire.open_envelope(value, sig, binding=B, name="access", me_kind="device",
                                   me_target=other.jkt, ecdh=other)
    assert plain["access_token"] == "t" and by["target"] == me.jkt and sig["expect"] == expect


def test_open_refuses_an_unsigned_envelope():
    # canopy-web's first seal is for the owner only (R4); no machine accepts it.
    with pytest.raises(wire.WireError) as e:
        _open(SEALED, None)
    assert e.value.code == "unsigned"


def test_open_refuses_a_mac_made_by_another_key_claiming_the_writer(ecdh_pair):
    impostor, _ = ecdh_pair
    _, honest = wire.seal_envelope(B, "refresh", PLAIN, RECIPIENTS, writer=WRITER,
                                   writer_target=WRITER.jkt, expect="")
    value, forged = wire.seal_envelope(B, "refresh", {**PLAIN, "refresh_token": "evil"}, RECIPIENTS,
                                       writer=impostor, writer_target=impostor.jkt, expect="")
    forged = {**forged, "by": honest["by"]}            # MACs by the impostor, writer named as WRITER
    with pytest.raises(wire.WireError) as e:
        _open(value, forged, kind="device", target=OTHER_DEVICE, key=KEYS[("device", OTHER_DEVICE)])
    assert e.value.code == "bad-mac"


def test_open_refuses_a_writer_whose_target_is_not_its_key(ecdh_pair):
    impostor, _ = ecdh_pair
    value, sig = wire.seal_envelope(B, "refresh", PLAIN, RECIPIENTS, writer=impostor,
                                    writer_target=impostor.jkt, expect="")
    sig = {**sig, "by": {**sig["by"], "target": WRITER.jkt}}
    with pytest.raises(wire.WireError) as e:
        _open(value, sig, kind="device", target=OTHER_DEVICE, key=KEYS[("device", OTHER_DEVICE)])
    assert e.value.code == "bad-sig"


def test_a_refresh_envelope_does_not_open_as_access():
    with pytest.raises(wire.WireError):
        _open(SEALED, DEVICE_SIG, name="access")


def test_an_envelope_does_not_open_under_another_binding():
    with pytest.raises(wire.WireError):
        wire.open_envelope(SEALED, DEVICE_SIG, binding=wire.binding_id("00000000-0000-4000-8000-000000000000"),
                           name="refresh", me_kind="device", me_target=WRITER.jkt, ecdh=WRITER)


def test_a_changed_expect_breaks_the_mac():
    with pytest.raises(wire.WireError) as e:
        _open(SEALED, {**DEVICE_SIG, "expect": ""})
    assert e.value.code == "bad-mac"


def test_pruning_another_recipient_keeps_my_mac_valid():
    keep = {("device", WRITER.jkt), ("member", "7")}
    value = _rebuild(SEALED, [e for e in _entries(SEALED) if (e["kind"], e["target"]) in keep])
    sig = {**DEVICE_SIG, "macs": [m for m in DEVICE_SIG["macs"] if (m["kind"], m["target"]) in keep]}
    plain, _ = _open(value, sig)
    assert plain == PLAIN


def test_a_pruned_recipient_cannot_open_it():
    keep = {("device", WRITER.jkt), ("member", "7")}
    value = _rebuild(SEALED, [e for e in _entries(SEALED) if (e["kind"], e["target"]) in keep])
    sig = {**DEVICE_SIG, "macs": [m for m in DEVICE_SIG["macs"] if (m["kind"], m["target"]) in keep]}
    with pytest.raises(wire.WireError) as e:
        _open(value, sig, target=OTHER_DEVICE, key=KEYS[("device", OTHER_DEVICE)])
    assert e.value.code == "not-for-me"


def test_a_box_that_is_not_a_recipient_is_refused(ecdh_pair):
    stranger, _ = ecdh_pair
    with pytest.raises(wire.WireError) as e:
        _open(SEALED, DEVICE_SIG, key=stranger)
    assert e.value.code == "not-for-me"


def test_my_entry_swapped_for_another_breaks_my_mac():
    # The server moves OTHER's ciphertext under MY name: the entry digest changes.
    entries = _entries(SEALED)
    mine = next(e for e in entries if e["target"] == WRITER.jkt)
    theirs = next(e for e in entries if e["target"] == OTHER_DEVICE)
    mine.update(epk=theirs["epk"], ct=theirs["ct"])
    with pytest.raises(wire.WireError) as e:
        _open(_rebuild(SEALED, entries), DEVICE_SIG)
    assert e.value.code == "bad-mac"


def test_a_well_maced_entry_that_does_not_decrypt_is_refused(ecdh_pair):
    # A writer that MACs junk is still refused when it does not open.
    me, _ = ecdh_pair
    value, _ = wire.seal_envelope(B, "refresh", PLAIN, [("device", me.jkt, me.public_jwk)],
                                  writer=me, writer_target=me.jkt, expect="")
    entries = _entries(value)
    ct = zk.unb64u(entries[0]["ct"])
    entries[0]["ct"] = zk.b64u(ct[:-1] + bytes([ct[-1] ^ 1]))
    junk = _rebuild(value, entries)
    _, sig = wire.seal_envelope(B, "refresh", PLAIN, [("device", me.jkt, me.public_jwk)],
                                writer=me, writer_target=me.jkt, expect="")
    sig = {**sig, "macs": [{"kind": "device", "target": me.jkt,
                            "mac": wire.envelope_mac(me, me.public_jwk, B, "refresh", "", entries[0])}]}
    with pytest.raises(wire.WireError) as e:
        wire.open_envelope(junk, sig, binding=B, name="refresh", me_kind="device", me_target=me.jkt, ecdh=me)
    assert e.value.code == "cannot-open"


def test_a_plaintext_that_is_not_a_v1_object_is_refused(ecdh_pair):
    me, _ = ecdh_pair
    for plain in ({"v": 2}, {"x": 1}):
        value, sig = wire.seal_envelope(B, "refresh", plain, [("device", me.jkt, me.public_jwk)],
                                        writer=me, writer_target=me.jkt, expect="")
        with pytest.raises(wire.WireError) as e:
            wire.open_envelope(value, sig, binding=B, name="refresh", me_kind="device",
                               me_target=me.jkt, ecdh=me)
        assert e.value.code == "bad-plaintext"


def test_an_envelope_at_another_kv_is_refused():
    body = zk.parse_sealed(SEALED)
    body["kv"] = 1
    value = zk.SEALED_PREFIX + zk.b64u(json.dumps(body, separators=(",", ":")).encode())
    with pytest.raises(wire.WireError):
        _open(value, DEVICE_SIG)


# ---- a malformed writer sig is a typed refusal, never a crash -----------------------

def _mac0():
    return DEVICE_SIG["macs"][0]


@pytest.mark.parametrize("sig", [
    "sig", [], 7, {}, {**DEVICE_SIG, "extra": 1},
    {k: v for k, v in DEVICE_SIG.items() if k != "expect"},
    {**DEVICE_SIG, "by": None}, {**DEVICE_SIG, "by": {**DEVICE_SIG["by"], "extra": 1}},
    {**DEVICE_SIG, "by": {**DEVICE_SIG["by"], "kind": "agent"}},
    {**DEVICE_SIG, "by": {**DEVICE_SIG["by"], "kind": ["device"]}},
    {**DEVICE_SIG, "by": {**DEVICE_SIG["by"], "target": None}},
    {**DEVICE_SIG, "by": {**DEVICE_SIG["by"], "target": DEVICE_SIG["by"]["target"] + "\n"}},
    {**DEVICE_SIG, "by": {**DEVICE_SIG["by"], "public_jwk": {**DEVICE_SIG["by"]["public_jwk"], "d": "AA"}}},
    {**DEVICE_SIG, "by": {**DEVICE_SIG["by"], "public_jwk": {**DEVICE_SIG["by"]["public_jwk"], "crv": "P-384"}}},
    {**DEVICE_SIG, "by": {**DEVICE_SIG["by"], "public_jwk": {**DEVICE_SIG["by"]["public_jwk"], "x": "AA"}}},
    {**DEVICE_SIG, "by": {**DEVICE_SIG["by"], "public_jwk": "jwk"}},
    {**MEMBER_SIG, "by": {**MEMBER_SIG["by"], "target": "07"}},
    {**MEMBER_SIG, "by": {**MEMBER_SIG["by"], "target": 7}},
    {**MEMBER_SIG, "by": {**MEMBER_SIG["by"], "target": "7\n"}},
    {**DEVICE_SIG, "expect": "ABC"}, {**DEVICE_SIG, "expect": None},
    {**DEVICE_SIG, "expect": DEVICE_SIG["expect"] + "\n"},
    {**DEVICE_SIG, "macs": "macs"}, {**DEVICE_SIG, "macs": [None]},
    {**DEVICE_SIG, "macs": DEVICE_SIG["macs"][:2]},
    {**DEVICE_SIG, "macs": DEVICE_SIG["macs"] + [_mac0()]},
    {**DEVICE_SIG, "macs": [{**_mac0(), "extra": 1}] + DEVICE_SIG["macs"][1:]},
    {**DEVICE_SIG, "macs": [{**_mac0(), "kind": ["device"]}] + DEVICE_SIG["macs"][1:]},
    {**DEVICE_SIG, "macs": [{**_mac0(), "target": {"x": 1}}] + DEVICE_SIG["macs"][1:]},
    {**DEVICE_SIG, "macs": [{**_mac0(), "mac": None}] + DEVICE_SIG["macs"][1:]},
    {**DEVICE_SIG, "macs": [{**_mac0(), "mac": _mac0()["mac"] + "\n"}] + DEVICE_SIG["macs"][1:]},
    {**DEVICE_SIG, "macs": [{**_mac0(), "mac": _mac0()["mac"] + "="}] + DEVICE_SIG["macs"][1:]},
    {**DEVICE_SIG, "macs": [{**_mac0(), "mac": "+" + _mac0()["mac"][1:]}] + DEVICE_SIG["macs"][1:]},
    {**DEVICE_SIG, "macs": [{**_mac0(), "mac": _mac0()["mac"][:-2]}] + DEVICE_SIG["macs"][1:]},
])
def test_a_malformed_writer_sig_is_refused_as_bad_sig(sig):
    with pytest.raises(wire.WireError) as e:
        _open(SEALED, sig)
    assert e.value.code == "bad-sig"


@pytest.mark.parametrize("value", ["", "zk1:abc", "zks1:", "zks1:!!!", "zks1:" + zk.b64u(b"[]"),
                                   "zks1:" + zk.b64u(b'{"kv":0,"r":[7]}'), None, 7])
def test_a_malformed_envelope_is_refused(value):
    with pytest.raises(wire.WireError):
        _open(value, DEVICE_SIG)


# ---- sealing refuses what a reader would refuse -----------------------------------

def test_seal_refuses_a_writer_target_that_is_not_its_key(ecdh_pair):
    me, other = ecdh_pair
    with pytest.raises(wire.WireError):
        wire.seal_envelope(B, "refresh", PLAIN, [("device", me.jkt, me.public_jwk)],
                           writer=me, writer_target=other.jkt, expect="")


@pytest.mark.parametrize("recipients", [
    [],
    [("device", "not-the-jkt", ENV["recipients"][0]["public_jwk"])],
    [("member", "07", ENV["recipients"][2]["public_jwk"])],
    [("agent", "1", ENV["recipients"][2]["public_jwk"])],
    [RECIPIENTS[0], RECIPIENTS[0]],
])
def test_seal_refuses_bad_recipients(ecdh_pair, recipients):
    me, _ = ecdh_pair
    with pytest.raises(wire.WireError) as e:
        wire.seal_envelope(B, "refresh", PLAIN, recipients, writer=me, writer_target=me.jkt, expect="")
    assert e.value.code == "bad-recipient"


def test_seal_refuses_more_than_64_recipients(ecdh_pair):
    me, _ = ecdh_pair
    many = []
    for _ in range(65):
        k = ec.generate_private_key(ec.SECP256R1()).public_key()
        many.append(("device", zk.jkt(k), zk.jwk(k)))
    with pytest.raises(wire.WireError):
        wire.seal_envelope(B, "refresh", PLAIN, many, writer=me, writer_target=me.jkt, expect="")


@pytest.mark.parametrize("expect", ["abc", None, ENV["sealed_digest"].upper()])
def test_seal_refuses_a_bad_expect(ecdh_pair, expect):
    me, _ = ecdh_pair
    with pytest.raises(wire.WireError):
        wire.seal_envelope(B, "refresh", PLAIN, [("device", me.jkt, me.public_jwk)],
                           writer=me, writer_target=me.jkt, expect=expect)


@pytest.mark.parametrize("name", ["", "Refresh", "token"])
def test_seal_and_open_take_only_the_two_names(ecdh_pair, name):
    me, _ = ecdh_pair
    with pytest.raises(wire.WireError):
        wire.seal_envelope(B, name, PLAIN, [("device", me.jkt, me.public_jwk)],
                           writer=me, writer_target=me.jkt, expect="")
    with pytest.raises(wire.WireError):
        _open(SEALED, DEVICE_SIG, name=name)


def test_never_raises_anything_but_wire_error_on_a_device_key_failure():
    def broken(_peer):
        raise device_key.DeviceKeyError("helper gone")
    key = device_key.EcdhKey(device_key.KIND_SOFTWARE, WRITER.public_jwk, broken)
    with pytest.raises(wire.WireError):
        _open(SEALED, DEVICE_SIG, key=key)

