"""`canopy runner …` — where work runs: park a box, bring it back, or move a
live session from one box to another.

`transfer` shares this group rather than getting its own because it answers the
same question pause/unpause do — WHICH box — and reuses the whole of this
module's resolution: name-or-id lookup, the ownership refusal below, and the
capabilities read that says whether a target could claim a session turn at all.
(It is deliberately not under `canopy sessions`, which means local transcript
LOGS — a different noun that happens to share a word.)



The remote half of the runner's local `~/.canopy/PAUSED` sentinel. Both set the
SAME state (canopy-web `Runner.paused`): the local file is a control surface the
daemon pushes up on change, this is the same command over HTTP. So there is one
value, and reaching it no longer requires a shell on the box — which is the whole
point, because the case that needs it most is a box you cannot log into.

That case is Jonathan's two macOS accounts (`Runner.host`: "Jonathan runs the fleet
under two accounts (token-limit failover)"). When one hits its session limit the
work moves to the other, and the limited account's runner should stop firing its own
schedules and inbox polls — but `~/.canopy` over there is owned by that account, so
the other one gets EPERM.

NOT `retire`, which is what people reached for before this existed: retire deletes
the runner's RunnerAssignment rows, `unretire` explicitly does not restore them, and
it 404s the daemon's own heartbeat and claim calls. It cost jj-mbp-cdp ten sessions
on 2026-07-25. Retire is a decommission; this is a park.
"""
from __future__ import annotations

import json
from pathlib import Path

import click

from orchestrator.agent_client import CanopyError
from orchestrator.project_dispatch import can_manage

RUNNERS_PATH = "/api/harness/runners/"


def _refuse_if_not_ours(r: dict, verb: str) -> None:
    """Say WHY, instead of letting the server's bare 404 be the answer.

    Pausing a runner is an act-on operation, gated on OWNERSHIP (`paired_by`),
    while listing one is only gated on tenancy — so `canopy runner list` shows a
    box that `pause` then 404s on. canopy-web's `_runner_visibility_q` names that
    asymmetry as deliberate and says the client is supposed to carry the
    invariant explicitly, via `can_manage`, rather than let someone "discover
    ownership from a bare 404 on an action it was told to try". This is that.

    The common cause is IDENTITY, not permissions, and it is invisible without
    this: `canopy_web.resolve_token` prefers an agent's own PAT over the
    operator's workbench-token, so running from inside an agent repo acts AS that
    agent — and an agent does not own the operator's laptop runner. Same command,
    run from anywhere else, works.
    """
    if can_manage(r):
        return
    owner = str(r.get("paired_by_email") or "").strip() or "someone else"
    raise click.ClickException(
        f"cannot {verb} '{r.get('name')}' — it is owned by {owner}, and this "
        f"identity is not that.\n"
        f"  Pausing acts ON a runner, so it needs ownership; listing only needs "
        f"tenancy, which is why the runner is visible but not actionable.\n"
        f"  If you ARE {owner}: you are probably running inside an agent repo, so "
        f"the CLI is acting as that agent (canopy_web.resolve_token prefers the "
        f"agent's own PAT). Run this from outside the repo, or set CANOPY_WEB_PAT "
        f"to your own token.\n"
        f"  Otherwise this is {owner}'s box to park — ask them, or use the local "
        f"sentinel there (`touch ~/.canopy/PAUSED`)."
    )


def _fetch(workspace: str = ""):
    from orchestrator import canopy_web
    ws = (workspace or "").strip()
    path = f"/api/w/{ws}/harness/runners/" if ws else RUNNERS_PATH
    rows = canopy_web.call("GET", path) or []
    if isinstance(rows, dict):
        rows = rows.get("items") or rows.get("results") or []
    return rows


def _resolve(name_or_id: str, workspace: str = "") -> dict:
    """Accept a NAME as well as a uuid — nobody reads uuids off a fleet listing, and
    a pause is usually typed in a hurry."""
    needle = (name_or_id or "").strip()
    if not needle:
        raise click.ClickException("name a runner (see `canopy runner list`)")
    rows = _fetch(workspace)
    exact = [r for r in rows
             if str(r.get("id")) == needle or str(r.get("name") or "") == needle]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise click.ClickException(
            f"'{needle}' matches {len(exact)} runners; use the id: "
            + ", ".join(f"{r.get('name')}={r.get('id')}" for r in exact))
    names = ", ".join(str(r.get("name") or "") for r in rows) or "(none visible)"
    raise click.ClickException(f"no runner named '{needle}'. Visible: {names}")


