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
  registered device key. Required by canopy-web only for an agent with
  ``require_device_binding`` on; ignored otherwise.

A 401 ``device_proof_required`` is turned into an error that says how to get this
machine approved. Nothing here prints a value.
"""
from __future__ import annotations

import os
from typing import Iterable, Optional
from urllib.parse import quote

from orchestrator import canopy_web, device_key

TURN_HEADER = "X-Canopy-Turn-Id"


class DeviceProofRequired(canopy_web.CanopyError):
    """canopy-web wants a device proof this machine could not give."""


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

    def get(names) -> dict:
        path = resolve_path(slug, names)
        try:
            return canopy_web.call("GET", path, token=token, base_url=base_url,
                                   transport=transport,
                                   headers=_headers(path, base_url, turn)) or {}
        except canopy_web.CanopyError as e:
            raise _explain_401(slug, e) from None

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
