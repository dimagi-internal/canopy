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
`~/.<slug>/canopy-instance.json`) as `AGENT_VAULT` / `AGENT_SLUG`, so a hand-run
`op inject -i .env.tpl …` resolves the template's `op://${AGENT_VAULT}/…` refs to the right
vault. Nothing is exported when there is no binding — no vault name is ever guessed.

Inert everywhere else: no CANOPY_AGENT (a human's own project), a key already present (a
cloud turn), or no CLAUDE_ENV_FILE → exits 0 silently. Fail-open: any failure prints ONE line
of context and exits 0 — a session without the key falls back to the desktop app, exactly as
before this hook existed. The key is never printed.

stdlib only — runs under the harness's python3, with no `orchestrator` on sys.path.
"""
import json
import os
import shlex
import shutil
import subprocess
import sys


def export_binding(slug: str, env_file: str) -> None:
    """AGENT_VAULT / AGENT_SLUG from this install's binding, into the session env. Silent
    when there is no binding or the session already has them."""
    if os.environ.get("AGENT_VAULT", "").strip():
        return
    path = os.path.join(os.path.expanduser("~"), f".{slug}", "canopy-instance.json")
    try:
        with open(path, encoding="utf-8") as fh:
            b = json.load(fh)
    except (OSError, ValueError):
        return
    vault = str((b or {}).get("vault") or "").strip() if isinstance(b, dict) else ""
    agent = str((b or {}).get("agent") or "").strip() if isinstance(b, dict) else ""
    if not vault or not agent:
        return
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
