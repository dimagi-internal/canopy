#!/usr/bin/env python3
"""Give an agent session its OWN 1Password key, and stage what it BORROWS, the way the
cloud runner does.

Cloud turns get `OP_SERVICE_ACCOUNT_TOKEN` from the runner (`cloud_runner._agent_op_token`):
the agent's own service-account key, custodied by canopy-web, scoped to its own vault. An
emdash / laptop session got nothing, so every `op read` went through the 1Password DESKTOP
app — which locks, and then `op` either fails ("account is not signed in") or hangs on an
unlock prompt that an unattended turn has nobody to answer. Measured 2026-10-07 on an Eva
emdash session: a credential mint stalled for 60s on exactly that.

This SessionStart hook closes the gap. When the session is an agent's (`$CANOPY_AGENT`, set
by every agent repo's .claude/settings.json) and has no key yet, it asks
`canopy agent op-token` (operator-authorized `/credentials/resolve`, the runner's own route)
and appends `export OP_SERVICE_ACCOUNT_TOKEN=…` to `$CLAUDE_ENV_FILE`, which Claude Code
sources before every Bash command in the session. Same key, same scope, same custodian as
the cloud — only the delivery differs.

Then it keeps the agent's `~/.<slug>/.env` current with its vault: `canopy cred sync`
re-injects it only when the vault's `content_version` or the `.env.tpl` changed, checked at
most every 5 minutes per agent — so a value changed in 1Password reaches the next session
instead of waiting for someone to re-run `op inject` by hand (2026-10-12: ACE's Drive root).

It also exports the instance this install is bound to (`canopy agent env` records it in
`canopy-instance.json` beside the agent's env — `~/.<slug>/`, or the agent plugin's data dir
`~/.claude/plugins/data/<slug>-<marketplace>/` for ACE; see `find_binding`) as `AGENT_VAULT` / `AGENT_SLUG`, so a hand-run
`op inject -i .env.tpl …` resolves the template's `op://${AGENT_VAULT}/…` refs to the right
vault. Nothing is exported when there is no binding — no vault name is ever guessed.

Inert everywhere else: no CANOPY_AGENT (a human's own project), a key already present (a
cloud turn), or no CLAUDE_ENV_FILE → exits 0 silently. Fail-open: any failure prints ONE line
of context and exits 0 — a session without the key falls back to the desktop app, exactly as
before this hook existed. The key is never printed.

stdlib only — runs under the harness's python3, with no `orchestrator` on sys.path.
"""
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys


