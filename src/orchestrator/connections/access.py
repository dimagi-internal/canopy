"""Getting a connection's access token on this machine (canopy-web spec §4.2, contract §0.6
routes 7-12). canopy-web never sees a token: it hands over sealed envelopes, this box opens
them with its own key, and everything is judged against the owner's signed statement.

Reuse a live access token, or take the one refresh lease, read again, refresh at the
provider, write back re-sealed envelopes, give the lease up, and only then hand the token
over. Four rules shape the code:

* **A whole read before any use** (``_Box.read``, in the order ``connections.trust`` gives):
  a poll is a read like any other, and a pin is saved only after a validated one.
* **Read again once the lease is held (R16).** A read from before the lease can hold a
  refresh token an earlier holder has already rotated away; refreshing with it would get
  ``invalid_grant`` and mark a healthy connection ``needs_reconnect``. Once this box holds
  the lease, no earlier holder can land a rotation.
* **The stored copy before this process's copy.** A rotating provider (Salesforce) has just
  killed the old refresh token, so the new one exists only in this process until the
  write-back lands. A failed write keeps the lease and retries from memory, for a time
  budget when a rotated token is at stake (R17); a refused one returns nothing. When the
  stored envelope moved underneath us (a keeper re-seal, a new share), the tokens are sealed
  again against what is stored now rather than dropped.
* **Lease timing on this box's monotonic clock from ``ttl_seconds``,** never from
  ``expires_at``, because this box's wall clock may be wrong.

A CanopyError from canopy-web (a refusal) propagates for the caller to word: it knows
whether it is a runner or a person. Everything refused HERE is a CredError. Neither ever
carries a token, and nothing here logs.
"""
from __future__ import annotations

import datetime as dt
import time
import types
from dataclasses import dataclass, field

from orchestrator import canopy_web
from orchestrator import vault_zk as zk
from orchestrator import vault_zk_cli
from orchestrator.connections import providers, trust, wire
from orchestrator.cred_cli import EXIT_REFUSED, CredError, _error_code, _status_of

WAIT_SECONDS = 30
POLL_SECONDS = 2
#: How many times a write-back is tried again after the first attempt, nothing rotated: the
#: refresh token still works, so a later refresher loses nothing.
WRITE_RETRIES = 3
#: How long a write-back keeps trying when it holds the only copy of a rotated token (R17).
ROTATED_WRITE_SECONDS = 90
RETRY_SECONDS = 2
RETRY_MAX_SECONDS = 15
#: canopy-web's limit on what `reconnect-required` takes as an error code.
_ERROR_MAX = 100
INVALID_GRANT = "invalid_grant"
#: The stored connection moved since it was read: read it again and seal against that.
_STALE = frozenset({"zk_value_changed", "zk_rewrap_required", "foreign_reader", "bad_writer_sig"})


@dataclass
class Paths:
    """The routes of one caller (a runner's turn, or a person's own CLI). All but ``material``
    hold ``{id}``: a connection's id is known only once its material has been read."""

    material: str
    lease: str
    sealed: str
    reconnect: str


@dataclass
class _Read:
    """One validated read. ``statement`` and ``owner_jkt`` are what the plaintexts were
    checked under; ``access`` is None when there is none this box could open."""

    material: dict = field(repr=False)
    refresh: dict = field(repr=False)
    access: dict | None = field(repr=False)
    statement: dict
    owner_jkt: str

    @property
    def connection_id(self) -> str:
        return self.statement["connection"]  # signed, and a canonical uuid


@dataclass
class _Lease:
    id: str
    ttl: float
    taken_at: float  # monotonic


def _refused(e: trust.TrustError | wire.WireError) -> CredError:
    return CredError(f"refused this connection's material ({e})")


def _transient(e: Exception) -> bool:
    """Says nothing about the connection: canopy-web could not answer, so try again."""
    if isinstance(e, canopy_web.CanopyError):
        status = _status_of(e)
        # A proof refused for want of a proof: `proof_headers` answers {} on a helper hiccup
        # (TPM, KMS, instance role), so one blip must not discard a rotated token (R17).
        return status == 0 or status == 429 or status >= 500 or _error_code(e) == "device_proof_required"
    return isinstance(e, (OSError, ValueError))


