"""The keeper's connection work (canopy-web spec §4.1, §4.5, plan Task B5): a box that holds a
connection re-seals it for machines the owner's signed statement allows, revokes one being
disconnected, and confirms.

``KeeperWeb`` stands where canopy-web does, refusing what ``keeper.reseal`` and
``keeper.confirm_revoked`` refuse, and the envelopes are real. Where a test forges something,
the attacker IS canopy-web: everything but a sealed value and the signed statement is its word."""
from __future__ import annotations

import json
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from orchestrator import canopy_web, device_key, runner_session, vault_keeper, vault_zk_cli
from orchestrator import vault_zk as zk
from orchestrator.connections import access, trust, wire
from orchestrator.connections.keep import keep_connection
from orchestrator.connections.providers import base as provider_base
from orchestrator.connections.providers.base import HttpResponse

CONN = "abcdef11-2222-4333-8444-555555555555"
OTHER_CONN = "abcdef11-2222-4333-8444-666666666666"
ACCOUNT = "00D000000000001/005000000000001"
OWNER_ID = "7"
RID = "11111111-1111-1111-1111-111111111111"
BASE = "https://canopy.test"
ACCESS_TOKEN = "sf-access-never-printed"
REFRESH = "sf-refresh-never-printed"
SECRET = "app-client-secret-never-printed"
SECRETS = (ACCESS_TOKEN, REFRESH, SECRET)


def _ecdh():
    priv = ec.generate_private_key(ec.SECP256R1())
    return device_key._sw_ecdh_key(priv.private_bytes(serialization.Encoding.PEM,
                                                      serialization.PrivateFormat.PKCS8,
                                                      serialization.NoEncryption()))


def _signer():
    priv = ec.generate_private_key(ec.SECP256R1())
    return priv, zk.jwk(priv.public_key())


def _sig(priv, msg: bytes) -> str:
    r, s = decode_dss_signature(priv.sign(msg, ec.ECDSA(hashes.SHA256())))
    return zk.b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def _public(key) -> dict:
    return {k: key.public_jwk[k] for k in ("kty", "crv", "x", "y")}


OWNER_KEY, OWNER_JWK = _signer()
ATTACKER_KEY, ATTACKER_JWK = _signer()
OWNER_JKT = zk.jwk_thumbprint(OWNER_JWK)
BOX = _ecdh()          # this keeper, in the owner's statement
PEER = _ecdh()         # another machine the owner lists, who wrote what is stored
AGENT_BOX = _ecdh()    # a machine the owner vouched for agent 12, waiting
MY_LAPTOP = _ecdh()    # a machine on the owner's my_computers, waiting
STRANGER = _ecdh()     # a machine nobody vouched for
OWNER_MEMBER = ("member", OWNER_ID, OWNER_JWK)


def _refresh_plain(**over) -> dict:
    return {"v": 1, "provider": "salesforce", "refresh_token": REFRESH, "client_id": "client-1",
            "login_host": "login.salesforce.com", "instance_url": "https://acme.my.salesforce.com",
            "account_id": ACCOUNT, "owner_key": OWNER_JKT, "issued_at": 1793500000, "scopes": "api refresh_token",
            "statement_floor": 3, **over}


def _google_plain() -> dict:
    return _refresh_plain(provider="google", login_host="", instance_url="", scopes="openid")


def _access_plain(**over) -> dict:
    return {"v": 1, "access_token": ACCESS_TOKEN, "expires_at": int(time.time()) + 1800,
            "instance_url": "https://acme.my.salesforce.com", "account_id": ACCOUNT, "scope": "api",
            "owner_key": OWNER_JKT, "statement_floor": 3, **over}


def _row(key) -> dict:
    return {"kind": "device", "target": key.jkt, "public_jwk": _public(key), "runner_name": f"box-{key.jkt[:4]}"}


def _endorse(key, *, agent="12", signer=OWNER_KEY, signer_jwk=OWNER_JWK) -> dict:
    return {"kind": "device", "target": key.jkt, "key_jkt": key.jkt, "by": OWNER_ID, "by_email": "o@dimagi.com",
            "by_jkt": zk.jwk_thumbprint(signer_jwk), "by_public_jwk": signer_jwk, "agent_id": agent,
            "sig": _sig(signer, zk.endorse_msg(agent, "device", key.jkt, key.jkt).encode())}


def _problem(method, path, status, error):
    body = {"title": "Refused", "detail": f"{error}: canopy-web says no.", "error": error}
    return canopy_web.CanopyError(f"{method} {path} -> {status}: {json.dumps(body)}")


