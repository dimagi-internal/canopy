"""`canopy runner …` — park a runner, or bring it back.

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

import click

from orchestrator.agent_client import CanopyError

RUNNERS_PATH = "/api/harness/runners/"


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


@click.group("runner")
def runner():
    """Park a runner, or bring it back."""


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
    try:
        out = canopy_web.call("POST", f"{RUNNERS_PATH}{r['id']}/unpause") or {}
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))
    if as_json:
        click.echo(json.dumps(out, indent=2))
        return
    click.echo(f"unpaused {out.get('name') or r.get('name')} — "
               f"now {out.get('status') or 'live'}")


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
