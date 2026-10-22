#!/usr/bin/env python3
"""Keep an agent session's env current from the agent's vault, and stage what it BORROWS,
the way the cloud runner does.

Every agent's secrets live in canopy vault (canopy-web, zero-knowledge) — Jonathan,
2026-10-21. For such an agent this hook only syncs: `canopy cred sync` refreshes
`~/.<slug>/.env` from canopy vault when the values changed, inside the session's local
turn. It says nothing about 1Password: the agent has no 1Password key, by design
(`canopy agent op-token` exits 3 for it).

LEGACY 1Password agents: cloud turns get `OP_SERVICE_ACCOUNT_TOKEN` from the runner (the
agent's own service-account key, custodied by canopy-web, scoped to its own vault). An
emdash / laptop session got nothing, so every `op read` went through the 1Password DESKTOP
app — which locks, and then `op` either fails ("account is not signed in") or hangs on an
unlock prompt that an unattended turn has nobody to answer. Measured 2026-10-07 on an Eva
emdash session: a credential mint stalled for 60s on exactly that.

For those, this hook closes the gap. When the session is an agent's (`$CANOPY_AGENT`, set
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

It never writes on a runner that keeps secrets off disk (`$CANOPY_AGENT_ENV_FILE` set: the
turn's env is that per-turn file, and the hook leaves it alone).

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
    changed; one line when it re-injected or failed.

    Never on a runner that handed this turn `$CANOPY_AGENT_ENV_FILE`: that file IS the
    turn's env (written at claim, removed when the turn ends), and a sync would write a
    persistent `~/.<slug>/.env` beside it — the copy the runner keeps off disk."""
    if os.environ.get("CANOPY_AGENT_ENV_FILE", "").strip():
        return
    env = dict(os.environ)
    if token:
        env["OP_SERVICE_ACCOUNT_TOKEN"] = token
    else:
        # No key: an agent on the canopy-web backend (Canopy Vault, canopy#946) has none
        # by design; `cred sync` then refreshes its env from canopy-web, and for any other
        # agent it is a no-op ("skipped").
        env.pop("OP_SERVICE_ACCOUNT_TOKEN", None)
    try:
        r = subprocess.run([canopy, "cred", "sync", "--agent", slug], capture_output=True,
                           text=True, timeout=25, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"canopy: could not check {slug}'s env against its vault ({type(e).__name__})")
        return
    last = ((r.stderr or "").strip().splitlines() or [""])[-1][:300]
    if r.returncode != 0:
        print(f"canopy: {slug}'s ~/.{slug}/.env may be stale — {last or 'cred sync failed'}")
    elif "re-injected from canopy-web" in last:
        print(f"canopy: {slug}'s credentials changed on canopy-web — re-wrote ~/.{slug}/.env")
    elif "re-injected" in last:
        print(f"canopy: {slug}'s vault changed — re-injected ~/.{slug}/.env")