class KeeperWeb:
    """canopy-web's keeper routes, as `apps/connections/keeper.py` has them."""

    def __init__(self, conn=CONN):
        self.conn, self.binding = conn, wire.binding_id(conn)
        self.statement = {"account_id": ACCOUNT, "agents": ["12"], "connection": conn, "endorsers": [],
                          "my_computers": sorted([BOX.jkt, PEER.jkt, MY_LAPTOP.jkt]), "v": 1, "version": 3}
        self.signer = OWNER_KEY
        self.owner_jwk = OWNER_JWK
        self.recipients = [{"kind": "member", "target": OWNER_ID, "public_jwk": OWNER_JWK, "runner_name": ""},
                           _row(BOX), _row(PEER)]
        self.endorsements: list[dict] = []
        self.provider = "salesforce"
        self.client_id = "client-1"
        self.holders = [OWNER_MEMBER, ("device", BOX.jkt, _public(BOX)), ("device", PEER.jkt, _public(PEER))]
        self.access_sealed, self.access_sig = "", None
        self.seal_refresh(_refresh_plain())
        self.puts: list[dict] = []
        self.confirmed: list[dict] = []
        self.order: list[str] = []
        self.put_failure = None
        self.confirm_failure = None

    def seal_refresh(self, plain, writer=PEER, recipients=None):
        self.sealed, self.sealed_sig = wire.seal_envelope(
            self.binding, wire.REFRESH_NAME, plain, recipients or self.holders,
            writer=writer, writer_target=writer.jkt, expect="")

    def seal_access(self, plain=None, writer=PEER, recipients=None):
        self.access_sealed, self.access_sig = wire.seal_envelope(
            self.binding, wire.ACCESS_NAME, plain or _access_plain(), recipients or self.holders,
            writer=writer, writer_target=writer.jkt, expect="")

    def waiting_for(self, *keys) -> list[str]:
        """List machines as canopy-web would: named in `recipients`, and waiting."""
        for key in keys:
            if not any(r["target"] == key.jkt for r in self.recipients):
                self.recipients.append(_row(key))
        return [k.jkt for k in keys]

    def work(self, waiting=(), *, revoking=False, revoke_at_provider=False) -> dict:
        return {"id": self.conn, "provider": self.provider, "workspace": "dimagi", "binding": self.binding,
                "account_id": ACCOUNT, "account_label": "jj@dimagi.com",
                "sealed": self.sealed, "sealed_sig": self.sealed_sig, "sealed_digest": wire.digest(self.sealed),
                "access_sealed": self.access_sealed, "access_sig": self.access_sig, "access_expires_at": None,
                "statement": self.statement, "statement_sig": _sig(self.signer, wire.statement_msg(self.statement)),
                "owner": {"user_id": OWNER_ID, "public_jwk": self.owner_jwk},
                "endorsements": self.endorsements, "recipients": self.recipients,
                "app": {"client_id": self.client_id, "client_secret": SECRET, "options": {}},
                "lease": None, "rotates_refresh_token": True,
                "waiting": list(waiting), "revoking": revoking, "revoke_at_provider": revoke_at_provider,
                "reseal_path": f"/keeper/{self.conn}", "revoked_path": f"/revoked/{self.conn}"}

    def __call__(self, method, path, body=None):
        self.order.append(f"{method} {path}")
        if method == "PUT" and path == f"/keeper/{self.conn}":
            return self._reseal(path, body)
        if method == "POST" and path == f"/revoked/{self.conn}":
            self.confirmed.append(body)
            if self.confirm_failure:
                status, error = self.confirm_failure
                raise _problem(method, path, status, error)
            return {}
        raise AssertionError(f"unexpected {method} {path}")

    def _reseal(self, path, body):
        self.puts.append(body)
        if self.put_failure:
            status, error = self.put_failure
            raise _problem("PUT", path, status, error)
        assert set(body) == {"sealed", "sealed_sig", "access_sealed", "access_sig", "expect"}
        old = {(e["kind"], e["target"]) for e in wire._sealed_entries(self.sealed)}
        if ("device", BOX.jkt) not in {(e["kind"], e["target"]) for e in wire._sealed_entries(body["sealed"])}:
            raise _problem("PUT", path, 403, "zk_not_a_holder")
        if body["expect"] != wire.digest(self.sealed):
            raise _problem("PUT", path, 409, "zk_value_changed")
        allowed = {(r["kind"], r["target"]) for r in self.recipients}
        keep = old & allowed
        checks = [(body["sealed"], body["sealed_sig"], wire.digest(self.sealed))]
        if self.access_sealed:
            if not body["access_sealed"]:
                raise _problem("PUT", path, 422, "zk_rewrap_required")
            checks.append((body["access_sealed"], body["access_sig"], wire.digest(self.access_sealed)))
        elif body["access_sealed"]:
            raise _problem("PUT", path, 409, "zk_value_changed")
        for value, sig, expect in checks:
            given = {(e["kind"], e["target"]) for e in wire._sealed_entries(value)}
            if given - allowed:
                raise _problem("PUT", path, 422, "foreign_reader")
            if keep - given:
                raise _problem("PUT", path, 422, "zk_rewrap_required")
            if sig["by"] != {"kind": "device", "target": BOX.jkt, "public_jwk": _public(BOX)} \
                    or sig["expect"] != expect:
                raise _problem("PUT", path, 422, "bad_writer_sig")
        self.sealed, self.sealed_sig = body["sealed"], body["sealed_sig"]
        if self.access_sealed:
            self.access_sealed, self.access_sig = body["access_sealed"], body["access_sig"]
        return {"machines": ["box"]}


class Http:
    """The provider, at the injected transport."""

    def __init__(self, web=None):
        self.web = web
        self.urls: list[str] = []
        self.forms: list[dict] = []
        self.status = 200
        self.answer = {}
        self.error = None

    def __call__(self, url, form, *, timeout):
        if self.web is not None:
            self.web.order.append("revoke@provider")
        self.urls.append(url)
        self.forms.append(form)
        if self.error:
            raise self.error
        return HttpResponse(self.status, self.answer)


@pytest.fixture(autouse=True)
def _pins_file(monkeypatch, tmp_path):
    path = tmp_path / "connection-pins.json"
    monkeypatch.setattr(trust, "PINS_FILE", path)
    return path


@pytest.fixture
def fake_web():
    return KeeperWeb()


@pytest.fixture
def http(fake_web):
    return Http(fake_web)


def _keep(fake_web, work, *, http=None, pins=None, ecdh=BOX):
    return keep_connection(work, pins={} if pins is None else pins, call=fake_web, ecdh=ecdh, http=http)


def _entries(value) -> set:
    return {(e["kind"], e["target"]) for e in wire._sealed_entries(value)}


def _open(value, sig, name, key, binding=wire.binding_id(CONN)):
    plain, by = wire.open_envelope(value, sig, binding=binding, name=name, me_kind="device", me_target=key.jkt,
                                   ecdh=key)
    return plain, by


def _text(res) -> str:
    return json.dumps(res.__dict__)


