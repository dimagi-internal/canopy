"""`canopy agents connect` — register the person's ONE agent catalog (canopy#851, rev. 2026-10-14).

The new-user half of `/canopy:setup`: after the browser sign-in (the PAT mint), this
registers `canopy-agents` — `https://<canopy-web>` + AGENTS_MARKETPLACE_PATH, which lists
every agent in every workspace the signed-in person belongs to — in Claude Code's USER
settings with `autoUpdate`, authenticated by the canopy plugin's headers helper in
`--archive` mode, then prints each agent's install line (`/plugin install
<agent>@canopy-agents`) and its own setup command. It installs nothing unless
`--install-all`. Each catalog entry is a `github` source, so installing an agent needs the
person's OWN GitHub read access to that agent's repo; canopy never lends access.

It also REMOVES the retired per-workspace `canopy-<ws>` entries (`/w/<ws>/marketplace.json`,
which now answers 410) from user settings, and says so. `canopy workspace connect` is
retired and errors, pointing here.

Why user settings and not `claude plugin marketplace add <url>`: `add` has no way to
send a header, and the catalog is filtered by the caller's membership, so it must be
fetched as the person. A user-settings `extraKnownMarketplaces` entry carries a
`headersHelper` that Claude Code runs without asking, including background auto-update.

Runners, vault keys, `gog login` and `op inject` are deliberately NOT here: they belong to
the operator who hosts an agent (`canopy agent bootstrap`), not to someone who uses one.

    canopy agents connect [--dry-run] [--install-all]
    canopy workspace list [--json]
    canopy workspace op-status          # one line; always exits 0
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

import click

from orchestrator import canopy_web

WORKSPACES_PATH = "/api/workspaces/"
# The person's one agent catalog (canopy-web). Change the path HERE if it moves.
AGENTS_MARKETPLACE_NAME = "canopy-agents"
AGENTS_MARKETPLACE_PATH = "/agents/marketplace.json"
# The retired per-workspace catalogs (canopy-web#1376; now 410) — removed from settings.
LEGACY_MARKETPLACE_RE = re.compile(r"/w/[^/]+/marketplace\.json$")
HELPER_REL = Path("plugins") / "canopy" / "scripts" / "canopy-web-mcp-headers.js"
OP_INSTALL_URL = "https://developer.1password.com/docs/cli/get-started/"

Runner = Callable[..., subprocess.CompletedProcess]


class WorkspaceSetupError(click.ClickException):
    pass


def config_dir() -> Path:
    """Claude Code's config dir: $CLAUDE_CONFIG_DIR, else ~/.claude."""
    env = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(env).expanduser() if env else Path.home() / ".claude"


def marketplace_url(base_url: Optional[str] = None) -> str:
    return f"{canopy_web.resolve_base_url(base_url)}{AGENTS_MARKETPLACE_PATH}"


def person_token() -> str:
    """The PERSON's canopy-web token — setup is a human connecting their own machine,
    so an agent PAT found from the cwd is the wrong identity here."""
    env = os.environ.get("CANOPY_WEB_PAT", "").strip()
    if env:
        return env
    try:
        return canopy_web.TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _need_token() -> str:
    tok = person_token()
    if not tok:
        raise WorkspaceSetupError(
            "not signed in to canopy-web — run /canopy:canopy-web-pat-mint (a one-click "
            f"browser sign-in that writes {canopy_web.TOKEN_FILE}), then re-run.")
    return tok


def list_workspaces() -> list[dict]:
    rows = canopy_web.call("GET", WORKSPACES_PATH, token=_need_token()) or []
    if isinstance(rows, dict):
        rows = rows.get("items") or rows.get("results") or []
    return [{"slug": str(r.get("slug") or ""), "name": str(r.get("name") or r.get("slug") or "")}
            for r in rows if isinstance(r, dict) and r.get("slug")]


