"""`canopy vault zk keep` — a runner passes on the vault keys it holds (canopy-web spec
2026-10-22-vault-access-follows-canopy-access-design.md §1).

Real crypto throughout: a target "got access" iff its own private key opens the wrap the
keeper made. canopy-web is a fake `call` that records what the keeper posts.
"""
from __future__ import annotations

import os

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from orchestrator import device_key, vault_keeper
from orchestrator import vault_zk as zk

AGENT_ID = "42"
KV = 1


def _key():
    k = ec.generate_private_key(ec.SECP256R1())
    return k, zk.jwk(k.public_key())


def _ecdh(priv, jwk) -> device_key.EcdhKey:
    def derive(point: bytes) -> bytes:
        peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
        return priv.exchange(ec.ECDH(), peer)
    return device_key.EcdhKey("software", jwk, derive)


def _opens(wrap, priv, jwk, info) -> bytes:
    return zk.unwrap_ecdh_es_with_z(wrap, _ecdh(priv, jwk).derive(zk.wrap_epk_point(wrap)),
                                    _ecdh(priv, jwk).point, info)


def _endorse(signer, kind, target, key_jkt, by="7", email="jj@dimagi.com"):
    msg = zk.endorse_msg(AGENT_ID, kind, target, key_jkt).encode()
    r, s = decode_dss_signature(signer.sign(msg, ec.ECDSA(hashes.SHA256())))
    return {"kind": kind, "target": target, "key_jkt": key_jkt, "by": by, "by_email": email,
            "by_jkt": zk.jkt(signer.public_key()), "by_public_jwk": zk.jwk(signer.public_key()),
            "sig": zk.b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))}


ME_KEY, ME_JWK = _key()          # this keeper box's ECDH key
JJ_KEY, JJ_JWK = _key()          # a person who approves machines
ED_KEY, ED_JWK = _key()          # an editor waiting for the key
BOX_KEY, BOX_JWK = _key()        # a new machine waiting for the key


@pytest.fixture
def vk():
    return os.urandom(32)


def _work(vk, pending, endorsements=()):
    me = _ecdh(ME_KEY, ME_JWK)
    return {"agent": "ace", "agent_id": AGENT_ID, "kv": KV, "device_key": "dk-me",
            "wrap": zk.wrap_ecdh_es(vk, ME_JWK, zk.device_info(AGENT_ID, KV, me.jkt)),
            "pending": list(pending), "endorsements": list(endorsements),
            "_base": "/api/harness/runners/r1/vault-keeper"}


def _member(user="9", jwk=ED_JWK, email="ed@dimagi.com"):
    return {"kind": "member", "target": user, "public_jwk": jwk, "email": email, "label": email}


def _machine(key_id="dk-new", jwk=BOX_JWK):
    return {"kind": "device", "target": key_id, "public_jwk": jwk, "ecdh_public_jwk": jwk,
            "ecdh_jkt": zk.jwk_thumbprint(jwk), "label": "stewari-mbp-cdp"}


class Recorder:
    def __init__(self):
        self.posts = []

    def __call__(self, method, path, body=None):
        self.posts.append((method, path, body))
        return {"granted": len((body or {}).get("wraps") or [])}


def test_a_waiting_member_gets_a_copy_they_can_open(vk):
    pins, call = {}, Recorder()
    res = vault_keeper.keep_one(_work(vk, [_member()]), pins=pins, call=call, ecdh=_ecdh(ME_KEY, ME_JWK))
    assert res.error == "" and len(res.granted) == 1 and res.pinned == ["ed@dimagi.com"]
    method, path, body = call.posts[0]
    assert (method, path) == ("POST", "/api/harness/runners/r1/vault-keeper/ace/wraps")
    [w] = body["wraps"]
    assert w["kind"] == "member" and w["target"] == "9"
    assert _opens(w["wrap"], ED_KEY, ED_JWK, zk.member_info(AGENT_ID, KV, "9")) == vk
    assert pins["9"]["jkt"] == zk.jwk_thumbprint(ED_JWK)