def _assert_no_secret(*texts):
    joined = "\n".join(texts)
    for secret in SECRETS:
        assert secret not in joined


# ── re-sealing ───────────────────────────────────────────────────────────────────────


def test_keeper_reseals_for_a_waiting_endorsed_box(fake_web):
    fake_web.seal_access()
    fake_web.endorsements = [_endorse(AGENT_BOX)]
    before = _open(fake_web.sealed, fake_web.sealed_sig, wire.REFRESH_NAME, BOX)[0]
    before_access = _open(fake_web.access_sealed, fake_web.access_sig, wire.ACCESS_NAME, BOX)[0]
    expect, access_expect = wire.digest(fake_web.sealed), wire.digest(fake_web.access_sealed)

    res = _keep(fake_web, fake_web.work(fake_web.waiting_for(AGENT_BOX)))

    assert res.error == "" and len(res.granted) == 1 and AGENT_BOX.jkt[:16] in res.granted[0]
    assert len(fake_web.puts) == 1
    body = fake_web.puts[0]
    assert body["expect"] == expect
    assert body["sealed_sig"]["expect"] == expect and body["access_sig"]["expect"] == access_expect
    assert body["sealed_sig"]["by"]["target"] == BOX.jkt and body["access_sig"]["by"]["target"] == BOX.jkt
    wanted = {("member", OWNER_ID), ("device", BOX.jkt), ("device", PEER.jkt), ("device", AGENT_BOX.jkt)}
    assert _entries(body["sealed"]) == wanted and _entries(body["access_sealed"]) == wanted
    for key in (AGENT_BOX, BOX, PEER):
        assert _open(body["sealed"], body["sealed_sig"], wire.REFRESH_NAME, key)[0] == before
        assert _open(body["access_sealed"], body["access_sig"], wire.ACCESS_NAME, key)[0] == before_access


def test_a_machine_on_my_computers_needs_no_endorsement(fake_web):
    res = _keep(fake_web, fake_web.work(fake_web.waiting_for(MY_LAPTOP)))
    assert res.error == "" and ("device", MY_LAPTOP.jkt) in _entries(fake_web.puts[0]["sealed"])


def test_what_the_keeper_wrote_passes_a_whole_read_on_the_new_box(fake_web):
    fake_web.seal_access()
    fake_web.endorsements = [_endorse(AGENT_BOX), _endorse(BOX)]
    fake_web.statement["my_computers"] = sorted([PEER.jkt])  # BOX, the writer, is proved by its endorsement
    waiting = fake_web.waiting_for(AGENT_BOX)
    res = _keep(fake_web, fake_web.work(waiting))
    assert res.error == "" and res.granted

    read = access.checked_read(fake_web.work(), provider="salesforce", ecdh=AGENT_BOX, pins={})
    assert read.refresh["refresh_token"] == REFRESH and read.access["access_token"] == ACCESS_TOKEN


def test_a_reseal_keeps_each_envelopes_own_floor_and_the_owner_key(fake_web):
    fake_web.seal_access(_access_plain(statement_floor=1))  # from before the last share: lower is legal
    res = _keep(fake_web, fake_web.work(fake_web.waiting_for(MY_LAPTOP)))
    assert res.error == ""
    body = fake_web.puts[0]
    refresh = _open(body["sealed"], body["sealed_sig"], wire.REFRESH_NAME, MY_LAPTOP)[0]
    access_plain = _open(body["access_sealed"], body["access_sig"], wire.ACCESS_NAME, MY_LAPTOP)[0]
    assert (refresh["statement_floor"], refresh["owner_key"]) == (3, OWNER_JKT)
    assert (access_plain["statement_floor"], access_plain["owner_key"]) == (1, OWNER_JKT)


def test_with_no_access_stored_only_refresh_is_resealed(fake_web):
    res = _keep(fake_web, fake_web.work(fake_web.waiting_for(MY_LAPTOP)))
    assert res.error == ""
    assert fake_web.puts[0]["access_sealed"] == "" and fake_web.puts[0]["access_sig"] is None


def test_nothing_waiting_writes_nothing(fake_web):
    res = _keep(fake_web, fake_web.work())
    assert fake_web.puts == [] and res.granted == [] and res.error == ""


def test_one_already_holding_is_no_reason_to_write(fake_web):
    res = _keep(fake_web, fake_web.work([PEER.jkt]))
    assert fake_web.puts == [] and res.granted == [] and res.error == ""


def test_keeper_refuses_device_not_in_statement_even_if_server_lists_it(fake_web):
    res = _keep(fake_web, fake_web.work(fake_web.waiting_for(STRANGER)))
    assert fake_web.puts == [] and "zk_unendorsed_recipient" in " ".join(res.skipped)
    assert res.granted == []


def test_a_stranger_waiting_beside_a_good_box_is_left_out(fake_web):
    fake_web.endorsements = [_endorse(AGENT_BOX)]
    res = _keep(fake_web, fake_web.work(fake_web.waiting_for(STRANGER, AGENT_BOX)))
    assert res.error == "" and len(res.granted) == 1
    assert "zk_unendorsed_recipient" in " ".join(res.skipped) and STRANGER.jkt[:16] in " ".join(res.skipped)
    assert ("device", STRANGER.jkt) not in _entries(fake_web.puts[0]["sealed"])
    assert ("device", AGENT_BOX.jkt) in _entries(fake_web.puts[0]["sealed"])