def fetch_catalog(token: str, *, transport: Optional[canopy_web.Transport] = None) -> dict:
    """GET the person's agent catalog as the person — to learn its plugins, and to fail
    HERE, with a readable reason, rather than inside Claude Code."""
    url = marketplace_url()
    transport = transport or canopy_web.urllib_transport
    try:
        status, text = transport("GET", url, {"Authorization": f"Bearer {token}",
                                              "Accept": "application/json"}, None)
    except OSError as e:
        raise WorkspaceSetupError(f"could not reach {url}: {e}")
    if status in (401, 403):
        raise WorkspaceSetupError(
            f"{url} -> {status}: canopy-web did not accept your sign-in. Re-run "
            f"/canopy:canopy-web-pat-mint.")
    if status in (404, 410):
        raise WorkspaceSetupError(
            f"{url} -> {status}: this canopy-web does not serve the agent catalog "
            f"({AGENTS_MARKETPLACE_NAME}) yet.")
    if not (200 <= status < 300):
        raise WorkspaceSetupError(f"{url} -> {status}: {text[:200]}")
    try:
        catalog = json.loads(text)
    except ValueError:
        # canopy-web's SPA answers unknown paths with its HTML shell (200).
        raise WorkspaceSetupError(
            f"{url} did not return a marketplace (got HTML/non-JSON): this canopy-web does "
            f"not serve the agent catalog yet.")
    if not isinstance(catalog, dict) or not str(catalog.get("name") or "").strip():
        raise WorkspaceSetupError(f"{url} is not a marketplace (no `name`)")
    return catalog


def helper_path(home: Optional[Path] = None) -> Optional[Path]:
    """A STABLE path to the headers helper. The marketplace clone
    (~/.claude/plugins/marketplaces/canopy) is updated in place, unlike the versioned
    plugin cache dir, which a canopy update deletes — and a settings entry pointing at
    a deleted file breaks every fetch silently."""
    cfg = home or config_dir()
    stable = cfg / "plugins" / "marketplaces" / "canopy" / HELPER_REL
    if stable.is_file():
        return stable
    try:
        reg = json.loads((cfg / "plugins" / "installed_plugins.json").read_text(encoding="utf-8"))
        root = Path(reg["plugins"]["canopy@canopy"][0]["installPath"])
        cand = root / "scripts" / "canopy-web-mcp-headers.js"
        return cand if cand.is_file() else None
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None


def helper_command(path: Path) -> str:
    """The `headersHelper` string: run through `sh` (cmd.exe on Windows), ≤500 chars."""
    node = "node"
    if os.name == "nt":
        return f'{node} "{path}" --archive'
    return f"{node} {shlex.quote(str(path))} --archive"


def marketplace_entry(url: str, helper_cmd: str) -> dict:
    return {"source": {"source": "url", "url": url, "headersHelper": helper_cmd},
            "autoUpdate": True}


def _legacy_names(known: dict) -> list[str]:
    """Entries pointing at a retired per-workspace catalog (`/w/<ws>/marketplace.json`)."""
    out = []
    for name, entry in known.items():
        src = entry.get("source") if isinstance(entry, dict) else None
        url = str(src.get("url") or "") if isinstance(src, dict) else ""
        if name != AGENTS_MARKETPLACE_NAME and LEGACY_MARKETPLACE_RE.search(url):
            out.append(name)
    return sorted(out)


