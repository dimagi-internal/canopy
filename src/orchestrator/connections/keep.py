"""A keeper's connection work (canopy-web spec §4.1, §4.5; contract §0.6 routes 13-15):
on each pass of ``canopy vault zk keep``, a box re-seals each connection it holds for the
machines waiting for it, and revokes each one being disconnected, then confirms.

canopy-web says who is waiting; the owner's signed statement says who gets a copy, so a
server that lies can delay a machine but never add a reader. Six rules:

* **A whole read first** (``access.checked_read``, the read a token uses), and the pin is saved
  once it is accepted, before anything is written or revoked, whether or not a write follows.
  An aborted read teaches this box nothing.
* **A re-seal changes who can read and nothing else.** The same plaintexts, each envelope's
  own ``statement_floor`` and ``owner_key`` as they are. The readers are those holding it now
  and those waiting, each only if ``trust.allowed`` admits them. A waiter who does not pass
  is named and left waiting. A holder who no longer passes is not sealed to again, and if
  canopy-web still lists it, its refusal (``zk_rewrap_required``) stops the write.
* **Both envelopes or neither.** canopy-web takes a re-seal only with ``access`` too, so an
  ``access`` this box cannot open or validate stops this connection for this pass. It is
  never used or re-sealed. A later pass tries again, after a refresher has written an
  ``access`` this box can open.
* **A revoke needs the same read.** A read that fails reaches no provider and confirms
  nothing: another holder may read it, and canopy-web's sweep ends it after 7 days.
* **Confirming deletes the row, so only a definite answer confirms.** Revoked means the
  provider said so, or said the token is already dead (``invalid_grant``, ``invalid_token``:
  also what a second try gets after a confirmation was lost), or this box was not told to
  revoke at all. Any other rejection by the provider confirms ``revoked: false``. A provider
  that gave no answer (outage, rate limit), or a refusal here before any call, confirms
  nothing: a later pass tries again, and the sweep is the backstop.
* **One bad connection stops only itself.** Every failure ends as a line in its
  ``KeepResult``, which holds nothing sealed: a code or a provider's scrubbed detail, or for
  anything unexpected the exception's type alone.

Only cloud boxes do this. canopy-web lists work for no other runner and refuses a laptop's
re-seal and confirmation, since the CLI cannot tell a cloud box by itself.
"""
from __future__ import annotations

import re

from orchestrator import canopy_web, device_key, vault_zk_cli
from orchestrator import vault_zk as zk
from orchestrator.connections import access, providers, trust, wire
from orchestrator.cred_cli import CONNECTION_CALL_TIMEOUT, CredError, _error_code, _status_of, _timeout_transport
from orchestrator.vault_keeper import KeepResult

KIND = "connection"
#: The provider's way of saying the token is gone already, which is what a revoke is for.
_DEAD_TOKEN = frozenset({access.INVALID_GRANT, "invalid_token"})
#: What canopy-web writes to its log as a provider's error; anything else it calls unrecognised.
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_.-]{0,63}")


def machine_call(token: str, base_url: str, transport=None):
    """canopy-web as this box on the connection routes: its runner token, a fresh device
    proof per request, and a timeout, so one hung call cannot use up the pass."""
    transport = transport or _timeout_transport(CONNECTION_CALL_TIMEOUT)

    def call(method: str, path: str, body=None) -> dict:
        return canopy_web.call(method, path, body, token=token, base_url=base_url, transport=transport,
                               headers=device_key.proof_headers(method, base_url + path))
    return call


def _unfinished(error: str) -> KeepResult:
    return KeepResult("work", error=error, kind=KIND)


def _why(e: canopy_web.CanopyError) -> str:
    """What canopy-web refused with: its code, not its text."""
    return _error_code(e) or f"HTTP {_status_of(e)}"


def pending_work(call, runner_id: str) -> tuple[list, list[KeepResult]]:
    """``(work, trouble)``: failing to ask stops no other keeper work. A canopy-web without
    the route has none to give."""
    try:
        body = call("GET", f"/api/harness/runners/{runner_id}/vault-keeper/connections")
    except canopy_web.CanopyError as e:
        if _status_of(e) == 404:
            return [], []
        return [], [_unfinished(f"could not read this box's connection work ({_why(e)})")]
    except (OSError, ValueError) as e:
        return [], [_unfinished(f"could not read this box's connection work ({type(e).__name__})")]
    work = body.get("connections") if isinstance(body, dict) else None
    if not isinstance(work, list):
        return [], [_unfinished("canopy-web's connection work is not a list")]
    return work, []


def keep_all(work: list, *, call, ecdh, http=None) -> list[KeepResult]:
    """Every connection in ``work``. ``pins`` is :data:`trust.PINS_FILE`'s content, never the
    vault keeper's people: a damaged file means no connection can be judged this pass."""
    if not work:
        return []
    try:
        pins = vault_zk_cli.load_pins(trust.PINS_FILE)
    except vault_zk_cli.ZkCliError as e:
        return [_unfinished(str(e))]
    return [keep_connection(w, pins=pins, call=call, ecdh=ecdh, http=http) for w in work]


