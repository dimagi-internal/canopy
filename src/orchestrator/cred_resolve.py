"""The one client for ``GET /api/agents/<slug>/credentials/resolve`` (canopy#946).

Every canopy caller that reads an agent's credentials from canopy-web goes through
:func:`resolve`, so the Canopy Vault additions to the contract (canopy-web#1538) are made
in one place:

* ``?refs=a,b`` — when the caller needs only some values. The server returns only those
  (intersected with what the agent declares). An OLDER server ignores the parameter and
  returns everything, and a newer one may leave out a ref the agent does not declare in
  ``runtime_sources`` — so when any requested ref is missing from the answer this asks
  ONCE more without ``refs`` and filters here. The caller always gets exactly the refs it
  asked for that exist, whichever server answered.
* ``X-Canopy-Turn-Id`` — when a runner turn is known, so the read log names the turn.
* ``DPoP`` — a device proof (`device_key.proof_headers`) when this machine holds a
  registered device key; on an EC2 cloud runner enrolled as ``aws-iam``, an
  ``X-Canopy-Aws-Proof`` instead (canopy-web#1549). Required by canopy-web only for an
  agent with ``require_device_binding`` on; ignored otherwise.

A 401 ``device_proof_required`` is turned into an error that says how to get this
machine approved. Nothing here prints a value.

**Zero-knowledge agents (canopy-web#1550).** For an agent with ZK on, canopy-web holds only
ciphertext and answers ``{zk: true, kv, values: {name: "zk1:…"}, wrap: {alg: "ecdh-es", …}}``
— the agent's vault key wrapped to THIS device's ECDH key. :func:`resolve` unwraps it here
(the ECDH key hands back Z; ``device_info(agent_id, kv, ecdh_jkt)`` binds the wrap), decrypts
every value, and returns the body with plaintext ``values`` — so ``canopy cred``,
``agent prepare --runner``, ``cred exec`` and ``cred sync`` handle a ZK agent exactly like a
custodial one. ``agent_id`` is canopy-web's Agent primary key: the resolve body's
``agent_id`` when the server sends it, else ``id`` from ``GET /api/agents/<slug>/``. A 409
``zk_wrap_missing`` (this device was never granted the current key version) becomes an error
that says to ask an admin to grant this device. A body without ``zk`` takes the old path,
untouched — unless a value in it is ``zks1:`` (sealed by canopy-web to this device's ECDH
key, before a vault-key holder settled it), which opens here too (:func:`open_zk`). Pass
``raw=True`` to get the stored ciphertext instead (``canopy vault zk settle``).
"""
from __future__ import annotations

import os
from typing import Iterable, Optional
from urllib.parse import quote

from orchestrator import canopy_web, device_key

TURN_HEADER = "X-Canopy-Turn-Id"
#: Why a read outside a turn is happening (canopy-web#1563). canopy-web skips its
#: "read outside a turn" alert for a named box that sends ``runner-refresh``.
PURPOSE_HEADER = "X-Canopy-Read-Purpose"
RUNNER_REFRESH = "runner-refresh"


class DeviceProofRequired(canopy_web.CanopyError):
    """canopy-web wants a device proof this machine could not give."""


class ZkWrapMissing(canopy_web.CanopyError):
    """A zero-knowledge agent whose vault key was never wrapped to this device."""


class ZkUnlockError(canopy_web.CanopyError):
    """A zero-knowledge answer this machine could not open (no ECDH key, a wrap for a
    different key, a value that does not authenticate). Never carries a value."""


def current_turn_id() -> str:
    """The runner turn this process belongs to, or "" (a person's session)."""
    env = os.environ.get("CANOPY_TURN_ID", "").strip()
    if env:
        return env
    try:
        from orchestrator import provenance
        return str(provenance.resolve_parent().get("turn_id") or "")
    except Exception:  # noqa: BLE001 — identity lookup must never break a resolve
        return ""


def resolve_path(slug: str, refs: Optional[Iterable[str]] = None) -> str:
    path = f"/api/agents/{slug}/credentials/resolve"
    names = [r for r in (refs or []) if r]
    if names:
        path += "?refs=" + ",".join(quote(n, safe="") for n in names)
    return path


