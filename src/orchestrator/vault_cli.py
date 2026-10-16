"""`canopy vault` — Canopy Vault: an agent's credentials on canopy-web instead of 1Password.

An OPT-IN alternative backend, chosen per agent (`Agent.credential_source`, canopy#946 /
canopy-web#1538). 1Password stays the default and stays fully supported; nothing here
changes an agent that has not been switched.

    canopy vault import  --agent X [--switch] [--repo DIR]   1Password -> canopy-web
    canopy vault export  --agent X [--check]                 canopy-web -> 1Password (rollback)
    canopy vault switch  --agent X --to canopy-web|1password
    canopy vault status  --agent X [--no-drift] [--json]
    canopy vault device  [--enroll | --reenroll | --refresh] [--runner ID] [--software]

WHAT IS AN AGENT'S SET OF REFS. Every `NAME=…` line of the agent's `.env.tpl` (the one
source of truth for what `~/.X/.env` holds): an `op://` reference is read from 1Password,
a literal (a non-secret like a channel address) is carried as-is so the canopy-web env is
complete. Plus any `runtime_sources` entry canopy-web records for the agent that the
template does not already name. Items another flow owns are left alone (`gog-token` is
minted by "Connect Google mailbox", the OAuth client lives in the shared vault) — the
same `agent_vault.KINDS` table `canopy agent vault` uses.

WHO RUNS IT. A PERSON who is the agent's owner or admin on canopy-web, with their own
`op` (biometric unlock is fine — this is a human command). An agent's service-account
key in the environment is dropped, exactly as `canopy agent vault` does. Values pass
through this process's memory and the canopy-web API; none is ever printed, logged or put
in argv.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

import click

from orchestrator import canopy_web, device_key
from orchestrator import agent_vault as av

SOURCES = ("1password", "canopy-web")
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
_WHOLE_REF = re.compile(r"^[\"']?(op://[^\s\"']+)[\"']?$")

Runner = Callable[..., subprocess.CompletedProcess]


class VaultCliError(click.ClickException):
    pass


# ──────────────────────────────────────────────────────────────────────────────
# the agent's refs
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Ref:
    name: str                 # the credential's name on canopy-web (an env var name)
    op: str = ""              # op://vault/item/field, ${AGENT_VAULT} already expanded
    literal: Optional[str] = None
    origin: str = ".env.tpl"  # ".env.tpl" | "runtime_sources"

    @property
    def item(self) -> str:
        parts = self.op.removeprefix("op://").split("/")
        return parts[1] if len(parts) >= 3 else ""

    @property
    def vault(self) -> str:
        return self.op.removeprefix("op://").split("/")[0] if self.op else ""

    @property
    def field_name(self) -> str:
        return self.op.rsplit("/", 1)[-1] if self.op else ""


def parse_template(text: str, *, vault: str, slug: str) -> tuple[list[Ref], list[str]]:
    """(refs, notes) from a .env.tpl. Comments and blank values are skipped; a value that
    embeds a reference inside other text is reported, not guessed at."""
    from orchestrator.agent_instance import expand_vars, inject_env
    env = inject_env({}, vault, slug)
    refs: list[Ref] = []
    notes: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = _LINE.match(s)
        if not m:
            continue
        name, raw = m.group(1), m.group(2)
        if " #" in raw and not raw.startswith(("'", '"')):
            raw = raw.split(" #", 1)[0].rstrip()
        if not raw or name in seen:
            continue
        seen.add(name)
        whole = _WHOLE_REF.match(raw)
        if whole:
            refs.append(Ref(name, op=expand_vars(whole.group(1), env)))
        elif "op://" in raw:
            notes.append(f"{name}: a reference inside other text — not imported")
        else:
            refs.append(Ref(name, literal=raw.strip("\"'")))
    return refs, notes


def refs_from_sources(sources: dict, known: set[str]) -> list[Ref]:
    out = []
    for name, src in sorted((sources or {}).items()):
        if name in known or not isinstance(src, dict) or src.get("local_only"):
            continue
        if src.get("op"):
            out.append(Ref(str(name), op=str(src["op"]), origin="runtime_sources"))
        elif "value" in src:
            out.append(Ref(str(name), literal=str(src["value"]), origin="runtime_sources"))
    return out


def owned_elsewhere(ref: Ref) -> str:
    """Why this ref is not the vault's to move ("" when it is): its item is minted or
    kept by another flow (agent_vault.KINDS)."""
    if not ref.op:
        return ""
    kind = av.kind_of(ref.item)
    if kind.kind in (av.LINKED, av.SHARED, av.UNUSED):
        return kind.what
    return ""


@dataclass
class Agent:
    slug: str
    record: dict
    vault: str
    refs: list[Ref]
    skipped: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    template: str = ""


def _web(token: str, transport=None) -> Callable:
    def call(method: str, path: str, body=None):
        try:
            return canopy_web.call(method, path, body, token=token, transport=transport)
        except canopy_web.CanopyError as e:
            raise VaultCliError(f"canopy-web refused {method} {path}: {str(e)[:300]}") from None
    return call


def load_agent(slug: str, *, token: str, repo: Optional[Path] = None,
               gh_runner: Runner = subprocess.run, transport=None) -> Agent:
    from orchestrator import cred_cli
    web = _web(token, transport)
    record = web("GET", f"/api/agents/{slug}/") or {}
    try:
        vault = cred_cli.agent_vault_name(slug)
    except cred_cli.CredError as e:
        raise VaultCliError(e.message)
    repo = repo or cred_cli._agent_repo(slug)
    try:
        text, where = av.load_template(record, repo, gh_runner)
    except av.VaultError as e:
        raise VaultCliError(str(e))
    refs, notes = parse_template(text, vault=vault, slug=slug)
    refs += refs_from_sources(record.get("runtime_sources") or {}, {r.name for r in refs})
    kept, skipped = [], []
    for r in refs:
        why = owned_elsewhere(r)
        (skipped.append((r.name, why)) if why else kept.append(r))
    return Agent(slug, record, vault, kept, skipped, notes, where)


# ──────────────────────────────────────────────────────────────────────────────
# 1Password, as the person
# ──────────────────────────────────────────────────────────────────────────────

def op_read(op: av.Op, ref: str) -> tuple[Optional[str], str]:
    """(value, "") or (None, why). The value goes from op's stdout (a pipe) to memory."""
    p = op._run("read", "--no-newline", ref)
    if p.returncode != 0:
        return None, op._why(p)
    return p.stdout, ""


