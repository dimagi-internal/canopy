"""`canopy agent env` — bind this install to ONE agent instance and fill its .env from
that instance's 1Password vault.

A plugin is code. An **instance** is canopy-web's Agent record: identity, workspace and
1Password vault (`Agent.op_vault`). A local install is bound to exactly ONE instance,
chosen once at setup and never switched mid-session; using another instance means running
setup again (Jonathan, 2026-10-14). So the vault is never hardcoded in an agent repo and
never derived from a naming convention: templates reference `op://${AGENT_VAULT}/…`, and
this module sets `AGENT_VAULT` from the instance canopy-web records.

    canopy agent env --plugin <name> [--template .env.tpl] [--out ~/.<name>/.env]
                     [--agent <instance>] [--vault <name>] [--force] [--print]

1. Resolve the instance: the catalog entry's `metadata.instances` (canopy-web's
   `/plugins/marketplace.json`), else the agents list filtered by `plugin.name`.
   `--agent` picks one; the instance this install is already bound to is kept; exactly one
   is used; several stop with a re-run line per instance; none stops.
2. Resolve the vault: `--vault`, else the instance's `op_vault`. Empty is an error — no
   convention fallback.
3. Record the binding beside the output (`<out-dir>/canopy-instance.json`). Re-binding to a
   DIFFERENT instance needs `--force`.
4. `op inject -i <template> -o <out>` with `AGENT_VAULT` / `AGENT_SLUG` in its environment
   (op substitutes `$VAR` / `${VAR}` in secret references — `op inject --help`).

`instance_vault(slug)` is the same answer for the rest of canopy (bootstrap, `canopy cred`):
the local binding, else canopy-web's record, else a loud error.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import click

from orchestrator import canopy_web

BINDING_NAME = "canopy-instance.json"
OP_ACCOUNT = "dimagi.1password.com"
OP_INSTALL_URL = "https://developer.1password.com/docs/cli/get-started/"
VAULT_VAR = "AGENT_VAULT"
SLUG_VAR = "AGENT_SLUG"
AGENTS_PATH = "/api/agents/"
NO_VAULT = ("this agent instance has no 1Password vault recorded on canopy-web; "
            "its owner sets it")

#: stderr fragments meaning "op is not signed in / locked", not "no access".
OP_SIGNIN_FAILURES = ("not currently signed in", "no accounts configured", "session expired",
                      "authorization timeout", "authorization prompt", "connect to 1password",
                      "prompterror", "account is not signed in")

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
_OP_REF_VAULT = re.compile(r"op://([^/\s]+)/")

Runner = Callable[..., subprocess.CompletedProcess]


class InstanceError(click.ClickException):
    """A resolution/binding/inject failure, with one actionable message."""


@dataclass(frozen=True)
class Instance:
    plugin: str
    agent: str
    workspace: str = ""
    #: None = canopy-web did not say (an older catalog); "" = it said there is none.
    vault: Optional[str] = None

    def label(self) -> str:
        return f"{self.agent} (workspace {self.workspace or '?'})"


# ──────────────────────────────────────────────────────────────────────────────
# templates
# ──────────────────────────────────────────────────────────────────────────────

def expand_vars(text: str, env: dict) -> str:
    """`$VAR` / `${VAR}` replaced from `env`, unknown names left as written — what
    `op inject` does to a template before it resolves the references."""
    return _VAR_RE.sub(lambda m: env.get(m.group(1) or m.group(2), m.group(0)), text)


def uses_vault_var(text: str) -> bool:
    return any((m.group(1) or m.group(2)) == VAULT_VAR for m in _VAR_RE.finditer(text))


def literal_vaults(text: str) -> list[str]:
    """Vaults a template names directly (`op://Agent-Ace/…`), not through a variable."""
    return sorted({v for v in _OP_REF_VAULT.findall(text) if "$" not in v})


def inject_env(base: dict, vault: str, slug: str) -> dict:
    """The environment `op inject` needs for a `${AGENT_VAULT}` template."""
    return {**base, VAULT_VAR: vault, SLUG_VAR: slug}


# ──────────────────────────────────────────────────────────────────────────────
# step 1: which instance
# ──────────────────────────────────────────────────────────────────────────────

def _vault_field(d: dict, key: str) -> Optional[str]:
    if key not in d:
        return None
    return str(d.get(key) or "").strip()


def instances_from_catalog(catalog: dict, plugin: str) -> Optional[list[Instance]]:
    """The instances of `plugin` in canopy-web's catalog, or None when the catalog does
    not list that plugin at all."""
    for entry in (catalog or {}).get("plugins") or []:
        if not isinstance(entry, dict) or entry.get("name") != plugin:
            continue
        rows = ((entry.get("metadata") or {}).get("instances")) or []
        return [Instance(plugin=plugin, agent=str(r.get("agent") or ""),
                         workspace=str(r.get("workspace") or ""),
                         vault=_vault_field(r, "vault"))
                for r in rows if isinstance(r, dict) and r.get("agent")]
    return None


def instances_from_agents(rows, plugin: str) -> list[Instance]:
    """The instances of `plugin` in `GET /api/agents/` (each item's `plugin.name`)."""
    if isinstance(rows, dict):
        rows = rows.get("items") or rows.get("results") or []
    out = []
    for r in rows or []:
        if not isinstance(r, dict) or not r.get("slug"):
            continue
        if ((r.get("plugin") or {}).get("name") or "") != plugin:
            continue
        ws = r.get("workspace")
        if isinstance(ws, dict):
            ws = ws.get("slug")
        out.append(Instance(plugin=plugin, agent=str(r["slug"]), workspace=str(ws or ""),
                            vault=_vault_field(r, "op_vault")))
    return out


def fetch_instances(plugin: str, *, token: str,
                    transport: Optional[canopy_web.Transport] = None) -> list[Instance]:
    """Every instance of `plugin` the person can see: the catalog first (it is the
    install source, so it names plugins exactly as installed), else the agents list."""
    from orchestrator.workspace_cli import WorkspaceSetupError, fetch_catalog
    try:
        found = instances_from_catalog(fetch_catalog(token, transport=transport), plugin)
    except WorkspaceSetupError:
        found = None
    if found is not None:
        return found
    try:
        rows = canopy_web.call("GET", f"{AGENTS_PATH}?limit=500", token=token,
                               transport=transport)
    except Exception as e:  # noqa: BLE001 — report, don't traceback
        raise InstanceError(f"could not list agents on canopy-web: {str(e).splitlines()[0][:200]}")
    return instances_from_agents(rows, plugin)


def rerun_line(plugin: str, agent: str, template: Optional[str], out: Optional[str]) -> str:
    parts = ["canopy", "agent", "env", "--plugin", plugin, "--agent", agent]
    if template:
        parts += ["--template", template]
    if out:
        parts += ["--out", out]
    return shlex.join(parts)


def choose_instance(plugin: str, instances: list[Instance], *, agent: str = "",
                    bound: str = "", template: Optional[str] = None,
                    out: Optional[str] = None) -> tuple[Instance, str]:
    """(instance, why). `agent` (an explicit --agent) wins; else the instance this
    install is already bound to; else the only one. Several or none raise."""
    if agent:
        for i in instances:
            if i.agent == agent:
                return i, "chosen with --agent"
        seen = ", ".join(i.agent for i in instances) or "none"
        raise InstanceError(
            f"'{agent}' is not an instance of plugin '{plugin}' you can see on canopy-web "
            f"(visible: {seen}). Ask its owner to add you to its workspace, or pick one of "
            f"those.")
    if bound:
        for i in instances:
            if i.agent == bound:
                return i, "the instance this install is bound to"
    if len(instances) == 1:
        return instances[0], f"the only instance of plugin '{plugin}' you can see"
    if not instances:
        raise InstanceError(
            f"no instance of plugin '{plugin}' is visible to you on canopy-web — nothing to "
            f"bind to. Ask the agent's owner to add you to its workspace (or check the "
            f"plugin name: it is the name you installed, e.g. `{plugin}@canopy-agents`).")
    lines = [f"plugin '{plugin}' has {len(instances)} instances you can see — pick one "
             f"(an install is bound to exactly one):"]
    for i in sorted(instances, key=lambda x: x.agent):
        lines.append(f"  {i.label()}:  {rerun_line(plugin, i.agent, template, out)}")
    raise InstanceError("\n".join(lines))


# ──────────────────────────────────────────────────────────────────────────────
# step 2: which vault
# ──────────────────────────────────────────────────────────────────────────────

def fetch_agent_vault(slug: str, *, token: Optional[str] = None,
                      transport: Optional[canopy_web.Transport] = None) -> str:
    """The `op_vault` canopy-web records for agent `slug` ("" when none): the agent
    record's `op_vault`, or — from a canopy-web that does not put it on the record yet —
    the agent's vault status (`GET /api/agents/<slug>/vault`, the name only, never the key).
    Both are the instance's RECORDED vault; neither is derived."""
    try:
        body = canopy_web.call("GET", f"{AGENTS_PATH}{slug}/", token=token,
                               transport=transport) or {}
        if "op_vault" in body:
            return str(body.get("op_vault") or "").strip()
        status = canopy_web.call("GET", f"{AGENTS_PATH}{slug}/vault", token=token,
                                 transport=transport) or {}
    except Exception as e:  # noqa: BLE001
        raise InstanceError(f"could not read agent '{slug}' from canopy-web: "
                            f"{str(e).splitlines()[0][:200]}")
    return str(status.get("vault") or "").strip()


def resolve_vault(inst: Instance, override: str = "", *,
                  fetch: Callable[[str], str]) -> tuple[str, str]:
    """(vault, where it came from). `--vault` wins, else the instance's op_vault (fetched
    from the agent record when the catalog did not carry it). Never a convention."""
    if (override or "").strip():
        return override.strip(), "--vault"
    vault = inst.vault if inst.vault is not None else fetch(inst.agent)
    if not vault:
        raise InstanceError(
            f"{NO_VAULT} (agent '{inst.agent}', workspace '{inst.workspace or '?'}'). "
            f"Ask its owner to record it on canopy-web (/agents/{inst.agent} → Settings → "
            f"vault), or pass --vault <name> if you know which vault to use.")
    return vault, "canopy-web"


# ──────────────────────────────────────────────────────────────────────────────
# step 3: the binding
# ──────────────────────────────────────────────────────────────────────────────

def binding_path(out: Path) -> Path:
    return Path(out).expanduser().parent / BINDING_NAME


def read_binding(path: Path) -> Optional[dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("agent") else None


def check_rebind(prior: Optional[dict], inst: Instance, *, force: bool, path: Path) -> str:
    """"" when there is nothing to say; a note when re-binding with --force. Raises when
    a different instance is asked for without --force."""
    if not prior or (prior.get("agent") == inst.agent and prior.get("plugin") == inst.plugin):
        return ""
    was = f"{prior.get('agent')} (workspace {prior.get('workspace') or '?'})"
    if not force:
        raise InstanceError(
            f"this install is bound to instance {was} since {prior.get('bound_at') or '?'} "
            f"({path}); refusing to switch it to {inst.label()}. An install uses exactly one "
            f"instance — re-run with --force to re-bind it on purpose.")
    return f"re-binding this install from {was} to {inst.label()} (--force)"


def write_binding(path: Path, inst: Instance, vault: str, prior: Optional[dict],
                  now: Optional[float] = None) -> dict:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now if now is not None else time.time()))
    same = bool(prior) and prior.get("agent") == inst.agent and prior.get("plugin") == inst.plugin
    data = {"plugin": inst.plugin, "agent": inst.agent, "workspace": inst.workspace,
            "vault": vault, "bound_at": (prior or {}).get("bound_at") if same else stamp}
    if same:
        data["updated_at"] = stamp
    _write_private(Path(path), json.dumps(data, indent=2) + "\n")
    return data


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ──────────────────────────────────────────────────────────────────────────────
# step 4: fill the template
# ──────────────────────────────────────────────────────────────────────────────

