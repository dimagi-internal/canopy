"""`canopy agent vault` — give an agent its 1Password vault, filled, and the key its runners
read it with. One command, safe to re-run; the setup checklist's "Give it a vault" step.

    canopy agent vault --slug lex [--workspace connect] [--vault Agent-Lex] [--repo DIR]
                       [--check] [--rotate-key] [--rotate-pat]

WHY ONE COMMAND (2026-10-15). Lex was created in the browser and stalled: nothing in the
create wizard or the checklist mentioned a vault; the vault bootstrap was a bash script in
canopy-web's deploy/secrets that scaffolded four EMPTY items and printed advice; the key was
minted by Ada's bin/ada-vault-provision with different permissions than the script told a
person to use; and the agent's own canopy token could only be minted on the server. This
replaces all of that.

WHAT IS TYPED BY A PERSON — AND WHAT IS NOT. Most of an agent's vault is produced by the
system. Each `op://${AGENT_VAULT}/<item>/<field>` reference in the agent's `.env.tpl` is one
of (`KINDS`, the one table):
  minted  — this command produces it: `canopy-pat`, the agent's own canopy-web token
            (`POST /api/agents/<slug>/canopy-token`). Every agent gets one, referenced or not.
  linked  — another control produces it: `gog-token` comes from "Connect Google mailbox" on
            canopy-web (a runner takes the newer of that copy and the vault's).
  shared  — lives in the workspace's shared vault, not here (`gog-oauth-client`).
  unused  — nothing reads it per agent any more (`claude-oauth-token`); reported, not made.
  asked   — everything else: a value only a person has (an outside service's key or login).
            Prompted for with hidden input when empty; never printed, never in argv.

WHO RUNS IT. A person with 1Password rights to create vaults and service accounts, who is
the agent's owner or admin on canopy-web (creating the agent's canopy login also takes
members.manage there). `op` runs as THAT PERSON: an exported OP_SERVICE_ACCOUNT_TOKEN (an
agent's own, on a fleet laptop) would win over the person's session and fail with 403s, so
it is dropped from op's environment, and the 1Password desktop app is used when unlocked
(OP_BIOMETRIC_UNLOCK_ENABLED). An agent session must not run it: values pass through this
process, and the fleet gating baseline rails the raw paths (op service-account create, the
canopy-token / vault MCP tools) toward it.

WHAT IT DOES, in order — `--check` does the reads and reports, writing nothing:
  1. canopy-web: the agent (you must be its owner or admin), its recorded vault and key.
  2. the vault: the recorded one, else --vault (required then — a vault name is never
     derived from the slug). Created if missing.
  3. the agent's owner is made a vault admin (they run the agent and rotate its secrets).
  4. each referenced item is created if missing and filled by its kind (above).
  5. the key: a 1Password service account `canopy-web-<slug>`, READ-ONLY on that vault and
     non-expiring, uploaded to canopy-web — only when none is stored (or --rotate-key).
     Runners only ever read an agent vault, and nothing watches a key's expiry date. Storing
     it asks the agent's cloud boxes to re-bootstrap (canopy-web does that).
"""
from __future__ import annotations

import base64
import getpass
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from orchestrator import canopy_web

OP_ACCOUNT = "dimagi.1password.com"
KEY_PERMISSIONS = "read_items"
VAULT_ADMIN_PERMISSIONS = ("view_items,create_items,edit_items,archive_items,delete_items,"
                           "view_and_copy_passwords,view_item_history,import_items,export_items,"
                           "copy_and_share_items,print_items,manage_vault")
PAT_ITEM, PAT_FIELD = "canopy-pat", "credential"
PAT_ENV_LINE = "CANOPY_WEB_PAT=op://${AGENT_VAULT}/canopy-pat/credential"

MINTED, LINKED, SHARED, UNUSED, ASKED = "minted", "linked", "shared", "unused", "asked"


@dataclass(frozen=True)
class Kind:
    kind: str
    what: str


#: Every item the fleet knows how to produce. Anything else an agent references is ASKED.
KINDS: dict[str, Kind] = {
    PAT_ITEM: Kind(MINTED, "its own canopy-web token, minted by canopy-web"),
    "gog-token": Kind(LINKED, "its Gmail token — click Connect Google mailbox on its canopy-web "
                              "settings; a runner takes the newer of that and this item"),
    "gog-oauth-client": Kind(SHARED, "the fleet's Google OAuth client — it lives in the "
                                     "workspace's shared vault, not the agent's"),
    "claude-oauth-token": Kind(UNUSED, "nothing reads it per agent: Claude logins travel between "
                                       "runners (the runner page)"),
    "gdrive-root-folder": Kind(ASKED, "the agent's own Google Drive folder ID (the part after "
                                      "/folders/ in its URL)"),
}
DEFAULT_ASKED = "a value only a person has — an outside service's key or login"