@pytest.mark.parametrize("endorsement", [
    pytest.param(lambda: _endorse(STRANGER, agent="99"), id="for-an-agent-not-listed"),
    pytest.param(lambda: _endorse(STRANGER, signer=ATTACKER_KEY, signer_jwk=ATTACKER_JWK), id="by-an-invented-person"),
    pytest.param(lambda: _endorse(AGENT_BOX), id="of-another-key"),
    pytest.param(lambda: {**_endorse(STRANGER), "sig": _endorse(AGENT_BOX)["sig"]}, id="signature-for-another-key"),
])
def test_no_valid_endorsement_is_no_seal(fake_web, endorsement):
    fake_web.endorsements = [endorsement()]
    res = _keep(fake_web, fake_web.work(fake_web.waiting_for(STRANGER)))
    assert fake_web.puts == [] and "zk_unendorsed_recipient" in " ".join(res.skipped)


def test_a_waiting_machine_canopy_web_gives_no_key_for_is_skipped(fake_web):
    res = _keep(fake_web, fake_web.work([MY_LAPTOP.jkt]))  # on my_computers, but not in `recipients`
    assert fake_web.puts == [] and "zk_unendorsed_recipient" in " ".join(res.skipped)


def test_a_key_swapped_on_a_waiting_row_is_never_sealed_to(fake_web):
    fake_web.recipients.append({**_row(MY_LAPTOP), "public_jwk": _public(STRANGER)})
    res = _keep(fake_web, fake_web.work([MY_LAPTOP.jkt]))
    assert fake_web.puts == [] and "zk_unendorsed_recipient" in " ".join(res.skipped)


@pytest.mark.parametrize("waiting", ["not-a-list", None, [None, 3, {"a": 1}]])
def test_a_malformed_waiting_list_adds_nobody(fake_web, waiting):
    work = fake_web.work()
    work["waiting"] = waiting
    res = _keep(fake_web, work)
    assert fake_web.puts == [] and res.granted == [] and res.error == ""


def test_a_holder_the_statement_no_longer_allows_is_not_sealed_to_again(fake_web):
    """Listed or not, this box does not write to it; canopy-web's refusal then stops the write."""
    fake_web.statement["my_computers"] = sorted([BOX.jkt, MY_LAPTOP.jkt])
    fake_web.seal_refresh(_refresh_plain(), writer=BOX)  # the writer is fine; PEER only holds a copy
    res = _keep(fake_web, fake_web.work(fake_web.waiting_for(MY_LAPTOP)))
    assert ("device", PEER.jkt) not in _entries(fake_web.puts[0]["sealed"])
    assert "zk_rewrap_required" in res.error and res.granted == []


def test_a_holder_proved_by_its_endorsement_stays(fake_web):
    fake_web.statement["my_computers"] = sorted([BOX.jkt, MY_LAPTOP.jkt])
    fake_web.endorsements = [_endorse(PEER)]
    res = _keep(fake_web, fake_web.work(fake_web.waiting_for(MY_LAPTOP)))
    assert res.error == "" and ("device", PEER.jkt) in _entries(fake_web.puts[0]["sealed"])


def test_a_holder_canopy_web_stopped_listing_drops_out(fake_web):
    fake_web.recipients = [r for r in fake_web.recipients if r["target"] != PEER.jkt]
    res = _keep(fake_web, fake_web.work(fake_web.waiting_for(MY_LAPTOP)))
    assert res.error == ""
    assert _entries(fake_web.puts[0]["sealed"]) == {("member", OWNER_ID), ("device", BOX.jkt),
                                                    ("device", MY_LAPTOP.jkt)}


def test_a_box_the_statement_does_not_allow_writes_nothing(fake_web):
    """Whatever it wrote, the next reader would refuse its writer."""
    fake_web.statement["my_computers"] = sorted([PEER.jkt, MY_LAPTOP.jkt])
    res = _keep(fake_web, fake_web.work(fake_web.waiting_for(MY_LAPTOP)))
    assert fake_web.puts == [] and res.granted == [] and res.error


def test_canopy_web_refusing_the_write_grants_nobody(fake_web):
    fake_web.put_failure = (409, "zk_value_changed")
    res = _keep(fake_web, fake_web.work(fake_web.waiting_for(MY_LAPTOP)))
    assert res.granted == [] and "zk_value_changed" in res.error
    _assert_no_secret(_text(res))


# ── an access envelope this box cannot vouch for ─────────────────────────────────────


def test_an_access_this_box_cannot_open_refuses_the_connection_this_pass(fake_web, _pins_file):
    """A re-seal must carry both envelopes, and this box can neither open this one nor drop
    it: leave it for the next pass, or for a refresher, whose new access will include it."""
    fake_web.seal_access(recipients=[OWNER_MEMBER, ("device", PEER.jkt, _public(PEER))])
    pins: dict = {}
    res = _keep(fake_web, fake_web.work(fake_web.waiting_for(MY_LAPTOP)), pins=pins)
    assert fake_web.puts == [] and res.granted == []
    assert "access" in res.error
    assert pins == {} and not _pins_file.exists()  # an aborted read saves nothing


@pytest.mark.parametrize("forge", [
    pytest.param(lambda web: web.seal_access(_access_plain(owner_key=zk.jwk_thumbprint(ATTACKER_JWK))),
                 id="another-owner"),
    pytest.param(lambda web: web.seal_access(_access_plain(account_id="00D000000000002/005000000000002")),
                 id="another-account"),
    pytest.param(lambda web: web.seal_access(_access_plain(instance_url="https://evil.example.com")), id="bad-host"),
    pytest.param(lambda web: web.seal_access(writer=STRANGER), id="writer-not-allowed"),
    pytest.param(lambda web: web.seal_access(_access_plain(statement_floor=9)), id="floor-above-statement"),
])
def test_an_access_that_does_not_validate_is_never_resealed(fake_web, _pins_file, forge):
    forge(fake_web)
    pins: dict = {}
    res = _keep(fake_web, fake_web.work(fake_web.waiting_for(MY_LAPTOP)), pins=pins)
    assert fake_web.puts == [] and res.granted == [] and res.error
    assert pins == {} and not _pins_file.exists()
    _assert_no_secret(_text(res))


