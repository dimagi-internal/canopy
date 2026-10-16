"""`canopy slack ingest` — read a whole Slack channel, with a person's permission.

canopy-web does the reading (canopy-web#1533): an agent ASKS, canopy posts an
Approve / Decline message in the channel, and only after a channel member who
administers the agent's workspace (or created the channel) approves does canopy
page the channel's history into a transcript. The agent never holds a Slack token
and never calls Slack — the fleet baseline denies that (`gating-baseline.json`).

    canopy slack ingest request --agent SLUG --channel C0123ABCD|#name --purpose "…" [--follow]
    canopy slack ingest status ID
    canopy slack ingest wait ID [--timeout 3600] [--interval 30]
    canopy slack ingest transcript ID [--out FILE]
    canopy slack ingest revoke ID
    canopy slack ingest list [--agent SLUG] [--status STATUS]
    canopy slack ingest allow --workspace WS --agent SLUG --channel C…|#name [--follow] [--note "…"]
    canopy slack ingest allowed --workspace WS
    canopy slack ingest disallow --workspace WS (--id ID | --agent SLUG --channel C…|#name)

`allow` is a workspace OWNER's explicit, standing permission for one agent to
read one channel: that agent's `request` then reads at once (approved by the
owner) and canopy posts a notice with Revoke in the channel instead of asking.
An owner runs it as themselves — the agent's own login can never allow itself.

The judgment — when to ask, what to say the read is for, what to do with a
decline, how to digest — is the `slack-channel-ingest` skill. This module only
does the parts that are easy to get silently wrong: the wait (a pending ingest is
a person who has not answered yet, never a failure) and the exit codes that tell
the skill which of those it is.

Identity follows `canopy_web.resolve_token()` — run it from the agent's repo so the
agent's own PAT is used: the transcript is readable by the agent's login ONLY.
"""
from __future__ import annotations

import time
from pathlib import Path

import click

from orchestrator import canopy_web

BASE = "/api/slack-ingests/"
#: Exit codes for `wait` (and `status`): what the skill branches on.
EXIT_READY, EXIT_REFUSED, EXIT_FAILED, EXIT_WAITING = 0, 4, 5, 6
#: Still moving: a person has not answered, or canopy is still reading.
OPEN = ("pending", "reading")


def _call(method: str, path: str, body=None):
    try:
        return canopy_web.call(method, path, body)
    except RuntimeError as exc:                 # CanopyError, or no PAT at all
        msg = str(exc)
        if " -> 404" in msg and method == "POST" and path == BASE:
            raise click.ClickException(
                "canopy-web has no /api/slack-ingests/ route (canopy-web#1533 is not deployed "
                "there yet), or the agent is not in a workspace you can see. Nothing was asked.") from None
        raise click.ClickException(msg) from None


def _line(i: dict) -> str:
    chan = f"#{i['channel_name']}" if i.get("channel_name") else i.get("channel", "")
    tail = f" — {i['error']}" if i.get("error") else ""
    return (f"{i['id']}  {i['status']:<8}  {i['agent']} <- {chan}  "
            f"{i.get('messages', 0)} msg{'  follow' if i.get('follow') else ''}{tail}")


def _exit_for(status: str) -> int:
    if status == "ready":
        return EXIT_READY
    if status in ("declined", "revoked"):
        return EXIT_REFUSED
    if status == "failed":
        return EXIT_FAILED
    return EXIT_WAITING


@click.group("slack")
def slack_group():
    """Slack, through canopy-web — the agent never holds a Slack token."""


@slack_group.group("ingest")
def ingest_group():
    """Read a whole channel — only after someone in it approves."""


@ingest_group.command("request")
@click.option("--agent", "agent", required=True, help="The agent's slug (the one that will read it).")
@click.option("--channel", required=True, help="The channel: its ID (C…/G…) or #name.")
@click.option("--purpose", required=True, help="Why — shown to the people asked to approve.")
@click.option("--follow", is_flag=True, help="Keep reading new messages until revoked.")
def request_cmd(agent: str, channel: str, purpose: str, follow: bool):
    """Ask. Posts ONE approval message in the channel; reads nothing."""
    i = _call("POST", BASE, {"agent": agent, "channel": channel.strip(), "purpose": purpose.strip(),
                             "follow": follow})
    click.echo(_line(i))
    where = i.get("channel_name") or i["channel"]
    if i["status"] == "reading":
        click.echo(f"#{where} is allow-listed for {agent}: canopy is reading it now and posted a notice there. "
                   f"Next: canopy slack ingest wait {i['id']}")
        return
    click.echo(f"asked in {where}: nothing is read until someone there "
               f"approves. Next: canopy slack ingest wait {i['id']}")


@ingest_group.command("status")
@click.argument("ingest_id")
def status_cmd(ingest_id: str):
    """One ingest's status. Exit 0 ready, 4 declined/revoked, 5 failed, 6 still open."""
    i = _call("GET", f"{BASE}{ingest_id}")
    click.echo(_line(i))
    raise SystemExit(_exit_for(i["status"]))


