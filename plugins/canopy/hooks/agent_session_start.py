#!/usr/bin/env python3
"""SessionStart: set up an agent session the way the cloud runner does.

Every agent's secrets live in the Canopy vault on canopy-web. For an agent session
(`$CANOPY_AGENT`, set by every agent repo's .claude/settings.json) this hook

* opens the person's LOCAL turn when the session is theirs on a registered private runner,
* stages the credentials the agent BORROWS (`canopy agent stage-delegated`),
* sets up the agent's Google login on this computer the first time, and
* keeps `~/.<slug>/.env` current: `canopy cred sync` re-writes it from the vault when the
  values changed, checked at most every 5 minutes per agent.

It never writes on a runner that keeps secrets off disk (`$CANOPY_AGENT_ENV_FILE` set: the
turn's env is that per-turn file, and the hook leaves it alone).

Inert everywhere else: no CANOPY_AGENT (a human's own project) -> exits 0 silently.
Fail-open: any failure prints ONE line of context and exits 0.

stdlib only — runs under the harness's python3, with no `orchestrator` on sys.path.
"""
import json
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


def sync_env(canopy: str, slug: str) -> None:
    """`canopy cred sync`: re-write ~/.<slug>/.env iff the agent's vault changed (throttled,
    one cheap probe when due). Silent when nothing changed; one line when it re-wrote or failed.

    Never on a runner that handed this turn `$CANOPY_AGENT_ENV_FILE`: that file IS the
    turn's env (written at claim, removed when the turn ends), and a sync would write a
    persistent `~/.<slug>/.env` beside it — the copy the runner keeps off disk."""
    if os.environ.get("CANOPY_AGENT_ENV_FILE", "").strip():
        return
    try:
        r = subprocess.run([canopy, "cred", "sync", "--agent", slug], capture_output=True,
                           text=True, timeout=25)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"canopy: could not check {slug}'s env against its vault ({type(e).__name__})")
        return
    last = ((r.stderr or "").strip().splitlines() or [""])[-1][:300]
    if r.returncode != 0:
        print(f"canopy: {slug}'s ~/.{slug}/.env may be stale — {last or 'cred sync failed'}")
    elif "re-injected from canopy-web" in last:
        print(f"canopy: {slug}'s credentials changed on canopy-web — re-wrote ~/.{slug}/.env")


def google_login(canopy: str, slug: str) -> None:
    """`canopy agent google-login --if-needed` inside a person's LOCAL turn: the agent's
    Google login (gog's OAuth client file, its token, the account mapping) put on this
    computer the first time — canopy-web hands a licence holder the agent's login only
    during their own turn, so `canopy agent use` cannot do it beforehand. Local files only
    once it is in place (no gog call, no network). Silent when present; one line otherwise.

    2026-10-28, failure investigation 17, turn c10c9748: a licensee's first ace turn on a
    Windows laptop died on `gog gmail search` — "OAuth client credentials missing" — because
    nothing on the licence path ever wrote gog's client file or imported ace's login."""
    try:
        r = subprocess.run([canopy, "agent", "google-login", "--slug", slug, "--if-needed",
                            "--no-verify"], capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"canopy: could not set up {slug}'s Google login here ({type(e).__name__})")
        return
    lines = [ln for ln in ((r.stdout or "") + (r.stderr or "")).strip().splitlines() if ln.strip()]
    last = (lines[-1] if lines else "")[:300]
    if r.returncode != 0:
        print(f"canopy: {slug}'s Google login is not set up on this computer — "
              f"{last or 'google-login failed'}")
    elif last and "present here" not in last and "canopy-hosted" not in last:
        print(f"canopy: {last}")


def _hook_input() -> dict:
    """SessionStart's stdin (`session_id`, `cwd`, `source`, …), or {}."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        data = json.loads(sys.stdin.read() or "{}")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


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
            if not confined:
                google_login(canopy, slug)
    if canopy and not confined and not on_runner:
        stage_delegated(canopy, slug)
    if canopy and not confined:
        sync_env(canopy, slug)
    return 0


if __name__ == "__main__":
    sys.exit(main())