def fingerprint(value: Optional[str]) -> str:
    return "" if value is None else hashlib.sha256(value.strip().encode()).hexdigest()


# ──────────────────────────────────────────────────────────────────────────────
# import
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Row:
    name: str
    state: str
    note: str = ""


@dataclass
class Result:
    slug: str
    rows: list[Row] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    ok: bool = True

    def as_dict(self) -> dict:
        return asdict(self)


def _require_admin(agent: Agent, what: str) -> None:
    if agent.record and agent.record.get("is_admin") is False:
        raise VaultCliError(f"you are not {agent.slug}'s owner or an admin on canopy-web, so "
                            f"canopy-web would refuse to {what} — ask its owner to add you")


def credential_status(slug: str, web) -> dict:
    rows = web("GET", f"/api/agents/{slug}/credentials/status") or []
    return {str(r.get("name")): r for r in rows if isinstance(r, dict)}


def run_import(slug: str, *, token: str, op: av.Op, switch: bool = False,
               repo: Optional[Path] = None, transport=None,
               gh_runner: Runner = subprocess.run) -> Result:
    agent = load_agent(slug, token=token, repo=repo, gh_runner=gh_runner, transport=transport)
    _require_admin(agent, "store its credentials")
    web = _web(token, transport)
    res = Result(slug, notes=list(agent.notes))
    res.notes += [f"{n}: left alone — {why}" for n, why in agent.skipped]
    why = op.signed_in()
    if why:
        raise VaultCliError(f"1Password is not answering as a person ({why}). Unlock the "
                            "1Password app (Settings → Developer → Integrate with 1Password "
                            "CLI) and re-run.")
    values: dict[str, str] = {}
    for r in agent.refs:
        if r.literal is not None:
            if r.literal.strip():
                values[r.name] = r.literal
                res.rows.append(Row(r.name, "literal", f"from {r.origin}"))
            continue
        val, err = op_read(op, r.op)
        if val is None or not val.strip():
            res.rows.append(Row(r.name, "unreadable", err or "empty in 1Password"))
            res.ok = False
            continue
        values[r.name] = val
        res.rows.append(Row(r.name, "read", f"1Password item {r.item}"))
    if values:
        web("PUT", f"/api/agents/{slug}/credentials", {"values": values})
        res.actions.append(f"wrote {len(values)} value(s) to canopy-web "
                           f"(1Password vault {agent.vault} untouched)")
    values = {}  # noqa: F841 — drop our only references
    present = credential_status(slug, web)
    missing = [r.name for r in agent.refs if not (present.get(r.name) or {}).get("set")]
    for row in res.rows:
        if row.name in missing and row.state in ("read", "literal"):
            row.state, row.note = "not-stored", "canopy-web does not report it set"
            res.ok = False
    if switch:
        if missing:
            res.notes.append(f"NOT switched: {', '.join(missing)} not on canopy-web")
            res.ok = False
        else:
            _switch(slug, "canopy-web", web)
            res.actions.append(f"switched {slug} to canopy-web")
    return res