def _op_failure(vault: str, reason: str) -> InstanceError:
    return InstanceError(
        f"could not fill the env from 1Password vault '{vault}' on {OP_ACCOUNT}: {reason}. "
        f"Sign in to {OP_ACCOUNT} with the 1Password CLI (`op signin`, or unlock the app with "
        f"its CLI integration on); if you are signed in and still cannot read '{vault}', ask "
        f"the agent's owner for access.")


def inject(template: Path, out: Path, *, vault: str, slug: str, runner: Optional[Runner] = None,
           base_env: Optional[dict] = None, op_account: str = OP_ACCOUNT) -> int:
    """`op inject -i template -o out` with AGENT_VAULT/AGENT_SLUG set; atomic, 0600.
    Returns the number of KEY=value lines written."""
    runner = runner or subprocess.run
    env = inject_env(dict(os.environ if base_env is None else base_env), vault, slug)
    out = Path(out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=str(out.parent), prefix=".tmp-", suffix=".env")
    os.close(fd)
    os.chmod(tmp, 0o600)
    argv = ["op", "inject", "-f", "-i", str(template), "-o", tmp]
    if not env.get("OP_SERVICE_ACCOUNT_TOKEN"):
        argv += ["--account", op_account]   # a service-account key names its own account
    try:
        try:
            r = runner(argv, capture_output=True, text=True, timeout=180, env=env)
        except FileNotFoundError:
            raise InstanceError(
                f"the 1Password CLI (`op`) is not installed — install it ({OP_INSTALL_URL}) "
                f"and sign in to {OP_ACCOUNT}, then re-run. This instance's vault is "
                f"'{vault}'; if you cannot read it, ask the agent's owner for access.")
        except subprocess.TimeoutExpired:
            raise _op_failure(vault, "`op` did not answer within 180s (1Password locked or "
                                     "waiting on a prompt?)")
        if r.returncode != 0:
            # op names the reference it could not resolve, never a value.
            first = ((r.stderr or "").strip().splitlines() or ["op inject failed"])[0][:300]
            if any(m in first.lower() for m in OP_SIGNIN_FAILURES):
                first = f"1Password is not signed in ({first})"
            raise _op_failure(vault, first)
        os.chmod(tmp, 0o600)
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return sum(1 for line in out.read_text(encoding="utf-8").splitlines()
               if "=" in line and not line.lstrip().startswith("#"))


