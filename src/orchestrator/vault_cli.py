"""`canopy vault` — Canopy Vault: an agent's credentials on canopy-web instead of 1Password.

An OPT-IN alternative backend, chosen per agent (`Agent.credential_source`, canopy#946 /
canopy-web#1538). 1Password stays the default and stays fully supported; nothing here
changes an agent that has not been switched.

    canopy vault import  --agent X [--switch] [--repo DIR]   1Password -> canopy-web
    canopy vault export  --agent X [--check]                 canopy-web -> 1Password (rollback)
    canopy vault switch  --agent X --to canopy-web|1password
    canopy vault status  --agent X [--no-drift] [--json]
    canopy vault device  [--enroll | --reenroll | --refresh] [--runner ID] [--kind K | --software]
    canopy vault device  --public-jwk [--kind software]      this key's PUBLIC JWK (made if missing)
    canopy vault device  --registered KEY_ID --runner ID     record a registration made elsewhere
    canopy vault binding --agent X on|off                    require an approved device key
    canopy vault zk status|enable|set|grant|rotate --agent X zero-knowledge (vault_zk_cli.py)
    canopy vault import --agent X --zk                       ... encrypted here

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
# The whole value is one reference. An item or section name may hold spaces
# ("op://${AGENT_VAULT}/ACE - Open Chat Studio/Config/golden_template_id"), so the
# reference runs to the end of the value; a second op:// means text around references.
_WHOLE_REF = re.compile(r"^[\"']?(op://[^\"']+?)[\"']?$")

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
        if whole and raw.count("op://") == 1:
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
               gh_runner: Runner = subprocess.run, zk: bool = False,
               zk_vault=None) -> Result:
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
    _copy_gmail_token(agent, op, web, values, res)
    if values and zk:
        # Zero-knowledge (canopy-web#1550): encrypted HERE under the vault key this device
        # unwraps; canopy-web only ever sees zk1: ciphertext.
        from orchestrator import vault_zk_cli
        v = zk_vault or vault_zk_cli.ZkVault(slug, token=token, transport=transport)
        try:
            out = vault_zk_cli.run_set(v, values)
        except click.ClickException as e:
            raise VaultCliError(e.message) from None
        res.actions += out.actions
        res.notes += vault_zk_cli.settle_quietly(slug, token=token, transport=transport, vault=v)
    elif values:
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
        gmail = _gmail_blocker(agent, present)
        if missing:
            res.notes.append(f"NOT switched: {', '.join(missing)} not on canopy-web")
            res.ok = False
        elif gmail:
            res.notes.append(f"NOT switched: {gmail}")
            res.ok = False
        else:
            _switch(slug, "canopy-web", web)
            res.actions.append(f"switched {slug} to canopy-web")
    return res


GMAIL_ITEM = "gog-token"


def _copy_gmail_token(agent: Agent, op: av.Op, web, values: dict, res: Result) -> None:
    """Carry the agent's Gmail token over when canopy-web has none.

    The runner bootstrap reads it straight from ``op://<vault>/gog-token`` — never through
    .env.tpl — and falls back to canopy-web's ``gog-token`` credential. A switched agent's
    runners stop receiving its 1Password key, so without this copy its Gmail silently stops
    on the first turn after the switch (found piloting Hal, 2026-10-17). The token JSON names
    the OAuth client that minted it, so a copy pairs with the right client on any box.
    Connect Google mailbox writes the same credential and stays the way to mint a new one.
    Under ``--zk`` it is encrypted here like every other value: the runner opens zero-
    knowledge values on the device (`cred_resolve.open_zk`) before the bootstrap reads it."""
    if (credential_status(agent.slug, web).get(GMAIL_ITEM) or {}).get("set"):
        return
    val, _ = op_read(op, f"op://{agent.vault}/{GMAIL_ITEM}/credential")
    if not val or not val.strip():
        return                       # no Gmail token in the vault: nothing to carry
    values[GMAIL_ITEM] = val
    res.rows.append(Row(GMAIL_ITEM, "read", "1Password item gog-token (the runner's Gmail token)"))


def _gmail_blocker(agent: Agent, present: dict) -> str:
    """Why a switch to canopy-web would cut off the agent's Gmail ("" when it would not)."""
    email = (agent.record.get("email") or "").strip()
    if not email or (present.get(GMAIL_ITEM) or {}).get("set"):
        return ""
    return (f"it has a mailbox ({email}) but canopy-web holds no {GMAIL_ITEM}, and after the "
            f"switch its runners can no longer read 1Password — run `canopy vault import "
            f"--agent {agent.slug}` (copies it) or Connect Google mailbox on its settings")


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
        gmail = _gmail_blocker(agent, present)
        if gmail:
            raise VaultCliError(f"refusing to switch {slug} to canopy-web: {gmail}")
        res.notes.append(f"all {len(needed)} declared ref(s) are on canopy-web")
        from orchestrator import vault_zk_cli
        res.notes += vault_zk_cli.settle_quietly(slug, token=token, transport=transport)
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
    if dev.get("role_arn"):
        line += f"\n  instance role: {dev['role_arn']}"
    if dev.get("status") == "pending" and dev.get("approve_url"):
        line += f"\n  approve it (passkey step-up): {dev['approve_url']}"
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
@click.option("--zk", "zk", is_flag=True,
              help="The agent is zero-knowledge: encrypt each value here under its vault key "
                   "(this device must hold a wrap) — canopy-web sees only ciphertext.")
def import_cmd(slug, repo, op_account, do_switch, zk):
    """Copy the agent's declared refs from 1Password (your own `op`) to canopy-web.

    Leaves the 1Password vault untouched. With --switch, also moves the agent to the
    canopy-web backend once canopy-web reports every declared ref present."""
    res = run_import(slug, token=_token(), op=av.Op(op_account), switch=do_switch, zk=zk,
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
    runner_id = runner_id or device_key.runner_id_from_config()
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


from orchestrator.vault_zk_cli import zk_group  # noqa: E402 — after vault_group exists

vault_group.add_command(zk_group)