def _switch(slug: str, to: str, web) -> None:
    from orchestrator import cred_cli
    body = web("PATCH", f"/api/agents/{slug}/credential-source", {"credential_source": to})
    got = (body or {}).get("credential_source")
    if got is not None and got != to:
        raise VaultCliError(f"canopy-web answered credential_source={got!r}, expected {to!r}")
    cred_cli._cache_drop(slug)  # the 10-minute /access cache still names the old backend


# ──────────────────────────────────────────────────────────────────────────────
# switch
# ──────────────────────────────────────────────────────────────────────────────

def run_switch(slug: str, to: str, *, token: str, repo: Optional[Path] = None,
               transport=None, gh_runner: Runner = subprocess.run) -> Result:
    if to not in SOURCES:
        raise VaultCliError(f"--to must be one of {', '.join(SOURCES)}")
    web = _web(token, transport)
    res = Result(slug)
    if to == "canopy-web":
        agent = load_agent(slug, token=token, repo=repo, gh_runner=gh_runner,
                           transport=transport)
        present = credential_status(slug, web)
        needed = [r.name for r in agent.refs if r.op or (r.literal or "").strip()]
        missing = [n for n in needed if not (present.get(n) or {}).get("set")]
        if missing:
            raise VaultCliError(f"refusing to switch {slug} to canopy-web: {', '.join(missing)} "
                                f"not stored there — run `canopy vault import --agent {slug}`")
        res.notes.append(f"all {len(needed)} declared ref(s) are on canopy-web")
    else:
        res.notes.append(f"values rotated on canopy-web during the pilot reach 1Password only "
                         f"through `canopy vault export --agent {slug}` — run it first")
    _switch(slug, to, web)
    res.actions.append(f"switched {slug} to {to}")
    return res


# ──────────────────────────────────────────────────────────────────────────────
# export (rollback)
# ──────────────────────────────────────────────────────────────────────────────

def run_export(slug: str, *, token: str, op: av.Op, check: bool = False,
               repo: Optional[Path] = None, transport=None,
               gh_runner: Runner = subprocess.run) -> Result:
    from orchestrator import cred_resolve
    agent = load_agent(slug, token=token, repo=repo, gh_runner=gh_runner, transport=transport)
    res = Result(slug)
    targets = [r for r in agent.refs if r.op]
    foreign = [r for r in targets if r.vault != agent.vault]
    targets = [r for r in targets if r.vault == agent.vault]
    res.notes += [f"{r.name}: lives in vault {r.vault}, not {agent.vault} — not exported"
                  for r in foreign]
    if not check:
        why = op.signed_in()
        if why:
            raise VaultCliError(f"1Password is not answering as a person ({why}).")
    try:
        body = cred_resolve.resolve(slug, token=token, refs=[r.name for r in targets],
                                    transport=transport)
    except canopy_web.CanopyError as e:
        raise VaultCliError(f"could not read {slug}'s values from canopy-web: {str(e)[:300]}")
    values = body.get("values") or {}
    for r in targets:
        new = values.get(r.name)
        if new is None or not str(new).strip():
            res.rows.append(Row(r.name, "not-on-canopy-web"))
            continue
        old, _ = op_read(op, r.op)
        if fingerprint(old) == fingerprint(str(new)):
            res.rows.append(Row(r.name, "same"))
            continue
        if check:
            res.rows.append(Row(r.name, "would-write", "differs" if old is not None else "absent"))
            continue
        try:
            doc = op.get_item(agent.vault, r.item)
            if doc is None:
                doc = op.create_item(agent.vault, r.item, r.field_name)
                res.actions.append(f"created {agent.vault}/{r.item}")
            op.set_field(agent.vault, doc, r.field_name, str(new))
        except av.VaultError as e:
            res.rows.append(Row(r.name, "FAILED", str(e)[:200]))
            res.ok = False
            continue
        res.rows.append(Row(r.name, "written", "differed" if old is not None else "was absent"))
    values = {}  # noqa: F841
    return res


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