def keep_connection(work: dict, *, pins: dict, call, ecdh, http=None) -> KeepResult:
    """One connection a box holds (``ConnectionKeeperWorkOut``). ``pins`` is
    :data:`trust.PINS_FILE`'s content, ``http`` replaces the provider transport. Never raises
    for anything this connection did."""
    res = KeepResult(_label(work), kind=KIND)
    try:
        provider = work.get("provider") if isinstance(work, dict) else None
        read = access.checked_read(work, provider=provider, ecdh=ecdh, pins=pins)
        if work.get("revoking") is True:
            access.save_pin(read.statement, read.owner_jkt, pins)
            _revoke(read, res, call, http)
        else:
            _reseal(read, res, call, ecdh, pins)
    except (trust.TrustError, wire.WireError, zk.ZkError, providers.ProviderError, CredError) as e:
        res.error = f"refused this connection ({e})"
    except canopy_web.CanopyError as e:
        res.error = f"canopy-web refused this connection's work ({_why(e)})"
    except Exception as e:  # noqa: BLE001 - one connection must not stop the pass; by type, so no value leaks
        res.error = f"could not finish this connection ({type(e).__name__})"
    return res


def _label(work) -> str:
    if not isinstance(work, dict):
        return "(malformed)"
    return f"{work.get('provider')} {work.get('account_label') or work.get('id')}"


# ---- re-sealing ----------------------------------------------------------------------

def _reseal(read: access._Read, res: KeepResult, call, ecdh, pins: dict) -> None:
    material = read.material
    if material.get("access_sealed") and read.access is None:
        res.error = ("holds an access envelope this box cannot open, and a re-seal must carry it: "
                     "left for a later pass or a refresher")
        return
    access.save_pin(read.statement, read.owner_jkt, pins)
    allowed = trust.allowed(material, read.statement, owner_jkt=read.owner_jkt, res=res)
    held = access._held(read)
    new = _admitted(material.get("waiting"), allowed, held, res)
    if not new:
        return
    if ("device", ecdh.jkt) not in allowed:
        res.error = "this box is not one the owner's statement allows, so no reader would accept what it wrote"
        return
    rows = [(kind, target, jwk) for (kind, target), jwk in allowed.items()
            if (kind, target) in held or (kind == "device" and target in new)]
    call("PUT", material["reseal_path"], _sealed_body(read, rows, ecdh))
    res.granted = [f"{_name(material, target)} — ECDH key {target[:16]}…" for target in sorted(new)]


def _admitted(waiting, allowed: dict, held: set, res: KeepResult) -> set[str]:
    """The waiting machines this box will add. One it holds already is nothing to add."""
    new: set[str] = set()
    for target in waiting if isinstance(waiting, list) else []:
        if not isinstance(target, str) or ("device", target) in held:
            continue
        if ("device", target) in allowed:
            new.add(target)
        else:
            res.skipped.append(f"{trust.UNENDORSED}: machine {target[:16]}… is not one the owner's statement allows")
    return new


def _sealed_body(read: access._Read, rows: list, ecdh) -> dict:
    material = read.material
    expect = wire.digest(material["sealed"])
    sealed, sealed_sig = _seal(material, wire.REFRESH_NAME, read.refresh, rows, expect, ecdh)
    body = {"sealed": sealed, "sealed_sig": sealed_sig, "access_sealed": "", "access_sig": None, "expect": expect}
    if read.access is not None:
        body["access_sealed"], body["access_sig"] = _seal(
            material, wire.ACCESS_NAME, read.access, rows, wire.digest(material["access_sealed"]), ecdh)
    return body


def _seal(material: dict, name: str, plain: dict, rows: list, expect: str, ecdh) -> tuple[str, dict]:
    return wire.seal_envelope(material["binding"], name, plain, rows, writer=ecdh, writer_target=ecdh.jkt,
                              expect=expect)


def _name(material: dict, target: str) -> str:
    for row in material.get("recipients") or []:
        if isinstance(row, dict) and row.get("target") == target and row.get("runner_name"):
            return str(row["runner_name"])
    return "machine"


# ---- revoking ------------------------------------------------------------------------

def _revoke(read: access._Read, res: KeepResult, call, http) -> None:
    """At the provider only when told, then confirm what it definitely answered."""
    revoked, error = True, ""
    outcome = "disconnected here; another connection of this account remains, so not revoked at the provider"
    if read.material.get("revoke_at_provider") is True:
        try:
            providers.get(read.refresh["provider"]).revoke(read.refresh, read.material.get("app"), http=http)
            outcome = "revoked at the provider"
        except providers.ProviderError as e:
            if e.code != "rejected":
                res.error = f"not revoked ({e}); nothing confirmed, so the next pass tries again"
                return
            if e.provider_code in _DEAD_TOKEN:
                outcome = "already dead at the provider"
            else:
                revoked, error = False, _revoke_code(e)
    try:
        call("POST", read.material["revoked_path"], {"revoked": revoked, "error": error})
    except canopy_web.CanopyError as e:
        if _status_of(e) != 404:  # 404: another holder confirmed first
            raise
    if revoked:
        res.done.append(outcome)
    else:
        res.error = f"the provider refused the revoke ({error}); told canopy-web, which deletes the connection"


def _revoke_code(e: providers.ProviderError) -> str:
    """Code-shaped or not at all: a provider's text is scrubbed of what we sent it, not of
    everything, and canopy-web keeps only a code."""
    return e.provider_code if _ERROR_CODE.fullmatch(e.provider_code) else e.code