def test_an_unsigned_access_is_refused(fake_web):
    fake_web.seal_access()
    fake_web.access_sig = None
    res = _keep(fake_web, fake_web.work(fake_web.waiting_for(MY_LAPTOP)))
    assert fake_web.puts == [] and "unsigned" in res.error


# ── forged material ──────────────────────────────────────────────────────────────────


def _forged_owner(web):
    """A wholly forged set on a box that has pinned the real owner."""
    jkt = zk.jwk_thumbprint(ATTACKER_JWK)
    web.seal_refresh(_refresh_plain(owner_key=jkt), writer=STRANGER)
    web.signer, web.owner_jwk = ATTACKER_KEY, ATTACKER_JWK
    web.statement["my_computers"] = sorted([STRANGER.jkt, BOX.jkt])
    web.recipients.append(_row(STRANGER))


@pytest.mark.parametrize("forge", [
    pytest.param(lambda web: setattr(web, "signer", ATTACKER_KEY), id="statement-signed-by-another"),
    pytest.param(lambda web: setattr(web, "owner_jwk", ATTACKER_JWK), id="owner-key-swapped"),
    pytest.param(lambda web: web.seal_refresh(_refresh_plain(), writer=STRANGER), id="writer-not-allowed"),
    pytest.param(lambda web: web.seal_refresh(_refresh_plain(statement_floor=9)), id="floor-above-statement"),
    pytest.param(lambda web: web.seal_refresh(_refresh_plain(login_host="evil.example.com")), id="bad-host"),
    pytest.param(lambda web: web.statement.update(version=2), id="statement-replayed"),
    pytest.param(lambda web: setattr(web, "sealed_sig", None), id="unsigned"),
])
def test_keeper_refuses_a_forged_envelope_and_writes_nothing(fake_web, _pins_file, forge):
    forge(fake_web)
    pins: dict = {}
    res = _keep(fake_web, fake_web.work(fake_web.waiting_for(MY_LAPTOP)), pins=pins)
    assert fake_web.puts == [] and res.granted == [] and res.error
    assert pins == {} and not _pins_file.exists()
    _assert_no_secret(_text(res))


def test_a_tampered_mac_is_refused(fake_web):
    other = KeeperWeb()
    fake_web.sealed_sig = other.sealed_sig
    res = _keep(fake_web, fake_web.work(fake_web.waiting_for(MY_LAPTOP)))
    assert fake_web.puts == [] and "bad-mac" in res.error


def test_a_box_holding_no_copy_opens_nothing(fake_web):
    res = _keep(fake_web, fake_web.work(fake_web.waiting_for(MY_LAPTOP)), ecdh=STRANGER)
    assert fake_web.puts == [] and "not-for-me" in res.error


def test_a_forged_set_on_a_box_that_pinned_the_owner_stops_at_the_pin(fake_web):
    _forged_owner(fake_web)
    pins = {CONN: {"owner_jkt": OWNER_JKT, "version": 3}}
    res = _keep(fake_web, fake_web.work([STRANGER.jkt]), pins=pins)
    assert fake_web.puts == [] and "statement-owner-changed" in res.error
    assert pins == {CONN: {"owner_jkt": OWNER_JKT, "version": 3}}


def test_a_statement_older_than_the_pin_is_refused(fake_web):
    res = _keep(fake_web, fake_web.work(fake_web.waiting_for(MY_LAPTOP)),
                pins={CONN: {"owner_jkt": OWNER_JKT, "version": 8}})
    assert fake_web.puts == [] and "statement-rollback" in res.error


# ── pins ─────────────────────────────────────────────────────────────────────────────


def test_a_validated_read_saves_the_pin_with_no_write(fake_web, _pins_file):
    res = _keep(fake_web, fake_web.work())
    assert fake_web.puts == [] and res.error == ""
    assert json.loads(_pins_file.read_text()) == {CONN: {"owner_jkt": OWNER_JKT, "version": 3}}


def test_a_validated_read_saves_the_pin_when_every_waiter_is_refused(fake_web, _pins_file):
    res = _keep(fake_web, fake_web.work(fake_web.waiting_for(STRANGER)))
    assert fake_web.puts == [] and res.skipped
    assert json.loads(_pins_file.read_text()) == {CONN: {"owner_jkt": OWNER_JKT, "version": 3}}


def test_the_pin_is_saved_before_the_write_goes_out(fake_web, _pins_file):
    seen = []
    real = fake_web._reseal

    def spying(path, body):
        seen.append(_pins_file.exists())
        return real(path, body)

    fake_web._reseal = spying
    _keep(fake_web, fake_web.work(fake_web.waiting_for(MY_LAPTOP)))
    assert seen == [True]


def test_saving_reloads_the_file_so_another_processes_newer_pin_stands(fake_web, _pins_file):
    other = "abcdef11-2222-4333-8444-777777777777"
    _pins_file.write_text(json.dumps({CONN: {"owner_jkt": OWNER_JKT, "version": 5},
                                      other: {"owner_jkt": "x", "version": 1}}))
    res = _keep(fake_web, fake_web.work(), pins={})  # loaded before the other process wrote
    assert res.error == ""
    on_disk = json.loads(_pins_file.read_text())
    assert on_disk[CONN]["version"] == 5 and other in on_disk


def test_a_pin_that_cannot_be_saved_stops_the_write(fake_web, _pins_file):
    _pins_file.write_text("{damaged")
    res = _keep(fake_web, fake_web.work(fake_web.waiting_for(MY_LAPTOP)))
    assert fake_web.puts == [] and res.granted == [] and res.error
    assert _pins_file.read_text() == "{damaged"


# ── revoking ─────────────────────────────────────────────────────────────────────────