def run_status(slug: str, *, token: str, op: Optional[av.Op], drift: bool = True,
               repo: Optional[Path] = None, transport=None,
               gh_runner: Runner = subprocess.run) -> dict:
    from orchestrator import cred_resolve
    agent = load_agent(slug, token=token, repo=repo, gh_runner=gh_runner, transport=transport)
    web = _web(token, transport)
    try:
        acc = web("GET", f"/api/agents/{slug}/credentials/access") or {}
    except VaultCliError:
        acc = {}
    present = credential_status(slug, web)
    web_values: dict = {}
    drift_note = ""
    if drift:
        try:
            web_values = cred_resolve.resolve(slug, token=token, refs=[r.name for r in agent.refs],
                                              transport=transport).get("values") or {}
        except canopy_web.CanopyError as e:
            drift_note = f"drift not checked: canopy-web would not resolve ({str(e)[:160]})"
        if op is not None and not drift_note and op.signed_in():
            drift_note = "drift not checked: 1Password is not answering as a person"
    rows = []
    for r in agent.refs:
        row = present.get(r.name) or {}
        d = ""
        if drift and not drift_note:
            web_fp = fingerprint(web_values.get(r.name)) if r.name in web_values else ""
            if r.literal is not None:
                src_fp = fingerprint(r.literal)
            else:
                val, _ = op_read(op, r.op) if op is not None else (None, "")
                src_fp = fingerprint(val)
            d = ("same" if web_fp and web_fp == src_fp else
                 "only-1password" if src_fp and not web_fp else
                 "only-canopy-web" if web_fp and not src_fp else
                 "missing-both" if not (web_fp or src_fp) else "DRIFT")
        rows.append({"name": r.name, "kind": "literal" if r.literal is not None else "op",
                     "present": bool(row.get("set")), "updated_at": row.get("updated_at"),
                     "last_read": _last_read(row), "drift": d})
    web_values = {}  # noqa: F841
    undeclared = sorted(n for n, row in present.items()
                        if n not in {r.name for r in agent.refs} and row.get("set"))
    return {"agent": slug, "credential_source": acc.get("credential_source") or "?",
            "vault": agent.vault, "template": agent.template, "refs": rows,
            "skipped": [{"name": n, "why": w} for n, w in agent.skipped],
            "stored_not_declared": undeclared, "notes": agent.notes + ([drift_note] if drift_note else []),
            "device": device_key.read_status()}


def render_status(st: dict) -> str:
    lines = [f"agent {st['agent']}: credential_source={st['credential_source']}  "
             f"vault={st['vault']}  (template: {st['template']})"]
    width = max([len(r["name"]) for r in st["refs"]] + [4])
    lines.append(f"  {'ref':<{width}}  {'on canopy-web':<13}  {'drift':<15}  last read")
    for r in st["refs"]:
        lines.append(f"  {r['name']:<{width}}  {'present' if r['present'] else 'MISSING':<13}  "
                     f"{r['drift'] or '-':<15}  {r['last_read'] or '-'}")
    for s in st["skipped"]:
        lines.append(f"  {s['name']:<{width}}  left alone — {s['why']}")
    if st["stored_not_declared"]:
        lines.append(f"  stored on canopy-web but not declared: {', '.join(st['stored_not_declared'])}")
    for n in st["notes"]:
        lines.append(f"  note: {n}")
    lines.append(render_device(st.get("device") or {}))
    return "\n".join(lines)


