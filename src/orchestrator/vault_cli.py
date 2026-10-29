"""`canopy vault` — Canopy Vault: an agent's credentials on canopy-web.

    canopy vault status  --agent X [--json]
    canopy vault device  [--enroll | --reenroll | --refresh] [--runner ID] [--kind K | --software]
    canopy vault device  --public-jwk [--kind software]      this key's PUBLIC JWK (made if missing)
    canopy vault device  --registered KEY_ID --runner ID     record a registration made elsewhere
    canopy vault binding --agent X on|off                    require an approved device key
    canopy vault zk status|enable|set|grant|rotate --agent X zero-knowledge (vault_zk_cli.py)
    canopy vault open-slot-data  < lease.json               a pool lease's sealed slot secrets
    canopy vault google-mint --agent X < work.json          finish a Google sign-in on a runner

Values are declared on the agent's Credentials page on canopy-web. Here, a PERSON who is the
agent's owner or admin sees which credentials are set, when each was last read, and this
machine's device key. Values pass through this process's memory and the canopy-web API; none
is ever printed, logged or put in argv.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Callable, Optional

import click

from orchestrator import canopy_web, device_key

Runner = Callable[..., subprocess.CompletedProcess]


class VaultCliError(click.ClickException):
    pass


def _web(token: str, transport=None) -> Callable:
    def call(method: str, path: str, body=None):
        try:
            return canopy_web.call(method, path, body, token=token, transport=transport)
        except canopy_web.CanopyError as e:
            raise VaultCliError(f"canopy-web refused {method} {path}: {str(e)[:300]}") from None
    return call


def credential_status(slug: str, web) -> dict:
    rows = web("GET", f"/api/agents/{slug}/credentials/status") or []
    return {str(r.get("name")): r for r in rows if isinstance(r, dict)}


# ──────────────────────────────────────────────────────────────────────────────
# status
# ──────────────────────────────────────────────────────────────────────────────

def _last_read(row: dict) -> str:
    for key in ("last_read_at", "last_read", "read_at"):
        v = row.get(key)
        if isinstance(v, dict):
            v = v.get("at") or v.get("when")
        if v:
            return str(v)
    return ""


def run_status(slug: str, *, token: str, transport=None) -> dict:
    web = _web(token, transport)
    present = credential_status(slug, web)
    rows = [{"name": name, "present": bool(row.get("set")), "updated_at": row.get("updated_at"),
             "last_read": _last_read(row)} for name, row in sorted(present.items())]
    return {"agent": slug, "refs": rows, "device": device_key.read_status()}


def render_status(st: dict) -> str:
    lines = [f"agent {st['agent']}: credentials on canopy-web"]
    width = max([len(r["name"]) for r in st["refs"]] + [4])
    lines.append(f"  {'name':<{width}}  {'state':<8}  last read")
    for r in st["refs"]:
        lines.append(f"  {r['name']:<{width}}  {'set' if r['present'] else 'MISSING':<8}  "
                     f"{r['last_read'] or '-'}")
    lines.append(render_device(st.get("device") or {}))
    return "\n".join(lines)


def render_device(dev: dict) -> str:
    if not dev.get("jkt"):
        return "device key: none on this machine — `canopy vault device --enroll`"
    line = (f"device key: {dev.get('kind', '?')} {dev['jkt'][:16]}… for runner "
            f"{dev.get('runner') or '?'} — {dev.get('status') or 'unknown'}")
    if dev.get("role_arn"):
        line += f"\n  instance role: {dev['role_arn']}"
    if dev.get("status") == "pending" and dev.get("approve_url"):
        line += f"\n  approve it (passkey step-up): {dev['approve_url']}"
    return line


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def _token() -> str:
    from orchestrator.workspace_cli import person_token
    tok = person_token()
    if not tok:
        raise VaultCliError("not signed in to canopy-web — run /canopy:canopy-web-pat-mint")
    return tok


AGENT_OPT = click.option("--agent", "slug", required=True, help="The agent's canopy-web slug.")


@click.group("vault")
def vault_group():
    """Canopy Vault: an agent's credentials on canopy-web. Never prints a value."""


@vault_group.command("status")
@AGENT_OPT
@click.option("--json", "as_json", is_flag=True)
def status_cmd(slug, as_json):
    """Each credential the agent has on canopy-web: set or missing, and when it was last
    read. Plus this machine's device key."""
    st = run_status(slug, token=_token())
    click.echo(json.dumps(st, indent=1, sort_keys=True, default=str) if as_json
               else render_status(st))