@ingest_group.command("wait")
@click.argument("ingest_id")
@click.option("--timeout", default=3600, show_default=True, help="Seconds to wait before giving up.")
@click.option("--interval", default=30, show_default=True, help="Seconds between checks.")
def wait_cmd(ingest_id: str, timeout: int, interval: int):
    """Wait for a person to answer and canopy to finish reading.

    Exit 0 ready, 4 declined/revoked (stop — do not ask again unasked), 5 failed,
    6 timed out still pending/reading (not a refusal: check again later)."""
    deadline = time.monotonic() + max(0, timeout)
    while True:
        i = _call("GET", f"{BASE}{ingest_id}")
        if i["status"] not in OPEN or time.monotonic() >= deadline:
            break
        time.sleep(max(1, interval))
    click.echo(_line(i))
    raise SystemExit(_exit_for(i["status"]))


@ingest_group.command("transcript")
@click.argument("ingest_id")
@click.option("--out", type=click.Path(dir_okay=False, path_type=Path), help="Write the Markdown here.")
def transcript_cmd(ingest_id: str, out: Path | None):
    """The channel as Markdown — readable by the requesting agent's own login only."""
    t = _call("GET", f"{BASE}{ingest_id}/transcript")
    if out is None:
        click.echo(t["markdown"], nl=False)
        return
    out.write_text(t["markdown"], encoding="utf-8")
    click.echo(f"wrote {t['messages']} message(s) ({t['status']}) to {out}")


@ingest_group.command("revoke")
@click.argument("ingest_id")
def revoke_cmd(ingest_id: str):
    """Stop reading and delete the transcript (or withdraw an unanswered request)."""
    click.echo(_line(_call("POST", f"{BASE}{ingest_id}/revoke")))


@ingest_group.command("list")
@click.option("--agent", "agent", default="", help="Only this agent's.")
@click.option("--status", default="", help="pending | reading | ready | declined | revoked | failed")
def list_cmd(agent: str, status: str):
    """The ingests you can see."""
    import urllib.parse

    q = urllib.parse.urlencode({k: v for k, v in (("agent", agent), ("status", status)) if v})
    rows = _call("GET", BASE + (f"?{q}" if q else ""))
    for i in rows or []:
        click.echo(_line(i))
    if not rows:
        click.echo("no channel ingests")


# --- the allow-list: an owner's explicit, standing permission -------------------

def _config_path(workspace: str) -> str:
    return f"/api/slack-config/{workspace}"


def _allowed_line(a: dict) -> str:
    chan = f"#{a['channel_name']}" if a.get("channel_name") else a["channel"]
    note = f" — {a['note']}" if a.get("note") else ""
    return (f"{a['id']}  {a['agent']} <- {chan}  {'read+follow' if a.get('follow') else 'read once'}  "
            f"by {a.get('added_by_email') or '?'}{note}")


def _print_allowed(cfg: dict) -> None:
    rows = (cfg.get("channel_ingest") or {}).get("allowed") or []
    for a in rows:
        click.echo(_allowed_line(a))
    if not rows:
        click.echo("no allow-listed channels")


@ingest_group.command("allow")
@click.option("--workspace", required=True, help="The agent's canopy workspace (you must own it).")
@click.option("--agent", "agent", required=True, help="The agent allowed to read the channel.")
@click.option("--channel", required=True, help="The channel: its ID (C…/G…) or #name.")
@click.option("--follow", is_flag=True, help="Also allow following new messages, not only one read.")
@click.option("--note", default="", help="Why — shown on the settings page and in the log.")
def allow_cmd(workspace: str, agent: str, channel: str, follow: bool, note: str):
    """Allow-list one channel for one agent (workspace owner). Reads nothing by itself."""
    cfg = _call("POST", f"{_config_path(workspace)}/channel-ingest/allowed",
                {"agent": agent, "channel": channel.strip(), "follow": follow, "note": note.strip()})
    _print_allowed(cfg)


@ingest_group.command("allowed")
@click.option("--workspace", required=True, help="The canopy workspace.")
def allowed_cmd(workspace: str):
    """The channels allow-listed in a workspace, per agent."""
    _print_allowed(_call("GET", _config_path(workspace)))


@ingest_group.command("disallow")
@click.option("--workspace", required=True, help="The canopy workspace (you must own it).")
@click.option("--id", "allow_id", default="", help="The entry's id (from `allowed`).")
@click.option("--agent", "agent", default="", help="Or: the agent …")
@click.option("--channel", default="", help="… and the channel (ID or #name).")
def disallow_cmd(workspace: str, allow_id: str, agent: str, channel: str):
    """Remove an allow-list entry. Revokes the reads it allowed and deletes their transcripts."""
    if not allow_id:
        if not (agent and channel):
            raise click.UsageError("give --id, or both --agent and --channel")
        want = channel.strip().lstrip("#").lower()
        rows = (_call("GET", _config_path(workspace)).get("channel_ingest") or {}).get("allowed") or []
        match = [a for a in rows if a["agent"] == agent and want in (a["channel"].lower(), a.get("channel_name", ""))]
        if not match:
            raise click.ClickException(f"{channel} is not allow-listed for {agent} in {workspace}")
        allow_id = match[0]["id"]
    _print_allowed(_call("DELETE", f"{_config_path(workspace)}/channel-ingest/allowed/{allow_id}"))