def render_device(dev: dict) -> str:
    if not dev.get("jkt"):
        return "device key: none on this machine — `canopy vault device --enroll`"
    line = (f"device key: {dev.get('kind', '?')} {dev['jkt'][:16]}… for runner "
            f"{dev.get('runner') or '?'} — {dev.get('status') or 'unknown'}")
    if dev.get("status") == "pending" and dev.get("approve_url"):
        line += f"\n  approve it (Touch ID passkey): {dev['approve_url']}"
    return line


def render(res: Result) -> str:
    lines = [f"agent {res.slug}"]
    width = max([len(r.name) for r in res.rows] + [4])
    for r in res.rows:
        lines.append(f"  {r.name:<{width}}  {r.state:<17} {r.note}")
    lines += [f"  done: {a}" for a in res.actions]
    lines += [f"  note: {n}" for n in res.notes]
    return "\n".join(lines)


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
REPO_OPT = click.option("--repo", type=click.Path(exists=True, file_okay=False), default=None,
                        help="The agent's checkout, for its .env.tpl (default: a local "
                             "checkout, else its repo on GitHub).")
OP_OPT = click.option("--op-account", default=av.OP_ACCOUNT, show_default=True)


@click.group("vault")
def vault_group():
    """Canopy Vault: keep an agent's credentials on canopy-web instead of 1Password.

    Opt-in per agent; 1Password stays the default. Never prints a value."""


@vault_group.command("import")
@AGENT_OPT
@REPO_OPT
@OP_OPT
@click.option("--switch", "do_switch", is_flag=True,
              help="Then set credential_source=canopy-web — only if every ref is stored.")
def import_cmd(slug, repo, op_account, do_switch):
    """Copy the agent's declared refs from 1Password (your own `op`) to canopy-web.

    Leaves the 1Password vault untouched. With --switch, also moves the agent to the
    canopy-web backend once canopy-web reports every declared ref present."""
    res = run_import(slug, token=_token(), op=av.Op(op_account), switch=do_switch,
                     repo=Path(repo) if repo else None)
    click.echo(render(res))
    if not res.ok:
        sys.exit(1)


@vault_group.command("export")
@AGENT_OPT
@REPO_OPT
@OP_OPT
@click.option("--check", is_flag=True, help="Say what would be written; write nothing.")
def export_cmd(slug, repo, op_account, check):
    """Write canopy-web's current values back into the agent's 1Password vault (rollback)."""
    res = run_export(slug, token=_token(), op=av.Op(op_account), check=check,
                     repo=Path(repo) if repo else None)
    click.echo(render(res))
    if not res.ok:
        sys.exit(1)


@vault_group.command("switch")
@AGENT_OPT
@REPO_OPT
@click.option("--to", "to", type=click.Choice(SOURCES), required=True)
def switch_cmd(slug, repo, to):
    """Set where the agent's credentials resolve from. To canopy-web only when every
    declared ref is stored there."""
    click.echo(render(run_switch(slug, to, token=_token(), repo=Path(repo) if repo else None)))


@vault_group.command("status")
@AGENT_OPT
@REPO_OPT
@OP_OPT
@click.option("--no-drift", is_flag=True,
              help="Skip the 1Password/canopy-web comparison (it reads both backends).")
@click.option("--json", "as_json", is_flag=True)
def status_cmd(slug, repo, op_account, no_drift, as_json):
    """Each declared ref: on canopy-web or not, last read, and drift between the two
    backends (a hash comparison — never the values). Plus this machine's device key."""
    st = run_status(slug, token=_token(), op=None if no_drift else av.Op(op_account),
                    drift=not no_drift, repo=Path(repo) if repo else None)
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
              help="Use a software key even on a Mac (no Secure Enclave helper).")
@click.option("--label", default="", help="Shown on canopy-web (default: host name).")
@click.option("--json", "as_json", is_flag=True)
def device_cmd(enroll, reenroll, refresh, runner_id, software, label, as_json):
    """This machine's device key: show it, enroll it, or re-enroll it.

    canopy-web hands a `require_device_binding` agent's credentials only to a request
    signed by an APPROVED device key. Enrolling registers the key as pending; a person
    approves it in canopy-web with a passkey (Touch ID)."""
    runner_id = runner_id or device_key.runner_id_from_config()
    web = _web(_token()) if (enroll or reenroll or refresh) else None
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
            st = device_key.enroll(runner_id=runner_id, call=web, label=label,
                                   allow_software=True if software else None)
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