def _rotated(tokens: providers.Tokens, refresh: dict) -> bool:
    return bool(tokens.refresh_token) and tokens.refresh_token != refresh["refresh_token"]


def _newer(read: _Read, seen: int | None) -> bool:
    """Is what this read holds a new token? New means a later expiry than ``seen``, because
    a keeper re-seal changes the ciphertext and not the token. Whatever its life, it is the
    newest there is."""
    a = read.access
    return bool(a) and (seen is None or a["expires_at"] > seen) and a["expires_at"] > time.time()


def _expiry(read: _Read) -> int | None:
    return read.access["expires_at"] if read.access else None


def _answer_from(read: _Read) -> dict:
    a = read.access
    return _answer(a["access_token"], a["expires_at"], a["instance_url"], read.material)


def _answer(token: str, expires_at: int, instance_url: str, material: dict) -> dict:
    label = material.get("account_label")
    return {"access_token": token, "instance_url": instance_url, "expires_at": expires_at,
            "account_label": label if isinstance(label, str) else ""}


def _save_pin(statement: dict, owner_jkt: str, pins: dict) -> None:
    """Remember what a validated read saw, on the file as it is NOW: two processes on one
    box can both be reading. Another read that got further stands."""
    trust.record_pin(statement, owner_jkt=owner_jkt, pins=pins)
    try:
        on_disk = vault_zk_cli.load_pins(trust.PINS_FILE)
        before = on_disk.get(statement["connection"])
        try:
            trust.record_pin(statement, owner_jkt=owner_jkt, pins=on_disk)
        except trust.TrustError as e:
            if e.code == "statement-rollback":
                return
            raise
        if on_disk[statement["connection"]] != before:
            vault_zk_cli.save_pins(on_disk, trust.PINS_FILE)
    except (vault_zk_cli.ZkCliError, OSError) as e:
        raise CredError(f"could not record this connection's pin in {trust.PINS_FILE} "
                        f"({type(e).__name__})") from None


