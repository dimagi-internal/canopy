"""Getting an access token (canopy-web spec §4.2, plan Task B4): reuse a live one, or take
the one lease, refresh at the provider, write back and only then hand it over.

``FakeWeb`` stands where canopy-web does and refuses what its ``tokens.write_back`` refuses,
so the envelopes under test are real ones. Where a test forges something, the attacker IS
canopy-web: everything but a sealed value and the signed statement is its word."""
from __future__ import annotations

import contextlib
import datetime as dt
import http.client as http_client
import json
import logging
import socket
import stat
import threading
import time

import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from orchestrator import canopy_web, cred_cli, device_key, runner_session
from orchestrator import vault_zk as zk
from orchestrator.connections import access, trust, wire
from orchestrator.connections.providers.base import HttpResponse
from orchestrator.cred_cli import CredError

CONN = "abcdef11-2222-4333-8444-555555555555"
ACCOUNT = "00D000000000001/005000000000001"
OWNER_ID = "7"
BINDING = wire.binding_id(CONN)
LEASE = "99999999-1111-4111-8111-000000000001"  # the first lease a FakeWeb hands out
RID, TID = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
OLD_ACCESS = "sf-access-OLD-never-printed"
NEW_ACCESS = "sf-access-NEW-never-printed"
REFRESH = "sf-refresh-SEALED-never-printed"
REFRESH_2 = "sf-refresh-SECOND-never-printed"
ROTATED = "sf-refresh-ROTATED-never-printed"
SECRET = "app-client-secret-never-printed"
SECRETS = (OLD_ACCESS, NEW_ACCESS, REFRESH, REFRESH_2, ROTATED, SECRET)
DELETE = f"/c/{CONN}/refresh-lease/{LEASE}"
PATHS = access.Paths(material="/m/salesforce/material", lease="/c/{id}/refresh-lease", sealed="/c/{id}/sealed",
                     reconnect="/c/{id}/reconnect-required")


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
BOX = _ecdh()          # this machine
PEER = _ecdh()         # another machine the owner lists, who wrote what is stored
AGENT_BOX = _ecdh()    # a machine the owner vouched for agent 12
STRANGER = _ecdh()     # a machine nobody vouched for


HOLDERS = [("member", OWNER_ID, OWNER_JWK), ("device", BOX.jkt, _public(BOX)), ("device", PEER.jkt, _public(PEER))]


def _refresh_plain(**over) -> dict:
    return {"v": 1, "provider": "salesforce", "refresh_token": REFRESH, "client_id": "client-1",
            "login_host": "login.salesforce.com", "instance_url": "https://acme.my.salesforce.com",
            "account_id": ACCOUNT, "owner_key": OWNER_JKT, "issued_at": 1793500000, "scopes": "api refresh_token",
            "statement_floor": 3, **over}


def _access_plain(token=OLD_ACCESS, expires_in=3600, **over) -> dict:
    return {"v": 1, "access_token": token, "expires_at": int(time.time()) + expires_in,
            "instance_url": "https://acme.my.salesforce.com", "account_id": ACCOUNT, "scope": "api",
            "owner_key": OWNER_JKT, "statement_floor": 3, **over}


def _row(key, kind="device", target=None) -> dict:
    return {"kind": kind, "target": target or key.jkt, "public_jwk": _public(key), "runner_name": "box"}


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _problem(method, path, status, error, **members):
    body = {"title": "Refused", "detail": f"{error}: canopy-web says no.", "error": error, **members}
    return canopy_web.CanopyError(f"{method} {path} -> {status}: {json.dumps(body)}")


class FakeWeb:
    """canopy-web's material, lease and write-back, with its refusals. The lease is
    `tokens.take_lease`'s: one holder, on the fake clock, a new id on takeover, and a write on
    an expired one accepted while nobody took it (R13)."""

    def __init__(self, clock=None, conn=CONN):
        self.clock = clock or Clock()
        self.conn, self.binding = conn, wire.binding_id(conn)
        self.order: list[str] = []
        self.polls = 0
        self.statement = {"account_id": ACCOUNT, "agents": ["12"], "connection": self.conn, "endorsers": [],
                          "my_computers": sorted([BOX.jkt, PEER.jkt]), "v": 1, "version": 3}
        self.signer = OWNER_KEY
        self.recipients = [{"kind": "member", "target": OWNER_ID, "public_jwk": OWNER_JWK, "runner_name": ""},
                           _row(BOX), _row(PEER)]
        self.provider = "salesforce"
        self.owner_jwk = OWNER_JWK
        self.endorsements: list[dict] = []
        self.client_id = "client-1"
        self.access_sealed, self.access_sig = "", None
        self.seal_refresh(_refresh_plain())
        self.lease_id = None
        self.lease_until = 0.0
        self.leases_issued = 0
        self.lease_written = False
        self.needs_reconnect = False
        self.ttl = 60
        self.lease_expires_at = "2030-01-01T00:00:00+00:00"
        self.lease_bodies: list[dict] = []
        self.deletes: list[str] = []
        self.reconnects: list[dict] = []
        self.puts: list[dict] = []
        self.put_failures: list = []
        self.lease_refusal = None
        self.material_refusal = None
        self.new_access_at_poll = None
        self.on_material = None
        self.on_lease = None
        self.on_put = None
        self.on_delete = None

    # ---- what is stored ------------------------------------------------------------

    def seal_refresh(self, plain, writer=PEER, recipients=None):
        self.sealed, self.sealed_sig = wire.seal_envelope(
            self.binding, wire.REFRESH_NAME, plain,
            recipients or HOLDERS,
            writer=writer, writer_target=writer.jkt, expect="")

    def seal_access(self, plain, writer=PEER, recipients=None):
        self.access_sealed, self.access_sig = wire.seal_envelope(
            self.binding, wire.ACCESS_NAME, plain,
            recipients or HOLDERS,
            writer=writer, writer_target=writer.jkt, expect="")

    def held_by_another(self) -> None:
        """Another machine (or another process here, without the id) holds the lease."""
        self.lease_id = "99999999-1111-4111-8111-00000000dead"
        self.lease_until = self.clock.now + 3600
        self.lease_written = False

    def lease_held_then_new_access(self, after_polls: int) -> None:
        self.held_by_another()
        self.new_access_at_poll = after_polls

    def material(self) -> dict:
        return {"id": self.conn, "provider": self.provider, "workspace": "dimagi", "binding": self.binding,
                "account_id": ACCOUNT, "account_label": "jj@dimagi.com",
                "sealed": self.sealed, "sealed_sig": self.sealed_sig, "sealed_digest": wire.digest(self.sealed),
                "access_sealed": self.access_sealed, "access_sig": self.access_sig, "access_expires_at": None,
                "statement": self.statement, "statement_sig": _sig(self.signer, wire.statement_msg(self.statement)),
                "owner": {"user_id": OWNER_ID, "public_jwk": self.owner_jwk},
                "endorsements": self.endorsements, "recipients": self.recipients,
                "app": {"client_id": self.client_id, "client_secret": SECRET, "options": {}},
                "lease": None, "rotates_refresh_token": True}

    # ---- the routes ----------------------------------------------------------------

    def __call__(self, method, path, body=None):
        if method == "GET" and path == PATHS.material:
            return self._get()
        if path == f"/c/{self.conn}/refresh-lease" and method == "POST":
            return self._lease(path, body)
        if path.startswith(f"/c/{self.conn}/refresh-lease/") and method == "DELETE":
            self.deletes.append(path)
            if self.on_delete:
                self.on_delete(self)
            if path.endswith(f"/{self.lease_id}"):
                self.lease_id = None
            return {}
        if path == f"/c/{self.conn}/sealed" and method == "PUT":
            return self._put(path, body)
        if path == f"/c/{self.conn}/reconnect-required" and method == "POST":
            self.reconnects.append(body)
            if body["lease_id"] != self.lease_id:
                raise _problem("POST", path, 409, "lease_lost")
            if body["error"] == "invalid_grant":
                self.needs_reconnect = True
            return {}
        raise AssertionError(f"unexpected {method} {path}")

    def _get(self):
        self.order.append("material")
        self.polls += 1
        if self.material_refusal:
            status, error = self.material_refusal
            raise _problem("GET", PATHS.material, status, error)
        if self.needs_reconnect:
            raise _problem("GET", PATHS.material, 409, "needs_reconnect")
        if self.new_access_at_poll is not None and self.polls == 1 + self.new_access_at_poll:
            self.seal_access(_access_plain(NEW_ACCESS, expires_in=1800))
        if self.on_material:
            self.on_material(self)
        return self.material()

    def _lease(self, path, body):
        self.lease_bodies.append(body)
        if body.get("lease_id") is None:
            self.order.append("lease")
        if self.on_lease:
            self.on_lease(self)
        if self.lease_refusal:
            raise _problem("POST", path, 409, self.lease_refusal)
        live = self.lease_id is not None and self.lease_until > self.clock.now
        same = self.lease_id is not None and body.get("lease_id") == self.lease_id
        if live and not same:
            raise _problem("POST", path, 409, "lease_held", retry_after=40)
        if not same:
            self.leases_issued += 1
            self.lease_id = f"99999999-1111-4111-8111-{self.leases_issued:012d}"
            self.lease_written = False
        self.lease_until = self.clock.now + self.ttl
        return {"lease_id": self.lease_id, "ttl_seconds": self.ttl, "expires_at": self.lease_expires_at}

    def _put(self, path, body):
        self.order.append("PUT sealed")
        self.puts.append(body)
        if self.on_put:
            self.on_put(self)
        if self.put_failures:
            failure = self.put_failures.pop(0)
            if isinstance(failure, BaseException):
                raise failure
            status, error = failure
            raise _problem("PUT", path, status, error)
        if body["lease_id"] != self.lease_id:
            raise _problem("PUT", path, 409, "lease_lost")
        if body["expect"] != wire.digest(self.sealed):
            raise _problem("PUT", path, 409, "zk_value_changed")
        if body["rotated"] != (body.get("sealed") is not None):
            raise _problem("PUT", path, 422, "rotation_mismatch")
        allowed = {(r["kind"], r["target"]) for r in self.recipients
                   if isinstance(r, dict) and isinstance(r["kind"], str) and isinstance(r["target"], str)}
        keep = {(e["kind"], e["target"]) for e in wire._sealed_entries(self.sealed)} & allowed
        checks = [(body["access_sealed"], body["access_sig"], wire.digest(self.access_sealed))]
        if body["rotated"]:
            checks.append((body["sealed"], body["sealed_sig"], wire.digest(self.sealed)))
        for value, sig, expect in checks:
            given = {(e["kind"], e["target"]) for e in wire._sealed_entries(value)}
            if given - allowed:
                raise _problem("PUT", path, 422, "foreign_reader")
            if keep - given:
                raise _problem("PUT", path, 422, "zk_rewrap_required")
            if sig["by"]["target"] != BOX.jkt or sig["expect"] != expect:
                raise _problem("PUT", path, 422, "bad_writer_sig")
        self.access_sealed, self.access_sig = body["access_sealed"], body["access_sig"]
        if body["rotated"]:
            self.sealed, self.sealed_sig = body["sealed"], body["sealed_sig"]
        self.lease_written = True
        return {"sealed_digest": wire.digest(self.sealed)}