def write_user_settings(name: str, entry: dict,
                        settings: Optional[Path] = None) -> tuple[bool, list[str]]:
    """Merge extraKnownMarketplaces[name] = entry into user settings, drop retired
    per-workspace entries, atomically, preserving every other key.
    Returns (file changed, legacy names removed)."""
    path = settings or (config_dir() / "settings.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError:
        raise WorkspaceSetupError(f"{path} is not valid JSON — fix it, then re-run")
    if not isinstance(data, dict):
        raise WorkspaceSetupError(f"{path} is not a JSON object")
    known = data.setdefault("extraKnownMarketplaces", {})
    if not isinstance(known, dict):
        raise WorkspaceSetupError(f"{path}: extraKnownMarketplaces is not a JSON object")
    removed = _legacy_names(known)
    if known.get(name) == entry and not removed:
        return False, []
    for old in removed:
        del known[old]
    known[name] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".settings-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return True, removed


def _claude(runner: Runner, args: list[str], timeout: int = 300,
            stdin_devnull: bool = False) -> subprocess.CompletedProcess:
    kw = dict(capture_output=True, text=True, timeout=timeout)
    if stdin_devnull:
        kw["stdin"] = subprocess.DEVNULL
    return runner(["claude", *args], **kw)


def _known(runner: Runner) -> set[str]:
    try:
        r = _claude(runner, ["plugin", "marketplace", "list", "--json"], timeout=60)
        rows = json.loads(r.stdout or "[]") if r.returncode == 0 else []
    except (OSError, ValueError, subprocess.SubprocessError):
        return set()
    return {str(x.get("name") or "") for x in rows if isinstance(x, dict)}


def materialize(name: str, runner: Runner) -> tuple[bool, str]:
    """Make Claude Code pick up the settings entry now, not at the next restart.

    The `plugin` subcommands don't reconcile `extraKnownMarketplaces`; session start
    does. `claude -p /help` starts a session that answers locally (no model call), which
    syncs the declared marketplace; `marketplace update` then fetches it through the
    helper. Measured on Claude Code 2.1.295."""
    if name not in _known(runner):
        try:
            _claude(runner, ["-p", "/help"], timeout=180, stdin_devnull=True)
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"could not start claude to sync settings ({type(e).__name__})"
    if name not in _known(runner):
        return False, (f"Claude Code has not picked up marketplace '{name}' yet — restart "
                       f"Claude Code, then run `claude plugin marketplace update {name}`")
    r = _claude(runner, ["plugin", "marketplace", "update", name], timeout=300)
    if r.returncode != 0:
        why = ((r.stderr or r.stdout or "").strip().splitlines() or ["no output"])[-1][:200]
        return False, f"`claude plugin marketplace update {name}` failed: {why}"
    return True, "registered and fetched"


def install_plugins(name: str, plugins: list[str], runner: Runner) -> list[tuple[str, bool, str]]:
    out = []
    for p in plugins:
        r = _claude(runner, ["plugin", "install", f"{p}@{name}", "--yes"], timeout=600)
        text = (r.stderr or r.stdout or "").strip().splitlines()
        out.append((p, r.returncode == 0, (text[-1] if text else "")[:200]))
    return out


def op_status(which: Callable[[str], Optional[str]] = shutil.which,
              runner: Runner = subprocess.run) -> tuple[bool, str]:
    """(ok, one line). Never fails setup: an agent only needs `op` when it keeps its
    credentials in 1Password AND you will act as it (`canopy cred check`)."""
    if not which("op"):
        return False, (f"1Password CLI: not installed — install it ({OP_INSTALL_URL}) and sign "
                       f"in (`op signin`) if you'll act as an agent that keeps its credentials "
                       f"in 1Password. Not needed to use agents.")
    try:
        r = runner(["op", "account", "list", "--format", "json"], capture_output=True,
                   text=True, timeout=20)
        accounts = json.loads(r.stdout or "[]") if r.returncode == 0 else []
    except (OSError, ValueError, subprocess.SubprocessError):
        accounts = []
    if not accounts:
        return False, ("1Password CLI: installed but no account signed in — run `op signin` "
                       "(or enable CLI integration in the 1Password app).")
    return True, f"1Password CLI: OK ({len(accounts)} account(s))"


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

@click.group("workspace")
def workspace_group():
    """Your canopy-web workspaces (list) and the 1Password check (op-status)."""


@workspace_group.command("list")
@click.option("--json", "as_json", is_flag=True)
def list_cmd(as_json):
    """The workspaces you belong to on canopy-web."""
    rows = list_workspaces()
    if as_json:
        click.echo(json.dumps(rows))
        return
    if not rows:
        click.echo("You are not a member of any canopy-web workspace yet — ask a workspace "
                   "admin to invite you.")
    for r in rows:
        click.echo(f"{r['slug']}\t{r['name']}")


@workspace_group.command("connect", hidden=True,
                         context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
def workspace_connect_retired():
    """Retired: use `canopy agents connect`."""
    raise WorkspaceSetupError(
        "`canopy workspace connect` is retired: there is one agent catalog per person now "
        f"({AGENTS_MARKETPLACE_NAME}). Run `canopy agents connect` (or /canopy:setup).")


@click.group("agents")
def agents_group():
    """Your agent catalog (canopy-agents): every agent in every workspace you belong to."""


@agents_group.command("connect")
@click.option("--dry-run", is_flag=True, help="Fetch the catalog and show the plan; change nothing.")
@click.option("--install-all", is_flag=True,
              help="Also install EVERY agent the catalog lists (default: install none).")
def agents_connect_cmd(dry_run, install_all):
    """Register your agent catalog (canopy-agents, autoUpdate on) and list its agents.

    Idempotent. Removes retired per-workspace `canopy-<ws>` entries from your settings.
    Installs nothing unless --install-all: install the agents you want with
    `/plugin install <agent>@canopy-agents`, then run that agent's setup command."""
    _connect(dry_run=dry_run, install_all=install_all)


def _agents(catalog: dict) -> list[dict]:
    return [p for p in catalog.get("plugins") or [] if isinstance(p, dict) and p.get("name")]


def _repo(p: dict) -> str:
    src = p.get("source")
    return str(src.get("repo") or "") if isinstance(src, dict) and src.get("source") == "github" else ""


def offer_lines(name: str, agents: list[dict]) -> list[str]:
    """Per agent: its exact install line, its own setup command, and its GitHub repo."""
    if not agents:
        return ["No agents to install yet — ask a workspace admin to add one."]
    rows = [(f"/plugin install {p['name']}@{name}", f"then /{p['name']}:setup",
             (f"[github: {_repo(p)}] " if _repo(p) else "")
             + " ".join(str(p.get("description") or "").split())[:100]) for p in agents]
    w0, w1 = max(len(r[0]) for r in rows), max(len(r[1]) for r in rows)
    out = ["Agents you can install (install only the ones you want):"]
    out += [f"  {a.ljust(w0)}  {b.ljust(w1)}  {c}".rstrip() for a, b, c in rows]
    out += ["Run each install line in Claude Code (or pick from the /plugin menu), then that "
            "agent's own setup command — it tells you what else that agent needs.",
            "Installing an agent needs your own GitHub read access to its repo; a private "
            "agent's install fails without it (ask its owner for access)."]
    return out


def _connect(*, dry_run: bool = False, install_all: bool = False,
             runner: Runner = subprocess.run,
             transport: Optional[canopy_web.Transport] = None) -> list[dict]:
    """Register the person's agent catalog; return its agent entries."""
    token = _need_token()
    catalog = fetch_catalog(token, transport=transport)
    name = AGENTS_MARKETPLACE_NAME
    served = str(catalog["name"]).strip()
    if served != name:
        raise WorkspaceSetupError(
            f"{marketplace_url()} names itself '{served}', expected '{name}' — canopy and "
            f"canopy-web disagree; update canopy (/canopy:update) and re-run.")
    agents = _agents(catalog)
    plugins = [str(p["name"]) for p in agents]
    url = marketplace_url()
    hp = helper_path()
    if hp is None:
        raise WorkspaceSetupError(
            "the canopy plugin's headers helper was not found — install canopy first "
            "(`claude plugin marketplace add dimagi-internal/canopy && claude plugin install "
            "canopy@canopy`), then re-run /canopy:setup")
    entry = marketplace_entry(url, helper_command(hp))
    click.echo(f"catalog    : '{name}' ({url}), {len(plugins)} agent(s): "
               f"{', '.join(plugins) or '(none)'}")
    if dry_run:
        click.echo("dry run — would add to user settings extraKnownMarketplaces:")
        click.echo(json.dumps({name: entry}, indent=2))
        path = config_dir() / "settings.json"
        try:
            known = json.loads(path.read_text(encoding="utf-8")).get("extraKnownMarketplaces") or {}
        except (OSError, ValueError, AttributeError):
            known = {}
        for old in _legacy_names(known if isinstance(known, dict) else {}):
            click.echo(f"dry run — would remove retired per-workspace marketplace '{old}'")
        if install_all and plugins:
            click.echo(f"dry run — would then install: {', '.join(f'{p}@{name}' for p in plugins)}")
        else:
            _echo_offer(name, agents)
        return agents
    changed, removed = write_user_settings(name, entry)
    click.echo(f"settings   : {'registered' if changed else 'already registered'} "
               f"'{name}' in {config_dir() / 'settings.json'} (autoUpdate on)")
    for old in removed:
        click.echo(f"settings   : removed retired per-workspace marketplace '{old}' — reinstall "
                   f"any agent you had from it as <agent>@{name}; `claude plugin marketplace "
                   f"remove {old}` clears its leftover copy")
    ok, how = materialize(name, runner)
    click.echo(f"marketplace: {how}")
    if not ok:
        raise WorkspaceSetupError(how)
    if not install_all or not plugins:
        _echo_offer(name, agents)
        return agents
    results = install_plugins(name, plugins, runner)
    for p, good, line in results:
        click.echo(f"install    : {p}@{name} {'OK' if good else 'FAIL — ' + line}")
    bad = [p for p, good, _ in results if not good]
    if bad:
        raise WorkspaceSetupError(f"{len(bad)} plugin(s) did not install: {', '.join(bad)} — "
                                  f"check you have GitHub read access to their repos, then retry "
                                  f"with `claude plugin install <name>@{name}`")
    click.echo("Restart Claude Code (or /reload-plugins) to load the new agents, then run each "
               "agent's own setup command (e.g. /<agent>:setup).")
    return agents


def _echo_offer(name: str, agents: list[dict]) -> None:
    click.echo("")
    for line in offer_lines(name, agents):
        click.echo(line)


@workspace_group.command("op-status")
def op_status_cmd():
    """One line on the 1Password CLI. Always exits 0."""
    _, line = op_status()
    click.echo(line)