def test_a_member_whose_pinned_key_changed_waits(vk):
    pins = {"9": {"jkt": "someone-elses-key", "email": "ed@dimagi.com"}}
    call = Recorder()
    res = vault_keeper.keep_one(_work(vk, [_member()]), pins=pins, call=call, ecdh=_ecdh(ME_KEY, ME_JWK))
    assert res.granted == [] and call.posts == []
    assert "vault key changed" in res.skipped[0]


def test_a_machine_needs_an_endorsement_from_someone_trusted(vk):
    call = Recorder()
    res = vault_keeper.keep_one(_work(vk, [_machine()]), pins={}, call=call, ecdh=_ecdh(ME_KEY, ME_JWK))
    assert res.granted == [] and call.posts == [] and "not endorsed" in res.skipped[0]

    box_jkt = zk.jwk_thumbprint(BOX_JWK)
    endorsed = [_endorse(JJ_KEY, "device", box_jkt, box_jkt)]
    res = vault_keeper.keep_one(_work(vk, [_machine()], endorsed), pins={}, call=call,
                                ecdh=_ecdh(ME_KEY, ME_JWK))
    assert len(res.granted) == 1 and res.pinned == ["jj@dimagi.com"]
    [w] = call.posts[0][2]["wraps"]
    assert w == {**w, "kind": "device", "target": "dk-new"}
    assert _opens(w["wrap"], BOX_KEY, BOX_JWK, zk.device_info(AGENT_ID, KV, box_jkt)) == vk


def test_a_forged_or_misdirected_endorsement_is_not_trust(vk):
    box_jkt = zk.jwk_thumbprint(BOX_JWK)
    forged = dict(_endorse(JJ_KEY, "device", box_jkt, box_jkt), sig=_endorse(
        JJ_KEY, "device", "other", "other")["sig"])
    other_key = _endorse(JJ_KEY, "device", "another-machine", "another-machine")
    call = Recorder()
    res = vault_keeper.keep_one(_work(vk, [_machine()], [forged, other_key]), pins={}, call=call,
                                ecdh=_ecdh(ME_KEY, ME_JWK))
    assert res.granted == [] and call.posts == []


def test_an_endorser_whose_pinned_key_changed_is_not_trust(vk):
    box_jkt = zk.jwk_thumbprint(BOX_JWK)
    pins = {"7": {"jkt": "jj-old-key", "email": "jj@dimagi.com"}}
    call = Recorder()
    res = vault_keeper.keep_one(_work(vk, [_machine()], [_endorse(JJ_KEY, "device", box_jkt, box_jkt)]),
                                pins=pins, call=call, ecdh=_ecdh(ME_KEY, ME_JWK))
    assert res.granted == [] and call.posts == []


def test_render_names_every_outcome():
    r = vault_keeper.KeepResult("ace", granted=["user 9"], skipped=["machine: not endorsed"],
                                pinned=["ed@dimagi.com"])
    out = vault_keeper.render([r])
    assert "granted: user 9" in out and "waiting: machine" in out and "pinned on first sight" in out
    assert vault_keeper.render([]) == "nothing waiting for a vault key this runner holds"