# ──────────────────────────────────────────────────────────────────────────────
# the same answer for the rest of canopy
# ──────────────────────────────────────────────────────────────────────────────

def instance_vault(slug: str, *, token: Optional[str] = None, home: Optional[Path] = None,
                   fetch: Optional[Callable[[str], str]] = None) -> str:
    """Agent `slug`'s vault: the binding this machine recorded at setup
    (~/.<slug>/canopy-instance.json), else canopy-web's `op_vault`. Raises InstanceError
    when neither has one — there is no naming-convention fallback."""
    b = read_binding((home or Path.home()) / f".{slug}" / BINDING_NAME)
    if b and b.get("agent") == slug and str(b.get("vault") or "").strip():
        return str(b["vault"]).strip()
    vault = (fetch or (lambda s: fetch_agent_vault(s, token=token)))(slug)
    if not vault:
        raise InstanceError(f"agent '{slug}': {NO_VAULT} (/agents/{slug} → Settings → vault).")
    return vault


# ──────────────────────────────────────────────────────────────────────────────
# the whole step
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class EnvResult:
    instance: Instance
    vault: str
    vault_source: str
    why: str
    binding: Path
    notes: list
    lines: int = -1


def run(plugin: str, *, template: Optional[Path], out: Path, agent: str = "", vault: str = "",
        force: bool = False, print_only: bool = False, token: str,
        list_instances: Optional[Callable[[str], list[Instance]]] = None,
        fetch_vault: Optional[Callable[[str], str]] = None,
        runner: Optional[Runner] = None, base_env: Optional[dict] = None) -> EnvResult:
    out = Path(out).expanduser()
    bpath = binding_path(out)
    prior = read_binding(bpath)
    instances = (list_instances or (lambda p: fetch_instances(p, token=token)))(plugin)
    tpl_s = str(template) if template else None
    inst, why = choose_instance(plugin, instances, agent=agent,
                                bound=(prior or {}).get("agent", "") if (prior or {}).get("plugin") == plugin else "",
                                template=tpl_s, out=str(out))
    chosen_vault, source = resolve_vault(
        inst, vault, fetch=fetch_vault or (lambda s: fetch_agent_vault(s, token=token)))
    notes: list[str] = []
    note = check_rebind(prior, inst, force=force, path=bpath)
    if note:
        notes.append(note)
    res = EnvResult(instance=inst, vault=chosen_vault, vault_source=source, why=why,
                    binding=bpath, notes=notes)
    if print_only:
        return res
    if template is None or not Path(template).is_file():
        raise InstanceError(f"no template at {template} — pass --template <path/.env.tpl>")
    text = Path(template).read_text(encoding="utf-8")
    fixed = literal_vaults(text)
    if fixed:
        notes.append(f"{template} names vault(s) {', '.join(fixed)} directly — those refs ignore "
                     f"the instance's vault; migrate them to op://${{{VAULT_VAR}}}/<item>/<field>")
    res.lines = inject(Path(template), out, vault=chosen_vault, slug=inst.agent, runner=runner,
                       base_env=base_env)
    write_binding(bpath, inst, chosen_vault, prior)
    return res