_REF = re.compile(r"op://\$\{?AGENT_VAULT\}?/([^/\s\"']+)/([^\s\"'#]+)")
_LITERAL_REF = re.compile(r"op://(?!\$)([^/\s\"']+)/")

Runner = Callable[..., subprocess.CompletedProcess]
Ask = Callable[[str], str]


class VaultError(Exception):
    """Stops the run; the message says what to do."""


def kind_of(item: str) -> Kind:
    return KINDS.get(item, Kind(ASKED, DEFAULT_ASKED))


def parse_refs(template: str) -> tuple[list[tuple[str, str]], list[str]]:
    """((item, field) per `op://${AGENT_VAULT}/…` ref, in order, de-duplicated;
    vaults named LITERALLY). Comment lines are skipped: the scaffold documents refs in
    comments with <placeholders>, and those are not refs."""
    refs: list[tuple[str, str]] = []
    literal: list[str] = []
    for line in template.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        for m in _REF.finditer(s):
            ref = (m.group(1), m.group(2).rsplit("/", 1)[-1])
            if ref not in refs:
                refs.append(ref)
        literal += [v for v in _LITERAL_REF.findall(s) if v not in literal]
    return refs, literal


# ──────────────────────────────────────────────────────────────────────────────
# 1Password, as the person
# ──────────────────────────────────────────────────────────────────────────────

def person_op_env(base: Optional[dict] = None) -> dict:
    """op's environment for a PERSON: never an agent's service-account token (it wins over
    the person's session, and can neither create a vault nor mint a key), and the desktop
    app's sign-in when it is unlocked."""
    env = dict(os.environ if base is None else base)
    env.pop("OP_SERVICE_ACCOUNT_TOKEN", None)
    env.setdefault("OP_BIOMETRIC_UNLOCK_ENABLED", "true")
    return env


class Op:
    """The `op` calls this needs. Values travel through stdin and memory only; nothing
    here prints one, and none is ever an argv element (argv is visible to `ps`)."""

    def __init__(self, account: str = OP_ACCOUNT, runner: Runner = subprocess.run,
                 env: Optional[dict] = None):
        self.account, self.runner, self.env = account, runner, person_op_env(env)

    def _run(self, *args: str, stdin: Optional[str] = None) -> subprocess.CompletedProcess:
        return self.runner(["op", *args, "--account", self.account], capture_output=True,
                           text=True, input=stdin, env=self.env)

    @staticmethod
    def _why(p: subprocess.CompletedProcess) -> str:
        """op's last error line (it never carries a secret: values only ever go to stdout)."""
        lines = (p.stderr or "").strip().splitlines()
        return lines[-1][:300] if lines else f"exit {p.returncode}"

    def signed_in(self) -> Optional[str]:
        """None when op answers as a person; else why not. `op whoami` is not the test: it
        says "no active session" under the desktop-app integration even when every real
        command works."""
        p = self._run("vault", "list", "--format", "json")
        return None if p.returncode == 0 else self._why(p)

    def vault_exists(self, vault: str) -> bool:
        return self._run("vault", "get", vault, "--format", "json").returncode == 0

    def create_vault(self, vault: str, description: str) -> None:
        p = self._run("vault", "create", vault, "--description", description)
        if p.returncode != 0:
            raise VaultError(f"could not create vault {vault}: {self._why(p)}")

    def vault_user_emails(self, vault: str) -> set[str]:
        p = self._run("vault", "user", "list", vault, "--format", "json")
        if p.returncode != 0:
            return set()
        return {str(u.get("email", "")).lower() for u in json.loads(p.stdout or "[]")}

    def grant_vault_admin(self, vault: str, email: str) -> None:
        p = self._run("vault", "user", "grant", "--vault", vault, "--user", email,
                      "--permissions", VAULT_ADMIN_PERMISSIONS)
        if p.returncode != 0:
            raise VaultError(f"could not make {email} an admin of {vault}: {self._why(p)}")

    def get_item(self, vault: str, title: str) -> Optional[dict]:
        p = self._run("item", "get", title, "--vault", vault, "--format", "json")
        return json.loads(p.stdout) if p.returncode == 0 and p.stdout.strip() else None

    def create_item(self, vault: str, title: str, field_name: str) -> dict:
        p = self._run("item", "create", "--category", "API Credential", "--title", title,
                      "--vault", vault, f"{field_name}[password]=", "--format", "json")
        if p.returncode != 0:
            raise VaultError(f"could not create {vault}/{title}: {self._why(p)}")
        return json.loads(p.stdout)

    def set_field(self, vault: str, item: dict, field_name: str, value: str) -> None:
        """Write `value` into the item's field by piping the item's JSON to `op item edit`
        (op's documented way to set a sensitive value without an assignment in argv)."""
        doc = json.loads(json.dumps(item))
        target = _find_field(doc, field_name)
        if target is None:
            doc.setdefault("fields", []).append(
                {"id": field_name, "label": field_name, "type": "CONCEALED", "value": value})
        else:
            target["value"] = value
        p = self._run("item", "edit", str(doc.get("id") or doc.get("title")), "--vault", vault,
                      stdin=json.dumps(doc))
        if p.returncode != 0:
            raise VaultError(f"could not write {vault}/{doc.get('title')}: {self._why(p)}")

    def mint_service_account(self, name: str, vault: str) -> str:
        """The raw key, in memory only (`--raw` puts it on stdout, which is a pipe here)."""
        p = self._run("service-account", "create", name, "--vault",
                      f"{vault}:{KEY_PERMISSIONS}", "--raw")
        token = (p.stdout or "").strip()
        if p.returncode != 0 or not token:
            raise VaultError(f"could not mint service account {name}: {self._why(p)}")
        return token