def test_a_workspace_vault_uses_its_grant_path_and_the_signers_agent(vk):
    """A workspace vault's machine endorsements were signed for an agent it serves:
    the keeper verifies each against the agent id the row names."""
    box_jkt = zk.jwk_thumbprint(BOX_JWK)
    other_agent = "99"
    msg = zk.endorse_msg(other_agent, "device", box_jkt, box_jkt).encode()
    r, s = decode_dss_signature(JJ_KEY.sign(msg, ec.ECDSA(hashes.SHA256())))
    e = {"kind": "device", "target": box_jkt, "key_jkt": box_jkt, "by": "7", "by_email": "jj@dimagi.com",
         "by_jkt": zk.jkt(JJ_KEY.public_key()), "by_public_jwk": zk.jwk(JJ_KEY.public_key()),
         "sig": zk.b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big")), "agent_id": other_agent}
    binding = "vault:abc"
    me = _ecdh(ME_KEY, ME_JWK)
    work = {"agent": "Shared", "agent_id": binding, "kv": KV, "device_key": "dk-me",
            "wrap": zk.wrap_ecdh_es(vk, ME_JWK, zk.device_info(binding, KV, me.jkt)),
            "pending": [_machine()], "endorsements": [e],
            "grant_path": "/api/harness/runners/r1/vault-keeper/vaults/abc/wraps", "_base": "ignored"}
    call = Recorder()
    res = vault_keeper.keep_one(work, pins={}, call=call, ecdh=me)
    assert len(res.granted) == 1
    method, path, body = call.posts[0]
    assert path == "/api/harness/runners/r1/vault-keeper/vaults/abc/wraps"
    assert _opens(body["wraps"][0]["wrap"], BOX_KEY, BOX_JWK, zk.device_info(binding, KV, box_jkt)) == vk


# ---- the keepers' shared record (canopy-web Vault.keeper_pins_zk) --------------------------

def _record(vk, people, kv=KV, version=3):
    import json
    value = zk.encrypt_value(vk, AGENT_ID, vault_keeper.PINS_NAME, kv, json.dumps({"v": 1, "people": people}))
    return {"value": value, "kv": kv, "version": version}


def _with_record(work, rec):
    return {**work, "pins": rec, "pins_path": "/api/harness/runners/r1/vault-keeper/ace/pins"}


def test_a_new_box_refuses_a_key_the_shared_record_pinned_differently(vk):
    # A brand-new cloud box (empty pin file) still knows ed's key from the vault itself.
    rec = _record(vk, {"9": {"jkt": "eds-real-key", "email": "ed@dimagi.com"}})
    call = Recorder()
    res = vault_keeper.keep_one(_with_record(_work(vk, [_member()]), rec), pins={}, call=call,
                                ecdh=_ecdh(ME_KEY, ME_JWK))
    assert res.granted == [] and call.posts == [] and "vault key changed" in res.skipped[0]


def test_a_person_pinned_first_here_is_written_back_over_the_version_read(vk):
    import json
    call = Recorder()
    work = _with_record(_work(vk, [_member()]), {"value": "", "kv": 0, "version": 0})
    res = vault_keeper.keep_one(work, pins={}, call=call, ecdh=_ecdh(ME_KEY, ME_JWK))
    assert len(res.granted) == 1
    method, path, body = call.posts[1]
    assert (method, path, body["expected_version"], body["kv"]) == ("PUT", work["pins_path"], 0, KV)
    people = json.loads(zk.decrypt_value(vk, AGENT_ID, vault_keeper.PINS_NAME, KV, body["value"]))["people"]
    assert people["9"]["jkt"] == zk.jwk_thumbprint(ED_JWK)


def test_this_box_and_the_record_disagreeing_means_the_person_waits(vk):
    rec = _record(vk, {"9": {"jkt": zk.jwk_thumbprint(ED_JWK), "email": "ed@dimagi.com"}})
    pins = {"9": {"jkt": "what-this-box-saw", "email": "ed@dimagi.com"}}
    call = Recorder()
    res = vault_keeper.keep_one(_with_record(_work(vk, [_member()]), rec), pins=pins, call=call,
                                ecdh=_ecdh(ME_KEY, ME_JWK))
    assert res.granted == [] and call.posts == [] and "shared record pin different keys" in res.skipped[0]


def test_a_record_that_does_not_open_stops_every_grant(vk):
    rec = _record(os.urandom(32), {})          # not made with this vault's key: forged
    call = Recorder()
    res = vault_keeper.keep_one(_with_record(_work(vk, [_member()]), rec), pins={}, call=call,
                                ecdh=_ecdh(ME_KEY, ME_JWK))
    assert res.granted == [] and call.posts == [] and "does not open" in res.error


def test_a_record_from_an_older_vault_key_is_started_afresh(vk):
    rec = _record(os.urandom(32), {}, kv=KV - 1 if KV > 1 else 0)
    call = Recorder()
    res = vault_keeper.keep_one(_with_record(_work(vk, [_member()]), {**rec, "kv": 0}), pins={},
                                call=call, ecdh=_ecdh(ME_KEY, ME_JWK))
    assert len(res.granted) == 1
