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
untouched.
"""
from __future__ import annotations

import os
from typing import Iterable, Optional
from urllib.parse import quote

from orchestrator import canopy_web, device_key

TURN_HEADER = "X-Canopy-Turn-Id"


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


def _headers(path: str, base_url: Optional[str], turn_id: str) -> dict:
    headers = {}
    if turn_id:
        headers[TURN_HEADER] = turn_id
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
            transport: Optional[canopy_web.Transport] = None) -> dict:
    """The resolve body (``values``, ``op_vault``, ``op_sa_token``, …). With ``refs``,
    ``values`` holds exactly those of them that exist. Raises canopy_web.CanopyError
    (DeviceProofRequired for a missing/unapproved device proof)."""
    wanted = [r for r in dict.fromkeys(refs or []) if r]
    turn = current_turn_id() if turn_id is None else turn_id
    agent_ids: dict = {}

    def get(names) -> dict:
        path = resolve_path(slug, names)
        try:
            body = canopy_web.call("GET", path, token=token, base_url=base_url,
                                   transport=transport,
                                   headers=_headers(path, base_url, turn)) or {}
        except canopy_web.CanopyError as e:
            raise _explain_401(slug, e) from None
        if isinstance(body, dict) and body.get("zk"):
            if "id" not in agent_ids:
                agent_ids["id"] = zk_agent_id(slug, body, token=token, base_url=base_url,
                                              transport=transport)
            body = open_zk(slug, body, agent_id=agent_ids["id"])
        return body

    body = get(wanted)
    if not wanted:
        return body
    values = body.get("values") or {}
    if not isinstance(values, dict):
        values = {}
    if any(n not in values for n in wanted):
        full = get([])
        values = full.get("values") or {}
        body = full if isinstance(values, dict) else body
        values = values if isinstance(values, dict) else {}
    return {**body, "values": {n: values[n] for n in wanted if n in values}}


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


def unwrap_device_vk(wrap: dict, *, agent_id: str, kv, ecdh=None) -> bytes:
    """The vault key, from a wrap made to this device's ECDH key. Raises ZkUnlockError."""
    from orchestrator import vault_zk as zk
    try:
        ecdh = ecdh if ecdh is not None else device_key.load_ecdh()
    except device_key.DeviceKeyError as e:
        raise ZkUnlockError(str(e)) from None
    if ecdh is None:
        raise ZkUnlockError("this machine has no ECDH device key to open a zero-knowledge "
                            "vault — `canopy vault device --enroll`, have it approved, then "
                            "ask an admin to grant this device")
    try:
        z = ecdh.derive(zk.wrap_epk_point(wrap))
        return zk.unwrap_ecdh_es_with_z(wrap, z, ecdh.point,
                                        zk.device_info(agent_id, kv, ecdh.jkt))
    except (zk.ZkError, device_key.DeviceKeyError) as e:
        raise ZkUnlockError(f"this device could not unwrap the vault key ({e}) — its wrap "
                            "may be for another key; re-grant this device") from None


def open_zk(slug: str, body: dict, *, agent_id: str, ecdh=None) -> dict:
    """A ZK resolve answer → the same body with plaintext ``values`` (``wrap`` dropped).
    Never logs or returns a key."""
    from orchestrator import vault_zk as zk
    kv = body.get("kv")
    wrap = body.get("wrap")
    if kv in (None, "") or not isinstance(wrap, dict):
        raise ZkUnlockError(f"canopy-web marked '{slug}' zero-knowledge but sent no kv/wrap")
    vk = unwrap_device_vk(wrap, agent_id=agent_id, kv=kv, ecdh=ecdh)
    raw = body.get("values") or {}
    if not isinstance(raw, dict):
        raise ZkUnlockError(f"canopy-web sent '{slug}' zero-knowledge values that are not a map")
    values = {}
    for name, stored in raw.items():
        if stored in (None, ""):
            values[name] = stored
            continue
        try:
            values[name] = zk.decrypt_value(vk, agent_id, str(name), kv, str(stored))
        except zk.ZkError as e:
            raise ZkUnlockError(f"'{slug}': {e}") from None
    vk = b""  # noqa: F841 — drop our only reference
    out = {k: v for k, v in body.items() if k != "wrap"}
    out["values"] = values
    return out