def _headers(path: str, base_url: Optional[str], turn_id: str, purpose: str = "") -> dict:
    headers = {}
    if turn_id:
        headers[TURN_HEADER] = turn_id
    if purpose:
        headers[PURPOSE_HEADER] = purpose
    url = canopy_web.resolve_base_url(base_url) + path
    headers.update(device_key.proof_headers("GET", url))
    return headers


def _explain_401(slug: str, err: Exception) -> Exception:
    text = str(err)
    if "-> 409" in text and "zk_wrap_missing" in text:
        st = device_key.read_status()
        which = (f"device key {str(st.get('jkt'))[:16]}… on runner {st.get('runner') or '?'}"
                 if st.get("jkt") else "this machine's device key")
        return ZkWrapMissing(
            f"'{slug}' is a zero-knowledge vault and {which} has no wrap of its current vault "
            f"key — ask an admin of '{slug}' to grant this device ('Grant pending' on the "
            f"agent's page, or `canopy vault zk grant --agent {slug}` from a machine that "
            "already has access)")
    if "-> 401" in text and "device_proof_required" in text:
        st = device_key.read_status()
        if st.get("approve_url") and st.get("status") == "pending":
            how = f"approve this machine's device key in canopy-web: {st['approve_url']}"
        elif st.get("jkt"):
            how = (f"this machine's device key is '{st.get('status') or 'unknown'}' — "
                   "`canopy vault device` shows it; re-enroll with `canopy vault device --reenroll`")
        else:
            how = "enroll this machine: `canopy vault device --enroll`, then approve it in canopy-web"
        return DeviceProofRequired(
            f"canopy-web will hand '{slug}'s credentials only to an approved device "
            f"(require_device_binding is on) — {how}")
    return err


def resolve(slug: str, *, token: Optional[str] = None, refs: Optional[Iterable[str]] = None,
            turn_id: Optional[str] = None, base_url: Optional[str] = None,
            transport: Optional[canopy_web.Transport] = None, raw: bool = False,
            purpose: str = "", fallback: bool = True) -> dict:
    """The resolve body (``values``, ``op_vault``, ``op_sa_token``, …). With ``refs``,
    ``values`` holds exactly those of them that exist. Raises canopy_web.CanopyError
    (DeviceProofRequired for a missing/unapproved device proof).

    A scoped answer missing a named VALUE is re-asked unscoped (an older server, or a
    name the runtime does not declare) — unless ``fallback=False``: a caller that names
    only what it uses, extras included, must get exactly that and never everything
    (the box's refresh, canopy-web#1563)."""
    wanted = [r for r in dict.fromkeys(refs or []) if r]
    turn = current_turn_id() if turn_id is None else turn_id
    from orchestrator import private_runner_session

    if private_runner_session.in_local_turn():
        # A local session on your private runner reads with the box's own key, under your
        # license, as you (capped only by the turn's own `acts_as`) — not with a PAT
        # (canopy-web#1705, #1756).
        token = private_runner_session.token() or token
    agent_ids: dict = {}

    def get(names) -> dict:
        path = resolve_path(slug, names)
        try:
            body = canopy_web.call("GET", path, token=token, base_url=base_url,
                                   transport=transport,
                                   headers=_headers(path, base_url, turn, purpose)) or {}
        except canopy_web.CanopyError as e:
            raise _explain_401(slug, e) from None
        if not raw and isinstance(body, dict) and (body.get("zk") or has_sealed(body)):
            if "id" not in agent_ids:
                agent_ids["id"] = zk_agent_id(slug, body, token=token, base_url=base_url,
                                              transport=transport)
            body = open_zk(slug, body, agent_id=agent_ids["id"])
        if not raw and isinstance(body, dict) and body.get("vaults"):
            body = merge_vault_layers(slug, body)
        return body

    body = get(wanted)
    if not wanted:
        return body
    values = body.get("values") or {}
    if not isinstance(values, dict):
        values = {}
    if fallback and any(n not in values for n in wanted):
        full = get([])
        values = full.get("values") or {}
        body = full if isinstance(values, dict) else body
        values = values if isinstance(values, dict) else {}
    return {**body, "values": {n: values[n] for n in wanted if n in values}}


def runner_resolve_path(runner_id: str) -> str:
    return f"/api/harness/runners/{runner_id}/credential/resolve"


