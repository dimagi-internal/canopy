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

Inert everywhere else: no CANOPY_AGENT (a human's own project), a key already present (a
cloud turn), or no CLAUDE_ENV_FILE → exits 0 silently. Fail-open: any failure prints ONE line
of context and exits 0 — a session without the key falls back to the desktop app, exactly as
before this hook existed. The key is never printed.

stdlib only — runs under the harness's python3, with no `orchestrator` on sys.path.
"""
import os
import shlex
import shutil
import subprocess
import sys


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


def main() -> int:
    slug = os.environ.get("CANOPY_AGENT", "").strip()
    env_file = os.environ.get("CLAUDE_ENV_FILE", "").strip()
    if not slug:
        return 0
    canopy = shutil.which("canopy")
    if canopy and not os.environ.get("CANOPY_PROFILE"):   # never into a caller's confined turn
        stage_delegated(canopy, slug)
    if not env_file or os.environ.get("OP_SERVICE_ACCOUNT_TOKEN"):
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