def _find_field(item: dict, name: str) -> Optional[dict]:
    for f in item.get("fields") or []:
        if name in (f.get("id"), f.get("label")):
            return f
    return None


def field_value(item: Optional[dict], name: str) -> str:
    f = _find_field(item or {}, name)
    return str((f or {}).get("value") or "")


# ──────────────────────────────────────────────────────────────────────────────
# the agent's .env.tpl
# ──────────────────────────────────────────────────────────────────────────────

def repo_slug(agent: dict) -> tuple[str, str]:
    """(owner/name, ref) of the agent's repo, from canopy-web's record."""
    definition = agent.get("definition") or {}
    url = str(definition.get("repo_url") or agent.get("repo_url") or "")
    m = re.search(r"github\.com[/:]([^/]+/[^/.\s]+)", url)
    name = m.group(1) if m else str((agent.get("plugin") or {}).get("repo") or "")
    return name, str(definition.get("repo_ref") or "")


def read_repo_file(agent: dict, repo: Optional[Path], rel: str,
                   runner: Runner = subprocess.run) -> tuple[Optional[str], str]:
    """(text or None when absent, where it came from): the local checkout when given, else
    the agent's repo on GitHub (its recorded ref) via `gh`."""
    if repo is not None:
        path = Path(repo) / rel
        return (path.read_text(encoding="utf-8") if path.is_file() else None), str(path)
    name, ref = repo_slug(agent)
    if not name:
        raise VaultError("canopy-web records no repository for this agent — pass --repo DIR")
    api = f"repos/{name}/contents/{rel}" + (f"?ref={ref}" if ref else "")
    p = runner(["gh", "api", api, "--jq", ".content"], capture_output=True, text=True)
    where = f"github.com/{name}/{rel}"
    if p.returncode != 0:
        if "404" in (p.stderr or "") or "Not Found" in (p.stderr or ""):
            return None, where
        raise VaultError(f"could not read {rel} from {name}: "
                         f"{(p.stderr or '').strip()[:200]} — pass --repo DIR")
    return base64.b64decode(p.stdout).decode("utf-8"), where


def load_template(agent: dict, repo: Optional[Path], runner: Runner = subprocess.run
                  ) -> tuple[str, str]:
    """(.env.tpl text, where it came from)."""
    text, where = read_repo_file(agent, repo, ".env.tpl", runner)
    if text is None:
        raise VaultError(f"{where} does not exist — is this the agent's repo?")
    return text, where


def mailbox_address(agent: dict, repo: Optional[Path], runner: Runner = subprocess.run) -> str:
    """The agent's mailbox: canopy-web's record, else the repo's config/agent.json `email`
    — the identity source `agent-publish register` reads. A browser-created agent's record
    can be blank while its repo names the address (lex, 2026-10-15)."""
    if agent.get("email"):
        return str(agent["email"])
    text, _ = read_repo_file(agent, repo, "config/agent.json", runner)
    try:
        return str(json.loads(text or "{}").get("email") or "")
    except ValueError:
        return ""


