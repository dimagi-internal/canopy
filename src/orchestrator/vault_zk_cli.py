"""`canopy vault zk` — run an agent's zero-knowledge vault from THIS device (canopy-web#1550).

For an agent with zero-knowledge on, canopy-web stores only ciphertext and wrapped keys.
This command acts as this machine's device, which is a member of the vault through its
device wrap (the vault key VK wrapped to its ECDH key — `device_key.load_ecdh`):

    canopy vault zk status --agent X              state, wraps, pending targets
    canopy vault zk enable --agent X [--yes]      custodial -> zero-knowledge (prints the
                                                  recovery code ONCE)
    canopy vault zk set    --agent X NAME         one value, read from stdin / a hidden prompt
    canopy vault zk set    --agent X NAME --sealed [--request ID] [--yes]
                                                  seal it (zks1) to the recipients a PINNED
                                                  person endorsed — no vault key needed
    canopy vault zk grant  --agent X [--yes]      wrap VK for every pending target
    canopy vault zk rotate --agent X [--drop K:T] new VK + kv, re-wrap the rest, re-encrypt,
                                                  new recovery code
    canopy vault zk settle --agent X              re-encrypt canopy-web's sealed (zks1) values
                                                  under the vault key
    canopy vault import --agent X --zk            1Password -> canopy-web, encrypted here

The endpoints (owner/admin only) are canopy-web's: ``GET /api/agents/<slug>/vault/zk``,
``POST …/vault/zk/enable``, ``PUT …/vault/zk/values``, ``POST …/vault/zk/wraps``,
``POST …/vault/zk/rotate`` and ``GET /api/vault/keyset`` (the caller's own keyset). The byte
formats are `orchestrator.vault_zk`'s, which reproduce the normative vectors.

What the server is trusted for: availability and the PUBLIC keys it lists as targets. A
grant wraps VK to whatever public key canopy-web names, so ``grant``/``enable``/``rotate``
list every target (label + thumbprint) and ask before wrapping; ``--yes`` skips the
question for unattended use. Nothing here prints, logs or sends a value, the vault key or
a recovery code — except the recovery code, printed exactly once on the terminal.

``set --sealed`` (give an agent a secret, canopy-web#1712) trusts the server for even
less: it seals only to recipients carrying a zk-v1 §7 endorsement by a person pinned in
``~/.canopy/vault-pins.json`` (keyed by person, like ``known_hosts``). Every thumbprint
is recomputed here; a first-seen endorser is shown (email + fingerprint) and confirmed
before pinning, and a pinned person whose key CHANGED stops the command.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import click

from orchestrator import canopy_web, cred_resolve, device_key
from orchestrator import vault_zk as zk

Confirm = Callable[[list], bool]


class ZkCliError(click.ClickException):
    pass


# ──────────────────────────────────────────────────────────────────────────────
# the server's rows, normalised (one place to adjust if the contract moves)
# ──────────────────────────────────────────────────────────────────────────────

def _ident(v) -> object:
    """A target id that may arrive bare or as a nested object ``{"id": …}``."""
    if isinstance(v, dict):
        return v.get("id")
    return v


def _row_target(row: dict, kind: str) -> object:
    keys = (("target", "device_key", "device_key_id", "id") if kind == "device"
            else ("target", "user", "user_id", "id"))
    for k in keys:
        v = _ident(row.get(k))
        if v not in (None, ""):
            return v
    return None


@dataclass
class Target:
    kind: str                   # "device" | "member"
    target: object              # device key id | user id
    public_jwk: dict
    label: str = ""
    #: device: the ECDH key's RFC 7638 thumbprint; member: the user id — what HKDF binds
    bind: str = ""

    def info(self, agent_id: str, kv) -> str:
        return (zk.device_info(agent_id, kv, self.bind) if self.kind == "device"
                else zk.member_info(agent_id, kv, self.bind))

    def key(self) -> tuple:
        return (self.kind, str(self.target))

    def describe(self) -> str:
        what = f"device key {self.target}" if self.kind == "device" else f"user {self.target}"
        thumb = zk.jwk_thumbprint(self.public_jwk)[:16]
        return f"{what}{' (' + self.label + ')' if self.label else ''} — ECDH key {thumb}…"


def target_from_row(row: dict) -> tuple[Optional[Target], str]:
    """(target, "") or (None, why it cannot be wrapped for)."""
    kind = str(row.get("kind") or "")
    if kind not in ("device", "member"):
        return None, f"unknown target kind {kind!r}"
    tgt = _row_target(row, kind)
    jwk = row.get("ecdh_public_jwk") if kind == "device" else None
    jwk = jwk or row.get("public_jwk")
    label = str(row.get("label") or row.get("name") or row.get("email") or "")
    if tgt in (None, "") or not isinstance(jwk, dict):
        return None, f"{kind} {tgt or '?'}: canopy-web sent no public key"
    try:
        zk.public_key_from_jwk(jwk)
    except zk.ZkError as e:
        return None, f"{kind} {tgt}: {e}"
    if kind == "device":
        bind = zk.jwk_thumbprint(jwk)
        claimed = row.get("ecdh_jkt")
        if claimed and claimed != bind:
            return None, (f"device {tgt}: canopy-web's ecdh_jkt {claimed} is not its key's "
                          f"thumbprint {bind} — not wrapping")
    else:
        bind = str(tgt)
    return Target(kind, tgt, {k: jwk[k] for k in ("kty", "crv", "x", "y")}, label, bind), ""


def _row_inactive(row: dict) -> bool:
    if row.get("active") is False or row.get("revoked") or row.get("stale"):
        return True
    status = row.get("status")
    return bool(status) and status not in ("active", "current")


# ──────────────────────────────────────────────────────────────────────────────
# a session against one agent
# ──────────────────────────────────────────────────────────────────────────────

class ZkVault:
    def __init__(self, slug: str, *, token: str, transport=None,
                 ecdh: Optional[device_key.EcdhKey] = None, status: Optional[dict] = None):
        self.slug, self.token, self.transport = slug, token, transport
        self._ecdh = ecdh
        self._status = status
        self._agent_id: Optional[str] = None
        self._state: Optional[dict] = None

    # -- transport --
    def web(self, method: str, path: str, body=None):
        try:
            return canopy_web.call(method, path, body, token=self.token,
                                   transport=self.transport)
        except canopy_web.CanopyError as e:
            raise ZkCliError(f"canopy-web refused {method} {path}: {str(e)[:300]}") from None

    @property
    def base(self) -> str:
        return f"/api/agents/{self.slug}/vault/zk"

    def state(self, refresh: bool = False) -> dict:
        if self._state is None or refresh:
            self._state = self.web("GET", self.base) or {}
        return self._state

    def agent_id(self) -> str:
        if self._agent_id is None:
            try:
                self._agent_id = cred_resolve.zk_agent_id(self.slug, self.state(),
                                                          token=self.token,
                                                          transport=self.transport)
            except canopy_web.CanopyError as e:
                raise ZkCliError(str(e)) from None
        return self._agent_id

    # -- this device --
    def ecdh(self) -> device_key.EcdhKey:
        if self._ecdh is None:
            try:
                self._ecdh = device_key.load_ecdh()
            except device_key.DeviceKeyError as e:
                raise ZkCliError(str(e)) from None
            if self._ecdh is None:
                raise ZkCliError(device_key.NO_ECDH_KEY)
        return self._ecdh

    def device_status(self) -> dict:
        if self._status is None:
            self._status = device_key.read_status()
        return self._status

    def self_target(self) -> Target:
        st = self.device_status()
        if not st.get("id"):
            raise ZkCliError("this machine's device key is not registered with canopy-web — "
                             "`canopy vault device --enroll`")
        if st.get("ecdh_jkt") and st["ecdh_jkt"] != self.ecdh().jkt:
            raise ZkCliError("the enrolled ECDH key is not the one on disk — "
                             "`canopy vault device --enroll` to re-register it")
        return Target("device", st["id"], self.ecdh().public_jwk, "this device", self.ecdh().jkt)

    def my_wrap(self) -> tuple[dict, object]:
        """(this device's wrap at the current kv, kv)."""
        st = self.state()
        kv = st.get("kv")
        mine_id = str(self.device_status().get("id") or "")
        jkt = self.ecdh().jkt
        for row in st.get("wraps") or []:
            if not isinstance(row, dict) or row.get("kind") != "device":
                continue
            if row.get("kv") not in (None, kv) or not isinstance(row.get("wrap"), dict):
                continue
            if row.get("ecdh_jkt") == jkt or (mine_id and str(_row_target(row, "device")) == mine_id):
                return row["wrap"], kv
        raise ZkCliError(f"this device holds no wrap of '{self.slug}'s vault key at kv {kv} — "
                         "it is not a member yet; ask an admin to grant it")

    def vk(self) -> tuple[bytes, object]:
        wrap, kv = self.my_wrap()
        try:
            return cred_resolve.unwrap_device_vk(wrap, agent_id=self.agent_id(), kv=kv,
                                                 ecdh=self.ecdh()), kv
        except canopy_web.CanopyError as e:
            raise ZkCliError(str(e)) from None

    # -- targets --
    def pending(self) -> tuple[list[Target], list[str]]:
        out, notes = [], []
        for row in self.state().get("pending") or self.state().get("pending_targets") or []:
            t, why = target_from_row(row) if isinstance(row, dict) else (None, "not an object")
            (out.append(t) if t else notes.append(f"skipped: {why}"))
        return out, notes

    def current(self, drop: set) -> tuple[list[Target], list[str]]:
        """The members at the current kv that keep access across a rotation."""
        kv = self.state().get("kv")
        out, notes = [], []
        for row in self.state().get("wraps") or []:
            if not isinstance(row, dict) or row.get("kind") not in ("device", "member"):
                continue
            if row.get("kv") not in (None, kv):
                continue
            t, why = target_from_row(row)
            if t is None:
                notes.append(f"NOT re-wrapped: {why}")
                continue
            if _row_inactive(row) or t.key() in drop:
                notes.append(f"dropped: {t.describe()}")
                continue
            out.append(t)
        return out, notes

    def keyset_target(self) -> tuple[Optional[Target], str]:
        try:
            ks = canopy_web.call("GET", "/api/vault/keyset", token=self.token,
                                 transport=self.transport) or {}
        except canopy_web.CanopyError as e:
            if "-> 404" in str(e):
                return None, "you have no vault keyset (set one up in canopy-web with a passkey)"
            return None, f"could not read your vault keyset ({str(e)[:160]})"
        jwk = ks.get("public_jwk")
        uid = _ident(ks.get("user_id") or ks.get("user") or ks.get("id"))
        if not isinstance(jwk, dict) or uid in (None, ""):
            return None, "you have no vault keyset (set one up in canopy-web with a passkey)"
        t, why = target_from_row({"kind": "member", "target": uid, "public_jwk": jwk,
                                  "label": "you"})
        return t, why

    def wrap_for(self, vk: bytes, kv, t: Target) -> dict:
        return {"kind": t.kind, "target": t.target,
                "wrap": zk.wrap_ecdh_es(vk, t.public_jwk, t.info(self.agent_id(), kv))}

    def recovery(self, vk: bytes, kv) -> tuple[dict, str]:
        r = zk.random_recovery_secret()
        return ({"kind": "recovery", "target": None,
                 "wrap": zk.wrap_recovery(vk, self.agent_id(), kv, r)}, zk.recovery_code(r))

    def encrypt(self, vk: bytes, kv, values: dict) -> list[dict]:
        aid = self.agent_id()
        return [{"name": n, "value_zk": zk.encrypt_value(vk, aid, n, kv, str(v))}
                for n, v in sorted(values.items())]


def _dedupe(targets: list[Target]) -> list[Target]:
    seen, out = set(), []
    for t in targets:
        if t.key() not in seen:
            seen.add(t.key())
            out.append(t)
    return out


def _confirmed(confirm: Optional[Confirm], targets: list[Target]) -> None:
    if confirm is not None and not confirm(targets):
        raise ZkCliError("nothing done — not confirmed")


@dataclass
class ZkResult:
    slug: str
    actions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    wrapped: list[str] = field(default_factory=list)
    #: shown once by the CLI, never logged or serialised
    recovery_code: str = field(default="", repr=False)
    kv: object = None


# ──────────────────────────────────────────────────────────────────────────────
# the operations
# ──────────────────────────────────────────────────────────────────────────────

def run_status(v: ZkVault) -> dict:
    st = v.state()
    pending, notes = v.pending()
    mine = ""
    try:
        v.my_wrap()
        mine = "holds a wrap at the current kv"
    except ZkCliError as e:
        mine = str(e)
    wraps = [{"kind": r.get("kind"), "target": _row_target(r, str(r.get("kind")))
              if r.get("kind") in ("device", "member") else None,
              "kv": r.get("kv"), "label": r.get("label") or ""}
             for r in st.get("wraps") or [] if isinstance(r, dict)]
    return {"agent": v.slug, "enabled": bool(st.get("enabled")), "kv": st.get("kv"),
            "wraps": wraps, "pending": [t.describe() for t in pending], "notes": notes,
            "this_device": mine}


def run_enable(v: ZkVault, *, confirm: Optional[Confirm] = None) -> ZkResult:
    st = v.state()
    # kv 0 is "zero-knowledge from birth, no vault key yet": enabling creates its first key.
    if st.get("enabled") and str(st.get("kv")) != "0":
        raise ZkCliError(f"'{v.slug}' is already zero-knowledge (kv {st.get('kv')}) — "
                         "use `grant`, `set` or `rotate`")
    res = ZkResult(v.slug, kv=1)
    targets = [v.self_target()]
    me, why = v.keyset_target()
    if me is not None:
        targets.append(me)
    else:
        res.notes.append(f"no member wrap for you: {why}")
    pending, notes = v.pending()
    res.notes += notes
    targets = _dedupe(targets + pending)
    try:
        body = cred_resolve.resolve(v.slug, token=v.token, transport=v.transport)
    except canopy_web.CanopyError as e:
        raise ZkCliError(f"could not read '{v.slug}'s current values: {str(e)[:300]}") from None
    values = {n: val for n, val in (body.get("values") or {}).items()
              if val is not None and str(val) != ""}
    _confirmed(confirm, targets)
    vk = zk.random_vk()
    kv = 1
    wraps = [v.wrap_for(vk, kv, t) for t in targets]
    rwrap, code = v.recovery(vk, kv)
    payload = {"kv": kv, "wraps": wraps + [rwrap], "values": v.encrypt(vk, kv, values)}
    vk = b""  # noqa: F841
    values = {}  # noqa: F841
    v.web("POST", f"{v.base}/enable", payload)
    res.actions.append(f"zero-knowledge ON for {v.slug}: {len(payload['values'])} value(s) "
                       f"encrypted here, vault key wrapped for {len(targets)} target(s) + recovery")
    res.wrapped = [t.describe() for t in targets]
    res.recovery_code = code
    return res


def run_set(v: ZkVault, values: dict) -> ZkResult:
    if not v.state().get("enabled"):
        raise ZkCliError(f"'{v.slug}' is not zero-knowledge — `canopy vault zk enable` first")
    vk, kv = v.vk()
    payload = {"kv": kv, "values": v.encrypt(vk, kv, values)}
    vk = b""  # noqa: F841
    v.web("PUT", f"{v.base}/values", payload)
    return ZkResult(v.slug, kv=kv, actions=[f"stored {len(payload['values'])} value(s) "
                                            f"encrypted at kv {kv}: "
                                            f"{', '.join(sorted(values))}"])


def run_grant(v: ZkVault, *, confirm: Optional[Confirm] = None) -> ZkResult:
    if not v.state().get("enabled"):
        raise ZkCliError(f"'{v.slug}' is not zero-knowledge — nothing to grant")
    pending, notes = v.pending()
    res = ZkResult(v.slug, notes=notes)
    if not pending:
        res.actions.append("nothing pending — every target holds a wrap at the current kv")
        return res
    _confirmed(confirm, pending)
    vk, kv = v.vk()
    wraps = [v.wrap_for(vk, kv, t) for t in pending]
    vk = b""  # noqa: F841
    v.web("POST", f"{v.base}/wraps", {"kv": kv, "wraps": wraps})
    res.kv = kv
    res.wrapped = [t.describe() for t in pending]
    res.actions.append(f"granted {len(wraps)} target(s) at kv {kv}")
    return res


def run_rotate(v: ZkVault, *, drop: Optional[set] = None,
               confirm: Optional[Confirm] = None) -> ZkResult:
    st = v.state()
    if not st.get("enabled"):
        raise ZkCliError(f"'{v.slug}' is not zero-knowledge — nothing to rotate")
    kv = st.get("kv")
    try:
        new_kv = int(kv) + 1
    except (TypeError, ValueError):
        raise ZkCliError(f"canopy-web reports kv {kv!r} — cannot rotate") from None
    v.vk()                                     # prove this device is a member first
    current, notes = v.current(drop or set())
    pending, pnotes = v.pending()
    targets = _dedupe(current + pending)
    res = ZkResult(v.slug, notes=notes + pnotes, kv=new_kv)
    if v.self_target().key() not in {t.key() for t in targets}:
        res.notes.append("this device is not among the remaining targets — after the "
                         "rotation it can no longer read the vault")
    if not targets:
        raise ZkCliError("no remaining target to wrap the new key for — refusing to rotate "
                         "into a vault only the recovery code opens")
    try:
        body = cred_resolve.resolve(v.slug, token=v.token, transport=v.transport)
    except canopy_web.CanopyError as e:
        raise ZkCliError(f"could not read '{v.slug}'s current values: {str(e)[:300]}") from None
    values = {n: val for n, val in (body.get("values") or {}).items()
              if val is not None and str(val) != ""}
    _confirmed(confirm, targets)
    vk = zk.random_vk()
    wraps = [v.wrap_for(vk, new_kv, t) for t in targets]
    rwrap, code = v.recovery(vk, new_kv)
    payload = {"new_kv": new_kv, "wraps": wraps + [rwrap],
               "values": v.encrypt(vk, new_kv, values)}
    vk = b""  # noqa: F841
    values = {}  # noqa: F841
    v.web("POST", f"{v.base}/rotate", payload)
    res.wrapped = [t.describe() for t in targets]
    res.recovery_code = code
    res.actions.append(f"rotated {v.slug} to kv {new_kv}: {len(payload['values'])} value(s) "
                       f"re-encrypted, {len(targets)} target(s) re-wrapped + a new recovery code")
    return res


def run_settle(v: ZkVault) -> ZkResult:
    """Re-encrypt every ``zks1:`` value — sealed by canopy-web to the devices it knew when
    it minted the value — as ``zk1:`` under the vault key, so every member can read it.
    Opens each with THIS device's entry; a value not sealed to this device is reported,
    not touched."""
    if not v.state().get("enabled"):
        raise ZkCliError(f"'{v.slug}' is not zero-knowledge — nothing to settle")
    res = ZkResult(v.slug)
    try:
        body = cred_resolve.resolve(v.slug, token=v.token, transport=v.transport, raw=True)
    except canopy_web.CanopyError as e:
        raise ZkCliError(f"could not read '{v.slug}'s stored values: {str(e)[:300]}") from None
    sealed = {str(n): str(s) for n, s in (body.get("values") or {}).items()
              if zk.is_sealed_value(s)}
    if not sealed:
        res.actions.append("nothing to settle — no sealed (zks1) values")
        return res
    plain = {}
    for name, stored in sorted(sealed.items()):
        try:
            plain[name] = cred_resolve.open_sealed_for_device(
                stored, agent_id=v.agent_id(), name=name, ecdh=v.ecdh())
        except canopy_web.CanopyError as e:
            res.notes.append(f"{name}: left sealed — {str(e)[:200]}")
    if not plain:
        return res
    vk, kv = v.vk()
    payload = {"kv": kv, "values": v.encrypt(vk, kv, plain)}
    vk = b""  # noqa: F841
    plain = {}  # noqa: F841
    v.web("PUT", f"{v.base}/values", payload)
    res.kv = kv
    names = ", ".join(p["name"] for p in payload["values"])
    res.actions.append(f"settled {len(payload['values'])} sealed value(s) under the vault key "
                       f"at kv {kv}: {names}")
    return res


def settle_quietly(slug: str, *, token: str, transport=None,
                   vault: Optional[ZkVault] = None) -> list[str]:
    """`run_settle` as a side step of import/switch: never raises, returns lines to report.
    Silent when the agent is not zero-knowledge or has nothing sealed."""
    v = vault or ZkVault(slug, token=token, transport=transport)
    try:
        if not v.state().get("enabled"):
            return []
    except Exception:  # noqa: BLE001 — cannot even tell whether it is ZK: nothing to say
        return []
    try:
        res = run_settle(v)
    except Exception as e:  # noqa: BLE001 — opportunistic: the caller's job already succeeded
        return [f"settle skipped: {str(e)[:200]}"]
    return [a for a in res.actions if not a.startswith("nothing to settle")] + res.notes


# ──────────────────────────────────────────────────────────────────────────────
# set --sealed: give an agent a secret without its vault key (zk-v1 §6 + §7)
# ──────────────────────────────────────────────────────────────────────────────
# canopy-web lists the recipients; the ENDORSEMENTS are what is trusted, and only those by
# a person pinned here. The server can still drop recipients (fails safe) but cannot add
# one: a key needs a pinned person's signature over (agent, kind, target, key thumbprint).

def pins_path() -> Path:
    return Path.home() / ".canopy" / "vault-pins.json"


def load_pins(path: Optional[Path] = None) -> dict:
    """``{user_id: {jkt, email, pinned_at}}`` — endorsers this person has accepted."""
    p = path or pins_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise ZkCliError(f"{p} is unreadable ({e}) — fix or remove it; refusing to guess "
                         "who you trust") from None
    if not isinstance(data, dict):
        raise ZkCliError(f"{p} is not a JSON object — refusing to guess who you trust")
    return data


def save_pins(pins: dict, path: Optional[Path] = None) -> None:
    p = path or pins_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(pins, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(p)


def fingerprint(jkt: str) -> str:
    """A key thumbprint in groups of four, the way the card shows it."""
    return " ".join(jkt[i:i + 4] for i in range(0, len(jkt), 4))


@dataclass
class Reader:
    kind: str
    target: str
    public_jwk: dict
    key_jkt: str          # recomputed here, never the server's
    label: str = ""

    def key(self) -> tuple:
        return (self.kind, self.target, self.key_jkt)

    def describe(self) -> str:
        what = "device" if self.kind == "device" else f"member {self.target}"
        return f"{what}{' (' + self.label + ')' if self.label else ''} — key {self.key_jkt[:16]}…"


@dataclass
class Endorser:
    user_id: str
    email: str
    jkt: str              # thumbprint of by_public_jwk, recomputed here
    endorses: list = field(default_factory=list)    # Reader keys

    def describe(self) -> str:
        return f"{self.email or 'user ' + self.user_id} — key fingerprint {fingerprint(self.jkt)}"


@dataclass
class SealPlan:
    agent: str
    agent_id: str
    kv: object
    readers: list = field(default_factory=list)     # Reader, verified-endorsed by an anchor
    skipped: list = field(default_factory=list)     # why each other recipient is left out
    pinned: list = field(default_factory=list)      # Endorser newly pinned by this run


ConfirmEndorsers = Callable[[list, dict], bool]


def _readers(rows) -> tuple[dict, list]:
    """The recipients canopy-web listed whose key is a real P-256 key and whose thumbprint
    (recomputed) is the one the server claims — keyed by (kind, target, key_jkt)."""
    out, skipped = {}, []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        kind, target = str(row.get("kind") or ""), str(row.get("target") or "")
        label = str(row.get("label") or "")
        name = f"{kind} {label or target[:16]}"
        if kind not in ("device", "member") or not target:
            skipped.append(f"{name}: not a device or member recipient")
            continue
        jwk = row.get("public_jwk")
        try:
            zk.public_key_from_jwk(jwk)
            real = zk.jwk_thumbprint(jwk)
        except (zk.ZkError, KeyError, TypeError):
            skipped.append(f"{name}: canopy-web sent no valid public key")
            continue
        if row.get("key_jkt") != real or (kind == "device" and target != real):
            skipped.append(f"{name}: canopy-web's thumbprint does not match the key it sent")
            continue
        jwk = {k: jwk[k] for k in ("kty", "crv", "x", "y")}
        out[(kind, target, real)] = Reader(kind, target, jwk, real, label)
    return out, skipped


def plan_sealed(body: dict, *, pins: dict,
                confirm: Optional[ConfirmEndorsers] = None) -> SealPlan:
    """Decide who a sealed value goes to. Pure apart from ``confirm`` (asked about every
    endorser seen for the first time; None = refuse them). Raises when a pinned person's
    key changed, and when no recipient is endorsed by anyone trusted — it never falls
    back to an unverified key."""
    agent_id = str(body.get("agent_id") or "")
    kv = body.get("kv")
    if not agent_id or kv in (None, ""):
        raise ZkCliError("canopy-web's recipients answer has no agent_id or kv")
    plan = SealPlan(str(body.get("agent") or ""), agent_id, kv)
    readers, plan.skipped = _readers(body.get("recipients"))

    # every endorser key that actually signed a current recipient's current key
    seen: dict = {}                                    # (by, jkt) -> Endorser
    for e in body.get("endorsements") or []:
        if not isinstance(e, dict):
            continue
        rk = (str(e.get("kind") or ""), str(e.get("target") or ""), str(e.get("key_jkt") or ""))
        if rk not in readers:
            continue
        bjwk = e.get("by_public_jwk")
        try:
            bjkt = zk.jwk_thumbprint(bjwk)
        except (KeyError, TypeError):
            continue
        if e.get("by_jkt") != bjkt or not zk.verify_endorsement(e, agent_id, bjwk):
            continue
        by = str(e.get("by") or "")
        if not by:
            continue
        en = seen.setdefault((by, bjkt), Endorser(by, str(e.get("by_email") or ""), bjkt))
        if rk not in en.endorses:
            en.endorses.append(rk)

    anchors: set = set()                               # (by, jkt) trusted for this run
    fresh: list = []
    keys_of: dict = {}
    for (by, bjkt) in seen:
        keys_of.setdefault(by, set()).add(bjkt)
    for (by, bjkt), en in seen.items():
        pin = pins.get(by)
        if isinstance(pin, dict) and pin.get("jkt"):
            if pin["jkt"] != bjkt:
                who = pin.get("email") or en.email or f"user {by}"
                raise ZkCliError(
                    f"{who}'s vault key changed since you pinned it (pinned "
                    f"{fingerprint(pin['jkt'])}, now {fingerprint(bjkt)}) — confirm with them "
                    f"before continuing. Nothing sealed. (Re-pin only once they confirm: remove "
                    f"user {by} from {pins_path()}.)")
            anchors.add((by, bjkt))
        elif len(keys_of[by]) > 1:
            raise ZkCliError(f"{en.email or 'user ' + by} appears with {len(keys_of[by])} "
                             "different vault keys — refusing to pin either. Nothing sealed.")
        else:
            fresh.append(en)

    if fresh:
        if confirm is not None and confirm(fresh, readers):
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for en in fresh:
                pins[en.user_id] = {"jkt": en.jkt, "email": en.email, "pinned_at": now}
                anchors.add((en.user_id, en.jkt))
                plan.pinned.append(en)
        else:
            plan.skipped.append("not trusted (declined): "
                                + ", ".join(en.describe() for en in fresh))

    trusted = {rk for (by, bjkt), en in seen.items() if (by, bjkt) in anchors
               for rk in en.endorses}
    for rk, r in readers.items():
        if rk in trusted:
            plan.readers.append(r)
        else:
            plan.skipped.append(f"{r.describe()}: not confirmed by a person you trust yet — "
                                "it gets the value when an admin next settles the vault")
    if not plan.readers:
        raise ZkCliError("no recipient is endorsed by a person you trust — nothing sealed "
                         "(an admin can confirm who can read this agent in canopy-web, "
                         "then run this again)")
    return plan


def run_set_sealed(v: ZkVault, name: str, read_value: Callable[[], str], *,
                   request_id: str = "", confirm: Optional[ConfirmEndorsers] = None,
                   pins_file: Optional[Path] = None) -> tuple[SealPlan, str]:
    """Seal ``name``'s value (from ``read_value``, called only once the readers are
    settled) to the endorsed recipients and store it — through the secret request when
    ``request_id`` is given. Returns (plan, the one line to print). Never the value."""
    agent_base = f"/api/agents/{v.slug}"
    if request_id:
        req = v.web("GET", f"{agent_base}/secret-requests/{request_id}") or {}
        if name and req.get("name") and req["name"] != name:
            raise ZkCliError(f"request {request_id} asks for {req['name']}, not {name}")
        name = name or str(req.get("name") or "")
        if req.get("status") not in (None, "open"):
            raise ZkCliError(f"request {request_id} is {req.get('status')} — nothing to answer")
    if not name:
        raise ZkCliError("which secret? give NAME (or --request ID)")
    path = f"{v.base}/recipients" + (f"?secret_request={request_id}" if request_id else "")
    body = v.web("GET", path) or {}
    pins = load_pins(pins_file)
    plan = plan_sealed(body, pins=pins, confirm=confirm)
    if plan.pinned:
        save_pins(pins, pins_file)
    value = read_value()
    if not value:
        raise ZkCliError("refusing to store an empty value")
    sealed = zk.seal_value(plan.agent_id, name, plan.kv, value,
                           [(r.kind, r.target, r.public_jwk) for r in plan.readers])
    value = ""  # noqa: F841
    if request_id:
        v.web("PUT", f"{agent_base}/secret-requests/{request_id}/value",
              {"kv": plan.kv, "value_zk": sealed})
    else:
        v.web("PUT", f"{v.base}/values", {"kv": plan.kv,
                                          "values": [{"name": name, "value_zk": sealed}]})
    n = len(plan.readers)
    return plan, f"{name} stored for {plan.agent or v.slug} (sealed to {n} reader{'s' if n != 1 else ''})"


# ──────────────────────────────────────────────────────────────────────────────
# rendering
# ──────────────────────────────────────────────────────────────────────────────

def render_status(st: dict) -> str:
    lines = [f"agent {st['agent']}: zero-knowledge {'ON' if st['enabled'] else 'off'}"
             + (f" (kv {st['kv']})" if st["enabled"] else "")]
    for w in st["wraps"]:
        lines.append(f"  wrap  {w['kind']:<8} {w['target'] if w['target'] is not None else '-'}"
                     f"  kv {w['kv']}  {w['label']}")
    for p in st["pending"]:
        lines.append(f"  PENDING  {p}")
    lines += [f"  note: {n}" for n in st["notes"]]
    lines.append(f"  this device: {st['this_device']}")
    return "\n".join(lines)


def render(res: ZkResult) -> str:
    lines = [f"agent {res.slug}"]
    lines += [f"  wrapped for: {w}" for w in res.wrapped]
    lines += [f"  done: {a}" for a in res.actions]
    lines += [f"  note: {n}" for n in res.notes]
    return "\n".join(lines)


def show_recovery_code(res: ZkResult, echo=click.echo, to_file: str = "") -> None:
    """The one place a recovery code is ever shown — on the terminal, once. With
    ``to_file`` it goes to a NEW 0600 file instead and only the path is shown, so an
    unattended run never puts the code in a transcript or log."""
    if not res.recovery_code:
        return
    if to_file:
        path = Path(to_file).expanduser()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(res.recovery_code + "\n")
        echo(f"RECOVERY CODE for {res.slug} (kv {res.kv}) written to {path} (0600) — "
             "canopy keeps no copy. Move it somewhere offline, then delete the file.")
        return
    echo("")
    echo(f"RECOVERY CODE for {res.slug} (kv {res.kv}) — shown ONCE; canopy keeps no copy.")
    echo("Store it offline. It opens the vault if every device and passkey is lost.")
    echo(f"    {res.recovery_code}")
    echo("")


def _ask(targets: list) -> bool:
    click.echo("The vault key will be wrapped to these PUBLIC keys, as canopy-web lists them:")
    for t in targets:
        click.echo(f"  - {t.describe()}")
    return click.confirm("Proceed?", default=False)


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

AGENT_OPT = click.option("--agent", "slug", required=True, help="The agent's canopy-web slug.")
YES_OPT = click.option("--yes", is_flag=True,
                       help="Do not ask before wrapping to the listed targets.")
RECOVERY_FILE_OPT = click.option(
    "--recovery-file", "recovery_file", default="", metavar="PATH",
    help="Write the recovery code to this NEW file (0600) instead of the terminal.")


def _refuse_existing(recovery_file: str) -> None:
    """Checked BEFORE the vault changes: a code that cannot be written is a code lost."""
    if recovery_file and Path(recovery_file).expanduser().exists():
        raise ZkCliError(f"--recovery-file {recovery_file} already exists — name a new file")


def _vault(slug: str) -> ZkVault:
    from orchestrator.vault_cli import _token
    return ZkVault(slug, token=_token())


@click.group("zk")
def zk_group():
    """Zero-knowledge vault (canopy-web#1550): this device as a member of an agent's vault.

    canopy-web stores only ciphertext and wrapped keys; values are encrypted and the vault
    key wrapped HERE. Never prints a value or a key (a recovery code is shown once)."""


@zk_group.command("status")
@AGENT_OPT
@click.option("--json", "as_json", is_flag=True)
def status_cmd(slug, as_json):
    """Whether the agent is zero-knowledge, its kv, wraps, pending targets, and whether
    this device holds a wrap."""
    st = run_status(_vault(slug))
    click.echo(json.dumps(st, indent=1, sort_keys=True, default=str) if as_json
               else render_status(st))


@zk_group.command("enable")
@AGENT_OPT
@YES_OPT
@RECOVERY_FILE_OPT
def enable_cmd(slug, yes, recovery_file):
    """Turn zero-knowledge on: a new vault key, every current value encrypted here, the
    key wrapped for this device, your keyset, each pending target and a recovery code."""
    _refuse_existing(recovery_file)
    res = run_enable(_vault(slug), confirm=None if yes else _ask)
    click.echo(render(res))
    show_recovery_code(res, to_file=recovery_file)


def _ask_endorsers(fresh: list, readers: dict) -> bool:
    click.echo("First contact: these people vouch for who can read this agent, and you have "
               "not pinned them yet. Check each fingerprint with them out of band.", err=True)
    for en in fresh:
        click.echo(f"  - {en.describe()}", err=True)
        for rk in en.endorses:
            click.echo(f"      endorses {readers[rk].describe()}", err=True)
    return click.confirm("Trust (pin) them and seal to what they endorse?", default=False,
                         err=True)


@zk_group.command("set")
@AGENT_OPT
@click.argument("name", required=False, default="")
@click.option("--from-env", "from_env", default="",
              help="Read the value from this environment variable instead of stdin.")
@click.option("--sealed", is_flag=True,
              help="Seal the value (zks1) to the agent's readers that a person you trust "
                   "endorsed — no vault key needed. Endorsers are pinned in "
                   "~/.canopy/vault-pins.json.")
@click.option("--request", "request_id", default="", metavar="ID",
              help="Answer the agent's secret request ID (implies --sealed; NAME optional).")
@click.option("--yes", is_flag=True,
              help="With --sealed: pin first-seen endorsers without asking.")
def set_cmd(slug, name, from_env, sealed, request_id, yes):
    """Store one value. The value is read from stdin (or a hidden prompt on a terminal, or
    --from-env) — never from argv. Default: encrypted here under the vault key (this
    device must hold a wrap). --sealed / --request: sealed to endorsed readers instead."""
    import os

    def read_value() -> str:
        if from_env:
            if from_env not in os.environ:
                raise ZkCliError(f"${from_env} is not set")
            return os.environ[from_env]
        if sys.stdin.isatty():
            return click.prompt(f"{name or 'value'}", hide_input=True)
        return sys.stdin.read().rstrip("\n")

    if sealed or request_id:
        if yes:
            confirm = lambda fresh, readers: True  # noqa: E731
        elif from_env or sys.stdin.isatty():
            confirm = _ask_endorsers
        else:
            def confirm(fresh, readers):     # the value is on stdin: never read it as a "y"
                raise ZkCliError(
                    "first contact with " + ", ".join(en.describe() for en in fresh)
                    + " needs a confirmation, and stdin carries the value — run it on a "
                      "terminal or with --from-env, or pass --yes once you have checked the "
                      "fingerprint")
        plan, line = run_set_sealed(_vault(slug), name, read_value, request_id=request_id,
                                    confirm=confirm)
        for en in plan.pinned:
            click.echo(f"pinned {en.describe()}", err=True)
        for s in plan.skipped:
            click.echo(f"not sealed to: {s}", err=True)
        click.echo(line)
        return
    if not name:
        raise ZkCliError("which secret? give NAME")
    value = read_value()
    if not value:
        raise ZkCliError("refusing to store an empty value")
    click.echo(render(run_set(_vault(slug), {name: value})))


@zk_group.command("grant")
@AGENT_OPT
@YES_OPT
def grant_cmd(slug, yes):
    """Wrap the vault key for every pending target (approved devices of routed runners,
    agent admins with a keyset) — unwrapped with this device's own wrap."""
    click.echo(render(run_grant(_vault(slug), confirm=None if yes else _ask)))


@zk_group.command("rotate")
@AGENT_OPT
@YES_OPT
@click.option("--drop", "drops", multiple=True, metavar="KIND:ID",
              help="Leave a member out of the new key, e.g. device:12 or member:7 "
                   "(revoked devices and removed admins canopy-web marks inactive are "
                   "left out already).")
@RECOVERY_FILE_OPT
def rotate_cmd(slug, yes, drops, recovery_file):
    """A new vault key and kv: re-wrap for every remaining member, re-encrypt every value,
    and a new recovery code. How access is REMOVED."""
    drop = set()
    for d in drops:
        kind, _, ident = d.partition(":")
        if kind not in ("device", "member") or not ident:
            raise ZkCliError(f"--drop {d!r}: use device:<key id> or member:<user id>")
        drop.add((kind, ident))
    _refuse_existing(recovery_file)
    res = run_rotate(_vault(slug), drop=drop, confirm=None if yes else _ask)
    click.echo(render(res))
    show_recovery_code(res, to_file=recovery_file)


@zk_group.command("settle")
@AGENT_OPT
def settle_cmd(slug):
    """Re-encrypt values canopy-web sealed to this device (``zks1:`` — a Gmail token from
    Connect Google mailbox, a rotated canopy-pat) under the vault key, so every member and
    device can read them. Import and switch do this on their own."""
    click.echo(render(run_settle(_vault(slug))))