def resolve_runner(runner_id: str, *, token: Optional[str] = None, base_url: Optional[str] = None,
                   transport: Optional[canopy_web.Transport] = None, ecdh=None) -> dict:
    """A cloud runner's OWN Claude credentials, out of the vault that holds them
    (canopy-web spec 2026-10-23-runner-claude-credentials-in-the-vault-design.md).

    canopy-web answers only the box's own runner token with a proof of its device key,
    and only with ciphertext: ``{vaults: [...]}`` in the shape an agent's resolve gives
    its workspace vault layers. Each opens here under its own key, unwrapped from this
    device's copy, exactly as :func:`merge_vault_layers` opens an agent's. Returns
    ``{values: {name: plaintext}, vault_layers: [...]}``; a vault this device may not
    open is named in ``vault_layers``, never a failed read. Never logs a value."""
    layers = _runner_layers(runner_id, token=token, base_url=base_url, transport=transport)
    merged = merge_vault_layers(f"runner {runner_id}", {"values": {}, "vaults": layers}, ecdh=ecdh)
    return {"values": merged["values"], "vault_layers": merged["vault_layers"]}


def _runner_call(method: str, path: str, runner_id: str, body=None, *, token, base_url, transport) -> dict:
    """One call to a runner's own vault route, with a device proof for THIS method."""
    url = canopy_web.resolve_base_url(base_url) + path
    try:
        return canopy_web.call(method, path, body, token=token, base_url=base_url, transport=transport,
                               headers=device_key.proof_headers(method, url)) or {}
    except canopy_web.CanopyError as e:
        text = str(e)
        if "device_proof_required" in text:
            raise DeviceProofRequired(
                f"canopy-web reaches runner {runner_id}'s credentials only with its own approved device "
                f"key: {text[:300]}") from None
        raise


def _runner_layers(runner_id: str, *, token, base_url, transport) -> list:
    body = _runner_call("GET", runner_resolve_path(runner_id), runner_id, token=token,
                        base_url=base_url, transport=transport)
    return body.get("vaults") or []


def seal_runner_values(runner_id: str, values: dict, *, token: Optional[str] = None,
                       base_url: Optional[str] = None, transport: Optional[canopy_web.Transport] = None,
                       ecdh=None) -> dict:
    """Seal a cloud runner's own credentials into the vault that serves it: open this
    device's copy of the vault key, encrypt each ``{name: plaintext}`` under it (bound
    to the vault and the name), and write the ciphertext with
    ``PUT /api/harness/runners/<id>/vault/values``. canopy-web never sees a value.
    Returns ``{names, vault_id}``. Raises ZkUnlockError when this device holds no copy
    of a vault key to seal with. Never logs a value."""
    from orchestrator import vault_zk as zk
    layer = next((lay for lay in _runner_layers(runner_id, token=token, base_url=base_url,
                                                transport=transport)
                  if isinstance(lay, dict) and isinstance(lay.get("wrap"), dict)), None)
    if layer is None:
        raise ZkUnlockError(f"runner {runner_id} holds no copy of a vault key to seal with — its "
                            "owner makes the vault's key on the runner's credentials page")
    binding, kv = str(layer["binding_id"]), layer["kv"]
    vk = unwrap_device_vk(layer["wrap"], agent_id=binding, kv=kv, ecdh=_device_ecdh(ecdh))
    sealed = [{"name": str(n), "value_zk": zk.encrypt_value(vk, binding, str(n), kv, str(v))}
              for n, v in values.items()]
    vk = b""  # noqa: F841 — drop our only reference
    out = _runner_call("PUT", f"/api/harness/runners/{runner_id}/vault/values", runner_id,
                       {"vault_id": layer["vault_id"], "kv": kv, "values": sealed},
                       token=token, base_url=base_url, transport=transport)
    return {"names": out.get("names") or [], "vault_id": str(layer["vault_id"])}


# ──────────────────────────────────────────────────────────────────────────────
# zero-knowledge answers (canopy-web#1550)
# ──────────────────────────────────────────────────────────────────────────────