@vault_group.command("device")
@click.option("--enroll", is_flag=True, help="Make the key if needed and register it.")
@click.option("--reenroll", is_flag=True,
              help="Revoke the registered key, delete it here, make and register a new one.")
@click.option("--refresh", is_flag=True, help="Re-read the key's status from canopy-web.")
@click.option("--runner", "runner_id", default="",
              help="Runner id to register for (default: ~/.canopy/runner.json's).")
@click.option("--software", is_flag=True,
              help="Use a software key even where a stronger kind exists (= --kind software).")
@click.option("--kind", type=click.Choice(device_key.KINDS), default=None,
              help="Insist on one kind (default: the strongest this machine has — "
                   "secure-enclave on a Mac, tpm on Windows, aws-iam on an EC2 box with an "
                   "instance role, else software).")
@click.option("--label", default="", help="Shown on canopy-web (default: host name).")
@click.option("--ecdh-jwk", "ecdh_jwk", is_flag=True,
              help="Make this machine's ECDH key if needed (for --kind, else the existing "
                   "key's kind) and print {kind, jkt, jwk} — its PUBLIC half. A cloud runner "
                   "enrolling itself uses this (canopy-web#1550).")
@click.option("--public-jwk", "public_jwk", is_flag=True,
              help="Make this machine's SIGNING key if needed (for --kind, else the "
                   "strongest) and print {kind, jkt, jwk, created} — its PUBLIC half, with no "
                   "canopy-web call. A box that pairs its units in one call (pair-by-role) "
                   "collects these.")
@click.option("--registered", "registered_id", default=None, metavar="KEY_ID",
              help="Record that canopy-web registered THIS key as device key KEY_ID for "
                   "--runner (as pair-by-role does), so its proofs are sent. Needs --runner.")
@click.option("--status", "reg_status", default="active", show_default=True,
              type=click.Choice(["active", "pending"]), help="With --registered.")
@click.option("--json", "as_json", is_flag=True)
def device_cmd(enroll, reenroll, refresh, runner_id, software, kind, label, ecdh_jwk,
               public_jwk, registered_id, reg_status, as_json):
    """This machine's device key: show it, enroll it, or re-enroll it.

    canopy-web hands a `require_device_binding` agent's credentials only to a request
    carrying a proof from an APPROVED device key: a DPoP JWS (secure-enclave, tpm,
    software) or, on an EC2 cloud runner, a signed STS GetCallerIdentity for the
    instance role (aws-iam). Enrolling registers it as pending; a person approves it in
    canopy-web with a passkey step-up."""
    if software and kind not in (None, device_key.KIND_SOFTWARE):
        raise VaultCliError("--software and --kind disagree")
    kind = kind or (device_key.KIND_SOFTWARE if software else None)
    if ecdh_jwk:
        click.echo(json.dumps(run_ecdh_jwk(kind), sort_keys=True))
        return
    if public_jwk:
        click.echo(json.dumps(run_public_jwk(kind), sort_keys=True))
        return
    if registered_id is not None:
        click.echo(json.dumps(run_registered(registered_id, runner_id, reg_status, kind),
                              sort_keys=True))
        return
    runner_id = runner_id or device_key.runner_id_from_config() or _computer_runner_id()
    token = _token() if (enroll or reenroll or refresh) else ""
    web = _web(token) if token else None
    try:
        if reenroll:
            old = device_key.read_status()
            if old.get("id"):
                try:
                    web("DELETE", f"{device_key.REGISTER_PATH}{old['id']}", None)
                except VaultCliError as e:
                    click.echo(f"[canopy vault] could not revoke the old key ({e}); "
                               "revoke it in canopy-web", err=True)
            device_key.remove_local()
        if enroll or reenroll:
            # A token revoked before this box had a key: ask through the stranded door.
            st = device_key.enroll(runner_id=runner_id, call=web, label=label, kind=kind,
                                   owner_token=token)
        elif refresh:
            st = device_key.refresh(runner_id=runner_id or device_key.read_status().get("runner", ""),
                                    call=web)
        else:
            st = device_key.read_status()
            key = device_key.load()
            if key is not None and not st.get("jkt"):
                st = {"kind": key.kind, "jkt": key.jkt, "status": "unregistered"}
    except device_key.DeviceKeyError as e:
        raise VaultCliError(str(e))
    if as_json:
        click.echo(json.dumps(st, sort_keys=True, default=str))
    else:
        click.echo(render_device(st))