SESSIONS_PATH = "/api/canopy-sessions/"


def _fetch_sessions(workspace: str = ""):
    from orchestrator import canopy_web
    rows = canopy_web.call("GET", SESSIONS_PATH, workspace=workspace or None) or []
    if isinstance(rows, dict):
        rows = rows.get("items") or rows.get("results") or []
    return rows


def _resolve_session(needle: str, workspace: str = "") -> dict:
    """Accept a uuid, a title, or a unique substring of either.

    Nobody has a session uuid to hand — you see one in the web UI or in
    `canopy runner list`-adjacent output and then want to move it. An ambiguous
    substring lists the candidates rather than picking one, because picking wrong
    here moves the WRONG live conversation onto another box.
    """
    needle = (needle or "").strip()
    if not needle:
        raise click.ClickException("name a session (uuid, or part of its title)")
    rows = _fetch_sessions(workspace)
    exact = [r for r in rows if str(r.get("id")) == needle
             or str(r.get("title") or "") == needle]
    if len(exact) == 1:
        return exact[0]
    low = needle.lower()
    fuzzy = [r for r in rows
             if low in str(r.get("title") or "").lower()
             or low in str(r.get("id") or "").lower()]
    if len(fuzzy) == 1:
        return fuzzy[0]
    if fuzzy:
        raise click.ClickException(
            f"'{needle}' matches {len(fuzzy)} sessions; use the id:\n" + "\n".join(
                f"  {r.get('id')}  {r.get('runner_name') or '(unbound)':<20} "
                f"{str(r.get('title') or '')[:48]}" for r in fuzzy))
    raise click.ClickException(
        f"no active session matching '{needle}'. Visible: "
        + (", ".join(str(r.get("title") or r.get("id")) for r in rows[:12])
           or "(none)"))


# What the CLI can honestly say without reading either box's disk. Deliberately
# NOT a git-state brief: for the cloud->laptop direction the source worktree is on
# another machine and unreachable, and for the two-account direction ada's
# `user-switch` already owns that procedure (it reads the sibling worktree, carries
# the unpushed commits and the uncommitted diff, and knows the four git-state
# shapes). Duplicating a thinner version of it here would be a stale copy of
# something another repo owns. Pass a real one with --brief/--brief-file.
_DEFAULT_BRIEF = """\
No handoff brief was supplied, so nothing about the prior worktree came with this
transfer. Before continuing: work out where the work stands from the thread above,
the repo's git log, and any open PRs — and say what you find before changing
anything. If something looks half-finished, ask rather than guess.
"""


@click.group("runner")
def runner():
    """The fleet — park a box, bring it back, or move a session between boxes."""


@runner.command("pause")
@click.argument("name_or_id")
@click.option("--note", default="",
              help="why it is parked — shown to whoever finds it idle later")
@click.option("--workspace", default="", help="read the fleet of ONE tenant")
@click.option("--json-output", "as_json", is_flag=True)
def pause_cmd(name_or_id, note, workspace, as_json):
    """Stop routing work to a runner, reversibly.

    Enforced server-side: `live_status` reports `paused` and `claim_next_turn`
    refuses anything not ONLINE, so this binds even against a runner too old to know
    the field exists, with no deploy on that box. It also outranks a PIN — a turn
    pinned to a paused runner stays QUEUED and lands on unpause.

    Stops STARTING work, never finishing it: an executing turn keeps its lease and
    reports completion normally.
    """
    from orchestrator import canopy_web
    r = _resolve(name_or_id, workspace)
    _refuse_if_not_ours(r, "pause")
    try:
        out = canopy_web.call("POST", f"{RUNNERS_PATH}{r['id']}/pause",
                              {"note": note}) or {}
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))
    if as_json:
        click.echo(json.dumps(out, indent=2))
        return
    click.echo(f"paused {out.get('name') or r.get('name')}"
               + (f" — {note}" if note else ""))
    click.echo("It keeps heartbeating (so it reads as alive, not dead) and claims "
               "nothing until unpaused.")
    click.echo(f"  resume:  canopy runner unpause {out.get('name') or r.get('name')}")