@dataclass
class _Box:
    """This machine asking for one provider's connection."""

    paths: Paths
    provider: str
    agent_id: str | None
    call: object
    ecdh: object
    pins: dict
    sleep: object
    monotonic: object
    http: object
    #: A cloud box re-seals for machines still waiting; a laptop never does (R18, §3).
    reseals_for_others: bool = False

    # ---- reading ---------------------------------------------------------------------

    def read(self) -> _Read:
        try:
            material = self.call("GET", self.paths.material)
        except canopy_web.CanopyError as e:
            if _status_of(e) == 404:
                raise CredError(f"no {self.provider} connection here: canopy-web knows of none",
                                EXIT_REFUSED) from None
            raise
        try:
            return self._validated(material)
        except (trust.TrustError, wire.WireError, zk.ZkError) as e:
            raise _refused(e) from None

    def _open(self, material: dict, value_field: str, sig_field: str, name: str) -> tuple[dict, dict]:
        return wire.open_envelope(material.get(value_field), material.get(sig_field),
                                  binding=material.get("binding"), name=name, me_kind="device",
                                  me_target=self.ecdh.jkt, ecdh=self.ecdh)

    def _validated(self, material) -> _Read:
        """The order is `connections.trust`'s: nothing is used before the last check."""
        if not isinstance(material, dict):
            raise trust.TrustError("bad-material", "canopy-web's answer is not an object")
        if material.get("provider") != self.provider:
            raise trust.TrustError("wrong-provider", f"this is not the {self.provider} connection asked for")
        refresh, by = self._open(material, "sealed", "sealed_sig", wire.REFRESH_NAME)
        owner_jkt = refresh.get("owner_key")  # never canopy-web's word
        statement = trust.verified_statement(material, owner_jkt=owner_jkt, pins=self.pins)
        if not trust.writer_allowed(by, material, statement, owner_jkt=owner_jkt):
            raise trust.TrustError("bad-writer", "the refresh envelope's writer is not one the statement allows")
        access = self._open_access(material, statement, owner_jkt)
        trust.check_plain(refresh, access, statement)
        if refresh["provider"] != self.provider:
            raise trust.TrustError("wrong-provider", f"what is sealed is not a {self.provider} connection")
        if self.agent_id is not None and self.agent_id not in statement["agents"]:
            raise trust.TrustError("agent-not-listed", "the owner's statement does not lend this to this agent")
        _save_pin(statement, owner_jkt, self.pins)
        return _Read(material, refresh, access, statement, owner_jkt)

    def _open_access(self, material: dict, statement: dict, owner_jkt: str) -> dict | None:
        if not material.get("access_sealed"):
            return None
        try:
            access, by = self._open(material, "access_sealed", "access_sig", wire.ACCESS_NAME)
        except wire.WireError as e:
            if e.code == "not-for-me":  # not checked, so never used: this box refreshes instead
                return None
            raise
        if not trust.writer_allowed(by, material, statement, owner_jkt=owner_jkt):
            raise trust.TrustError("bad-writer", "the access envelope's writer is not one the statement allows")
        return access

    # ---- another holder --------------------------------------------------------------

    def wait_for_new_access(self, first: _Read) -> _Read:
        """Someone else is refreshing: a parallel refresh would kill a rotated token, so wait
        for what they write."""
        seen = _expiry(first)
        deadline = self.monotonic() + WAIT_SECONDS
        for _ in range(WAIT_SECONDS // POLL_SECONDS):
            if self.monotonic() >= deadline:
                break
            self.sleep(POLL_SECONDS)
            try:
                read = self.read()
            except (canopy_web.CanopyError, OSError, ValueError) as e:
                if _transient(e):
                    continue
                raise
            if _newer(read, seen):
                return read
        raise CredError("refresh in progress elsewhere")

    # ---- the lease -------------------------------------------------------------------

    def _path(self, template: str, read: _Read) -> str:
        return template.replace("{id}", read.connection_id)

    def take_lease(self, read: _Read) -> _Lease | None:
        """The one lease, or None while another holds it."""
        taken_at = self.monotonic()
        try:
            out = self.call("POST", self._path(self.paths.lease, read), {})
        except canopy_web.CanopyError as e:
            if _error_code(e) == "lease_held":
                return None
            raise
        lease_id = _lease_id(out)
        if not lease_id:
            raise CredError("canopy-web answered a refresh lease this CLI cannot read")
        return _Lease(lease_id, _ttl(out), taken_at)

    def renew_if_due(self, lease: _Lease, read: _Read) -> None:
        """Past half its life by THIS box's monotonic clock. canopy-web accepts a write on an
        expired lease nobody took (R13), so failing to reach it here stops nothing; another
        machine holding it, or the connection going away, does. A new id means this lease
        ran out and this box has just been given the next one: it is what a write must name,
        and ``expect`` still catches anyone who wrote in between."""
        if self.monotonic() - lease.taken_at < lease.ttl / 2:
            return
        taken_at = self.monotonic()
        try:
            out = self.call("POST", self._path(self.paths.lease, read), {"lease_id": lease.id})
        except (canopy_web.CanopyError, OSError, ValueError) as e:
            if _transient(e):
                return
            raise
        lease.ttl, lease.taken_at = _ttl(out, default=lease.ttl), taken_at
        lease.id = _lease_id(out) or lease.id

    def _quietly(self, method: str, path: str, body=None) -> None:
        """A courtesy to canopy-web: failing it must not hide why we are here."""
        try:
            self.call(method, path, body)
        except (canopy_web.CanopyError, OSError, ValueError):
            pass

    def release(self, lease: _Lease, read: _Read) -> None:
        self._quietly("DELETE", f"{self._path(self.paths.lease, read)}/{lease.id}")

    # ---- the provider ----------------------------------------------------------------

    def refreshed(self, read: _Read, lease: _Lease) -> providers.Tokens:
        provider = providers.get(self.provider)
        try:
            return provider.refresh(read.refresh, read.material.get("app"), http=self.http)
        except providers.ProviderError as e:
            reconnect = self._path(self.paths.reconnect, read)
            if e.code == "rejected" and e.provider_code == INVALID_GRANT:
                self._quietly("POST", reconnect, {"lease_id": lease.id, "error": INVALID_GRANT})
                raise CredError(f"{provider.title} refused this connection's refresh token: its owner must "
                                "connect it again in canopy-web", EXIT_REFUSED) from None
            if e.code == "rejected" and e.provider_code:
                self._quietly("POST", reconnect, {"lease_id": lease.id, "error": e.provider_code[:_ERROR_MAX]})
            self.release(lease, read)
            raise CredError(e.detail or e.code) from None

    # ---- writing back ----------------------------------------------------------------

    def _recipients(self, read: _Read) -> list[tuple[str, str, dict]]:
        """Who this write seals to. A cloud box: everyone canopy-web lists, each of whom the
        statement must allow, else nobody, since what it lists is never something a box would
        refuse (§0.5). A laptop: who holds the envelope now, narrowed by what the statement
        allows, never a machine still waiting (R18)."""
        res = types.SimpleNamespace(skipped=[])
        allowed = trust.allowed(read.material, read.statement, owner_jkt=read.owner_jkt, res=res)
        if self.reseals_for_others:
            rows = trust.refuse_unallowed(read.material.get("recipients"), allowed)
        else:
            held = {(e["kind"], e["target"]) for e in wire._sealed_entries(read.material["sealed"])}
            rows = [(kind, target, jwk) for (kind, target), jwk in allowed.items() if (kind, target) in held]
        if not 1 <= len(rows) <= wire.MAX_LISTED:
            raise trust.TrustError(trust.UNENDORSED, f"an envelope has 1-{wire.MAX_LISTED} recipients")
        return rows

    def _checked_recipients(self, read: _Read, lease: _Lease) -> None:
        """Before the refresh: a rotating provider's refresh cannot be taken back."""
        try:
            self._recipients(read)
        except (trust.TrustError, wire.WireError) as e:
            self.release(lease, read)
            raise _refused(e) from None

    def _body(self, read: _Read, tokens: providers.Tokens, lease: _Lease) -> dict:
        m, refresh = read.material, read.refresh
        access = {"v": 1, "access_token": tokens.access_token, "expires_at": tokens.expires_at,
                  "instance_url": refresh["instance_url"], "account_id": refresh["account_id"],
                  "scope": tokens.scope, "owner_key": refresh["owner_key"],
                  "statement_floor": refresh["statement_floor"]}
        rotated = _rotated(tokens, refresh)
        expect = wire.digest(m["sealed"])
        try:
            rows = self._recipients(read)
            value, sig = self._seal(m, wire.ACCESS_NAME, access, rows, wire.digest(m.get("access_sealed") or ""))
            body = {"lease_id": lease.id, "access_sealed": value, "access_sig": sig, "expect": expect,
                    "access_expires_at": dt.datetime.fromtimestamp(tokens.expires_at, dt.UTC).isoformat(),
                    "rotated": rotated}
            if rotated:
                body["sealed"], body["sealed_sig"] = self._seal(
                    m, wire.REFRESH_NAME, {**refresh, "refresh_token": tokens.refresh_token}, rows, expect)
        except (trust.TrustError, wire.WireError) as e:
            raise _refused(e) from None
        return body

    def _seal(self, m: dict, name: str, plain: dict, rows: list, expect: str) -> tuple[str, dict]:
        return wire.seal_envelope(m["binding"], name, plain, rows, writer=self.ecdh, writer_target=self.ecdh.jkt,
                                  expect=expect)

    def _attempts(self, *, rotated: bool):
        """One more try each time round, after a pause: for a time budget, backing off, when
        a rotated token exists only here; else a few times."""
        yield
        if not rotated:
            for _ in range(WRITE_RETRIES):
                self.sleep(RETRY_SECONDS)
                yield
            return
        deadline = self.monotonic() + ROTATED_WRITE_SECONDS
        pause = RETRY_SECONDS
        while (left := deadline - self.monotonic()) > 0:
            self.sleep(min(pause, left))
            pause = min(pause * 2, RETRY_MAX_SECONDS)
            yield

    def write_back(self, read: _Read, tokens: providers.Tokens, lease: _Lease) -> dict:
        """Return only once what the refresh produced is stored. While this runs the lease is
        kept: giving it up would invite another refresh of a token this one may have killed."""
        body = self._body(read, tokens, lease)
        for _ in self._attempts(rotated=body["rotated"]):
            self.renew_if_due(lease, read)
            body["lease_id"] = lease.id
            try:
                self.call("PUT", self._path(self.paths.sealed, read), body)
                break
            except (canopy_web.CanopyError, OSError, ValueError) as e:
                if _transient(e):
                    continue
                if _error_code(e) not in _STALE:
                    raise
            try:
                current = self.read()
            except (canopy_web.CanopyError, OSError, ValueError) as e:
                if _transient(e):
                    continue
                raise
            if current.material.get("access_sealed") == body["access_sealed"]:
                break  # an earlier attempt landed and only its answer was lost
            if current.refresh["refresh_token"] != read.refresh["refresh_token"]:
                raise CredError("this connection's refresh token was replaced while it was being "
                                "refreshed here; nothing was written")
            read, body = current, self._body(current, tokens, lease)
        else:
            raise CredError("could not write this connection's new token back to canopy-web; the token "
                            "was not used. If its refresh token rotated, its owner may have to connect it "
                            "again")
        return _answer(tokens.access_token, tokens.expires_at, read.refresh["instance_url"], read.material)


def _lease_id(lease_answer) -> str:
    lease_id = lease_answer.get("lease_id") if isinstance(lease_answer, dict) else None
    return lease_id if isinstance(lease_id, str) and wire._UUID.fullmatch(lease_id) else ""


def _ttl(lease_answer, *, default: float | None = None) -> float:
    ttl = lease_answer.get("ttl_seconds") if isinstance(lease_answer, dict) else None
    if type(ttl) is int and ttl > 0:
        return ttl
    if default is not None:
        return default
    raise CredError("canopy-web answered a refresh lease this CLI cannot read")


def access_token(paths: Paths, *, provider: str, min_life: int, call, ecdh, pins: dict,
                 agent_id: str | None = None, reseals_for_others: bool = False, refresh: bool = False,
                 sleep=time.sleep, monotonic=time.monotonic, http=None) -> dict:
    """``{access_token, instance_url, expires_at, account_label}`` for this machine.

    ``call(method, path, body=None)`` is canopy-web, with this machine's token and a fresh
    device proof per call. ``ecdh`` is this machine's key. ``pins`` is the content of
    ``trust.PINS_FILE``. ``provider`` is what was asked for, and ``agent_id`` (a turn's
    agent's primary key, None for a person's own CLI) what the statement must list.
    ``reseals_for_others``: a cloud box, whose write-back also reaches machines still
    waiting. ``refresh`` wants a new token even if the stored one is alive; ``http``
    replaces the provider transport (``providers.base.post_form``)."""
    box = _Box(paths, provider, agent_id, call, ecdh, pins, sleep, monotonic, http, reseals_for_others)
    first = box.read()
    live = first.access
    if live and not refresh and live["expires_at"] - time.time() > min_life:
        return _answer_from(first)
    lease = box.take_lease(first)
    if lease is None:
        return _answer_from(box.wait_for_new_access(first))
    try:
        read = box.read()  # R16: only now can no earlier holder land a rotation
    except Exception:
        box.release(lease, first)
        raise
    if _newer(read, _expiry(first)):
        box.release(lease, read)
        return _answer_from(read)
    box._checked_recipients(read, lease)
    tokens = box.refreshed(read, lease)
    try:
        answer = box.write_back(read, tokens, lease)
    except Exception:
        # Nothing rotated: the stored token still works, so nobody need wait out the lease.
        # Rotated: it stays held, and its expiry is recorded as `lease_expired_unwritten`.
        if not _rotated(tokens, read.refresh):
            box.release(lease, read)
        raise
    box.release(lease, read)
    return answer