def zk_agent_id(slug: str, body: Optional[dict] = None, *, token: Optional[str] = None,
                base_url: Optional[str] = None,
                transport: Optional[canopy_web.Transport] = None) -> str:
    """canopy-web's Agent primary key, as the string the ZK formats bind: the answer's
    own ``agent_id`` when it carries one, else ``GET /api/agents/<slug>/``'s ``id`` (the
    same membership gate as resolve, so any caller that may resolve may read it)."""
    hint = (body or {}).get("agent_id")
    if hint not in (None, ""):
        return str(hint)
    try:
        rec = canopy_web.call("GET", f"/api/agents/{slug}/", token=token, base_url=base_url,
                              transport=transport) or {}
    except canopy_web.CanopyError as e:
        raise ZkUnlockError(f"could not read '{slug}'s canopy-web id, which its "
                            f"zero-knowledge values are bound to: {str(e)[:200]}") from None
    if rec.get("id") in (None, ""):
        raise ZkUnlockError(f"canopy-web's record for '{slug}' carries no id — cannot open "
                            "its zero-knowledge values")
    return str(rec["id"])


def _device_ecdh(ecdh=None):
    """This machine's ECDH key, or ZkUnlockError saying how to get one."""
    try:
        ecdh = ecdh if ecdh is not None else device_key.load_ecdh()
    except device_key.DeviceKeyError as e:
        raise ZkUnlockError(str(e)) from None
    if ecdh is None:
        raise ZkUnlockError(device_key.NO_ECDH_KEY)
    return ecdh


def unwrap_device_vk(wrap: dict, *, agent_id: str, kv, ecdh=None) -> bytes:
    """The vault key, from a wrap made to this device's ECDH key. Raises ZkUnlockError."""
    from orchestrator import vault_zk as zk
    ecdh = _device_ecdh(ecdh)
    try:
        z = ecdh.derive(zk.wrap_epk_point(wrap))
        return zk.unwrap_ecdh_es_with_z(wrap, z, ecdh.point,
                                        zk.device_info(agent_id, kv, ecdh.jkt))
    except (zk.ZkError, device_key.DeviceKeyError) as e:
        raise ZkUnlockError(f"this device could not unwrap the vault key ({e}) — its wrap "
                            "may be for another key; re-grant this device") from None


def open_sealed_for_device(stored: str, *, agent_id: str, name: str, ecdh=None) -> str:
    """A ``zks1:`` value canopy-web sealed to this device's ECDH key (its own entry,
    ``kind=device``, ``target`` = the ECDH jkt). Raises ZkUnlockError."""
    from orchestrator import vault_zk as zk
    ecdh = _device_ecdh(ecdh)
    try:
        entry, _ = zk.sealed_entry(stored, name, "device", ecdh.jkt)
        z = ecdh.derive(zk.sealed_epk_point(entry))
        return zk.open_sealed_with_z(stored, agent_id, name, "device", ecdh.jkt, z)
    except (zk.ZkError, device_key.DeviceKeyError) as e:
        raise ZkUnlockError(f"this device could not open the sealed value ({e}) — ask an "
                            "admin to settle it (`canopy vault zk settle`) or re-grant this "
                            "device") from None


def has_sealed(body) -> bool:
    from orchestrator import vault_zk as zk
    values = (body or {}).get("values") if isinstance(body, dict) else None
    return isinstance(values, dict) and any(zk.is_sealed_value(v) for v in values.values())


def merge_vault_layers(slug: str, body: dict, *, ecdh=None) -> dict:
    """Layer the workspace vaults canopy-web lists under the agent's own values
    (canopy-web spec 2026-10-22-vault-access-follows-canopy-access-design.md §4).

    ``body["vaults"]`` is in order — root workspace first, vaults attached to the agent
    last — and a later layer wins a name clash; the agent's own ``values`` win over all.
    Each layer's ``zk1:`` values open under ITS key, unwrapped from this device's copy and
    bound to its ``binding_id``. A layer this device may not open (``wrap: null``), or
    cannot, is skipped and named in ``vault_layers`` — never a failed resolve: the
    agent's own secrets still arrive. Never logs or returns a key."""
    from orchestrator import vault_zk as zk
    layered: dict = {}
    report = []
    ecdh_key = None
    for layer in body.get("vaults") or []:
        if not isinstance(layer, dict):
            continue
        name = str(layer.get("name") or layer.get("vault_id") or "?")
        wrap, binding, kv = layer.get("wrap"), str(layer.get("binding_id") or ""), layer.get("kv")
        raw = layer.get("values") or {}
        if not isinstance(wrap, dict) or not binding or not isinstance(raw, dict):
            report.append({"vault": name, "opened": False, "why": "this device holds no copy of its key"})
            continue
        try:
            ecdh_key = ecdh_key or _device_ecdh(ecdh)
            vk = unwrap_device_vk(wrap, agent_id=binding, kv=kv, ecdh=ecdh_key)
            opened = {n: zk.decrypt_value(vk, binding, str(n), kv, str(v)) for n, v in raw.items()
                      if zk.is_zk_value(v)}
            vk = b""  # noqa: F841 — drop our only reference
        except (ZkUnlockError, zk.ZkError, device_key.DeviceKeyError) as e:
            report.append({"vault": name, "opened": False, "why": str(e)[:200]})
            continue
        layered.update(opened)
        report.append({"vault": name, "opened": True, "names": sorted(opened)})
    own = body.get("values") if isinstance(body.get("values"), dict) else {}
    out = {k: v for k, v in body.items() if k != "vaults"}
    out["values"] = {**layered, **own}
    out["vault_layers"] = report
    return out