@runner.command("unpause")
@click.argument("name_or_id")
@click.option("--workspace", default="", help="read the fleet of ONE tenant")
@click.option("--json-output", "as_json", is_flag=True)
def unpause_cmd(name_or_id, workspace, as_json):
    """Resume routing to a parked runner.

    The exact inverse of `pause` — it clears the flag and nothing else, because
    pause destroyed nothing to restore. (Contrast `unretire`, which cannot undo
    retire's deleted assignment rows and says so.)

    Anything that queued while it was parked becomes claimable at once, so expect a
    burst rather than a trickle.
    """
    from orchestrator import canopy_web
    r = _resolve(name_or_id, workspace)
    _refuse_if_not_ours(r, "unpause")
    try:
        out = canopy_web.call("POST", f"{RUNNERS_PATH}{r['id']}/unpause") or {}
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))
    if as_json:
        click.echo(json.dumps(out, indent=2))
        return
    click.echo(f"unpaused {out.get('name') or r.get('name')} — "
               f"now {out.get('status') or 'live'}")


@runner.command("transfer")
@click.argument("session")
@click.option("--to", "target", required=True, metavar="RUNNER",
              help="the runner to move it onto (name or id)")
@click.option("--brief", default="", help="the handoff the receiving session reads")
@click.option("--brief-file", type=click.Path(exists=True, dir_okay=False),
              help="read the handoff from a file (preferred for anything long)")
@click.option("--stop", is_flag=True,
              help="cancel the source box's in-flight turn first, instead of refusing")
@click.option("--workspace", default="", help="act within ONE tenant")
@click.option("--json-output", "as_json", is_flag=True)
def transfer_cmd(session, target, brief, brief_file, stop, workspace, as_json):
    """Move a live session onto another runner — cloud -> laptop, or between the
    two macOS accounts — carrying its message history across.

    \b
      canopy runner transfer 169212e2 --to jj-mbp-cdp --brief-file handoff.md

    The target opens a FRESH session (it cannot resume another box's claude
    session), so the brief is not decoration — it is the entire context the
    receiving agent gets. Server-side, the transfer also opens a new transcript
    epoch so the new box's ordinals land above the inherited history instead of
    deleting it; `index_offset` in the output is that boundary.

    Not the same as `canopy_sessions`' `place`, which re-pins one queued turn and
    leaves the binding on the old box — so the next ship 404s and the next send
    sticks to where you were moving away FROM. Doing it that way by hand on
    2026-09-12 moved execution correctly and silently dropped the session's whole
    pre-transfer history.

    Reports LAUNCHED, never done: a pinned turn lands within seconds, and the
    agent picking the thread up is a separate question from the move succeeding.
    """
    from orchestrator import canopy_web
    if brief_file:
        brief = Path(brief_file).read_text()
    s = _resolve_session(session, workspace)
    r = _resolve(target, workspace)
    _refuse_if_not_ours(r, "transfer a session onto")

    source = str(s.get("runner_name") or "") or "(unbound)"
    if source == str(r.get("name")):
        raise click.ClickException(
            f"session '{s.get('title') or s.get('id')}' is already on "
            f"{r.get('name')} — nothing to move.")
    # A session-incapable target is refused SERVER-side (the pin would otherwise be
    # unclaimable forever), but saying it here costs one dict lookup and names the
    # actual fix instead of a 422.
    if not (r.get("capabilities") or {}).get("sessions"):
        raise click.ClickException(
            f"'{r.get('name')}' is not session-capable (capabilities.sessions is not "
            f"true), so a session turn pinned to it could never be claimed.\n"
            f"  Fix it on that box, or pick a different target "
            f"(`canopy runner list --json-output` shows capabilities).")

    if stop:
        try:
            canopy_web.call("POST", f"{SESSIONS_PATH}{s['id']}/stop",
                            {}, workspace=workspace or None)
        except (CanopyError, RuntimeError) as e:
            raise click.ClickException(f"could not stop the session first: {e}")

    body = {"runner": str(r["id"]), "brief": brief or _DEFAULT_BRIEF}
    try:
        out = canopy_web.call("POST", f"{SESSIONS_PATH}{s['id']}/transfer", body,
                              workspace=workspace or None) or {}
    except (CanopyError, RuntimeError) as e:
        msg = str(e)
        if "409" in msg or "still executing" in msg:
            raise click.ClickException(
                f"{s.get('title') or s.get('id')} has a turn still executing on "
                f"{source}, and a box mid-thought would keep writing into the epoch "
                f"this closes.\n  Re-run with --stop to cancel it first, or wait for "
                f"it to finish.")
        raise click.ClickException(msg)

    if as_json:
        click.echo(json.dumps(out, indent=2))
        return
    click.echo(f"LAUNCHED (unverified) — moved '{s.get('title') or s.get('id')}' "
               f"{out.get('transferred_from') or source} -> {out.get('runner')}")
    click.echo(f"  turn:          {out.get('turn_id')}")
    click.echo(f"  epoch base:    {out.get('index_offset')}  "
               f"(history below this was carried across, not dropped)")
    if not brief:
        click.echo("  brief:         DEFAULT — the receiving session was told to work "
                   "out the state itself.")
        click.echo("                 For a two-account move, ada's `user-switch` "
                   "composes a real one (branch, unpushed commits, uncommitted diff).")
    click.echo(f"\nVerify it landed THERE, not merely that it was accepted:")
    click.echo(f"  canopy runner list                 # {out.get('runner')} online + ready")
    click.echo(f"  # then re-read the session — runner_name should be "
               f"{out.get('runner')} and the thread should have a new reply")


