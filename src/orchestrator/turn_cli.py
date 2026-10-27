"""`canopy turn abort` — end this turn FAILED because it cannot do its work.

2026-10-27, ACE turn 599fad3b: its first Drive call failed on credentials, and the turn
emailed the outside person who asked that "this runner has no logins", marked her thread
read and finished `done`. Nothing alarmed. Jonathan: "this should be a clear error generated
by Canopy that causes the turn to not even get finished and creates an error and alarm to
resolve."

    abort → POST /api/harness/turns/<id>/abort {reason, tool}. canopy-web ends the turn FAILED,
            which opens (or counts) its failure investigation, raises the alert and starts the
            debugger's turn, and tells a box still running the session to stop
            (canopy-web apps/harness/turn_abort.py).

It also LOCKS the turn on this machine (`~/.canopy/turn-abort/<turn_id>.json`): the canopy
plugin's `credential_guard` then refuses every further tool call in that turn, so nothing
reaches a counterpart and the triggering thread stays unread for the re-run. The lock is
written even when canopy-web cannot be reached — a turn that cannot do its work must not go
on working.

The canopy plugin's `credential_guard` hook runs this by itself when a tool fails on
credentials; an agent runs it when it recognises a failure the hook missed. Exit 0 = the turn
is failed on canopy-web; 3 = only locked locally (say so in the one line you end on).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import click

from orchestrator import canopy_web

#: Read at call time (tests move it). The hook keeps the same path (credential_guard.py).
LOCK_DIR = Path.home() / ".canopy" / "turn-abort"
NOT_FILED = 3


def lock_path(turn_id: str) -> Path:
    return LOCK_DIR / f"{turn_id}.json"


def write_lock(turn_id: str, *, reason: str, tool: str = "", filed: bool) -> None:
    """Best-effort, atomic, 0600."""
    path = lock_path(turn_id)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"turn_id": turn_id, "reason": reason, "tool": tool, "filed": filed,
                       "at": time.time()}, fh)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def abort_turn(turn_id: str, *, reason: str, tool: str = "") -> dict:
    """File the abort on canopy-web. Raises `canopy_web.CanopyError` on a refusal."""
    return canopy_web.call("POST", f"/api/harness/turns/{turn_id}/abort",
                           {"reason": reason, "tool": tool})


def current_turn_id() -> str:
    from orchestrator.provenance import resolve_parent

    return resolve_parent().get("turn_id") or ""


@click.group("turn")
def turn_group():
    """This turn, from inside it."""


@turn_group.command("abort")
@click.option("--reason", required=True,
              help="What failed, in a sentence. The tool's own error is the best evidence.")
@click.option("--tool", default="", help="The tool that failed.")
@click.option("--turn", "turn_id", default="",
              help="Default: the turn this session runs for (env, caller envelope, task record).")
def abort_cmd(reason, tool, turn_id):
    """End this turn FAILED and raise it — never report a turn that could not work as done."""
    turn_id = turn_id or current_turn_id()
    if not turn_id:
        raise click.ClickException(
            "no turn to abort: this session was not started by canopy (no CANOPY_TURN_ID, "
            "caller envelope or task record). Nothing to fail; tell the person in the session.")
    try:
        turn = abort_turn(turn_id, reason=reason, tool=tool)
        filed, error = True, ""
    except (canopy_web.CanopyError, RuntimeError, OSError) as e:
        turn, filed, error = {}, False, str(e)
    write_lock(turn_id, reason=reason, tool=tool, filed=filed)
    out = {"turn_id": turn_id, "filed": filed, "status": turn.get("status") if filed else None,
           "next": ("Stop now. Send nothing and mark nothing read; end your reply with one line "
                    "saying the turn was aborted and why. Canopy has raised it with the owner."
                    if filed else
                    "canopy-web did not take the abort, so the turn is only locked here. Stop "
                    "now, send nothing, and end your reply with one line: \"turn aborted, NOT "
                    f"filed on canopy-web: {reason}\".")}
    if error:
        out["error"] = error
    click.echo(json.dumps(out, indent=2))
    if not filed:
        sys.exit(NOT_FILED)