def open_zk(slug: str, body: dict, *, agent_id: str, ecdh=None) -> dict:
    """A ZK resolve answer → the same body with plaintext ``values`` (``wrap`` dropped).
    ``zk1:`` values open under the vault key (unwrapped from this device's wrap);
    ``zks1:`` values — sealed by canopy-web to named devices, before anyone settled them
    under the vault key — open with this device's own ECDH key. A custodial answer that
    carries a sealed value is opened the same way. Never logs or returns a key."""
    from orchestrator import vault_zk as zk
    kv = body.get("kv")
    wrap = body.get("wrap")
    raw = body.get("values") or {}
    if not isinstance(raw, dict):
        raise ZkUnlockError(f"canopy-web sent '{slug}' zero-knowledge values that are not a map")
    needs_vk = any(zk.is_zk_value(v) for v in raw.values())
    if body.get("zk") and needs_vk and (kv in (None, "") or not isinstance(wrap, dict)):
        raise ZkUnlockError(f"canopy-web marked '{slug}' zero-knowledge but sent no kv/wrap")
    ecdh = _device_ecdh(ecdh)
    vk = (unwrap_device_vk(wrap, agent_id=agent_id, kv=kv, ecdh=ecdh)
          if needs_vk and isinstance(wrap, dict) else None)
    values = {}
    for name, stored in raw.items():
        if stored in (None, ""):
            values[name] = stored
        elif zk.is_sealed_value(stored):
            values[name] = open_sealed_for_device(str(stored), agent_id=agent_id,
                                                  name=str(name), ecdh=ecdh)
        elif zk.is_zk_value(stored) and vk is not None:
            try:
                values[name] = zk.decrypt_value(vk, agent_id, str(name), kv, str(stored))
            except zk.ZkError as e:
                raise ZkUnlockError(f"'{slug}': {e}") from None
        elif zk.is_zk_value(stored):
            raise ZkUnlockError(f"'{slug}': {name} is zero-knowledge but canopy-web sent no "
                                "wrap of the vault key for this device")
        else:
            values[name] = stored
    vk = b""  # noqa: F841 — drop our only reference
    out = {k: v for k, v in body.items() if k != "wrap"}
    out["values"] = values
    if isinstance(body.get("delegated"), dict):
        out["delegated"], out["delegated_errors"] = open_delegated(body["delegated"], agent_id=agent_id, ecdh=ecdh)
    return out


def open_delegated(entries: dict, *, agent_id: str, ecdh=None) -> tuple[dict, dict]:
    """``({name: plaintext}, {name: why})`` for the credentials people delegated to the agent
    (canopy-web ``accounts``): each sealed to the machines that run it, opened here with
    this device's own key. One that is not sealed to this device yet is reported, never a
    failed resolve — a machine that holds it re-seals it (``canopy vault zk keep``)."""
    from orchestrator import vault_zk as zk
    opened, errors = {}, {}
    for name, entry in entries.items():
        stored = entry.get("value_zk") if isinstance(entry, dict) else None
        if not zk.is_sealed_value(stored):
            errors[str(name)] = "not a sealed value"
            continue
        try:
            opened[str(name)] = open_sealed_for_device(str(stored), agent_id=agent_id, name=str(name), ecdh=ecdh)
        except ZkUnlockError as e:
            errors[str(name)] = str(e)[:300]
    return opened, errors