@runner.command("list")
@click.option("--workspace", default="", help="read the fleet of ONE tenant")
@click.option("--json-output", "as_json", is_flag=True)
def list_cmd(workspace, as_json):
    """The fleet and what each box is doing — including who is parked."""
    try:
        rows = _fetch(workspace)
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))
    if as_json:
        click.echo(json.dumps(
            [{"name": r.get("name"), "id": r.get("id"), "status": r.get("status"),
              "ready": r.get("ready"), "paused": r.get("paused"),
              "paused_note": r.get("paused_note"), "host": r.get("host"),
              "can_manage": can_manage(r), "owner": r.get("paired_by_email"),
              "projects": (r.get("capabilities") or {}).get("projects") or []}
             for r in rows], indent=2))
        return
    if not rows:
        click.echo("no runners visible.")
        return
    for r in rows:
        flags = []
        if not r.get("ready", True):
            flags.append("not ready")
        # `status_note == "paused"` is the LEGACY signal: what a runner too old to
        # know about `Runner.paused` emits for its local ~/.canopy/PAUSED sentinel.
        # Read it too, or during the rollout a genuinely parked box lists as plain
        # `online` — which is the exact misreading this whole feature exists to end.
        legacy = str(r.get("status_note") or "").strip().lower() == "paused"
        if r.get("paused") or str(r.get("status")) == "paused" or legacy:
            note = r.get("paused_note") or (r.get("status_note") if legacy else "")
            flags.append("PAUSED" + (f": {note}" if note and note != "paused" else ""))
        tail = ("  [" + ", ".join(flags) + "]") if flags else ""
        click.echo(f"{str(r.get('name')):<22} {str(r.get('status')):<10} "
                   f"{str(r.get('host') or ''):<34}{tail}")


# ── credentials ─────────────────────────────────────────────────────────────
#: The cascade, in the order the runner tries it. Subscriptions first (already
#: paid for), the metered API key last.
_CRED_SLOTS = (
    ("claude_token", "subscription 1", "has_claude_token",
     "Primary Claude subscription. Every turn runs on this until its cap trips."),
    ("claude_token_secondary", "subscription 2", "has_claude_token_secondary",
     "Second subscription. Takes over automatically when #1 hits its weekly cap."),
    ("claude_api_key", "API key", "has_claude_api_key",
     "Last resort — METERED, bills per token. Falling back to it notifies you."),
)