class Http:
    """The provider, at the injected transport."""

    def __init__(self, web, clock):
        self.web, self.clock = web, clock
        self.refreshes = 0
        self.forms: list[dict] = []
        self.urls: list[str] = []
        self.status = 200
        self.answer = {"access_token": NEW_ACCESS, "scope": "api"}
        self.takes = 0.0
        self.error = None
        self.live_refresh_token = None  # set: any other token gets invalid_grant, as at Salesforce
        self.on_call = None

    def __call__(self, url, form, *, timeout):
        self.refreshes += 1
        self.forms.append(form)
        self.urls.append(url)
        self.web.order.append("refresh@provider")
        self.clock.now += self.takes
        if self.on_call:
            self.on_call()
        if self.error:
            raise self.error
        if self.live_refresh_token and form["refresh_token"] != self.live_refresh_token:
            return HttpResponse(400, {"error": "invalid_grant"})
        return HttpResponse(self.status, self.answer)


@pytest.fixture(autouse=True)
def _pins_file(monkeypatch, tmp_path):
    path = tmp_path / "connection-pins.json"
    monkeypatch.setattr(trust, "PINS_FILE", path)
    return path


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def fake_web(clock):
    return FakeWeb(clock)


@pytest.fixture
def http(fake_web, clock):
    return Http(fake_web, clock)


@pytest.fixture
def get_token(fake_web, http, clock):
    def run(**over):
        kw = {"provider": "salesforce", "min_life": 300, "call": fake_web, "ecdh": BOX, "pins": {},
              "sleep": clock.sleep, "monotonic": clock.monotonic, "http": http, **over}
        return access.access_token(PATHS, **kw)
    return run


def _open(value, sig, name, key=PEER):
    plain, _ = wire.open_envelope(value, sig, binding=BINDING, name=name, me_kind="device", me_target=key.jkt,
                                  ecdh=key)
    return plain


def _targets(value) -> set:
    return {(e["kind"], e["target"]) for e in wire._sealed_entries(value)}


# ── reuse ────────────────────────────────────────────────────────────────────────────

def test_a_fresh_access_token_is_used_without_a_lease(fake_web, http, get_token):
    fake_web.seal_access(_access_plain())
    out = get_token()
    assert fake_web.order == ["material"] and http.refreshes == 0
    assert set(out) == {"access_token", "instance_url", "expires_at", "account_label"}
    assert out["access_token"] == OLD_ACCESS
    assert out["instance_url"] == "https://acme.my.salesforce.com"
    assert out["account_label"] == "jj@dimagi.com"


def test_an_access_token_inside_min_life_is_not_reused(fake_web, http, get_token):
    fake_web.seal_access(_access_plain(expires_in=200))
    assert get_token(min_life=300)["access_token"] == NEW_ACCESS
    assert http.refreshes == 1


def test_force_refresh_skips_a_live_access_token(fake_web, http, get_token):
    fake_web.seal_access(_access_plain())
    assert get_token(refresh=True)["access_token"] == NEW_ACCESS
    assert http.refreshes == 1


# ── lease, refresh, write-back ───────────────────────────────────────────────────────

def test_refresh_takes_the_lease_writes_back_then_stages(fake_web, http, get_token):
    out = get_token()
    assert fake_web.order == ["material", "lease", "material", "refresh@provider", "PUT sealed"]
    assert fake_web.deletes == [DELETE] and fake_web.lease_id is None
    assert out["access_token"] == NEW_ACCESS
    assert http.urls == ["https://login.salesforce.com/services/oauth2/token"]
    assert http.forms[0]["refresh_token"] == REFRESH and http.forms[0]["client_secret"] == SECRET
    put = fake_web.puts[0]
    assert put["lease_id"] == LEASE and put["rotated"] is False and "sealed" not in put
    assert put["expect"] == wire.digest(fake_web.sealed)
    when = dt.datetime.fromisoformat(put["access_expires_at"])
    assert when.tzinfo is not None and int(when.timestamp()) == out["expires_at"]