def test_keeper_revokes_then_confirms(fake_web, http):
    res = _keep(fake_web, fake_web.work(revoking=True, revoke_at_provider=True), http=http)
    assert fake_web.order == ["revoke@provider", f"POST /revoked/{CONN}"]
    assert http.urls == ["https://login.salesforce.com/services/oauth2/revoke"]
    assert http.forms[0]["token"] == REFRESH and http.forms[0]["client_secret"] == SECRET
    assert fake_web.confirmed == [{"revoked": True, "error": ""}]
    assert res.error == "" and res.done
    _assert_no_secret(_text(res))


def test_keeper_skips_provider_revoke_when_told_another_connection_remains(fake_web, http):
    res = _keep(fake_web, fake_web.work(revoking=True, revoke_at_provider=False), http=http)
    assert http.urls == []
    assert fake_web.confirmed == [{"revoked": True, "error": ""}]
    assert res.error == "" and res.done


@pytest.mark.parametrize("told", ["false", "true", 1, None])
def test_only_a_true_flag_revokes_at_the_provider(fake_web, http, told):
    work = fake_web.work(revoking=True)
    work["revoke_at_provider"] = told
    _keep(fake_web, work, http=http)
    assert http.urls == [] and fake_web.confirmed == [{"revoked": True, "error": ""}]


@pytest.mark.parametrize("told,urls", [(True, ["https://oauth2.googleapis.com/revoke"]), (False, [])])
def test_a_google_disconnect_revokes_only_when_told(told, urls):
    web = KeeperWeb()
    web.provider = "google"
    web.seal_refresh(_google_plain())
    http = Http(web)
    res = keep_connection(web.work(revoking=True, revoke_at_provider=told), pins={}, call=web, ecdh=BOX, http=http)
    assert res.error == "" and http.urls == urls
    assert web.confirmed == [{"revoked": True, "error": ""}]


def test_a_token_already_dead_counts_as_revoked(fake_web, http):
    http.status, http.answer = 400, {"error": "invalid_grant", "error_description": "expired"}
    res = _keep(fake_web, fake_web.work(revoking=True, revoke_at_provider=True), http=http)
    assert fake_web.confirmed == [{"revoked": True, "error": ""}]
    assert res.error == ""


@pytest.mark.parametrize("status,answer,code", [
    (400, {"error": "unsupported_token_type"}, "unsupported_token_type"),
    (503, {"error": "busy"}, "unavailable"),
    (302, None, "unavailable"),
    (400, {"error": f"Bad Thing with {REFRESH} and {SECRET}"}, "rejected"),
    (400, {"error": "x" * 300}, "rejected"),
    (400, {}, "rejected"),
])
def test_any_other_failure_confirms_not_revoked_with_a_code_and_no_token(fake_web, http, status, answer, code):
    http.status, http.answer = status, answer
    res = _keep(fake_web, fake_web.work(revoking=True, revoke_at_provider=True), http=http)
    assert fake_web.confirmed == [{"revoked": False, "error": code}]
    assert len(fake_web.confirmed[0]["error"]) <= 100
    _assert_no_secret(_text(res), json.dumps(fake_web.confirmed))
    assert res.error


def test_a_transport_failure_confirms_not_revoked(fake_web, http):
    http.error = ConnectionError(f"could not post {REFRESH}")
    res = _keep(fake_web, fake_web.work(revoking=True, revoke_at_provider=True), http=http)
    assert fake_web.confirmed == [{"revoked": False, "error": "unavailable"}]
    _assert_no_secret(_text(res), json.dumps(fake_web.confirmed))


def test_a_swapped_client_is_not_sent_the_token_and_confirms_not_revoked(fake_web, http):
    fake_web.client_id = "client-of-the-attacker"
    _keep(fake_web, fake_web.work(revoking=True, revoke_at_provider=True), http=http)
    assert http.urls == []
    assert fake_web.confirmed == [{"revoked": False, "error": "client-mismatch"}]


def test_a_forged_revoking_envelope_reaches_no_provider_and_confirms_nothing(fake_web, http, _pins_file):
    """It is no holder's to judge: another holder may still read it, and the sweep ends it."""
    fake_web.seal_refresh(_refresh_plain(), writer=STRANGER)
    pins: dict = {}
    res = _keep(fake_web, fake_web.work(revoking=True, revoke_at_provider=True), http=http, pins=pins)
    assert http.urls == [] and fake_web.confirmed == [] and res.error
    assert pins == {} and not _pins_file.exists()


def test_revoking_needs_no_access_this_box_can_open(fake_web, http):
    fake_web.seal_access(recipients=[OWNER_MEMBER, ("device", PEER.jkt, _public(PEER))])
    res = _keep(fake_web, fake_web.work(revoking=True, revoke_at_provider=True), http=http)
    assert res.error == "" and http.urls and fake_web.confirmed == [{"revoked": True, "error": ""}]


def test_revoking_writes_no_envelope_even_with_machines_listed_waiting(fake_web, http):
    _keep(fake_web, fake_web.work(fake_web.waiting_for(MY_LAPTOP), revoking=True), http=http)
    assert fake_web.puts == [] and fake_web.confirmed


def test_a_validated_revoke_saves_the_pin_before_the_provider_is_called(fake_web, http, _pins_file):
    seen = []
    http_call = http.__call__

    class Spy:
        def __call__(self, url, form, *, timeout):
            seen.append(_pins_file.exists())
            return http_call(url, form, timeout=timeout)

    _keep(fake_web, fake_web.work(revoking=True, revoke_at_provider=True), http=Spy())
    assert seen == [True]


def test_another_holder_confirming_first_is_done(fake_web, http):
    fake_web.confirm_failure = (404, "not_found")
    res = _keep(fake_web, fake_web.work(revoking=True, revoke_at_provider=True), http=http)
    assert res.error == "" and res.done


