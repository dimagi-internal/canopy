"""`canopy closeout {record|show}` — record an incident-style turn's closeout facts; print the
status line its verdict allows. The verdict is code (`orchestrator.closeout`), never typed.
"""
from __future__ import annotations

import json
import os
import sys

import click

from orchestrator import closeout as co

SESSION_ENVS = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")


def _session_id(given: str | None) -> str:
    sid = (given or "").strip() or next(
        (os.environ[e].strip() for e in SESSION_ENVS if os.environ.get(e, "").strip()), "")
    if not sid:
        raise click.ClickException(
            "no session id: pass --session-id (the Claude session id; Claude Code exports it "
            "to tools as $CLAUDE_CODE_SESSION_ID)")
    return sid


def _turn_id(given: str | None, sid: str) -> str:
    if given:
        return given.strip()
    if os.environ.get("CANOPY_TURN_ID", "").strip():
        return os.environ["CANOPY_TURN_ID"].strip()
    try:  # the caller_context hook records the turn per session
        with open(os.path.expanduser(f"~/.canopy/caller/presence/{sid}.json"), encoding="utf-8") as fh:
            return str(json.load(fh).get("turn_id") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def _load(path: str) -> dict:
    try:
        raw = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
        rec = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise click.ClickException(f"closeout record {path}: {exc}") from exc
    if not isinstance(rec, dict):
        raise click.ClickException("closeout record must be a JSON object")
    return rec


@click.group(name="closeout")
def closeout_group():
    """An incident-style turn's closeout verdict (CLOSED | MONITORING | NEEDS_YOU | INCOMPLETE)."""


@closeout_group.command("record")
@click.option("--file", "path", required=True, help="the closeout record JSON ('-' = stdin)")
@click.option("--step", "steps", multiple=True, metavar="NAME=EXIT",
              help="append a close-out step and its exit code (repeatable)")
@click.option("--improvement-policy", "policy_path", default=None, metavar="FILE",
              help="the agent's improvement policy JSON ({required, prose_only:[{repo, paths}]}); "
                   "opts this record into the merged-PR improvement rule")
@click.option("--session-id", default=None, help="default: $CLAUDE_CODE_SESSION_ID")
@click.option("--turn-id", default=None, help="default: $CANOPY_TURN_ID / the caller record")
@click.option("--dry-run", is_flag=True, help="compute and print; store nothing")
@click.option("--json", "as_json", is_flag=True, help="print the stored document as JSON")
def record_cmd(path, steps, policy_path, session_id, turn_id, dry_run, as_json):
    """Compute the verdict from the record, store it for the Stop hook, print the status line.

    An improvement PR in the record is checked with `gh pr view` (its exit code is a step).
    Exits 3 on INCOMPLETE (the turn must keep working or record owners), 0 otherwise.
    End the turn on the LAST line this prints, verbatim."""
    rec = _load(path)
    if steps:
        rec["closeout_steps"] = list(rec.get("closeout_steps") or [])
        for s in steps:
            name, _, code = s.rpartition("=")
            try:
                rec["closeout_steps"].append({"name": name or s, "exit_code": int(code)})
            except ValueError:
                rec["closeout_steps"].append({"name": s, "exit_code": None})
    if policy_path:
        policy = _load(policy_path)
        rec["improvement_policy"] = policy.get("improvement", policy)
    rec = co.check_improvement(rec)  # verifies the improvement PR with gh (dry runs too)
    if dry_run:
        doc = {"record": rec, **co.compute(rec).as_dict()}
    else:
        sid = _session_id(session_id)
        doc = co.write(rec, sid, turn_id=_turn_id(turn_id, sid))
    if as_json:
        click.echo(json.dumps(doc, indent=1, ensure_ascii=False))
    else:
        for p in doc["problems"]:
            click.echo(f"closeout: {p}", err=True)
        click.echo(doc["status_line"])
    sys.exit(3 if doc["verdict"] == co.INCOMPLETE else 0)


@closeout_group.command("show")
@click.option("--session-id", default=None)
@click.option("--json", "as_json", is_flag=True)
def show_cmd(session_id, as_json):
    """The latest verdict recorded for this session."""
    doc = co.read(_session_id(session_id))
    if not doc:
        raise click.ClickException("no closeout recorded for this session")
    click.echo(json.dumps(doc, indent=1, ensure_ascii=False) if as_json else doc.get("status_line", ""))