def test_a_new_access_has_exactly_the_contracts_fields_and_the_refresh_floor(fake_web, get_token):
    fake_web.seal_refresh(_refresh_plain(statement_floor=2))
    fake_web.seal_access(_access_plain(statement_floor=1, expires_in=10))
    out = get_token()
    plain = _open(fake_web.puts[0]["access_sealed"], fake_web.puts[0]["access_sig"], wire.ACCESS_NAME)
    assert set(plain) == wire.ACCESS_KEYS
    assert plain["owner_key"] == OWNER_JKT and plain["statement_floor"] == 2
    assert plain["access_token"] == NEW_ACCESS and plain["expires_at"] == out["expires_at"]
    assert plain["account_id"] == ACCOUNT and plain["instance_url"] == "https://acme.my.salesforce.com"


def test_the_writer_macs_name_what_each_envelope_replaces(fake_web, http, get_token):
    fake_web.seal_access(_access_plain(expires_in=10))
    stored_access, stored_sealed = fake_web.access_sealed, fake_web.sealed
    http.answer = {"access_token": NEW_ACCESS, "refresh_token": ROTATED}
    get_token()
    put = fake_web.puts[0]
    assert put["access_sig"]["expect"] == wire.digest(stored_access)
    assert put["sealed_sig"]["expect"] == wire.digest(stored_sealed)


def test_the_first_access_ever_written_replaces_nothing(fake_web, get_token):
    get_token()
    assert fake_web.puts[0]["access_sig"]["expect"] == ""


def test_a_refresh_token_handed_back_unchanged_is_not_a_rotation(fake_web, http, get_token):
    http.answer = {"access_token": NEW_ACCESS, "refresh_token": REFRESH}
    get_token()
    assert fake_web.puts[0]["rotated"] is False and "sealed" not in fake_web.puts[0]


def test_rotated_refresh_token_is_written_back_before_it_is_relied_on(fake_web, http, get_token):
    http.answer = {"access_token": NEW_ACCESS, "refresh_token": ROTATED}
    seen_at_put = []
    fake_web.on_put = lambda web: seen_at_put.append(web.puts[-1])
    get_token()
    put = seen_at_put[0]
    assert put["rotated"] is True
    plain = _open(put["sealed"], put["sealed_sig"], wire.REFRESH_NAME)
    assert plain == {**_refresh_plain(), "refresh_token": ROTATED}  # the token is all that changes
    assert fake_web.sealed == put["sealed"]


def test_write_back_seals_to_every_allowed_recipient(fake_web, get_token):
    get_token(reseals_for_others=True)
    want = {("member", OWNER_ID), ("device", BOX.jkt), ("device", PEER.jkt)}
    assert _targets(fake_web.puts[0]["access_sealed"]) == want


def test_a_box_that_cannot_open_the_stored_access_refreshes_instead(fake_web, http, get_token):
    fake_web.seal_access(_access_plain(), recipients=[("device", PEER.jkt, _public(PEER))])
    assert get_token()["access_token"] == NEW_ACCESS
    assert http.refreshes == 1


def test_failed_write_back_retries_from_memory_and_stages_nothing_until_it_lands(fake_web, http, get_token):
    fake_web.put_failures = [(503, "unavailable"), OSError("no route"), (500, "boom")]
    out = get_token()
    assert out["access_token"] == NEW_ACCESS
    assert http.refreshes == 1 and fake_web.deletes == [DELETE]
    assert len(fake_web.puts) == 4
    assert fake_web.access_sealed == fake_web.puts[-1]["access_sealed"]


def test_nothing_rotated_stops_after_three_retries_and_frees_the_lease(fake_web, http, get_token):
    """The sealed token still works, so nobody need wait out the lease."""
    fake_web.put_failures = [(503, "unavailable")] * 4
    with pytest.raises(CredError) as e:
        get_token()
    assert len(fake_web.puts) == 4 and http.refreshes == 1
    assert fake_web.deletes == [DELETE]
    assert NEW_ACCESS not in str(e.value)


def _always_503(web):
    web.put_failures = [(503, "unavailable")]


def test_a_rotated_token_gets_a_time_budget_renewing_the_lease(fake_web, http, clock, get_token):
    http.answer = {"access_token": NEW_ACCESS, "refresh_token": ROTATED}
    fake_web.on_put = _always_503
    start = clock.now
    with pytest.raises(CredError) as e:
        get_token()
    assert clock.now - start == access.ROTATED_WRITE_SECONDS == 90
    assert len(fake_web.puts) > 4 and http.refreshes == 1
    assert clock.slept[:4] == [2, 4, 8, 15] and max(clock.slept) == access.RETRY_MAX_SECONDS
    assert {"lease_id": LEASE} in fake_web.lease_bodies
    # Still held: its expiry is what canopy-web records as `lease_expired_unwritten`.
    assert fake_web.deletes == [] and fake_web.lease_id == LEASE
    assert ROTATED not in str(e.value)


def test_a_rotated_token_lands_late_within_its_budget(fake_web, http, clock, get_token):
    http.answer = {"access_token": NEW_ACCESS, "refresh_token": ROTATED}
    start = clock.now
    fake_web.on_put = lambda web: _always_503(web) if clock.now - start < 75 else None
    assert get_token()["access_token"] == NEW_ACCESS
    assert _open(fake_web.sealed, fake_web.sealed_sig, wire.REFRESH_NAME)["refresh_token"] == ROTATED
    assert fake_web.deletes == [DELETE]


def test_a_missing_device_proof_is_retried_from_memory(fake_web, http, get_token):
    """`proof_headers` answers {} on a helper hiccup: one TPM, KMS or IAM blip."""
    http.answer = {"access_token": NEW_ACCESS, "refresh_token": ROTATED}
    fake_web.put_failures = [(401, "device_proof_required")] * 2
    assert get_token()["access_token"] == NEW_ACCESS
    assert http.refreshes == 1 and len(fake_web.puts) == 3
    assert _open(fake_web.sealed, fake_web.sealed_sig, wire.REFRESH_NAME)["refresh_token"] == ROTATED


def test_a_failed_release_does_not_fail_the_stage(fake_web, get_token):
    def down(web):
        raise OSError("no route")
    fake_web.on_delete = down
    assert get_token()["access_token"] == NEW_ACCESS
    fake_web.lease_id = None

    def refused(web):
        raise _problem("DELETE", "/x", 500, "boom")
    fake_web.on_delete = refused
    assert get_token(refresh=True)["access_token"] == NEW_ACCESS


# ── reading again under the lease (R16) ──────────────────────────────────────────────

def test_a_token_rotated_before_the_lease_is_not_refreshed_with(fake_web, http, get_token):
    """P reads RT1, H writes RT2, P takes the lease. RT1 is dead at the provider, and
    refreshing with it would mark a healthy connection needs_reconnect for every machine."""
    http.live_refresh_token = REFRESH_2

    def rotated_elsewhere(web):
        if not web.leases_issued:
            web.seal_refresh(_refresh_plain(refresh_token=REFRESH_2))
    fake_web.on_lease = rotated_elsewhere
    assert get_token()["access_token"] == NEW_ACCESS
    assert http.forms[0]["refresh_token"] == REFRESH_2
    assert fake_web.reconnects == [] and fake_web.needs_reconnect is False
    assert fake_web.puts[0]["expect"] == wire.digest(fake_web.sealed)


def test_a_newer_access_found_under_the_lease_is_returned_and_the_lease_freed(fake_web, http, get_token):
    fake_web.seal_access(_access_plain(expires_in=10))

    def landed_elsewhere(web):
        web.seal_refresh(_refresh_plain(refresh_token=REFRESH_2))
        web.seal_access(_access_plain(NEW_ACCESS, expires_in=1800))
    fake_web.on_lease = landed_elsewhere
    assert get_token()["access_token"] == NEW_ACCESS
    assert http.refreshes == 0 and fake_web.puts == []
    assert fake_web.order == ["material", "lease", "material"] and fake_web.deletes == [DELETE]


