"""`canopy agent env` — bind this install to ONE agent instance and fill its .env from
that instance's Canopy vault (canopy-web).

A plugin is code. An **instance** is canopy-web's Agent record: identity, workspace and
credentials. A local install is bound to exactly ONE instance, chosen once at setup and never
switched mid-session; using another instance means running setup again (Jonathan,
2026-10-14).

    canopy agent env --plugin <name> [--out ~/.<name>/.env] [--agent <instance>] [--force]
                     [--print]

1. Resolve the instance: the catalog entry's `metadata.instances` (canopy-web's
   `/plugins/marketplace.json`), else the agents list filtered by `plugin.name`.
   `--agent` picks one; the instance this install is already bound to is kept; exactly one
   is used; several stop with a re-run line per instance; none stops.
2. Record the binding beside the output (`<out-dir>/canopy-instance.json`). Re-binding to a
   DIFFERENT instance needs `--force`.
3. Fill the env from the instance's Canopy vault (`canopy cred`'s resolve: values decrypted on
   this device, written 0600).
"""
from __future__ import annotations

import json
import os
import shlex
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import click

from orchestrator import canopy_web

BINDING_NAME = "canopy-instance.json"
AGENTS_PATH = "/api/agents/"


class InstanceError(click.ClickException):
    """A resolution/binding/inject failure, with one actionable message."""


@dataclass(frozen=True)
class Instance:
    plugin: str
    agent: str
    workspace: str = ""

    def label(self) -> str:
        return f"{self.agent} (workspace {self.workspace or '?'})"


# ──────────────────────────────────────────────────────────────────────────────
# step 1: which instance
# ──────────────────────────────────────────────────────────────────────────────

def instances_from_catalog(catalog: dict, plugin: str) -> Optional[list[Instance]]:
    """The instances of `plugin` in canopy-web's catalog, or None when the catalog does
    not list that plugin at all."""
    for entry in (catalog or {}).get("plugins") or []:
        if not isinstance(entry, dict) or entry.get("name") != plugin:
            continue
        rows = ((entry.get("metadata") or {}).get("instances")) or []
        return [Instance(plugin=plugin, agent=str(r.get("agent") or ""),
                         workspace=str(r.get("workspace") or ""))
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
        out.append(Instance(plugin=plugin, agent=str(r["slug"]), workspace=str(ws or "")))
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


def rerun_line(plugin: str, agent: str, out: Optional[str]) -> str:
    parts = ["canopy", "agent", "env", "--plugin", plugin, "--agent", agent]
    if out:
        parts += ["--out", out]
    return shlex.join(parts)


def choose_instance(plugin: str, instances: list[Instance], *, agent: str = "",
                    bound: str = "", out: Optional[str] = None) -> tuple[Instance, str]:
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
        lines.append(f"  {i.label()}:  {rerun_line(plugin, i.agent, out)}")
    raise InstanceError("\n".join(lines))


# ──────────────────────────────────────────────────────────────────────────────
# step 2: the binding
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


def write_binding(path: Path, inst: Instance, prior: Optional[dict],
                  now: Optional[float] = None) -> dict:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now if now is not None else time.time()))
    same = bool(prior) and prior.get("agent") == inst.agent and prior.get("plugin") == inst.plugin
    data = {"plugin": inst.plugin, "agent": inst.agent, "workspace": inst.workspace,
            "bound_at": (prior or {}).get("bound_at") if same else stamp}
    if same:
        data["updated_at"] = stamp
    _write_private(Path(path), json.dumps(data, indent=2) + "\n")
    return data


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        if hasattr(os, "fchmod") and os.name != "nt":  # no file modes on Windows
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
# the whole step
# ──────────────────────────────────────────────────────────────────────────────

def canopy_vault_env(slug: str, out: Path) -> int:
    """Write agent `slug`'s env from the Canopy vault to `out` (0600). Returns the var count."""
    from orchestrator.cred_cli import CredError, fetch_canopy_web_values, write_env_from_values
    try:
        n, _ = write_env_from_values(slug, Path(out), fetch_canopy_web_values(slug))
    except CredError as e:
        raise InstanceError(
            f"could not fill the env from {slug}'s Canopy vault: {e.message} — "
            f"`canopy agent use {slug}` says what this computer still needs (sign-in, "
            f"device key, a grant, your licence); a licence reads only inside your own "
            f"Claude Code session of {slug}.")
    return n


@dataclass
class EnvResult:
    instance: Instance
    why: str
    binding: Path
    notes: list
    lines: int = -1


def run(plugin: str, *, out: Path, agent: str = "", force: bool = False,
        print_only: bool = False, token: str,
        list_instances: Optional[Callable[[str], list[Instance]]] = None,
        from_canopy_vault: Optional[Callable[[str, Path], int]] = None) -> EnvResult:
    out = Path(out).expanduser()
    bpath = binding_path(out)
    prior = read_binding(bpath)
    instances = (list_instances or (lambda p: fetch_instances(p, token=token)))(plugin)
    inst, why = choose_instance(plugin, instances, agent=agent,
                                bound=(prior or {}).get("agent", "") if (prior or {}).get("plugin") == plugin else "",
                                out=str(out))
    notes = [n for n in [check_rebind(prior, inst, force=force, path=bpath)] if n]
    res = EnvResult(instance=inst, why=why, binding=bpath, notes=notes)
    if print_only:
        return res
    res.lines = (from_canopy_vault or canopy_vault_env)(inst.agent, out)
    write_binding(bpath, inst, prior)
    return res
