"""`canopy project …` — send a one-shot coding session at a REPO.

The sibling of `canopy agent dispatch`, deliberately a separate command rather than
a `--project` flag on that one. `canopy agent dispatch --project connect-labs` reads
as a contradiction at the shell, and the two targets differ in more than a flag: a
project turn resolves its tenant differently (workspace, required), preflights
differently (the runner must REPORT the repo), and writes no board record at all.
Overloading one command would make most of its options conditional on which target
you picked, which is exactly the shape that hides the sharp edges again.

See `orchestrator.project_dispatch` for why each of those three differences exists.
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import click

from orchestrator.agent_client import CanopyError

RUNNERS_PATH = "/api/harness/runners/"
WORKSPACES_PATH = "/api/workspaces/"


def _emit(obj):
    click.echo(json.dumps(obj, indent=2))


def _runners_path(workspace: str = "") -> str:
    """Where to read the fleet from.

    With a workspace, the TENANT route — the same tenant the turn is enqueued into.
    The flat route returns the union of every workspace the caller belongs to
    (canopy-web #509 scoped the read by tenant rather than by who paired what), so
    preflighting on it and then POSTing to one workspace can pass on workspace A's
    runners and enqueue into workspace B, where nothing serves the repo. That is
    #428's queue-forever failure reached through a new door.
    """
    workspace = (workspace or "").strip()
    return f"/api/w/{workspace}/harness/runners/" if workspace else RUNNERS_PATH


def _fetch_runners(workspace: str = ""):
    from orchestrator import canopy_web
    rows = canopy_web.call("GET", _runners_path(workspace)) or []
    if isinstance(rows, dict):
        rows = rows.get("items") or rows.get("results") or []
    return rows


def _my_workspace_slugs():
    from orchestrator import canopy_web
    try:
        rows = canopy_web.call("GET", WORKSPACES_PATH) or []
    except (CanopyError, RuntimeError):
        return []
    if isinstance(rows, dict):
        rows = rows.get("items") or rows.get("results") or []
    return [str(r.get("slug") or "") for r in rows if r.get("slug")]


@click.group("project")
def project():
    """Drive work at a repo (as opposed to at an agent)."""


@project.command("dispatch")
@click.argument("project_name")
@click.option("--workspace", default="",
              help="The turn's tenant. Required unless you belong to exactly one "
                   "workspace or CANOPY_WEB_WORKSPACE is set — a project turn has no "
                   "agent to derive tenancy from.")
@click.option("--prompt", default="", help="The brief the session receives.")
@click.option("--prompt-file", type=click.Path(exists=True, dir_okay=False), default=None,
              help="Read the brief from a file (for briefs too long to quote on a shell line).")
@click.option("--title", default="",
              help="Short label for the work — only feeds the idempotency key "
                   "(there is no project board to title). Defaults to the prompt's head.")
@click.option("--idempotency-key", default=None,
              help="Override the derived (project, title, day) key — pass a fresh one "
                   "to deliberately re-dispatch the same work.")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def project_dispatch_cmd(project_name, workspace, prompt, prompt_file, title,
                         idempotency_key, as_json):
    """Trigger a runner session in a repo — `canopy project dispatch connect-labs`.

    The one-shot counterpart to a schedule, aimed at a codebase rather than at a
    fleet agent. The runner claims the turn and spawns a visible emdash session in
    that repo, which you can watch and interrupt.

    Refuses up front if no live runner reports the project: such a turn is accepted
    with a 201 and then never claimed by anything, which looks exactly like nothing
    happening. The preflight is not optional and there is no way to declare the repo
    from here — canopy-web reports `capabilities.projects` from the box on every
    heartbeat, so you make a repo routable by opening it in emdash on that runner
    (or setting `RUNNER_PROJECTS`), and the refusal tells you which machine.

    Writes NO board task — a repo has no agent board, and inventing one would be a
    record nothing reads.

    Reports the result as LAUNCHED (unverified): a harness turn flips to `done`
    within seconds carrying "created session '<name>'", which is the runner finishing,
    not the work succeeding. Verify with `canopy project turns <project>`.
    """
    from orchestrator import canopy_web
    from orchestrator.agent_dispatch import DispatchError, summarize_turn
    from orchestrator.project_dispatch import (
        blocked_message,
        build_project_turn_payload,
        classify_runners,
        derive_project_idempotency_key,
        project_turns_path,
        resolve_workspace_choice,
        unknown_message,
    )

    if prompt_file:
        prompt = Path(prompt_file).read_text()

    warnings: list[str] = []
    try:
        ws = resolve_workspace_choice(
            workspace,
            canopy_web.resolve_workspace(None) or "",
            _my_workspace_slugs,      # lazy — not fetched when the answer is pinned
        )

        # Unconditional. The escape hatch (`--no-preflight`) existed because this
        # answer could not be trusted: the fleet was invisible to non-pairers (#509)
        # and the capability list was hand-typed (#513). Both are fixed server-side,
        # so a refusal here is a fact about the tenant — and routing around it is
        # precisely what left a turn queued forever in #428.
        # Read the fleet of the tenant this turn is going INTO, not the union of
        # every workspace the caller belongs to — see `_runners_path`.
        classified = classify_runners(_fetch_runners(ws), project_name,
                                      tenant_scoped=True)
        if classified["blocked"]:
            raise click.ClickException(blocked_message(classified, ws))
        if classified["unknown"]:
            warnings.append(unknown_message(project_name))
        if not classified["serving"] and classified["degraded"]:
            names = ", ".join(str(r.get("name") or "") for r in classified["degraded"])
            notes = "; ".join(
                n for n in (str(r.get("ready_note") or "").strip()
                            for r in classified["degraded"]) if n
            )
            warnings.append(
                f"the only runner(s) reporting '{project_name}' ({names}) report "
                f"NOT READY{' — ' + notes if notes else ''}. The turn will queue "
                "until one recovers rather than starting now."
            )

        day = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
        key = idempotency_key or derive_project_idempotency_key(
            project_name, title or prompt[:80], day)
        payload = build_project_turn_payload(project_name, prompt=prompt,
                                             idempotency_key=key)
        turn = canopy_web.call("POST", project_turns_path(ws), payload)
    except DispatchError as e:
        raise click.ClickException(str(e))
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))

    summary = summarize_turn(turn)
    if as_json:
        _emit({"project": project_name, "workspace": ws, "idempotency_key": key,
               "warnings": warnings, "turn": summary})
        return

    click.echo(f"Project:   {project_name}")
    click.echo(f"Workspace: {ws}")
    click.echo(f"Turn:      {summary['id']}  →  {summary['headline']}")
    for w in warnings:
        click.echo(f"\nWARNING: {w}")
    click.echo("")
    click.echo("This is a LAUNCH, not a result — the session may not have read the brief yet.")
    click.echo(f"  verify:  canopy project turns {project_name}")


@project.command("runners")
@click.argument("project_name", required=False, default="")
@click.option("--workspace", default="",
              help="Read one tenant's fleet — the same scope a dispatch into that "
                   "workspace preflights on. Without it you see the union of every "
                   "workspace you belong to, which is not what any single dispatch "
                   "is judged against.")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def project_runners_cmd(project_name, workspace, as_json):
    """Which runners can serve which repos — the answer to "why did nothing happen?".

    With a project name, splits the fleet the way a dispatch preflight does: who
    would claim it now, who would once they recover, and which machines would need
    the repo opened on them.

    Ownership is shown on rows you may not mutate. Since canopy-web #509 the list is
    scoped by tenant rather than by who paired what, so it now contains runners you
    can see and cannot touch — printing them unmarked would imply otherwise, and the
    remedy for those is a different person's action.
    """
    from orchestrator.project_dispatch import (
        can_manage, classify_runners, declared_projects,
    )

    try:
        runners = _fetch_runners(workspace)
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))

    scoped = bool((workspace or "").strip())
    where = f" in workspace '{workspace}'" if scoped else ""

    if not project_name:
        if as_json:
            _emit([{"name": r.get("name"), "status": r.get("status"),
                    "ready": r.get("ready"), "can_manage": can_manage(r),
                    "owner": r.get("paired_by_email"),
                    "projects": declared_projects(r)}
                   for r in runners])
            return
        if not runners:
            click.echo(f"no runners{where}."
                       if scoped else
                       "no runners visible — you have no workspace memberships to "
                       "read a fleet from.")
            return
        for r in runners:
            flag = "" if r.get("ready", True) else "  (not ready)"
            owner = str(r.get("paired_by_email") or "").strip()
            mine = "" if can_manage(r) else f"  (owned by {owner or 'someone else'})"
            click.echo(f"{str(r.get('name')):<20} {str(r.get('status')):<8}{flag}{mine}")
            click.echo(f"    projects: {', '.join(declared_projects(r)) or '(none)'}")
        return

    c = classify_runners(runners, project_name, tenant_scoped=scoped)
    if as_json:
        _emit({k: [r.get("name") for r in v] if isinstance(v, list) else v
               for k, v in c.items()})
        return
    click.echo(f"project '{project_name}'{where}:")
    for bucket, label in (("serving", "would claim now"),
                          ("degraded", "reports it but NOT READY"),
                          ("unreported_yours", "yours, not reporting it"),
                          ("unreported_theirs", "someone else's, not reporting it"),
                          ("offline", "not live")):
        names = ", ".join(str(r.get("name") or "") for r in c[bucket])
        click.echo(f"  {label:<34} {names or '—'}")
    if c["unknown"]:
        # Must match what the preflight concludes, or one command calls the dispatch
        # impossible while the other one runs it.
        click.echo("\nUNKNOWN: you can see no runners at all, and have no workspace "
                   "memberships to read a fleet from — nothing here says whether one "
                   "serves this project.\nA dispatch will enqueue with a warning; "
                   f"verify with: canopy project turns {project_name}")
    elif c["blocked"]:
        click.echo(f"\nBLOCKED: a dispatch{where} would queue forever.")
        if c["unreported_yours"]:
            names = ", ".join(str(r.get("name") or "") for r in c["unreported_yours"])
            click.echo(f"Fix on the runner ({names}): open '{project_name}' as a "
                       "project in emdash there, or add it to RUNNER_PROJECTS. "
                       "The list refreshes on the next heartbeat.")
        elif c["unreported_theirs"]:
            click.echo("The live runners here are not yours — ask their owner to open "
                       "the repo on one, or pair your own.")
        elif not runners and scoped:
            # Nothing to name a remedy on: the tenant itself is empty. Usually the
            # workspace is wrong rather than the fleet missing, so say both.
            click.echo(f"Workspace '{workspace}' has no runners at all — either you "
                       "meant a different one, or none is paired here yet.")
        elif not scoped:
            click.echo("Re-check per workspace: "
                       f"canopy project runners {project_name} --workspace <ws>")


@project.command("turns")
@click.argument("project_name")
@click.option("--limit", default=10, type=int)
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def project_turns_cmd(project_name, limit, as_json):
    """Recent harness turns for a repo — how you check what a dispatch actually did.

    Filtered client-side: the harness's turn list takes an `agent` filter and has no
    `project` one, so asking the server would silently return every turn.

    Statuses read through the same honest lens as `dispatch`: a `done` launch turn is
    `launched`, never `complete`.
    """
    from orchestrator import canopy_web
    from orchestrator.agent_dispatch import summarize_turn

    try:
        rows = canopy_web.call("GET", "/api/harness/turns/?limit=200") or []
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))
    if isinstance(rows, dict):
        rows = rows.get("items") or rows.get("results") or []
    rows = [t for t in rows if str(t.get("project") or "") == project_name][:limit]

    if as_json:
        _emit([{**summarize_turn(t), "created_at": t.get("created_at"),
                "workspace_slug": t.get("workspace_slug"), "prompt": t.get("prompt")}
               for t in rows])
        return
    if not rows:
        click.echo(f"no harness turns for project {project_name}")
        return
    for t in rows:
        s = summarize_turn(t)
        click.echo(f"{str(t.get('created_at'))[:16]}  {s['state']:<9} {s['id']}")
        click.echo(f"    {s['headline']}")
        first = (str(t.get("prompt") or "").strip().splitlines() or [""])[0]
        if first:
            click.echo(f"    prompt: {first[:110]}")