def test_a_reseal_found_under_the_lease_is_not_a_newer_access(fake_web, http, get_token):
    fake_web.seal_access(_access_plain(expires_in=3600))
    plain = _open(fake_web.access_sealed, fake_web.access_sig, wire.ACCESS_NAME)
    fake_web.on_lease = lambda web: web.seal_access(plain)
    assert get_token(refresh=True)["access_token"] == NEW_ACCESS
    assert http.refreshes == 1


def test_a_second_read_refused_gives_the_lease_back(fake_web, http, get_token):
    def revoke(web):
        web.material_refusal = (409, "revoking")
    fake_web.on_lease = revoke
    with pytest.raises(canopy_web.CanopyError, match="revoking"):
        get_token()
    assert http.refreshes == 0 and fake_web.deletes == [DELETE]


def test_a_second_read_that_fails_validation_gives_the_lease_back(fake_web, http, get_token):
    fake_web.on_lease = lambda web: web.seal_refresh(_refresh_plain(), writer=STRANGER)
    with pytest.raises(CredError, match="writer"):
        get_token()
    assert http.refreshes == 0 and fake_web.deletes == [DELETE]


def test_a_write_that_landed_though_its_answer_was_lost_is_not_a_failure(fake_web, http, get_token):
    def land_then_lose_the_answer(web):
        if len(web.puts) == 1:
            put = web.puts[0]
            web.access_sealed, web.access_sig = put["access_sealed"], put["access_sig"]
            web.sealed, web.sealed_sig = put["sealed"], put["sealed_sig"]
            raise OSError("connection reset")
    fake_web.on_put = land_then_lose_the_answer
    http.answer = {"access_token": NEW_ACCESS, "refresh_token": ROTATED}
    assert get_token()["access_token"] == NEW_ACCESS
    assert http.refreshes == 1 and len(fake_web.puts) == 2


def test_a_keeper_reseal_during_the_refresh_does_not_lose_a_rotated_token(fake_web, http, get_token):
    """The stored envelope moved under us: same token, re-sealed. The rotated one exists
    only in this process, so seal it again against what is stored now."""
    http.answer = {"access_token": NEW_ACCESS, "refresh_token": ROTATED}

    def reseal_once(web):
        if len(web.puts) == 1:
            web.seal_refresh(_refresh_plain(), writer=BOX)
    fake_web.on_put = reseal_once
    get_token()
    assert http.refreshes == 1 and len(fake_web.puts) == 2
    assert _open(fake_web.sealed, fake_web.sealed_sig, wire.REFRESH_NAME)["refresh_token"] == ROTATED


def test_a_new_share_during_the_refresh_is_sealed_under_its_floor_and_recipients(fake_web, http, get_token):
    def share_once(web):
        if len(web.puts) == 1:
            web.statement = {**web.statement, "version": 4}
            web.seal_refresh(_refresh_plain(statement_floor=4))
    fake_web.on_put = share_once
    get_token()
    plain = _open(fake_web.access_sealed, fake_web.access_sig, wire.ACCESS_NAME)
    assert plain["statement_floor"] == 4


def test_a_refresh_token_someone_else_replaced_meanwhile_is_a_conflict_not_an_overwrite(fake_web, http, get_token):
    http.answer = {"access_token": NEW_ACCESS, "refresh_token": ROTATED}

    def other_rotation(web):
        web.seal_refresh(_refresh_plain(refresh_token="someone-elses-new-token"))
    fake_web.on_put = other_rotation
    with pytest.raises(CredError):
        get_token()
    assert len(fake_web.puts) == 1


def test_a_lease_taken_over_during_the_refresh_is_not_written_through(fake_web, http, clock, get_token):
    """It ran out and another machine took it: renewal says so, and nothing is written."""
    http.takes = 70
    http.on_call = lambda: (fake_web.held_by_another())
    with pytest.raises(canopy_web.CanopyError, match="lease_held"):
        get_token()
    assert fake_web.puts == []


def _rotating(http):
    http.answer = {"access_token": NEW_ACCESS, "refresh_token": ROTATED}


def test_a_lost_answer_to_a_new_id_renewal_does_not_lose_a_rotated_token(fake_web, http, get_token):
    """canopy-web issued a new id and the answer never came, so this box is locked out of its
    own lease until that lapses, which the budget outlasts."""
    _rotating(http)
    http.takes = 70
    http.on_call = lambda: setattr(fake_web, "lease_id", None)
    real, lost = fake_web._lease, []

    def answer_lost(path, body):
        out = real(path, body)
        if body.get("lease_id") and not lost:
            lost.append(out["lease_id"])
            raise OSError("connection reset")
        return out
    fake_web._lease = answer_lost
    assert get_token()["access_token"] == NEW_ACCESS
    assert http.refreshes == 1
    assert _open(fake_web.sealed, fake_web.sealed_sig, wire.REFRESH_NAME)["refresh_token"] == ROTATED
    assert fake_web.puts[-1]["lease_id"] not in (LEASE, lost[0])


def test_a_real_holder_outlasting_the_budget_stops_a_rotated_token_with_no_write(fake_web, http, clock, get_token):
    _rotating(http)
    http.takes = 70
    http.on_call = fake_web.held_by_another
    start = clock.now
    with pytest.raises(CredError, match="could not write"):
        get_token()
    assert fake_web.puts == [] and clock.now - start >= access.ROTATED_WRITE_SECONDS


def test_a_real_holder_who_wrote_is_caught_by_expect(fake_web, http, clock, get_token):
    _rotating(http)
    http.takes = 70
    http.on_call = fake_web.held_by_another

    def holder_finishes(path, body, real=fake_web._lease):
        if clock.now > 1100:
            fake_web.lease_id = None
            fake_web.seal_refresh(_refresh_plain(refresh_token=REFRESH_2))
        return real(path, body)
    fake_web._lease = holder_finishes
    with pytest.raises(CredError, match="replaced"):
        get_token()
    assert len(fake_web.puts) == 1
    assert _open(fake_web.sealed, fake_web.sealed_sig, wire.REFRESH_NAME)["refresh_token"] == REFRESH_2


def test_lease_lost_with_no_lost_answer_stops_a_rotated_token(fake_web, http, get_token):
    """No renewal went astray, so this is a real holder."""
    _rotating(http)
    fake_web.on_put = lambda web: setattr(web, "lease_id", "99999999-1111-4111-8111-00000000beef")
    with pytest.raises(canopy_web.CanopyError, match="lease_lost"):
        get_token()
    assert len(fake_web.puts) == 1


def test_a_second_read_of_another_connection_refreshes_nothing(fake_web, http, clock, get_token):
    other_conn = "abcdef11-2222-4333-8444-666666666666"
    other = FakeWeb(clock, conn=other_conn)
    other.statement = {**other.statement, "connection": other_conn}

    def call(method, path, body=None):
        if method == "GET" and fake_web.leases_issued:
            return other.material()
        return fake_web(method, path, body)
    with pytest.raises(CredError, match="another connection"):
        get_token(call=call)
    assert http.refreshes == 0 and fake_web.puts == []
    assert fake_web.deletes == [DELETE]


def test_a_write_under_a_lease_taken_over_since_the_renewal_is_lost(fake_web, http, get_token):
    def taken(web):
        web.lease_id = "99999999-1111-4111-8111-00000000beef"
    fake_web.on_put = taken
    with pytest.raises(canopy_web.CanopyError, match="lease_lost"):
        get_token()
    assert len(fake_web.puts) == 1


