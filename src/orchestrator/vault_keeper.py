"""`canopy vault zk keep` — this runner passes on the vault keys it holds, with nobody asked
(canopy-web spec 2026-10-22-vault-access-follows-canopy-access-design.md §1).

Under zero-knowledge only a holder of an agent's vault key can give a copy to someone new.
A runner that holds one is a holder and is online, so on its own clock it asks canopy-web
who is waiting (``GET /api/harness/runners/<id>/vault-keeper``) and wraps the key to each
target that passes the TRUST CHECK here — canopy-web says who is eligible; this box decides
whom it trusts, so a server that lies can delay a grant but never add a reader:

* **a person** (a member of the agent's vault) is trusted on first sight: their key is
  pinned in ``~/.canopy/vault-pins.json`` (the pin store ``vault zk set --sealed`` uses,
  keyed by person like ``known_hosts``). A pinned person whose key CHANGED is skipped until
  an admin confirms it with them (Jonathan's decision 2, 2026-10-22);
* **a machine** needs an endorsement (zk-v1 §7) of its exact key by a person this box
  trusts — pinned, or pinned now on first sight under the same rule. Approving a machine
  makes that endorsement (canopy-web ``approveAndGrant``).

A target that fails is left waiting and named in the output; nothing is ever wrapped to an
unverified key "to make it work". canopy-web accepts a keeper's wraps only for targets with
no copy yet, so a keeper never replaces one. Nothing here prints a key or a value.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from orchestrator import canopy_web, cred_resolve, device_key
from orchestrator import vault_zk as zk
from orchestrator.vault_zk_cli import (
    Target,
    ZkCliError,
    fingerprint,
    load_pins,
    save_pins,
    target_from_row,
)


@dataclass
class KeepResult:
    agent: str
    granted: list = field(default_factory=list)     # Target.describe()
    skipped: list = field(default_factory=list)     # why each other target waits
    pinned: list = field(default_factory=list)      # people pinned on first sight
    error: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _trust_person(pins: dict, user_id: str, email: str, jkt: str, res: KeepResult) -> bool:
    """Trusted on first sight; a changed key is refused until an admin confirms it."""
    pin = pins.get(user_id)
    if isinstance(pin, dict) and pin.get("jkt"):
        if pin["jkt"] == jkt:
            return True
        who = pin.get("email") or email or f"user {user_id}"
        res.skipped.append(f"{who}: vault key changed since this box pinned it (pinned "
                           f"{fingerprint(pin['jkt'])}, now {fingerprint(jkt)}) — an admin must "
                           f"confirm with them, then remove user {user_id} from the pin file")
        return False
    pins[user_id] = {"jkt": jkt, "email": email, "pinned_at": _now()}
    res.pinned.append(email or f"user {user_id}")
    return True


def plan(work: dict, pins: dict) -> tuple[list[Target], KeepResult]:
    """Which of ``work["pending"]`` this box will wrap to, and why each other is left.
    Pure apart from ``pins``, which gains every person pinned on first sight."""
    res = KeepResult(str(work.get("agent") or ""))
    agent_id = str(work.get("agent_id") or "")
    endorsements = [e for e in work.get("endorsements") or [] if isinstance(e, dict)]
    out: list[Target] = []
    for row in work.get("pending") or []:
        if not isinstance(row, dict):
            continue
        t, why = target_from_row(row)
        if t is None:
            res.skipped.append(why)
            continue
        jkt = zk.jwk_thumbprint(t.public_jwk)
        if t.kind == "member":
            if _trust_person(pins, str(t.target), t.label, jkt, res):
                out.append(t)
            continue
        if _endorsed(t, jkt, endorsements, agent_id, pins, res):
            out.append(t)
        else:
            res.skipped.append(f"{t.describe()}: not endorsed by a person this box trusts — the "
                               "person who approves it signs it (or an admin confirms it in canopy-web)")
    return out, res


def _endorsed(t: Target, jkt: str, endorsements: list, agent_id: str, pins: dict,
              res: KeepResult) -> bool:
    for e in endorsements:
        if (e.get("kind"), str(e.get("target") or ""), e.get("key_jkt")) != ("device", jkt, jkt):
            continue
        bjwk = e.get("by_public_jwk")
        try:
            bjkt = zk.jwk_thumbprint(bjwk)
        except (KeyError, TypeError):
            continue
        if e.get("by_jkt") != bjkt or not zk.verify_endorsement(e, agent_id, bjwk):
            continue
        by = str(e.get("by") or "")
        if by and _trust_person(pins, by, str(e.get("by_email") or ""), bjkt, res):
            return True
    return False


def _wrap(vk: bytes, agent_id: str, kv, t: Target) -> dict:
    return {"kind": t.kind, "target": str(t.target),
            "wrap": zk.wrap_ecdh_es(vk, t.public_jwk, t.info(agent_id, kv))}


def keep_one(work: dict, *, pins: dict, call, ecdh) -> KeepResult:
    """Grant what ``work`` (one agent from the keeper read) allows. ``call(method, path,
    body)`` reaches canopy-web as this runner."""
    targets, res = plan(work, pins)
    if not targets:
        return res
    agent_id, kv = str(work["agent_id"]), work["kv"]
    try:
        vk = cred_resolve.unwrap_device_vk(work["wrap"], agent_id=agent_id, kv=kv, ecdh=ecdh)
    except canopy_web.CanopyError as e:
        res.error = str(e)
        return res
    wraps = [_wrap(vk, agent_id, kv, t) for t in targets]
    vk = b""  # noqa: F841
    try:
        call("POST", f"{work['_base']}/{work['agent']}/wraps", {"kv": kv, "wraps": wraps})
    except canopy_web.CanopyError as e:
        res.error = f"canopy-web refused the copies: {str(e)[:300]}"
        return res
    res.granted = [t.describe() for t in targets]
    return res


def run_keep(*, runner_id: Optional[str] = None, base_url: Optional[str] = None,
             transport=None, ecdh=None, pins_file=None) -> list[KeepResult]:
    """One keeper pass over every agent this runner holds a vault key for."""
    from orchestrator import runner_session

    base = canopy_web.resolve_base_url(base_url)
    runner_id = runner_id or (device_key.runner_id_from_config()
                              or str(device_key.read_status().get("runner") or "").strip())
    if not runner_id:
        raise ZkCliError("this machine is not a canopy runner (no runner id) — nothing to keep")
    token = runner_session.token(base, transport=transport)
    if not token:
        raise ZkCliError("no runner token: this machine's device key is not approved yet")

    def call(method, path, body=None):
        return canopy_web.call(method, path, body, token=token, base_url=base, transport=transport)

    path = f"/api/harness/runners/{runner_id}/vault-keeper"
    work = (call("GET", path) or {}).get("agents") or []
    if not work:
        return []
    if ecdh is None:
        try:
            ecdh = device_key.load_ecdh()
        except device_key.DeviceKeyError as e:
            raise ZkCliError(str(e)) from None
        if ecdh is None:
            raise ZkCliError(device_key.NO_ECDH_KEY)
    pins = load_pins(pins_file)
    out = []
    for w in work:
        out.append(keep_one({**w, "_base": path}, pins=pins, call=call, ecdh=ecdh))
    save_pins(pins, pins_file)
    return out


def render(results: list[KeepResult]) -> str:
    if not results:
        return "nothing waiting for a vault key this runner holds"
    lines = []
    for r in results:
        lines.append(f"agent {r.agent}")
        lines += [f"  granted: {g}" for g in r.granted]
        lines += [f"  pinned on first sight: {p}" for p in r.pinned]
        lines += [f"  waiting: {s}" for s in r.skipped]
        if r.error:
            lines.append(f"  error: {r.error}")
    return "\n".join(lines)