def _computer_runner_id() -> str:
    """This computer's own runner id from its registration for local use
    (~/.canopy/private-runner.json) — canopy-web gives a computer with no runner app a
    "computer" runner to hold its device key. "" when there is none."""
    from orchestrator.local_cli import read_private_runner
    return str(read_private_runner().get("runner_id") or "")


RUNNER_SESSIONS_PATH = "/api/runner-sessions/"


def run_runner_session(runner_id: str, base_url: str = "", *, transport=None) -> dict:
    """Sign this runner in AS ITSELF: trade a proof of this machine's approved device
    key for an hour-long runner token (canopy-web `POST /api/runner-sessions/`, #1594).
    No person's PAT is sent — the proof is the whole authority, so a token tidied up
    in Settings → Tokens can never strand a runner again (2026-10-17, cloud-ec2-2).

    Returns `{access_token, expires_at, runner}`. Raises VaultCliError when there is
    no runner id, no proof this machine can make (no key, or not registered), or
    canopy-web refuses it (the key is not approved yet)."""
    runner_id = runner_id or device_key.runner_id_from_config()
    if not runner_id:
        raise VaultCliError("no runner id (pass --runner, or pair: ~/.canopy/runner.json)")
    url = canopy_web.resolve_base_url(base_url or None) + RUNNER_SESSIONS_PATH
    proof = device_key.proof_headers("POST", url)
    if not proof:
        raise VaultCliError("this machine has no registered device key to prove itself with "
                            "— canopy vault device --enroll")
    headers = {**canopy_web.provenance_headers(), **proof, "Content-Type": "application/json"}
    status, text = (transport or canopy_web.urllib_transport)(
        "POST", url, headers, json.dumps({"runner": runner_id}).encode("utf-8"))
    if status != 201:
        raise VaultCliError(f"canopy-web refused the device proof ({status}) — is this "
                            f"machine's device key approved? {text[:300]}")
    return json.loads(text)


@vault_group.command("runner-session")
@click.option("--runner", "runner_id", default="",
              help="Runner id (default: ~/.canopy/runner.json's).")
@click.option("--base-url", default="", help="canopy-web (default: CANOPY_WEB_API_URL).")
def runner_session_cmd(runner_id, base_url):
    """Print a runner token for this machine, bought with its device key, as JSON
    `{access_token, expires_at, runner}`. The canopy runner calls this instead of
    sending its owner's PAT."""
    click.echo(json.dumps(run_runner_session(runner_id, base_url), sort_keys=True))


@vault_group.command("runner-credentials")
@click.option("--runner", "runner_id", default="",
              help="Runner id (default: ~/.canopy/runner.json's).")
@click.option("--base-url", default="", help="canopy-web (default: CANOPY_WEB_API_URL).")
@click.option("--print-json", "print_json", is_flag=True, required=True,
              help="Print `{values, vault_layers}` as one JSON line (the runner reads it).")
def runner_credentials_cmd(runner_id, base_url, print_json):
    """A cloud runner's own Claude credentials, opened from the vault that holds them
    with this machine's device key. For the canopy runner, which runs it with its own
    runner token as CANOPY_TOKEN; it prints plaintext, so only with --print-json."""
    from orchestrator import cred_resolve

    runner_id, token = _runner_and_token(runner_id, base_url)
    try:
        out = cred_resolve.resolve_runner(runner_id, token=token, base_url=base_url or None)
    except canopy_web.CanopyError as e:
        raise VaultCliError(str(e)[:400]) from None
    click.echo(json.dumps(out, sort_keys=True))