def test_a_confirmation_that_fails_is_reported_for_the_next_pass(fake_web, http):
    fake_web.confirm_failure = (500, "boom")
    res = _keep(fake_web, fake_web.work(revoking=True, revoke_at_provider=True), http=http)
    assert res.error and res.done == []
    _assert_no_secret(_text(res))


# ── one bad connection ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("work", ["not an object", None, [], {}, {"id": CONN}, {"id": CONN, "provider": ["x"]}])
def test_nonsense_work_is_an_error_not_a_crash(fake_web, work):
    res = _keep(fake_web, work)
    assert res.error and fake_web.puts == [] and fake_web.confirmed == []


def test_an_unexpected_failure_is_named_by_its_type_only(fake_web):
    def explode(*a, **k):
        raise RuntimeError(f"holding {REFRESH} and {ACCESS_TOKEN}")

    res = keep_connection(fake_web.work(fake_web.waiting_for(MY_LAPTOP)), pins={}, call=explode, ecdh=BOX)
    assert "RuntimeError" in res.error
    _assert_no_secret(_text(res))


def test_a_lost_connection_to_canopy_web_is_an_error_not_a_crash(fake_web):
    def lost(*a, **k):
        raise ConnectionError(f"lost, holding {REFRESH}")

    res = keep_connection(fake_web.work(fake_web.waiting_for(MY_LAPTOP)), pins={}, call=lost, ecdh=BOX)
    assert res.error and res.granted == []
    _assert_no_secret(_text(res))


def test_nothing_is_printed_or_logged_with_a_secret(fake_web, http, capsys, caplog):
    fake_web.seal_access()
    results = [_keep(fake_web, fake_web.work(fake_web.waiting_for(MY_LAPTOP, STRANGER)))]
    http.status, http.answer = 400, {"error": f"no {REFRESH}", "error_description": f"secret {SECRET}"}
    results.append(_keep(fake_web, fake_web.work(revoking=True, revoke_at_provider=True), http=http))
    out, err = capsys.readouterr()
    _assert_no_secret(out, err, caplog.text, vault_keeper.render(results), *map(_text, results))


def test_a_result_names_the_connection_and_not_its_token(fake_web):
    res = _keep(fake_web, fake_web.work())
    assert res.kind == "connection" and "salesforce" in res.agent and "jj@dimagi.com" in res.agent


# ── the provider's transport ─────────────────────────────────────────────────────────


def test_provider_revoke_takes_the_transport_like_refresh_does():
    from orchestrator.connections import providers

    http = Http()
    providers.get("salesforce").revoke(_refresh_plain(), {"client_id": "client-1", "client_secret": SECRET},
                                       http=http)
    assert http.urls == ["https://login.salesforce.com/services/oauth2/revoke"]


# ── run_keep ─────────────────────────────────────────────────────────────────────────


class Server:
    """What `run_keep` reaches through its transport: the vault keeper's answer, the
    connection keeper's, and the routes `KeeperWeb` serves."""

    def __init__(self, webs=()):
        self.webs = list(webs)
        self.vault: dict = {"agents": [], "delegated": []}
        self.work: list = []
        self.connections_status = 200
        self.requests: list[tuple[str, str, dict]] = []

    def __call__(self, method, url, headers, data):
        path = url[len(BASE):]
        self.requests.append((method, path, dict(headers)))
        body = json.loads(data) if data else None
        if path == f"/api/harness/runners/{RID}/vault-keeper":
            return 200, json.dumps(self.vault)
        if path == f"/api/harness/runners/{RID}/vault-keeper/connections":
            if self.connections_status != 200:
                return self.connections_status, json.dumps({"error": "nope", "detail": "nope"})
            return 200, json.dumps({"connections": self.work})
        for web in self.webs:
            if path.endswith(web.conn):
                try:
                    return 200, json.dumps(web(method, path, body))
                except canopy_web.CanopyError as e:
                    return 409, str(e).split(": ", 1)[1]
        raise AssertionError(f"unexpected {method} {path}")


@pytest.fixture
def box(monkeypatch, tmp_path):
    monkeypatch.setattr(runner_session, "token", lambda *a, **k: "runner-token")
    monkeypatch.setattr(device_key, "proof_headers", lambda method, url: {"DPoP": f"proof {method} {url}"})

    def run(server, **over):
        kw = {"runner_id": RID, "base_url": BASE, "transport": server, "ecdh": BOX,
              "pins_file": tmp_path / "vault-pins.json", **over}
        return vault_keeper.run_keep(**kw)
    return run


def _paths(server) -> list[str]:
    return [f"{m} {p}" for m, p, _ in server.requests]


def test_run_keep_includes_connections(fake_web, box):
    server = Server([fake_web])
    server.work = [fake_web.work(fake_web.waiting_for(MY_LAPTOP))]
    results = box(server)
    assert _paths(server)[:2] == [f"GET /api/harness/runners/{RID}/vault-keeper",
                                  f"GET /api/harness/runners/{RID}/vault-keeper/connections"]
    assert len(results) == 1 and results[0].granted and results[0].error == ""
    assert ("device", MY_LAPTOP.jkt) in _entries(fake_web.sealed)


def test_connection_routes_carry_a_device_proof_and_the_vault_route_stays_as_it_was(fake_web, box):
    server = Server([fake_web])
    server.work = [fake_web.work(fake_web.waiting_for(MY_LAPTOP))]
    box(server)
    by_path = {f"{m} {p}": h for m, p, h in server.requests}
    assert "DPoP" not in by_path[f"GET /api/harness/runners/{RID}/vault-keeper"]
    conn_get = f"GET /api/harness/runners/{RID}/vault-keeper/connections"
    assert by_path[conn_get]["DPoP"] == f"proof GET {BASE}/api/harness/runners/{RID}/vault-keeper/connections"
    assert by_path[f"PUT /keeper/{CONN}"]["DPoP"] == f"proof PUT {BASE}/keeper/{CONN}"
    assert all(h["Authorization"] == "Bearer runner-token" for _, _, h in server.requests)


