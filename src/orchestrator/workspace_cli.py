"""`canopy workspace` — connect this machine to a canopy-web workspace's agents (canopy#851).

The new-user half of `/canopy:setup`: after the browser sign-in (the PAT mint), the
person picks a workspace and this registers that workspace's agent marketplace —
`https://<canopy-web>/w/<ws>/marketplace.json` (canopy-web#1376) — in Claude Code's USER
settings with `autoUpdate`, authenticated by the canopy plugin's headers helper in
`--archive` mode, then installs every agent plugin it lists.

Why user settings and not `claude plugin marketplace add <url>`: `add` has no way to
send a header, and the catalog is filtered by the caller's membership, so it must be
fetched as the person. A user-settings `extraKnownMarketplaces` entry carries a
`headersHelper` that Claude Code runs without asking, for the catalog AND every archive
download on that origin, including background auto-update.

Runners, vault keys, `gog login` and `op inject` are deliberately NOT here: they belong to
the operator who hosts an agent (`canopy agent bootstrap`), not to someone who uses one.

    canopy workspace list [--json]
    canopy workspace connect <ws> [--dry-run] [--no-install]
    canopy workspace op-status          # one line; always exits 0
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

import click

from orchestrator import canopy_web

WORKSPACES_PATH = "/api/workspaces/"
HELPER_REL = Path("plugins") / "canopy" / "scripts" / "canopy-web-mcp-headers.js"
OP_INSTALL_URL = "https://developer.1password.com/docs/cli/get-started/"

Runner = Callable[..., subprocess.CompletedProcess]


class WorkspaceSetupError(click.ClickException):
    pass


def config_dir() -> Path:
    """Claude Code's config dir: $CLAUDE_CONFIG_DIR, else ~/.claude."""
    env = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(env).expanduser() if env else Path.home() / ".claude"


def marketplace_url(workspace: str, base_url: Optional[str] = None) -> str:
    return f"{canopy_web.resolve_base_url(base_url)}/w/{workspace}/marketplace.json"


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


def fetch_catalog(workspace: str, token: str, *,
                  transport: Optional[canopy_web.Transport] = None) -> dict:
    """GET the workspace's marketplace.json as the person — to learn its `name` and
    plugins, and to fail HERE, with a readable reason, rather than inside Claude Code."""
    url = marketplace_url(workspace)
    transport = transport or canopy_web.urllib_transport
    try:
        status, text = transport("GET", url, {"Authorization": f"Bearer {token}",
                                              "Accept": "application/json"}, None)
    except OSError as e:
        raise WorkspaceSetupError(f"could not reach {url}: {e}")
    if status in (401, 403):
        raise WorkspaceSetupError(
            f"{url} -> {status}: canopy-web did not accept your sign-in for workspace "
            f"'{workspace}'. Re-run /canopy:canopy-web-pat-mint, and check you are a member "
            f"of '{workspace}'.")
    if status == 404:
        raise WorkspaceSetupError(
            f"{url} -> 404: no agent marketplace for workspace '{workspace}' (wrong slug, "
            f"or this canopy-web does not serve workspace marketplaces yet — canopy-web#1376).")
    if not (200 <= status < 300):
        raise WorkspaceSetupError(f"{url} -> {status}: {text[:200]}")
    try:
        catalog = json.loads(text)
    except ValueError:
        # canopy-web's SPA answers unknown /w/<ws>/… paths with its HTML shell (200).
        raise WorkspaceSetupError(
            f"{url} did not return a marketplace (got HTML/non-JSON): this canopy-web does "
            f"not serve workspace marketplaces yet (canopy-web#1376), or '{workspace}' is "
            f"not a workspace slug.")
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


def write_user_settings(name: str, entry: dict, settings: Optional[Path] = None) -> bool:
    """Merge extraKnownMarketplaces[name] = entry into user settings, atomically,
    preserving every other key. Returns True when the file changed."""
    path = settings or (config_dir() / "settings.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError:
        raise WorkspaceSetupError(f"{path} is not valid JSON — fix it, then re-run")
    if not isinstance(data, dict):
        raise WorkspaceSetupError(f"{path} is not a JSON object")
    known = data.setdefault("extraKnownMarketplaces", {})
    if known.get(name) == entry:
        return False
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
    return True


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
    """Connect this machine to a canopy-web workspace's agents (canopy#851)."""


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


@workspace_group.command("connect")
@click.argument("workspace")
@click.option("--dry-run", is_flag=True, help="Fetch the catalog and show the plan; change nothing.")
@click.option("--no-install", is_flag=True, help="Register the marketplace but install nothing.")
def connect_cmd(workspace, dry_run, no_install):
    """Register WORKSPACE's agent marketplace (autoUpdate on) and install its agents."""
    _connect(workspace, dry_run=dry_run, no_install=no_install)


def _connect(workspace: str, *, dry_run: bool = False, no_install: bool = False,
             runner: Runner = subprocess.run,
             transport: Optional[canopy_web.Transport] = None) -> None:
    token = _need_token()
    catalog = fetch_catalog(workspace, token, transport=transport)
    name = str(catalog["name"]).strip()
    plugins = [str(p.get("name")) for p in catalog.get("plugins") or []
               if isinstance(p, dict) and p.get("name")]
    url = marketplace_url(workspace)
    hp = helper_path()
    if hp is None:
        raise WorkspaceSetupError(
            "the canopy plugin's headers helper was not found — install canopy first "
            "(`claude plugin marketplace add dimagi-internal/canopy && claude plugin install "
            "canopy@canopy`), then re-run /canopy:setup")
    entry = marketplace_entry(url, helper_command(hp))
    click.echo(f"workspace '{workspace}': marketplace '{name}' ({url}), "
               f"{len(plugins)} agent plugin(s): {', '.join(plugins) or '(none)'}")
    if dry_run:
        click.echo("dry run — would add to user settings extraKnownMarketplaces:")
        click.echo(json.dumps({name: entry}, indent=2))
        return
    changed = write_user_settings(name, entry)
    click.echo(f"settings   : {'registered' if changed else 'already registered'} "
               f"'{name}' in {config_dir() / 'settings.json'} (autoUpdate on)")
    ok, how = materialize(name, runner)
    click.echo(f"marketplace: {how}")
    if not ok:
        raise WorkspaceSetupError(how)
    if no_install or not plugins:
        return
    results = install_plugins(name, plugins, runner)
    for p, good, line in results:
        click.echo(f"install    : {p}@{name} {'OK' if good else 'FAIL — ' + line}")
    bad = [p for p, good, _ in results if not good]
    if bad:
        raise WorkspaceSetupError(f"{len(bad)} plugin(s) did not install: {', '.join(bad)} — "
                                  f"retry with `claude plugin install <name>@{name}`")
    click.echo("Restart Claude Code (or /reload-plugins) to load the new agents.")


@workspace_group.command("op-status")
def op_status_cmd():
    """One line on the 1Password CLI. Always exits 0."""
    _, line = op_status()
    click.echo(line)