@vault_group.command("open-slot-data")
@click.option("--capability", default="", help="The pool capability (default: the lease's `capability`).")
@click.option("--slot", default="", help="The slot (default: the lease's `slot`).")
def open_slot_data_cmd(capability, slot):
    """Open a pool lease's slot data with this machine's device key.

    Reads, as JSON on stdin, the lease canopy-web returned to the user who acquired it
    (or just its `slot_data`, with --capability and --slot), and prints
    `{"public": {...}, "secret": {key: plaintext}}` — plaintext, so pipe it straight into
    the program that needs it, never into a log. A canopy-web that still sends `secret` in
    clear is passed through."""
    from orchestrator import cred_resolve

    try:
        body = json.loads(sys.stdin.read() or "null")
    except ValueError:
        raise VaultCliError("stdin is not JSON (a lease, or its slot_data)") from None
    if not isinstance(body, dict):
        raise VaultCliError("pass a lease (or its slot_data) as a JSON object on stdin")
    data = body.get("slot_data") if "slot_data" in body else body
    if data is None:
        raise VaultCliError("this lease carries no slot_data (not ready, not yours, or the slot has none)")
    try:
        out = cred_resolve.open_slot_data(data, capability=capability or str(body.get("capability") or ""),
                                          slot=slot or str(body.get("slot") or ""))
    except canopy_web.CanopyError as e:
        raise VaultCliError(str(e)[:400]) from None
    click.echo(json.dumps(out, sort_keys=True))


@vault_group.command("runner-seal")
@click.option("--runner", "runner_id", default="",
              help="Runner id (default: ~/.canopy/runner.json's).")
@click.option("--base-url", default="", help="canopy-web (default: CANOPY_WEB_API_URL).")
def runner_seal_cmd(runner_id, base_url):
    """Seal a cloud runner's own credentials into its vault: reads `{name: plaintext}`
    as JSON on stdin (names its credential list uses, `claude-token:<key>`), prints
    `{names, vault_id}`. For the canopy runner, as itself (CANOPY_TOKEN)."""
    from orchestrator import cred_resolve

    runner_id, token = _runner_and_token(runner_id, base_url)
    try:
        values = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        raise VaultCliError("stdin is not JSON `{name: value}`") from None
    if not isinstance(values, dict) or not values:
        raise VaultCliError("nothing to seal: pass `{name: value}` on stdin")
    try:
        out = cred_resolve.seal_runner_values(runner_id, values, token=token, base_url=base_url or None)
    except canopy_web.CanopyError as e:
        raise VaultCliError(str(e)[:400]) from None
    click.echo(json.dumps(out, sort_keys=True))


@vault_group.command("google-mint")
@AGENT_OPT
@click.option("--runner", "runner_id", default="",
              help="Runner id (default: ~/.canopy/runner.json's).")
@click.option("--base-url", default="", help="canopy-web (default: CANOPY_WEB_API_URL).")
def google_mint_cmd(slug, runner_id, base_url):
    """Finish an agent's Google sign-in on this box (canopy-web never sees the token).

    Reads, as JSON on stdin, the work canopy-web handed the runner (`code`, `redirect_uri`,
    `client_id`, `client`, `expected_account`, `agent_id`, `kv`, `recipients`) plus the
    `code_verifier` the runner kept. Exchanges the code with Google using the OAuth client
    from the agent's vault, and prints ONE JSON line: `{name, kv, value_zk, account}` —
    the token encrypted for the agent — or `{error, detail}` (exit 1). For the canopy
    runner, as itself (CANOPY_TOKEN); never prints the token."""
    from orchestrator import google_mint

    try:
        work = json.loads(sys.stdin.read() or "null")
    except ValueError:
        work = None
    if not isinstance(work, dict):
        click.echo(json.dumps({"error": "bad-request", "detail": "stdin is not a JSON object"}))
        raise SystemExit(1)
    _, token = _runner_and_token(runner_id, base_url)
    try:
        out = google_mint.run(slug, work, token=token, base_url=base_url or None)
    except google_mint.MintError as e:
        click.echo(json.dumps(e.out(), sort_keys=True))
        raise SystemExit(1) from None
    click.echo(json.dumps(out, sort_keys=True))


def _runner_and_token(runner_id: str, base_url: str) -> tuple[str, str]:
    """This runner's id and its OWN runner token: CANOPY_TOKEN (what the runner passes),
    else one bought with this machine's device key — never a person's token, which
    canopy-web refuses here (`runner_only`)."""
    from orchestrator import runner_session

    runner_id = runner_id or device_key.runner_id_from_config()
    if not runner_id:
        raise VaultCliError("no runner id (pass --runner, or pair: ~/.canopy/runner.json)")
    token = (os.environ.get("CANOPY_TOKEN", "").strip()
             or runner_session.token(canopy_web.resolve_base_url(base_url or None)))
    if not token:
        raise VaultCliError("no runner token: run as the canopy runner (CANOPY_TOKEN), or on a "
                            "machine whose approved device key can buy one")
    return runner_id, token