def _mint_setup_token() -> str:
    """Run `claude setup-token` against the operator's terminal, then take the
    result by hidden prompt.

    Deliberately NOT captured from stdout: setup-token is an interactive browser
    login, and capturing its stream to scrape a token out of it breaks the very
    TUI the human has to click through. Letting it own the terminal and then
    asking for the value is duller and works.
    """
    import subprocess

    click.echo("\n  Launching `claude setup-token` — sign in as the account you want, "
               "then copy the token it prints.\n")
    try:
        subprocess.run(["claude", "setup-token"], check=False)
    except FileNotFoundError:
        raise click.ClickException(
            "`claude` is not on PATH here. Mint the token on a machine that has it "
            "(`claude setup-token`) and re-run this with the paste option.")
    return click.prompt("  Paste the token", hide_input=True, default="", show_default=False)


def _prompt_slot(field: str, label: str, blurb: str, already_set: bool) -> str | None:
    """Return a new value for one slot, or None to leave it as-is."""
    state = "already set" if already_set else "not set"
    click.echo(f"\n{label} ({state})\n  {blurb}")
    choices = "m=mint / p=paste / k=keep" if already_set else "m=mint / p=paste / s=skip"
    while True:
        pick = (click.prompt(f"  [{choices}]", default="k" if already_set else "s",
                             show_default=True) or "").strip().lower()[:1]
        if pick in ("k", "s", ""):
            return None
        if pick == "m":
            value = _mint_setup_token()
        elif pick == "p":
            value = click.prompt("  Paste the value", hide_input=True, default="",
                                 show_default=False)
        else:
            click.echo("  pick m, p, or " + ("k" if already_set else "s"))
            continue
        value = (value or "").strip()
        if value:
            return value
        click.echo("  nothing entered — leaving this slot unchanged")
        return None


@runner.command("credential")
@click.argument("name_or_id")
@click.option("--workspace", default="", help="Tenant, when the runner is not in your default one.")
@click.option("--show", is_flag=True, help="Only report which slots are set, and exit.")
def runner_credential(name_or_id, workspace, show):
    """Set a cloud runner's Claude credentials — the failover cascade.

    A subscription's weekly cap stops EVERY agent on the box at once, so a runner
    carries an ordered cascade: subscription 1, then subscription 2, then a
    metered API key. The runner advances on a cap and re-runs the turn; reaching
    the API key notifies you, because it bills per token.

    Values are read by HIDDEN prompt and sent straight to canopy-web (encrypted at
    rest) — they are never echoed, never stored in a file, and never land in your
    shell history. Laptop runners don't use this; they use emdash's ambient auth.
    """
    from orchestrator import canopy_web

    try:
        r = _resolve(name_or_id, workspace)
        _refuse_if_not_ours(r, "set credentials on")
        if str(r.get("kind") or "") != "cloud":
            click.echo(f"note: '{r.get('name')}' is a {r.get('kind')} runner — it uses "
                       f"emdash's ambient Claude auth and ignores this bundle.")
        path = f"/api/harness/runners/{r['id']}/credential"
        # The masked view comes back from a no-op POST (every field defaults to
        # None = unchanged). The GET on this path returns REAL VALUES for the
        # runner to consume, and we are not putting those on an operator's screen
        # just to render a checklist.
        status = canopy_web.call("POST", path, {}) or {}

        click.echo(f"\nrunner: {r.get('name')}  ({r.get('status')}"
                   f"{', paused' if r.get('paused') else ''})")
        if show:
            for field, label, has_key, _ in _CRED_SLOTS:
                click.echo(f"  {'set  ' if status.get(has_key) else 'unset'}  {label}")
            return

        body: dict[str, str] = {}
        for field, label, has_key, blurb in _CRED_SLOTS:
            value = _prompt_slot(field, label, blurb, bool(status.get(has_key)))
            if value is not None:
                body[field] = value

        if not body:
            click.echo("\nnothing changed.")
            return

        result = canopy_web.call("POST", path, body) or {}
        click.echo("\nstored (encrypted). cascade now:")
        for field, label, has_key, _ in _CRED_SLOTS:
            click.echo(f"  {'set  ' if result.get(has_key) else 'unset'}  {label}")
        if not result.get("has_claude_token_secondary") and not result.get("has_claude_api_key"):
            click.echo("\nwarning: only one credential is set — a usage cap will stop every "
                       "agent on this box with nothing to fail over to.")
        click.echo("\nThe runner stages these at start-up, so it picks them up on its next "
                   "restart (the auto-updater bounces it within ~30 min, or restart it now "
                   "with `systemctl restart canopy-runner` on the box).")
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))