def test_a_renewal_that_answers_a_new_lease_id_is_what_the_write_names(fake_web, http, get_token):
    """This box's lease ran out and was given up by whoever took it: it now holds the next."""
    http.takes = 70

    def lapsed(): fake_web.lease_id = None
    http.on_call = lapsed
    assert get_token()["access_token"] == NEW_ACCESS
    assert fake_web.puts[0]["lease_id"] != LEASE and fake_web.puts[0]["lease_id"] == f"{LEASE[:-1]}2"
    assert fake_web.deletes == [f"/c/{CONN}/refresh-lease/{fake_web.puts[0]['lease_id']}"]


def test_a_write_on_an_expired_lease_nobody_took_still_lands(fake_web, http, clock, get_token):
    """R13, with the renewal unable to reach canopy-web."""
    http.takes = 70
    real = fake_web._lease

    def flaky(path, body):
        if body.get("lease_id"):
            raise OSError("no route")
        return real(path, body)
    fake_web._lease = flaky
    assert get_token()["access_token"] == NEW_ACCESS


@pytest.mark.parametrize("error", ["lease_lost", "rotation_mismatch"])
def test_a_refused_write_is_not_retried_and_returns_nothing(fake_web, get_token, error):
    fake_web.put_failures = [(409, error)]
    with pytest.raises(canopy_web.CanopyError):
        get_token()
    assert len(fake_web.puts) == 1


def test_revoking_answer_to_the_write_back_returns_nothing(fake_web, get_token):
    fake_web.put_failures = [(409, "revoking")]
    with pytest.raises(canopy_web.CanopyError) as e:
        get_token()
    assert "revoking" in str(e.value) and len(fake_web.puts) == 1


# ── another holder ───────────────────────────────────────────────────────────────────

def test_second_process_waits_for_new_access_instead_of_refreshing(fake_web, http, clock, get_token):
    fake_web.lease_held_then_new_access(after_polls=2)
    out = get_token()
    assert fake_web.order == ["material", "lease", "material", "material"]
    assert out["access_token"] == NEW_ACCESS
    assert http.refreshes == 0 and fake_web.polls == 3
    assert clock.slept == [access.POLL_SECONDS] * 2


def test_a_waiter_gives_up_after_the_wait(fake_web, http, clock, get_token):
    fake_web.held_by_another()
    start = clock.now
    with pytest.raises(CredError, match="refresh in progress elsewhere"):
        get_token()
    assert http.refreshes == 0
    assert clock.now - start == access.WAIT_SECONDS
    assert access.WAIT_SECONDS == 30 and access.POLL_SECONDS == 2


def test_a_keeper_reseal_of_the_same_token_is_not_a_new_access(fake_web, http, get_token):
    """A re-seal changes the ciphertext and not the token: a caller asking for a new
    token after a 401 must not be handed the dead one."""
    fake_web.seal_access(_access_plain(expires_in=3600))
    plain = _open(fake_web.access_sealed, fake_web.access_sig, wire.ACCESS_NAME)
    fake_web.held_by_another()
    fake_web.on_material = lambda web: web.seal_access(plain) if web.polls == 2 else None
    with pytest.raises(CredError, match="refresh in progress elsewhere"):
        get_token(refresh=True)


def test_a_waiter_validates_what_it_waits_for(fake_web, http, get_token):
    fake_web.held_by_another()

    def forge(web):
        if web.polls == 2:
            web.seal_access(_access_plain(NEW_ACCESS), writer=STRANGER)
    fake_web.on_material = forge
    with pytest.raises(CredError, match="bad-writer|writer"):
        get_token()


def test_a_waiter_stops_when_the_connection_is_going_away(fake_web, clock, get_token):
    fake_web.held_by_another()

    def revoke(web):
        if web.polls == 1:
            web.material_refusal = (409, "revoking")
    fake_web.on_material = revoke
    with pytest.raises(canopy_web.CanopyError, match="revoking"):
        get_token()


# ── the lease's clock ────────────────────────────────────────────────────────────────

def test_lease_renewal_uses_monotonic_ttl(fake_web, http, clock, get_token):
    # expires_at says the lease has long gone (a box whose clock runs fast would believe
    # it); ttl_seconds and the monotonic clock say it has not.
    fake_web.lease_expires_at = "2000-01-01T00:00:00+00:00"
    http.takes = 5
    get_token()
    assert fake_web.lease_bodies == [{}]          # no renewal: 5 s of a 60 s lease

    fake2 = FakeWeb(clock)
    fake2.lease_expires_at = "2099-01-01T00:00:00+00:00"   # says plenty; the box has used 45 of 60 s
    http2 = Http(fake2, clock)
    http2.takes = 45
    get_token(call=fake2, http=http2)
    assert fake2.lease_bodies == [{}, {"lease_id": LEASE}]
    assert fake2.order == ["material", "lease", "material", "refresh@provider", "PUT sealed"]


def test_renewal_follows_the_ttl_the_server_answered(fake_web, http, clock, get_token):
    fake_web.ttl = 10
    http.takes = 6
    get_token()
    assert fake_web.lease_bodies == [{}, {"lease_id": LEASE}]


def test_retries_keep_the_lease_alive(fake_web, http, clock, get_token):
    fake_web.ttl = 4
    fake_web.put_failures = [(503, "unavailable"), (503, "unavailable")]
    get_token()
    assert fake_web.lease_bodies.count({"lease_id": LEASE}) == 2


def test_a_renewal_that_cannot_reach_canopy_web_does_not_stop_the_write(fake_web, http, get_token):
    """R13: a write on an expired lease is accepted while nobody took it."""
    http.takes = 45
    real = fake_web._lease

    def flaky(path, body):
        if body.get("lease_id"):
            raise OSError("no route")
        return real(path, body)
    fake_web._lease = flaky
    assert get_token()["access_token"] == NEW_ACCESS


# ── the provider says no ─────────────────────────────────────────────────────────────

def test_invalid_grant_reports_reconnect_required(fake_web, http, get_token):
    http.status = 400
    http.answer = {"error": "invalid_grant", "error_description": "expired"}
    with pytest.raises(CredError) as e:
        get_token()
    assert e.value.exit_code == cred_cli.EXIT_REFUSED
    assert fake_web.reconnects == [{"lease_id": LEASE, "error": "invalid_grant"}]
    assert fake_web.puts == []
    assert REFRESH not in str(e.value)
    with pytest.raises(canopy_web.CanopyError, match="needs_reconnect"):
        get_token()


def test_another_rejection_is_noted_not_a_reconnect_and_frees_the_lease(fake_web, http, get_token):
    http.status = 401
    http.answer = {"error": "invalid_client"}
    with pytest.raises(CredError) as e:
        get_token()
    assert e.value.exit_code == cred_cli.EXIT_ERROR
    assert fake_web.reconnects == [{"lease_id": LEASE, "error": "invalid_client"}]
    assert fake_web.deletes == [DELETE]


def test_a_provider_outage_is_not_a_reconnect_and_frees_the_lease(fake_web, http, get_token):
    http.status = 503
    http.answer = None
    with pytest.raises(CredError) as e:
        get_token()
    assert e.value.exit_code == cred_cli.EXIT_ERROR
    assert fake_web.reconnects == [] and fake_web.deletes == [DELETE]


def test_an_app_client_other_than_the_sealed_one_is_refused_before_any_call(fake_web, http, get_token):
    fake_web.client_id = "their-client"
    with pytest.raises(CredError, match="client"):
        get_token()
    assert http.refreshes == 0
    assert fake_web.deletes == [DELETE]


# ── who wrote it ─────────────────────────────────────────────────────────────────────

