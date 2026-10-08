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

It also gives the session the agent's GitHub identity (canopy#832) — the twin of the
cloud runner's per-turn `github_env`. `canopy agent github-env` resolves the agent's
credential (canopy-web's `AgentDelegation`, same custodian, same operator-authorized route)
and the hook appends GH_TOKEN (gh), a credential helper and git author/committer to the env
file, replacing emdash's GIT_CONFIG_PARAMETERS helper (the machine owner's `gh` login). With
NO credential it writes a REFUSAL instead — a helper that says why and tells git to quit, and
an empty gh config — so an agent never silently pushes as the human. Not touched: a cloud
turn (`CANOPY_REQUESTED_BY` set — the runner already decided), or a human opting in with
CANOPY_ALLOW_OPERATOR_IDENTITY=1 (the same opt-in as canopy#813).

Inert everywhere else: no CANOPY_AGENT (a human's own project), a key already present (a
cloud turn), or no CLAUDE_ENV_FILE → exits 0 silently. Fail-open: any failure prints ONE line
of context and exits 0 — a session without the key falls back to the desktop app, exactly as
before this hook existed. The key is never printed.

stdlib only — runs under the harness's python3, with no `orchestrator` on sys.path.
"""
from __future__ import annotations

import os
import re
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


ALLOW_OPERATOR_ENV = "CANOPY_ALLOW_OPERATOR_IDENTITY"
_HELPER_KEY = "credential.https://github.com.helper"


def _sq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def refusal_lines(slug: str, why: str, home: str | None = None) -> str:
    """Env-file lines that leave the session with NO GitHub identity: git's helpers are
    cleared and replaced by one that prints `why` and tells git to quit (no prompt, no
    next helper), and gh gets an empty config dir instead of the machine's login."""
    why = re.sub(r"[^\w .,:;/()@<>+=#-]", " ", why)[:300]   # no quotes/$/` inside the helper
    msg = (f"canopy: refusing to act on GitHub as this machine's own login — {slug} has no "
           f"GitHub identity ({why}). An admin sets it at /agents/{slug}/settings -> "
           f"Credentials -> GitHub; check with: canopy agent github --slug {slug}")
    helper = f'!f() {{ test "$1" = get || exit 0; echo "{msg}" >&2; echo quit=1; }}; f'
    params = f"{_sq(_HELPER_KEY)}={_sq('')} {_sq(_HELPER_KEY)}={_sq(helper)}"
    gh_dir = os.path.join(home or os.path.expanduser("~"), ".canopy", "github", "no-identity")
    try:
        os.makedirs(gh_dir, exist_ok=True)
    except OSError:
        pass
    return ("unset GH_TOKEN GITHUB_TOKEN\n"
            f"export GIT_CONFIG_PARAMETERS={shlex.quote(params)}\n"
            f"export GH_CONFIG_DIR={shlex.quote(gh_dir)}\n"
            "export CANOPY_AGENT_GITHUB=none\n")


def stage_github(canopy: str | None, slug: str, env_file: str, *, env=None, run=subprocess.run) -> str:
    """Give this agent session its GitHub identity, or a refusal. Returns the one line of
    context to print ("" when the session is not ours to touch)."""
    env = os.environ if env is None else env
    if "CANOPY_REQUESTED_BY" in env:     # a cloud runner turn: the runner already decided
        return ""
    if (env.get(ALLOW_OPERATOR_ENV) or "").strip() == "1":
        return (f"canopy: {ALLOW_OPERATOR_ENV}=1 — git/gh in this {slug} session use this "
                f"machine's own GitHub login")
    if env.get("CANOPY_PROFILE"):
        why = "a caller's confined turn is never given its agent's GitHub identity"
    elif not canopy:
        why = "no `canopy` CLI on PATH to resolve it"
    else:
        try:
            r = run([canopy, "agent", "github-env", "--slug", slug],
                    capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as e:
            r, why = None, f"could not run `canopy agent github-env` ({type(e).__name__})"
        if r is not None:
            out = r.stdout or ""
            if r.returncode == 0 and "export GH_TOKEN=" in out:
                try:
                    with open(env_file, "a", encoding="utf-8") as fh:
                        fh.write(out)
                except OSError as e:
                    return f"canopy: could not write {slug}'s GitHub identity to the session env ({e})"
                said = ((r.stderr or "").strip().splitlines() or ["its GitHub identity"])[-1]
                return f"canopy: {said[:200]}"
            why = ((r.stderr or "").strip().splitlines() or ["no identity returned"])[-1]
            why = why.removeprefix("Error: ")[:240]
    try:
        with open(env_file, "a", encoding="utf-8") as fh:
            fh.write(refusal_lines(slug, why))
    except OSError as e:
        return f"canopy: could not write {slug}'s GitHub refusal to the session env ({e})"
    return (f"canopy: {slug} has no GitHub identity here ({why}) — git push and gh writes "
            f"are refused rather than run as this machine's own login")


def main() -> int:
    slug = os.environ.get("CANOPY_AGENT", "").strip()
    env_file = os.environ.get("CLAUDE_ENV_FILE", "").strip()
    if not slug:
        return 0
    canopy = shutil.which("canopy")
    if canopy and not os.environ.get("CANOPY_PROFILE"):   # never into a caller's confined turn
        stage_delegated(canopy, slug)
    if env_file:
        said = stage_github(canopy, slug, env_file)
        if said:
            print(said)
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