@vault_group.command("binding")
@AGENT_OPT
@click.argument("state", type=click.Choice(["on", "off"]))
@click.option("--json", "as_json", is_flag=True)
def binding_cmd(slug, state, as_json):
    """Turn device binding on or off for an agent (`require_device_binding`).

    On: canopy-web resolves the agent's credentials only for a request carrying a proof
    from an APPROVED device key of a runner the agent routes to — enroll and approve every
    such runner's key first (`canopy vault device`), or its turns lose their credentials.
    Owner/admin only; canopy-web may ask for a passkey step-up."""
    res = run_binding(slug, state == "on", token=_token())
    click.echo(json.dumps(res, sort_keys=True) if as_json else
               f"agent {slug}: require_device_binding={'on' if res['require_device_binding'] else 'off'}")


def run_binding(slug: str, on: bool, *, token: str, transport=None) -> dict:
    web = _web(token, transport)
    body = web("PATCH", f"/api/agents/{slug}/device-binding",
               {"require_device_binding": bool(on)}) or {}
    got = body.get("require_device_binding")
    if got is not None and bool(got) != bool(on):
        raise VaultCliError(f"canopy-web answered require_device_binding={got!r}, expected {on!r}")
    return {"agent": slug, "require_device_binding": bool(on if got is None else got)}


def run_public_jwk(kind: Optional[str] = None) -> dict:
    """This machine's signing key (made if missing): {kind, jkt, jwk, created} — public
    only. An aws-iam identity has no JWK, so it is refused: ask for `--kind software`."""
    try:
        key, created = device_key.ensure(kind=kind)
    except device_key.DeviceKeyError as e:
        raise VaultCliError(str(e))
    if key.kind == device_key.KIND_AWS_IAM:
        raise VaultCliError("this machine's device key is its aws-iam instance role, which has "
                            "no public JWK — pass --kind software")
    return {"kind": key.kind, "jkt": key.jkt, "jwk": key.public_jwk, "created": created}


def run_registered(key_id: str, runner_id: str, status: str = "active",
                   kind: Optional[str] = None) -> dict:
    """Cache that canopy-web registered this machine's key as ``key_id`` for ``runner_id``
    (written by whoever paired it — e.g. pair-by-role for every unit on a box), so
    `proof_headers` attaches its proofs. Only the public thumbprints are recorded."""
    import time

    if not (key_id or "").strip():
        raise VaultCliError("--registered needs the device key id canopy-web returned")
    if not (runner_id or "").strip():
        raise VaultCliError("--registered needs --runner (the runner this key signs in as)")
    try:
        key = device_key.load()
        if key is None:
            raise VaultCliError("no device key here to record — `canopy vault device "
                                "--public-jwk --kind software` makes one")
        if kind is not None and key.kind != kind:
            raise VaultCliError(f"this machine's key is {key.kind}, not {kind}")
        st = {"id": key_id.strip(), "jkt": key.jkt, "status": status, "kind": key.kind,
              "runner": runner_id.strip(), "approve_url": "", "checked_at": time.time()}
        if key.role_arn:
            st["role_arn"] = key.role_arn
        try:
            ecdh = device_key.load_ecdh()
        except device_key.DeviceKeyError:
            ecdh = None
        if ecdh is not None:
            st.update(ecdh_jkt=ecdh.jkt, ecdh_kind=ecdh.kind)
        device_key.write_status(st)
    except device_key.DeviceKeyError as e:
        raise VaultCliError(str(e))
    return st


def run_ecdh_jwk(kind: Optional[str] = None) -> dict:
    """This machine's ECDH key (made if missing): {kind, jkt, jwk} — public only."""
    try:
        if kind is None:
            existing = device_key.load()
            if existing is None:
                raise VaultCliError("no device key here yet — pass --kind, or "
                                    "`canopy vault device --enroll` first")
            kind = existing.kind
        key, created = device_key.ensure_ecdh(kind)
    except device_key.DeviceKeyError as e:
        raise VaultCliError(str(e))
    return {"kind": key.kind, "jkt": key.jkt, "jwk": key.public_jwk, "created": created}


from orchestrator.vault_zk_cli import ws_group, zk_group  # noqa: E402 — after vault_group exists

vault_group.add_command(zk_group)
vault_group.add_command(ws_group)