def test_forged_access_sealed_for_a_stranger_account_is_refused(fake_web, get_token):
    fake_web.seal_access(_access_plain(account_id="00Dstranger/005stranger"))
    with pytest.raises(CredError, match="access-mismatch"):
        get_token()
    # … and on another host, which the account check does not catch.
    fake_web.seal_access(_access_plain(instance_url="https://evil.example.com"))
    with pytest.raises(CredError, match="bad-host"):
        get_token()


def test_unsigned_envelope_is_refused_by_a_machine(fake_web, http, get_token):
    fake_web.sealed_sig = None
    with pytest.raises(CredError, match="unsigned"):
        get_token()
    fake_web = FakeWeb()
    fake_web.seal_access(_access_plain())
    fake_web.access_sig = None
    with pytest.raises(CredError, match="unsigned"):
        get_token(call=fake_web)
    assert http.refreshes == 0


def test_access_sealed_written_by_an_unlisted_device_is_refused(fake_web, http, get_token):
    fake_web.seal_access(_access_plain(), writer=STRANGER)
    with pytest.raises(CredError, match="writer"):
        get_token()
    assert http.refreshes == 0


def test_refresh_written_by_an_unlisted_device_is_refused(fake_web, http, get_token):
    fake_web.seal_refresh(_refresh_plain(), writer=STRANGER)
    with pytest.raises(CredError, match="writer"):
        get_token()
    assert http.refreshes == 0


def test_the_owner_key_comes_from_the_envelope_never_from_canopy_web(fake_web, get_token):
    # canopy-web names a key of its own and signs with it, over the real envelope.
    fake_web.owner_jwk, fake_web.signer = ATTACKER_JWK, ATTACKER_KEY
    with pytest.raises(CredError, match="bad-owner-key"):
        get_token()


def test_a_write_back_is_authenticated_for_the_next_reader(fake_web, http, get_token):
    get_token()
    # Another box reads what this one wrote, under the same rules.
    assert get_token(ecdh=PEER)["access_token"] == NEW_ACCESS
    assert http.refreshes == 1


def test_write_back_never_adds_a_recipient_the_statement_does_not_allow(fake_web, http, get_token):
    fake_web.recipients = [*fake_web.recipients, _row(STRANGER)]
    with pytest.raises(CredError, match="zk_unendorsed_recipient"):
        get_token(reseals_for_others=True)
    # Refused before a refresh could use up a rotating token, and the lease given back.
    assert http.refreshes == 0 and fake_web.puts == []
    assert fake_web.deletes == [DELETE]


def _vouch_for_agent_box(web):
    web.endorsements = [{
        "kind": "device", "target": AGENT_BOX.jkt, "key_jkt": AGENT_BOX.jkt, "by": OWNER_ID, "by_email": "o@x.com",
        "by_jkt": OWNER_JKT, "by_public_jwk": OWNER_JWK, "agent_id": "12",
        "sig": _sig(OWNER_KEY, zk.endorse_msg("12", "device", AGENT_BOX.jkt, AGENT_BOX.jkt).encode())}]
    web.recipients = [*web.recipients, _row(AGENT_BOX)]


def test_a_cloud_boxs_write_back_reaches_a_machine_still_waiting(fake_web, get_token):
    _vouch_for_agent_box(fake_web)
    get_token(reseals_for_others=True)
    assert _targets(fake_web.puts[0]["access_sealed"]) == {*_held(), ("device", AGENT_BOX.jkt)}


def _held() -> set:
    return {(kind, target) for kind, target, _ in HOLDERS}


def test_a_laptops_write_back_never_reseals_for_a_machine_still_waiting(fake_web, http, get_token):
    _vouch_for_agent_box(fake_web)
    http.answer = {"access_token": NEW_ACCESS, "refresh_token": ROTATED}
    get_token()
    put = fake_web.puts[0]
    assert _targets(put["access_sealed"]) == _held() and _targets(put["sealed"]) == _held()


def test_a_laptops_write_back_ignores_a_stranger_who_holds_nothing(fake_web, get_token):
    fake_web.recipients = [*fake_web.recipients, _row(STRANGER)]
    assert get_token()["access_token"] == NEW_ACCESS
    assert _targets(fake_web.puts[0]["access_sealed"]) == _held()


def test_a_laptops_write_back_drops_a_holder_nobody_lists_any_more(fake_web, get_token):
    fake_web.seal_refresh(_refresh_plain(), recipients=[*HOLDERS, ("device", STRANGER.jkt, _public(STRANGER))])
    assert get_token()["access_token"] == NEW_ACCESS
    assert _targets(fake_web.puts[0]["access_sealed"]) == _held()


def test_a_laptops_write_back_drops_a_listed_holder_the_statement_does_not_allow(fake_web, http, get_token):
    """It holds the envelope and canopy-web lists it, but the owner's word is no. Dropped, the
    write would fail canopy-web's `keep` and a rotated token could never land, so a laptop
    refuses before any provider call."""
    fake_web.seal_refresh(_refresh_plain(), recipients=[*HOLDERS, ("device", STRANGER.jkt, _public(STRANGER))])
    fake_web.recipients = [*fake_web.recipients, _row(STRANGER)]
    http.answer = {"access_token": NEW_ACCESS, "refresh_token": ROTATED}
    with pytest.raises(CredError, match="zk_unendorsed_recipient"):
        get_token()
    assert http.refreshes == 0 and fake_web.puts == []
    assert fake_web.deletes == [DELETE]


def test_a_laptop_refuses_a_listed_holder_shown_with_another_key(fake_web, http, get_token):
    fake_web.recipients = [fake_web.recipients[0], {**_row(BOX), "public_jwk": _public(STRANGER)},
                           fake_web.recipients[2]]
    with pytest.raises(CredError, match="zk_unendorsed_recipient"):
        get_token()
    assert http.refreshes == 0


@pytest.mark.parametrize("junk", [{"kind": ["x"], "target": "y"}, {"kind": "device", "target": {}}, "nope"])
def test_a_malformed_recipient_is_no_crash_on_a_laptop(fake_web, get_token, junk):
    fake_web.recipients = [*fake_web.recipients, junk]
    assert get_token()["access_token"] == NEW_ACCESS


def test_a_vouched_for_box_is_sealed_to_and_a_key_swap_is_not(fake_web, get_token):
    _vouch_for_agent_box(fake_web)
    get_token(reseals_for_others=True)
    assert ("device", AGENT_BOX.jkt) in _targets(fake_web.puts[0]["access_sealed"])

    swapped = {**_row(AGENT_BOX), "public_jwk": _public(STRANGER)}
    fake_web.recipients = [*fake_web.recipients[:-1], swapped]
    with pytest.raises(CredError, match="zk_unendorsed_recipient"):
        get_token(reseals_for_others=True, refresh=True)


# ── what was asked for ───────────────────────────────────────────────────────────────

def test_material_for_another_provider_is_refused(fake_web, http, get_token):
    fake_web.provider = "google"
    with pytest.raises(CredError, match="provider"):
        get_token()
    assert http.refreshes == 0


def test_a_sealed_value_of_another_provider_is_refused(fake_web, http, get_token):
    # canopy-web's label says salesforce; what is sealed does not.
    fake_web.seal_refresh(_refresh_plain(provider="google", login_host="", instance_url=""))
    with pytest.raises(CredError, match="provider"):
        get_token()
    assert http.refreshes == 0


def test_a_turn_refuses_a_statement_that_does_not_list_its_agent(fake_web, http, get_token):
    fake_web.seal_access(_access_plain())
    assert get_token(agent_id="12")["access_token"] == OLD_ACCESS
    with pytest.raises(CredError, match="agent"):
        get_token(agent_id="40")