def _load_binding(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            b = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(b, dict):
        return {}
    if not str(b.get("vault") or "").strip() or not str(b.get("agent") or "").strip():
        return {}
    return b


def _installed_data_dirs(home: str, slug: str) -> set:
    """The plugin-data dir names (`<plugin>-<marketplace>`, as Claude Code names them) of
    every installed `<slug>@<marketplace>` plugin."""
    try:
        with open(os.path.join(home, ".claude", "plugins", "installed_plugins.json"),
                  encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return set()
    plugins = data.get("plugins", data) if isinstance(data, dict) else {}
    names = set()
    for key in plugins if isinstance(plugins, dict) else ():
        plugin, _, market = str(key).partition("@")
        if plugin == slug and market:
            names.add(re.sub(r"[^A-Za-z0-9_-]", "-", f"{plugin}-{market}"))
    return names


def find_binding(slug: str, home: str):
    """(binding, None) for the ONE binding `canopy agent env` recorded for this agent, or
    (None, why) when it found several it cannot choose between, or (None, None) for none.

    `canopy agent env` writes the binding beside the env it fills, and agents put that env
    in different places: Ada/Eva/Hal in `~/.<slug>/`, ACE in its plugin data dir
    (`$CLAUDE_PLUGIN_DATA` = `~/.claude/plugins/data/<plugin>-<marketplace>/`). This hook is
    the CANOPY plugin's, so its own `$CLAUDE_PLUGIN_DATA` is canopy's data dir, never the
    agent's — it is not consulted. Order: `~/.<slug>/`, then each
    `~/.claude/plugins/data/<slug>-*/` whose binding names plugin `<slug>`; several there →
    the one whose dir matches an installed `<slug>@<marketplace>`, else ambiguous."""
    b = _load_binding(os.path.join(home, f".{slug}", "canopy-instance.json"))
    if b:
        return b, None
    data_root = os.path.join(home, ".claude", "plugins", "data")
    found = {}
    for d in sorted(glob.glob(os.path.join(glob.escape(data_root), f"{glob.escape(slug)}-*"))):
        b = _load_binding(os.path.join(d, "canopy-instance.json"))
        if b and b.get("plugin") == slug:
            found[os.path.basename(d)] = b
    if len(found) <= 1:
        return (next(iter(found.values())), None) if found else (None, None)
    installed = [n for n in found if n in _installed_data_dirs(home, slug)]
    if len(installed) == 1:
        return found[installed[0]], None
    return None, (f"{len(found)} {slug} bindings ({', '.join(sorted(found))}) and "
                  f"{'none' if not installed else 'several'} match an installed plugin")


def export_binding(slug: str, env_file: str) -> None:
    """AGENT_VAULT / AGENT_SLUG from this install's binding, into the session env. Silent
    when there is no binding or the session already has them; one line when ambiguous."""
    if os.environ.get("AGENT_VAULT", "").strip():
        return
    b, why = find_binding(slug, os.path.expanduser("~"))
    if why:
        print(f"canopy: not exporting {slug}'s AGENT_VAULT — {why}; "
              f"re-run `canopy agent env --plugin {slug}` for the install you use")
        return
    if not b:
        return
    vault = str(b["vault"]).strip()
    agent = str(b["agent"]).strip()
    try:
        with open(env_file, "a", encoding="utf-8") as fh:
            fh.write(f"export AGENT_VAULT={shlex.quote(vault)}\n"
                     f"export AGENT_SLUG={shlex.quote(agent)}\n")
    except OSError as e:
        print(f"canopy: could not export {slug}'s bound vault to the session env ({e})")


def stage_delegated(canopy: str, slug: str) -> None:
    """Stage the credentials this agent BORROWS (`canopy agent stage-delegated`):
    today the Salesforce identity lent to it (canopy-web#1291), which chrome-sales
    reads from ~/.canopy/delegated/<slug>/chrome-sales/ in agent sessions. Silent
    when it worked or there is nothing to stage; one line when it failed."""
    try:
        r = subprocess.run([canopy, "agent", "stage-delegated", "--slug", slug],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"canopy: could not stage {slug}'s delegated credentials ({type(e).__name__})")
        return
    if r.returncode != 0:
        why = ((r.stderr or "").strip().splitlines() or ["no output"])[-1][:200]
        print(f"canopy: could not stage {slug}'s delegated credentials ({why})")


def sync_env(canopy: str, slug: str, token: str) -> None:
    """`canopy cred sync` with the agent's key: re-inject ~/.<slug>/.env iff its vault
    or .env.tpl changed (throttled, one cheap probe when due). Silent when nothing
    changed; one line when it re-injected or failed."""
    try:
        r = subprocess.run([canopy, "cred", "sync", "--agent", slug], capture_output=True,
                           text=True, timeout=25,
                           env={**os.environ, "OP_SERVICE_ACCOUNT_TOKEN": token})
    except (OSError, subprocess.SubprocessError) as e:
        print(f"canopy: could not check {slug}'s env against its vault ({type(e).__name__})")
        return
    last = ((r.stderr or "").strip().splitlines() or [""])[-1][:300]
    if r.returncode != 0:
        print(f"canopy: {slug}'s ~/.{slug}/.env may be stale — {last or 'cred sync failed'}")
    elif "re-injected" in last:
        print(f"canopy: {slug}'s vault changed — re-injected ~/.{slug}/.env")


def main() -> int:
    slug = os.environ.get("CANOPY_AGENT", "").strip()
    env_file = os.environ.get("CLAUDE_ENV_FILE", "").strip()
    if not slug:
        return 0
    if env_file:
        export_binding(slug, env_file)
    canopy = shutil.which("canopy")
    confined = bool(os.environ.get("CANOPY_PROFILE"))   # a caller's confined turn
    if canopy and not confined:
        stage_delegated(canopy, slug)
    present = os.environ.get("OP_SERVICE_ACCOUNT_TOKEN", "").strip()
    if present:
        if canopy and not confined:
            sync_env(canopy, slug, present)
        return 0
    if not env_file:
        return 0
    if not canopy:
        print(f"canopy: no `canopy` CLI on PATH — {slug}'s `op` calls will use the 1Password app")
        return 0
    try:
        r = subprocess.run([canopy, "agent", "op-token", "--slug", slug],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"canopy: could not fetch {slug}'s 1Password key ({type(e).__name__}) — "
              f"`op` will use the 1Password app")
        return 0
    token = (r.stdout or "").strip()
    if r.returncode != 0 or not token or "\n" in token:
        why = ((r.stderr or "").strip().splitlines() or ["no key returned"])[-1][:200]
        print(f"canopy: no 1Password key for {slug} ({why}) — `op` will use the 1Password app")
        return 0
    try:
        with open(env_file, "a", encoding="utf-8") as fh:
            fh.write(f"export OP_SERVICE_ACCOUNT_TOKEN={shlex.quote(token)}\n")
    except OSError as e:
        print(f"canopy: could not write {slug}'s 1Password key to the session env ({e})")
    if not confined:
        sync_env(canopy, slug, token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