def test_connection_work_uses_the_connection_pins_and_not_the_vault_pins(fake_web, box, tmp_path, _pins_file):
    vault_pins = tmp_path / "vault-pins.json"
    vault_pins.write_text(json.dumps({"20": {"jkt": "person", "email": "a@b"}}))
    server = Server([fake_web])
    server.vault = {"agents": [], "delegated": [_delegated_stub()]}
    server.work = [fake_web.work()]
    box(server)
    assert list(json.loads(vault_pins.read_text())) == ["20"]
    assert list(json.loads(_pins_file.read_text())) == [CONN]


def _delegated_stub() -> dict:
    return {"agent_id": "12", "name": "NOPE", "agent": "a", "value_zk": "not-sealed", "reseal_path": "/x", "expect": ""}


def test_with_only_connection_work_the_vault_pin_file_is_not_touched(fake_web, box, tmp_path):
    server = Server([fake_web])
    server.work = [fake_web.work()]
    box(server)
    assert not (tmp_path / "vault-pins.json").exists()


def test_one_bad_connection_does_not_stop_the_pass(box):
    good = KeeperWeb()
    bad = KeeperWeb(OTHER_CONN)
    bad.seal_refresh(_refresh_plain(), writer=STRANGER)
    server = Server([bad, good])
    server.work = ["not an object", bad.work(bad.waiting_for(MY_LAPTOP)), good.work(good.waiting_for(MY_LAPTOP))]
    results = box(server)
    assert [bool(r.error) for r in results] == [True, True, False]
    assert bad.puts == [] and results[2].granted


def test_a_server_without_the_route_is_no_connection_work(box):
    server = Server()
    server.connections_status = 404
    assert box(server, ecdh=None) == []


def test_a_failing_connections_read_is_reported_and_the_vault_work_still_runs(box):
    server = Server()
    server.connections_status = 500
    server.vault = {"agents": [], "delegated": [_delegated_stub()]}
    results = box(server)
    assert len(results) == 2
    assert results[0].agent.startswith("a ") and results[1].kind == "connection" and results[1].error


def test_a_failing_connections_read_alone_is_reported_without_loading_a_key(box):
    server = Server()
    server.connections_status = 401
    results = box(server, ecdh=None)
    assert [r.kind for r in results] == ["connection"] and results[0].error


def test_a_damaged_connection_pin_file_stops_connections_and_not_the_vault_work(fake_web, box, _pins_file):
    _pins_file.write_text("{damaged")
    server = Server([fake_web])
    server.vault = {"agents": [], "delegated": [_delegated_stub()]}
    server.work = [fake_web.work(fake_web.waiting_for(MY_LAPTOP))]
    results = box(server)
    assert fake_web.puts == []
    assert results[0].agent.startswith("a ")
    assert results[-1].kind == "connection" and results[-1].error
    assert _pins_file.read_text() == "{damaged"


def test_a_laptop_gets_no_work_and_does_nothing(box, tmp_path):
    """canopy-web answers a laptop runner an empty list (`keeper.work`): nothing to open,
    no key loaded, no file written."""
    server = Server()
    assert box(server, ecdh=None) == []
    assert vault_keeper.render([]) == "nothing waiting for a vault key this runner holds"
    assert not (tmp_path / "vault-pins.json").exists()


def test_run_keep_revokes_through_the_provider_transport(fake_web, box, monkeypatch):
    calls = []

    def post_form(url, form, *, timeout):
        calls.append(url)
        return HttpResponse(200, {})

    monkeypatch.setattr(provider_base, "post_form", post_form)
    server = Server([fake_web])
    server.work = [fake_web.work(revoking=True, revoke_at_provider=True)]
    results = box(server)
    assert calls == ["https://login.salesforce.com/services/oauth2/revoke"]
    assert fake_web.confirmed == [{"revoked": True, "error": ""}]
    assert results[0].done


def test_render_names_a_connections_outcomes(fake_web):
    res = _keep(fake_web, fake_web.work(fake_web.waiting_for(MY_LAPTOP, STRANGER)))
    text = vault_keeper.render([res])
    assert text.startswith("connection salesforce") and "granted:" in text and "waiting:" in text


def test_load_pins_reads_what_the_keeper_saved(fake_web, _pins_file):
    _keep(fake_web, fake_web.work())
    assert vault_zk_cli.load_pins(trust.PINS_FILE)[CONN]["version"] == 3


def test_a_pin_on_disk_governs_a_pass_run_keep_starts(fake_web, box, _pins_file):
    _pins_file.write_text(json.dumps({CONN: {"owner_jkt": OWNER_JKT, "version": 8}}))
    server = Server([fake_web])
    server.work = [fake_web.work(fake_web.waiting_for(MY_LAPTOP))]
    results = box(server)
    assert fake_web.puts == [] and "statement-rollback" in results[0].error


@pytest.mark.parametrize("flag", ["false", 1, None])
def test_only_a_true_flag_means_revoking(fake_web, http, flag):
    work = fake_web.work(revoking=True, revoke_at_provider=True)
    work["revoking"] = flag
    _keep(fake_web, work, http=http)
    assert http.urls == [] and fake_web.confirmed == []


def test_what_canopy_web_says_in_a_refusal_is_not_repeated(fake_web):
    def refuses(method, path, body=None):
        raise canopy_web.CanopyError(f'{method} {path} -> 409: {{"error": "zk_value_changed", "detail": "{REFRESH}"}}')

    res = keep_connection(fake_web.work(fake_web.waiting_for(MY_LAPTOP)), pins={}, call=refuses, ecdh=BOX)
    assert "zk_value_changed" in res.error
    _assert_no_secret(_text(res))