# ── pins ─────────────────────────────────────────────────────────────────────────────

def _pins(path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def test_a_validated_read_saves_the_pin_even_with_no_write(fake_web, get_token, _pins_file):
    fake_web.seal_access(_access_plain())
    pins: dict = {}
    get_token(pins=pins)
    assert _pins(_pins_file) == {CONN: {"owner_jkt": OWNER_JKT, "version": 3}}
    assert pins == {CONN: {"owner_jkt": OWNER_JKT, "version": 3}}


@pytest.mark.parametrize("spoil", ["writer", "plain", "floor", "access-owner"])
def test_an_aborted_read_saves_no_pin(fake_web, get_token, _pins_file, spoil):
    if spoil == "writer":
        fake_web.seal_access(_access_plain(), writer=STRANGER)
    elif spoil == "plain":
        fake_web.seal_access(_access_plain(account_id="00Dother"))
    elif spoil == "floor":
        fake_web.seal_refresh(_refresh_plain(statement_floor=9))
    else:
        fake_web.seal_access(_access_plain(owner_key=zk.jwk_thumbprint(ATTACKER_JWK)))
    pins: dict = {}
    with pytest.raises(CredError):
        get_token(pins=pins)
    assert pins == {} and not _pins_file.exists()


def test_a_newer_pin_another_process_saved_is_kept(fake_web, get_token, _pins_file):
    fake_web.seal_access(_access_plain())
    _pins_file.write_text(json.dumps({CONN: {"owner_jkt": OWNER_JKT, "version": 5}, "other": {"owner_jkt": "x", "version": 1}}))
    get_token(pins={})           # read before that process saved
    assert _pins(_pins_file)[CONN]["version"] == 5
    assert "other" in _pins(_pins_file)


def test_saving_a_pin_keeps_what_another_process_added(fake_web, get_token, _pins_file):
    fake_web.seal_access(_access_plain())
    _pins_file.write_text(json.dumps({"other": {"owner_jkt": "x", "version": 1}}))
    get_token(pins={})
    assert set(_pins(_pins_file)) == {CONN, "other"}


def test_saved_pins_are_private_and_leave_no_temp_file(fake_web, get_token, _pins_file):
    fake_web.seal_access(_access_plain())
    get_token(pins={})
    assert stat.S_IMODE(_pins_file.stat().st_mode) == 0o600
    assert [p.name for p in _pins_file.parent.iterdir() if p.name.endswith(".tmp")] == []


def test_a_rolled_back_statement_is_refused(fake_web, get_token, http):
    with pytest.raises(CredError, match="statement-rollback"):
        get_token(pins={CONN: {"owner_jkt": OWNER_JKT, "version": 9}})
    assert http.refreshes == 0


# ── material refusals ────────────────────────────────────────────────────────────────

def test_material_404_is_not_connected_exit_3(fake_web, get_token):
    fake_web.material_refusal = (404, "gone")
    with pytest.raises(CredError) as e:
        get_token()
    assert e.value.exit_code == cred_cli.EXIT_REFUSED


def test_no_secret_in_any_message_or_log(fake_web, http, get_token, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    messages = []
    http.answer = {"access_token": NEW_ACCESS, "refresh_token": ROTATED}
    get_token()
    fake_web.put_failures = [(503, "unavailable")] * 4
    with pytest.raises(CredError) as e:
        get_token(refresh=True)
    messages.append(str(e.value))
    http.status, http.answer = 400, {"error": "invalid_grant", "error_description": f"bad {REFRESH} {SECRET}"}
    fake_web.put_failures = []
    with pytest.raises(CredError) as e:
        get_token(refresh=True)
    messages.append(str(e.value))
    out, err = capsys.readouterr()
    text = "\n".join([*messages, out, err, caplog.text])
    for secret in SECRETS:
        assert secret not in text


def test_a_read_repr_shows_no_token(fake_web, get_token):
    fake_web.seal_access(_access_plain())
    box = access._Box(PATHS, "salesforce", None, fake_web, BOX, {}, time.sleep, time.monotonic, None)
    text = repr(box.read())
    for secret in SECRETS:
        assert secret not in text


# ── the commands ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_home(monkeypatch, tmp_path, fake_web, http):
    """The real commands over FakeWeb: `canopy_web.call` is where it plugs in, with the paths
    the commands really use mapped onto the ones FakeWeb serves."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(cred_cli.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(cred_cli, "CACHE_DIR", home / ".canopy")
    monkeypatch.setattr(cred_cli, "CACHE_FILE", home / ".canopy" / "cred-cache.json")
    monkeypatch.setattr(cred_cli, "_human_token", lambda: "pat_human")
    monkeypatch.setattr(device_key, "load_ecdh", lambda: BOX)
    monkeypatch.setattr(device_key, "proof_headers", lambda method, url: {"DPoP": f"proof {method} {url}"})
    monkeypatch.setattr(runner_session, "token", lambda *a, **k: "")
    monkeypatch.setattr("orchestrator.connections.providers.base.post_form", http)
    monkeypatch.setattr(access, "RETRY_SECONDS", 0)
    for name in ("CANOPY_TURN_ID", "CANOPY_AGENT_SLUG", "CANOPY_AGENT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    calls: list[dict] = []
    fake_web.calls = calls

    def fake_call(method, path, body=None, **kw):
        calls.append({"method": method, "path": path, "token": kw.get("token"), "headers": kw.get("headers")})
        if path == "/api/agents/eva/":
            return {"id": 12, "slug": "eva"}
        mapped = (path.replace("/api/me/connections/salesforce/material", PATHS.material)
                  .replace(f"/api/harness/runners/{RID}/turns/{TID}/connections/salesforce", PATHS.material)
                  .replace("?workspace=dimagi", ""))
        for prefix in ("/api/me/connections/", f"/api/harness/runners/{RID}/connections/"):
            mapped = mapped.replace(prefix, "/c/")
        return fake_web(method, mapped, body)

    monkeypatch.setattr(canopy_web, "call", fake_call)
    return home


def _stage(env=None, *args):
    return CliRunner().invoke(cred_cli.cred_group,
                              ["connection-token", "salesforce", "--runner", "--agent", "eva", "--runner-id", RID,
                               "--turn", TID, *args], env={"CANOPY_TOKEN": "crt_runner", **(env or {})})


def _person(*args):
    return CliRunner().invoke(cred_cli.cred_group, ["connection-token", "salesforce", *args])


def test_a_cloud_stage_reaches_waiting_machines_and_a_person_does_not(fake_web, tmp_home):
    _vouch_for_agent_box(fake_web)
    assert _person().exit_code == 0
    assert ("device", AGENT_BOX.jkt) not in _targets(fake_web.puts[-1]["access_sealed"])
    assert _stage(None, "--refresh").exit_code == 0
    assert ("device", AGENT_BOX.jkt) in _targets(fake_web.puts[-1]["access_sealed"])


def test_a_broken_answer_is_a_lost_connection_with_no_traceback_or_content(monkeypatch):
    for broken in (http_client.IncompleteRead(SECRET.encode()), http_client.BadStatusLine(SECRET)):
        def urlopen(req, timeout, broken=broken):
            raise broken
        monkeypatch.setattr("urllib.request.urlopen", urlopen)
        with pytest.raises(OSError) as e:
            cred_cli._timeout_transport(1)("GET", "https://x/", {}, None)
        assert SECRET not in str(e.value) and e.value.__suppress_context__
        assert access._transient(e.value)


@pytest.mark.parametrize("status_line", ["503 Service Unavailable", "200 OK", "409 Conflict"])
def test_an_answer_cut_short_on_a_real_socket_is_a_lost_connection(status_line):
    """An error's body can break off too: `e.read()` runs in the HTTPError handler."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def serve():
        conn, _ = server.accept()
        conn.settimeout(3)
        conn.recv(4096)
        conn.sendall(f"HTTP/1.1 {status_line}\r\nContent-Length: 100\r\n\r\n{SECRET}".encode())
        # End the answer early with a clean FIN, and read on: closing on unread request bytes
        # would be a reset, which never reaches the short body.
        conn.shutdown(socket.SHUT_WR)
        with contextlib.suppress(OSError):
            while conn.recv(4096):
                pass
        conn.close()
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        with pytest.raises(OSError) as e:
            cred_cli._timeout_transport(3)("PUT", f"http://127.0.0.1:{server.getsockname()[1]}/x", {}, b"{}")
    finally:
        thread.join(3)
        server.close()
    assert SECRET not in str(e.value) and "IncompleteRead" in str(e.value)
    assert access._transient(e.value)


def test_staged_file_format_is_unchanged(fake_web, tmp_home):
    res = _stage()
    path = tmp_home / ".canopy" / "connections" / "eva" / "salesforce.json"
    assert res.exit_code == 0, res.output
    assert res.output.strip() == str(path)
    assert set(json.loads(path.read_text())) == {"access_token", "instance_url", "expires_at", "account_label"}
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert NEW_ACCESS not in res.output


def test_the_runner_uses_its_own_routes_token_and_proof(fake_web, tmp_home):
    _stage()
    material = next(c for c in fake_web.calls if c["path"].endswith("/connections/salesforce"))
    assert material["path"] == f"/api/harness/runners/{RID}/turns/{TID}/connections/salesforce"
    assert material["token"] == "crt_runner"
    assert material["headers"]["DPoP"].startswith(f"proof GET https://canopy.dimagi.com{material['path']}")
    routes = [c["path"] for c in fake_web.calls if "/refresh-lease" in c["path"] or c["path"].endswith("/sealed")]
    assert routes == [f"/api/harness/runners/{RID}/connections/{CONN}/refresh-lease",
                      f"/api/harness/runners/{RID}/connections/{CONN}/sealed",
                      f"/api/harness/runners/{RID}/connections/{CONN}/refresh-lease/{LEASE}"]


def test_a_turn_checks_its_own_agent_against_the_statement(fake_web, tmp_home):
    fake_web.statement = {**fake_web.statement, "agents": ["40"]}
    res = _stage()
    assert res.exit_code == cred_cli.EXIT_ERROR
    assert not (tmp_home / ".canopy" / "connections" / "eva" / "salesforce.json").exists()


@pytest.mark.parametrize("error", ["not_connected", "waiting", "revoking"])
def test_runner_material_refusals_are_silent_exit_3(fake_web, tmp_home, error):
    fake_web.material_refusal = (409, error)
    assert _stage().exit_code == cred_cli.EXIT_REFUSED


def test_needs_reconnect_is_not_silent(fake_web, tmp_home):
    fake_web.material_refusal = (409, "needs_reconnect")
    res = _stage()
    assert res.exit_code == cred_cli.EXIT_ERROR and "needs_reconnect" in res.output


def test_revoking_answer_stages_nothing(fake_web, tmp_home):
    fake_web.put_failures = [(409, "revoking")]
    res = _stage()
    assert res.exit_code == cred_cli.EXIT_REFUSED
    assert not (tmp_home / ".canopy" / "connections" / "eva" / "salesforce.json").exists()
    assert NEW_ACCESS not in res.output


def test_a_lease_revoking_answer_stages_nothing(fake_web, http, tmp_home):
    fake_web.lease_refusal = "revoking"
    res = _stage()
    assert res.exit_code == cred_cli.EXIT_REFUSED and http.refreshes == 0
    assert not (tmp_home / ".canopy" / "connections" / "eva" / "salesforce.json").exists()


def test_a_refused_write_stages_nothing_and_a_landed_one_does(fake_web, tmp_home):
    path = tmp_home / ".canopy" / "connections" / "eva" / "salesforce.json"
    fake_web.put_failures = [(503, "unavailable")] * 4
    assert _stage().exit_code == cred_cli.EXIT_ERROR and not path.exists()
    fake_web.put_failures = []
    assert _stage().exit_code == 0 and path.exists()


def test_a_lost_connection_to_canopy_web_is_an_error_not_a_crash(monkeypatch, tmp_home):
    def down(*a, **k):
        raise OSError("network down")
    monkeypatch.setattr(canopy_web, "call", down)
    res = CliRunner().invoke(cred_cli.cred_group, ["connection-token", "salesforce"])
    assert res.exit_code == cred_cli.EXIT_ERROR and isinstance(res.exception, SystemExit)
    assert "could not reach canopy-web" in res.output


def test_the_person_path_uses_their_pat_and_writes_their_cache(fake_web, tmp_home):
    res = _person()
    path = tmp_home / ".canopy" / "connections" / "salesforce.json"
    assert res.exit_code == 0, res.output
    assert res.output.strip() == str(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    material = fake_web.calls[0]
    assert material["path"] == "/api/me/connections/salesforce/material"
    assert material["token"] == "pat_human" and material["headers"]["DPoP"].startswith("proof GET ")
    assert [c["path"] for c in fake_web.calls[1:]] == [f"/api/me/connections/{CONN}/refresh-lease",
                                                       "/api/me/connections/salesforce/material",
                                                       f"/api/me/connections/{CONN}/sealed",
                                                       f"/api/me/connections/{CONN}/refresh-lease/{LEASE}"]


def test_the_person_path_names_its_workspace(fake_web, tmp_home):
    res = _person("--workspace", "dimagi")
    assert res.exit_code == 0, res.output
    assert fake_web.calls[0]["path"] == "/api/me/connections/salesforce/material?workspace=dimagi"
    assert res.output.strip().endswith("salesforce@dimagi.json")


def test_person_path_without_an_enrolled_key_says_how_to_enroll(monkeypatch, fake_web, tmp_home):
    monkeypatch.setattr(device_key, "load_ecdh", lambda: None)
    with pytest.raises(CredError) as err:
        cred_cli.connection_token("salesforce")
    assert "canopy vault device --enroll" in str(err.value)
    assert err.value.exit_code == cred_cli.EXIT_USAGE
    assert fake_web.order == []


@pytest.mark.parametrize("error", ["machine_not_enrolled", "device_proof_required"])
def test_a_machine_canopy_web_does_not_know_says_how_to_enroll(fake_web, tmp_home, error):
    fake_web.material_refusal = (403, error)
    res = _person()
    assert res.exit_code == cred_cli.EXIT_USAGE
    assert "canopy vault device --enroll" in res.output


def test_person_path_refused_when_use_on_my_computers_is_off(fake_web, tmp_home, monkeypatch):
    def refuse(method, path, body=None, **kw):
        raise canopy_web.CanopyError(
            f"{method} {path} -> 403: " + json.dumps({
                "error": "not_on_my_computers",
                "detail": "Turn on Use on my computers for jj@dimagi.com in Settings → Connections."}))
    monkeypatch.setattr(canopy_web, "call", refuse)
    res = _person()
    assert res.exit_code == cred_cli.EXIT_USAGE
    assert "Turn on Use on my computers for jj@dimagi.com" in res.output
    assert not (tmp_home / ".canopy" / "connections" / "salesforce.json").exists()


def test_the_person_cache_is_reused_without_the_network(fake_web, tmp_home):
    assert _person().exit_code == 0
    seen = len(fake_web.calls)
    assert _person().exit_code == 0
    assert len(fake_web.calls) == seen
    assert _person("--refresh").exit_code == 0
    assert len(fake_web.calls) > seen