def _hook_input() -> dict:
    """SessionStart's stdin (`session_id`, `cwd`, `source`, …), or {}."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        data = json.loads(sys.stdin.read() or "{}")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


#: `canopy agent op-token` exits with this for an agent whose secrets live in canopy vault.
OP_TOKEN_CANOPY_VAULT = 3

#: Set by the runner app's headless engine (canopy-web runner/headless.py) on every
#: Claude it starts for a turn — the emdash engine passes `$CANOPY_TURN_ID` instead.
RUNNER_ENV_MARKERS = ("CANOPY_HEADLESS", "CANOPY_ONE_SHOT_TURN")
#: The runner app's claude-desktop / claude-cli engines put every session they open in a
#: worktree holding this channel directory (canopy-web runner/desktop.py `CHANNEL`).
#: The Claude app, not the runner, starts that session's process, so no runner env can
#: reach it: the worktree is the only mark a runner session carries there.
RUNNER_DESKTOP_CHANNEL = ".canopy-desktop"


def local_eligible(home: str, cwd: str = "") -> bool:
    """Is this a person's own session on their registered private runner?

    Yes when `~/.canopy/private-runner.json` exists (`canopy agent use <slug>` registered this
    computer for local use) and the
    session is not a runner's: no `$CANOPY_TURN_ID` (a cloud turn), no confined profile,
    no headless/one-shot marker (the runner app's headless engine starts claude with
    `CANOPY_HEADLESS=1 CANOPY_ONE_SHOT_TURN=1` and no turn id), not inside a desktop-engine
    worktree (`<cwd>/.canopy-desktop/`), and not an emdash task on a machine that runs the
    runner app (`~/.canopy/runner.json` — emdash cannot pass a turn id, so on a runner box
    every emdash session is presumed the runner's, whose turn already exists). A private
    runner may itself be a runner app working only for its owner (canopy-web#1705): its
    turns are already turns, and a second, local one would switch their credential reads
    from the runner to the license. canopy-web#1704."""
    if not os.path.isfile(os.path.join(home, ".canopy", "private-runner.json")):
        return False
    if os.environ.get("CANOPY_TURN_ID", "").strip() or os.environ.get("CANOPY_PROFILE"):
        return False
    if any(os.environ.get(v, "").strip() for v in RUNNER_ENV_MARKERS):
        return False
    if cwd and os.path.isdir(os.path.join(cwd, RUNNER_DESKTOP_CHANNEL)):
        return False
    if (os.environ.get("EMDASH_TASK_ID", "").strip()
            and os.path.isfile(os.path.join(home, ".canopy", "runner.json"))):
        return False
    return True


def open_local_turn(canopy: str, slug: str, data: dict, env_file: str) -> str:
    """Open this session's LOCAL turn (`canopy local open`) and export its id, so every
    later canopy call in the session — a credential read above all — names it. Returns
    the turn id, or "" (one line printed saying why). Never fails the session."""
    session_id = str(data.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    if not session_id:
        return ""
    try:
        r = subprocess.run([canopy, "local", "open", "--slug", slug, "--session-id", session_id,
                            "--cwd", str(data.get("cwd") or os.getcwd()),
                            "--transcript", str(data.get("transcript_path") or "")],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"canopy: could not open a local turn for {slug} ({type(e).__name__})")
        return ""
    if r.returncode != 0:
        why = ((r.stderr or "").strip().splitlines() or ["no output"])[-1][:300]
        print(f"canopy: this session is not a local turn of {slug} — {why}")
        return ""
    try:
        state = json.loads((r.stdout or "").strip().splitlines()[-1])
        turn_id = str(state["turn_id"])
    except (ValueError, KeyError, IndexError):
        print(f"canopy: `canopy local open` gave no turn for {slug}")
        return ""
    if env_file:
        try:
            with open(env_file, "a", encoding="utf-8") as fh:
                fh.write(f"export CANOPY_TURN_ID={shlex.quote(turn_id)}\n"
                         f"export CANOPY_AGENT_SLUG={shlex.quote(slug)}\n"
                         "export CANOPY_LOCAL_TURN=1\n")
        except OSError as e:
            print(f"canopy: could not export the local turn to the session env ({e})")
    print(f"canopy: local turn of {slug} on private runner {state.get('private_runner') or '?'} — "
          f"{state.get('turn_mode') or '?'} ({state.get('turn_mode_basis') or ''}) — "
          f"{state.get('url') or turn_id}")
    return turn_id


def main() -> int:
    slug = os.environ.get("CANOPY_AGENT", "").strip()
    env_file = os.environ.get("CLAUDE_ENV_FILE", "").strip()
    if not slug:
        return 0
    data = _hook_input()
    if env_file:
        export_binding(slug, env_file)
    canopy = shutil.which("canopy")
    confined = bool(os.environ.get("CANOPY_PROFILE"))   # a caller's confined turn
    # A runner turn (it has a turn id) already had its borrowed credentials staged by the
    # runner with the runner's own identity (cloud_runner._stage_salesforce). Asking again
    # here would use the agent's PAT, which /credentials/resolve refuses (lex, 2026-10-16).
    on_runner = bool(os.environ.get("CANOPY_TURN_ID", "").strip())
    # A person's own session on their private runner: record it as a local turn FIRST, and
    # carry its id into every credential read below (canopy-web#1704) — that is what
    # admits a license holder, and what the read log names.
    if canopy and local_eligible(os.path.expanduser("~"), str(data.get("cwd") or os.getcwd())):
        turn_id = open_local_turn(canopy, slug, data, env_file)
        if turn_id:
            os.environ["CANOPY_TURN_ID"] = turn_id
            # The reads below are this local turn's: on a private runner they sign in
            # with the box's own key (orchestrator/private_runner_session.py).
            os.environ["CANOPY_LOCAL_TURN"] = "1"
    if canopy and not confined and not on_runner:
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
    if r.returncode == OP_TOKEN_CANOPY_VAULT:
        # canopy vault: no 1Password key by design — nothing to say about 1Password.
        if not confined:
            sync_env(canopy, slug, "")
        return 0
    if r.returncode != 0 or not token or "\n" in token:
        why = ((r.stderr or "").strip().splitlines() or ["no key returned"])[-1][:200]
        print(f"canopy: no 1Password key for {slug} ({why}) — `op` will use the 1Password app")
        if not confined:
            sync_env(canopy, slug, "")   # a canopy-web-backed agent refreshes from there
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