# ──────────────────────────────────────────────────────────────────────────────
# the run
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Row:
    item: str
    kind: str
    state: str   # filled | minted | entered | missing | created-empty | elsewhere | skipped
    note: str = ""


@dataclass
class Report:
    slug: str
    vault: str = ""
    template: str = ""
    actions: list[str] = field(default_factory=list)
    rows: list[Row] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    key: str = ""

    @property
    def missing(self) -> list[Row]:
        return [r for r in self.rows if r.state in ("missing", "created-empty")]


def _web(token, transport, workspace):
    def call(method: str, path: str, body=None):
        try:
            return canopy_web.call(method, path, body, token=token, workspace=workspace,
                                   transport=transport)
        except canopy_web.CanopyError as e:
            text = str(e)
            if " -> 404:" in text and "<html" in text.lower():
                raise VaultError(f"canopy-web has no {method} {path} — it is running an older "
                                 "version than this canopy; deploy it, then re-run") from None
            raise VaultError(f"canopy-web refused {method} {path}: {text[:300]}") from None
    return call


def run(slug: str, *, token: str, workspace: str = "", vault: str = "",
        repo: Optional[Path] = None, check: bool = False, rotate_key: bool = False,
        rotate_pat: bool = False, op: Optional[Op] = None, ask: Optional[Ask] = None,
        transport: Optional[canopy_web.Transport] = None,
        gh_runner: Runner = subprocess.run, log: Callable[[str], None] = lambda _: None
        ) -> Report:
    op = op or Op()
    web = _web(token, transport, workspace or None)
    rep = Report(slug=slug)

    # 1. canopy-web
    agent = web("GET", f"/api/agents/{slug}/")
    ws = workspace or str(agent.get("workspace") or "")
    web = _web(token, transport, ws or None)
    if not agent.get("is_admin") and not check:
        raise VaultError(f"you are not {slug}'s owner or an admin on canopy-web, so its vault "
                         "and key would be refused — ask its owner to add you as an admin")
    state = web("GET", f"/api/agents/{slug}/vault")
    recorded = str(state.get("vault") or "").strip()
    if recorded and vault and vault != recorded:
        raise VaultError(f"{slug}'s vault is {recorded} on canopy-web; refusing to use {vault}. "
                         "Moving an agent to another vault is a deliberate act: change it on "
                         "its canopy-web settings first.")
    rep.vault = recorded or vault
    if not rep.vault:
        # Never derived from the slug (Jonathan, 2026-10-14): a vault belongs to the instance,
        # and someone may run this agent with another one. The person creating it names it.
        raise VaultError(f"canopy-web records no vault for {slug} — pass --vault <name>: the "
                         "1Password vault to create for it (or an existing one to use)")

    # 2. 1Password as a person
    why = op.signed_in()
    if why:
        raise VaultError(
            f"1Password is not answering as a person ({why}). Unlock the 1Password app with "
            "Settings → Developer → 'Integrate with 1Password CLI' on, or run "
            f"`eval \"$(op signin --account {op.account})\"` in this shell, then re-run.")

    # 3. the template
    text, rep.template = load_template(agent, repo, gh_runner)
    refs, literal = parse_refs(text)
    for v in literal:
        rep.notes.append(f".env.tpl names vault {v} literally — use op://${{AGENT_VAULT}}/… so "
                         "the instance's vault is used")
    if (PAT_ITEM, PAT_FIELD) not in refs:
        rep.notes.append(f".env.tpl does not use its own canopy token — add `{PAT_ENV_LINE}` "
                         "or its turns sign in to canopy-web as a person")
        refs.append((PAT_ITEM, PAT_FIELD))

    # 4. the vault, and its owner as an admin
    exists = op.vault_exists(rep.vault)
    if not exists:
        if check:
            rep.notes.append(f"vault {rep.vault} does not exist yet")
            rep.rows = [Row(i, kind_of(i).kind, "missing", kind_of(i).what) for i, _ in refs]
            rep.key = "not stored" if not state.get("key_set") else "stored"
            return rep
        op.create_vault(rep.vault, f"Secrets for the {slug} agent (canopy agent vault)")
        rep.actions.append(f"created vault {rep.vault}")
    owner = str((agent.get("owner") or {}).get("email") or "").lower()
    if owner and owner not in op.vault_user_emails(rep.vault):
        if check:
            rep.notes.append(f"{owner} (its owner) is not an admin of {rep.vault}")
        else:
            op.grant_vault_admin(rep.vault, owner)
            rep.actions.append(f"made {owner} (its owner) an admin of {rep.vault}")

    # 5. the items
    for item, fld in refs:
        k = kind_of(item)
        if k.kind in (SHARED, UNUSED):
            rep.rows.append(Row(item, k.kind, "skipped", k.what))
            continue
        doc = op.get_item(rep.vault, item)
        value = field_value(doc, fld)
        if k.kind == LINKED:
            rep.rows.append(Row(item, k.kind, "filled" if value else "elsewhere", k.what))
            continue
        if value and not (k.kind == MINTED and rotate_pat):
            rep.rows.append(Row(item, k.kind, "filled", k.what))
            continue
        if check:
            rep.rows.append(Row(item, k.kind, "missing", k.what))
            continue
        if doc is None:
            doc = op.create_item(rep.vault, item, fld)
            rep.actions.append(f"created {rep.vault}/{item}")
        if k.kind == MINTED:
            body = web("POST", f"/api/agents/{slug}/canopy-token",
                       {"email": mailbox_address(agent, repo, gh_runner),
                        "label": f"{slug} agent (canopy agent vault)"})
            raw = str(body.pop("raw", ""))
            op.set_field(rep.vault, doc, fld, raw)
            raw = ""  # noqa: F841 — drop our only reference
            made = " (created its canopy login)" if body.get("created_user") else ""
            rep.rows.append(Row(item, k.kind, "minted",
                                f"token for {body.get('user_email')}{made}"))
            continue
        entered = ask(f"{item} ({k.what})") if ask else ""
        if entered:
            op.set_field(rep.vault, doc, fld, entered)
            entered = ""  # noqa: F841
            rep.rows.append(Row(item, k.kind, "entered", k.what))
        else:
            rep.rows.append(Row(item, k.kind, "created-empty", k.what))

    # 6. the key
    if state.get("key_set") and not rotate_key and recorded == rep.vault:
        rep.key = "stored"
    elif check:
        rep.key = "not stored"
    else:
        name = f"canopy-web-{slug}"
        key = op.mint_service_account(name, rep.vault)
        try:
            after = web("PUT", f"/api/agents/{slug}/vault",
                        {"vault": rep.vault, "service_key": key})
        except VaultError as e:
            raise VaultError(f"minted {name} but canopy-web refused it ({str(e)[:200]}). The "
                             f"service account is live and stored NOWHERE: delete {name} in "
                             "1Password (Developer → Service accounts), fix the cause, re-run.")
        finally:
            key = ""  # noqa: F841
        if not (after.get("key_set") and after.get("vault") == rep.vault):
            raise VaultError(f"canopy-web answered {after}; expected {rep.vault} with a key set")
        rep.key = "minted and stored"
        rep.actions.append(f"minted {name} ({rep.vault}:{KEY_PERMISSIONS}) and stored it on "
                           "canopy-web — its cloud boxes re-bootstrap it")
        if state.get("key_set"):
            rep.notes.append(f"replaced a key: delete the OLD service account for {rep.vault} in "
                             f"1Password (Developer → Service accounts) once a runner reports "
                             f"env_ok for {slug} — keep {name}")
    return rep


def hidden_prompt(label: str) -> str:
    """Hidden input; empty skips. Never echoed, never logged."""
    return getpass.getpass(f"  value for {label} (hidden; Enter to skip): ").strip()


def render(rep: Report) -> str:
    lines = [f"agent {rep.slug} → vault {rep.vault}   (template: {rep.template or '?'})"]
    lines += [f"  done: {a}" for a in rep.actions]
    width = max([len(r.item) for r in rep.rows] + [4])
    for r in rep.rows:
        lines.append(f"  {r.item:<{width}}  {r.kind:<7} {r.state:<13} {r.note}")
    lines.append(f"  key: {rep.key}")
    lines += [f"  note: {n}" for n in rep.notes]
    minted = [r.item for r in rep.missing if r.kind == MINTED]
    asked = [r.item for r in rep.missing if r.kind != MINTED]
    if minted:
        lines.append(f"  STILL EMPTY: {', '.join(minted)} — re-run without --check to mint")
    if asked:
        lines.append(f"  STILL EMPTY: {', '.join(asked)} — re-run in a terminal to be asked, or "
                     f"fill them in 1Password ({rep.vault})")
    return "\n".join(lines)
