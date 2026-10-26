"""`canopy agent …` — thin CLI over AgentClient for shell-driven agents."""
import json
import os
import sys
from pathlib import Path

import click

from orchestrator.agent_client import AgentClient, catalog_from_repo, CanopyError


def _client(slug, **identity):
    return AgentClient({"slug": slug, **{k: v for k, v in identity.items() if v}})


def _emit(obj):
    click.echo(json.dumps(obj))


@click.group()
def agent():
    """Talk to canopy-web's agent workspace (/api/agents)."""


@agent.command("register")
@click.option("--slug", required=True)
@click.option("--name", default="")
@click.option("--email", default="")
@click.option("--description", default="")
@click.option("--persona", default="")
@click.option("--avatar-url", default="")
@click.option("--workspace", default="",
              help="canopy-web workspace slug to home the agent in (e.g. 'connect'). "
                   "Setting it on an already-registered agent MOVES it, history and all "
                   "(see `canopy agent move --dry-run` to preview a move). Omit to leave "
                   "placement alone — which means a NEW agent lands in the default "
                   "workspace, which any @dimagi.com address may self-join as an "
                   "editor, and an editor can delete an agent.")
@click.option("--repo", "repo", default="",
              help="The agent's GitHub repo (owner/name or its URL) — what its runners "
                   "clone. Omitted: inferred from the git origin when run inside the agent's "
                   "own repo, and sent only while canopy-web records none (never moves one).")
def agent_register(slug, name, email, description, persona, avatar_url, workspace, repo):
    """Upsert agent identity.

    canopy-web's upsert REPLACES every identity field and requires `name`, so on an agent
    that already exists an omitted option is filled from its current record. Without
    that, `register --slug fizzy --workspace strategy` 422'd, and adding `--name` to get
    past it silently blanked the agent's email, description and persona (Shayoni,
    2026-09-29)."""
    given = {"name": name, "email": email, "description": description,
             "persona": persona, "avatar_url": avatar_url}
    try:
        if not all(given.values()):
            try:
                current = _client(slug).get_agent()
            except CanopyError as e:
                if "-> 404:" not in str(e):
                    raise
                current = {}
            given = {k: v or str(current.get(k) or "") for k, v in given.items()}
            if not given["name"]:
                raise click.ClickException(f"agent '{slug}' is not registered yet (or not "
                                           "visible to you) — pass --name to create it")
        c = _client(slug, workspace=workspace, **given)
        c.identity.repo_url = _repo_for(slug, repo, c) or None
        _emit(c.register())
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))


def _repo_for(slug: str, given: str, client) -> str:
    """The repo to record: an explicit --repo (normalised; refused if it is not a GitHub
    repo), else the cwd's git origin when the cwd is THIS agent's repo and canopy-web holds
    no repo for the agent yet. "" = send nothing."""
    from orchestrator import agent_web
    if given:
        url = agent_web.normalize_repo_url(given)
        if not url:
            raise click.ClickException(f"--repo {given!r} is not a GitHub repo "
                                       "(owner/name or https://github.com/owner/name)")
        return url
    here = Path.cwd()
    for d in (here, *here.parents):
        pj = d / ".claude-plugin" / "plugin.json"
        if pj.is_file():
            try:
                if json.loads(pj.read_text(encoding="utf-8")).get("name") != slug:
                    return ""
            except (OSError, ValueError):
                return ""
            url = agent_web.repo_url(d)
            if not url:
                return ""
            try:
                recorded = str(client.get_agent().get("repo_url") or "")
            except CanopyError as e:
                recorded = "" if "-> 404" in str(e) else "?"
            return "" if recorded else url
    return ""


@agent.command("move")
@click.option("--slug", required=True)
@click.option("--to", "workspace", required=True, help="The workspace slug to move the agent to.")
@click.option("--dry-run", is_flag=True,
              help="Change nothing; report what would move, stay, and stop working.")
def agent_move(slug, workspace, dry_run):
    """Move an agent to another workspace, with its history.

    One operation on canopy-web (POST /api/agents/{slug}/move): turns, chat
    sessions, mail, capability grants and agent-scoped rules move together in
    one transaction, or nothing does. Prints the report — per-table `tables`,
    `left_behind`, and `stops_working` (e.g. a mail address on a domain the
    destination cannot use). Needs the agent's owner or an admin, with editor
    or above in the destination. Run with --dry-run first."""
    try:
        _emit(_client(slug).move(workspace, dry_run=dry_run))
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))


@agent.command("sync")
@click.option("--slug", required=True)
@click.option("--doc-url", required=True)
@click.option("--title", required=True)
@click.option("--summary", default="")
@click.option("--grades", default="{}", help="JSON object of self-grades")
@click.option("--period-start", required=True)
@click.option("--period-end", required=True)
@click.option("--source", default="manager-sync")
def agent_sync(slug, doc_url, title, summary, grades, period_start, period_end, source):
    """Post a manager sync."""
    try:
        c = _client(slug)
        _emit(c.post_sync(period_start=period_start, period_end=period_end, title=title,
                          summary=summary, doc_url=doc_url, self_grades=json.loads(grades), source=source))
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))


@agent.command("turn")
@click.option("--slug", required=True)
@click.option("--title", required=True, help="What the turn did, in one line.")
@click.option("--summary", default="", help="The close-out summary.")
@click.option("--task", "task_ext_ids", multiple=True,
              help="ext_id of a request this turn advanced (repeatable).")
@click.option("--work-product-url", "work_product_urls", multiple=True,
              help="url of a deliverable produced this turn (repeatable).")
@click.option("--source", default="turn")
@click.option("--session-id", "cli_session_id", default="",
              help="Claude session id (the dedup key); auto-derived with --upload.")
@click.option("--upload", is_flag=True,
              help="Reduce + upload the transcript and link it to the turn (optional).")
@click.option("--transcript", type=click.Path(exists=True), default=None,
              help="Transcript .jsonl to upload (default: newest for the cwd).")
@click.option("--full", is_flag=True, help="Upload the raw transcript instead of the reduced one.")
@click.option("--visibility", type=click.Choice(["link", "private"]), default="link")
def agent_turn(slug, title, summary, task_ext_ids, work_product_urls, source,
               cli_session_id, upload, transcript, full, visibility):
    """Package this turn as a unit of work; optionally upload its transcript.

    The transcript is OPTIONAL — without --upload this just records the request(s)
    advanced, the summary, and the deliverables. With --upload it reduces the
    session (conversation-only) to a /share/<token> link hung off the turn."""
    try:
        session_slug = share_token = ""
        if upload:
            from orchestrator import session_upload
            path = Path(transcript) if transcript else session_upload.discover_transcript(Path.cwd())
            body = session_upload.upload_transcript(path, title=title, visibility=visibility, full=full)
            session_slug = body.get("slug", "") or ""
            share_token = body.get("share_token", "") or ""
            cli_session_id = cli_session_id or body.get("cli_session_id", "") or path.stem
        if not cli_session_id:
            raise click.ClickException(
                "pass --session-id (the Claude session id), or --upload to derive it from the transcript")
        _emit(_client(slug).post_turn(
            cli_session_id=cli_session_id, title=title, summary=summary,
            task_ext_ids=list(task_ext_ids), work_product_urls=list(work_product_urls),
            session_slug=session_slug, share_token=share_token, source=source))
    except (CanopyError, RuntimeError, OSError) as e:
        raise click.ClickException(str(e))


@agent.command("skills")
@click.option("--slug", required=True)
@click.option("--from-repo", "skills_root", type=click.Path(exists=True),
              help="glob <root>/*/SKILL.md into the catalog")
@click.option("--url-template", default="", help="e.g. https://github.com/org/repo/blob/main/skills/{name}/SKILL.md")
@click.option("--json", "json_file", type=click.Path(exists=True), help="explicit catalog JSON")
def agent_skills(slug, skills_root, url_template, json_file):
    """Replace the skill catalog (from a repo glob or a JSON file)."""
    try:
        if skills_root:
            items = catalog_from_repo(skills_root, url_template or "{name}")
        elif json_file:
            items = json.load(open(json_file, encoding="utf-8"))
        else:
            raise click.ClickException("pass --from-repo or --json")
        _emit(_client(slug).put_skills(items))
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))


@agent.group("interface")
def agent_interface():
    """An agent's DECLARED INTERFACE — who may make it do what: the whole agent for
    trusted addresses (`full:`, e.g. contact@dimagi.com:verified), a confined
    capability for everyone else. LIVE STATE held on canopy-web, never a file in
    the agent's repo; also editable on the agent's Overview page."""


@agent_interface.command("get")
@click.option("--slug", required=True)
def agent_interface_get(slug):
    """Print the interface as saved (YAML), or the parsed form if it has none."""
    try:
        body = _client(slug).get_interface()
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))
    click.echo(body.get("source") or json.dumps(body.get("interface") or {}, indent=2))


@agent_interface.command("set")
@click.option("--slug", required=True)
@click.option("--file", "file_", required=True, type=click.Path(exists=True, dir_okay=False),
              help="YAML to save — any path; it is not read from, or kept in, the repo.")
def agent_interface_set(slug, file_):
    """Save the interface on canopy-web. Validated there; a bad file is refused
    with the reason. Needs the agent's owner or an admin."""
    try:
        _emit(_client(slug).put_interface_source(Path(file_).read_text(encoding="utf-8")))
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))


@agent.command("tasks-create")
@click.option("--slug", required=True)
@click.option("--json", "json_file", required=True, type=click.Path(exists=True),
              help="JSON file: [{ext_id?,title,next_action,status,owner,assigned,…}]")
def agent_tasks_create(slug, json_file):
    """Create tasks from a JSON list — safe to re-run.

    Each task that names an ext_id is keyed `<slug>:<ext_id>`, so a second run hands
    back the tasks the first one made instead of duplicating them. It does NOT update
    a task that already exists — that is `agent set`."""
    try:
        tasks = json.load(open(json_file, encoding="utf-8"))
        if not isinstance(tasks, list):
            raise click.ClickException("tasks file must be a JSON list")
        _emit(_client(slug).create_tasks(tasks))
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))


@agent.command("actions")
@click.option("--slug", required=True)
def agent_actions(slug):
    """List the actions people took on the agent's tasks that it has not carried out yet
    (approve / decline / reply / dispatch / done) — the queue a turn drains, oldest first.
    Carry each out, then `agent applied --id <N>`."""
    try:
        actions = _client(slug).pending_actions()
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))
    if not actions:
        click.echo("no pending actions")
        return
    for a in actions:
        comment = f"  {a.comment}" if a.comment else ""
        click.echo(f"  #{a.id} {a.action} -> {a.task_ext_id or '(no task)'}  [{a.by}]{comment}")


@agent.command("doctor")
@click.option("--repo", type=click.Path(exists=True, file_okay=False),
              help="Agent repo root (default: cwd). Identity from its config/agent.json.")
@click.option("--slug", "slug", default="",
              help="Agent slug — locate its local repo instead of --repo.")
@click.option("--all", "all_agents", is_flag=True,
              help="Run across EVERY discovered agent in the fleet (ignores --repo/--slug).")
@click.option("--fix", "do_fix", is_flag=True,
              help="Attempt the safe, non-interactive repairs (provision secrets, register on "
                   "canopy-web), then re-check. Interactive steps are still only printed.")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def agent_doctor(repo, slug, all_agents, do_fix, as_json):
    """Diagnose ONE agent's operational readiness on THIS machine (or the whole fleet with --all).

    Read-only composition of the existing point-checks: identity
    (config/agent.json), gating rails, secrets manifest (provisionable),
    live gog email auth, canopy-web registration + board. Exits non-zero
    if any check fails. `canopy doctor` covers the plugin install; this
    covers the agent. `--all` sweeps every discovered agent and exits
    non-zero if ANY agent has a failing check — the fleet readiness gate
    that complements `canopy fleet-align` (which checks shared-artifact drift).
    """
    from pathlib import Path

    from orchestrator.agent_doctor import heal_agent, run_agent_doctor
    from orchestrator.agent_email import AgentEmailError, find_agent_repo

    def _heal_and_recheck(path, results):
        """Apply the safe fixers, then re-run the checks so the verdict reflects reality."""
        actions = heal_agent(path, results)
        if not actions:
            return results, all(r.ok for r in results), []
        fresh, ok = run_agent_doctor(path)
        return fresh, ok, actions

    if all_agents:
        from orchestrator.fleet_align import checkout_warnings, discover_agents
        fleet = []
        healed = []
        for a in sorted(discover_agents(), key=lambda x: x.slug):
            results, ok = run_agent_doctor(a.path)
            if do_fix and not ok:
                results, ok, actions = _heal_and_recheck(a.path, results)
                healed.extend((a.slug, *act) for act in actions)
            fleet.append((a.slug, str(a.path), results, ok))
        # An agent whose checkout is parked off its default branch is INVISIBLE to discovery —
        # it produces no row at all, so a fleet-wide "all ready" can be a confident green over a
        # fleet that is quietly missing members. Surface that before the per-agent verdicts.
        drift = checkout_warnings()
        any_fail = any(not ok for *_, ok in fleet)
        if as_json:
            click.echo(json.dumps({
                "ok": not any_fail,
                "discovered": len(fleet),
                "checkout_warnings": drift,
                "agents": [
                    {"slug": s, "repo": p, "ok": ok,
                     "checks": [r.to_dict() for r in rs]}
                    for s, p, rs, ok in fleet
                ],
            }, indent=2))
        else:
            for slug_, label, ok_, detail in healed:
                click.echo(f"  [{'FIXED' if ok_ else 'FAILED'}] {slug_}: {label} — {detail}")
            if healed:
                click.echo()
            if drift:
                # NOT auto-healed: a checkout can carry unpushed commits, and blindly
                # fast-forwarding is how that work gets stranded. Report and let a human look.
                click.echo(f"Checkout drift — {len(drift)} repo(s) may be hidden from discovery:")
                for w in drift:
                    click.echo(f"  ! {w}")
                click.echo()
            for s, p, rs, ok in fleet:
                click.echo(f"[{'OK  ' if ok else 'FAIL'}] {s}")
                for r in rs:
                    if not r.ok:
                        click.echo(f"         - {r.name}: {r.detail}")
            click.echo()
            n_fail = sum(1 for *_, ok in fleet if not ok)
            # Never print a bare "all ready" under a drift list — a stale or off-branch
            # checkout is exactly how an agent goes missing from the sweep unnoticed.
            caveat = f" ({len(drift)} checkout warning(s) above)" if drift else ""
            click.echo(f"{n_fail}/{len(fleet)} agent(s) have failing checks — fix above.{caveat}"
                       if any_fail
                       else f"All {len(fleet)} discovered agent(s) ready on this machine.{caveat}")
        if any_fail:
            raise SystemExit(1)
        return

    try:
        repo_dir = Path(repo) if repo else (find_agent_repo(slug) if slug else Path.cwd())
    except AgentEmailError as e:
        raise click.ClickException(str(e))
    results, overall_ok = run_agent_doctor(repo_dir)
    actions = []
    if do_fix and not overall_ok:
        results, overall_ok, actions = _heal_and_recheck(repo_dir, results)

    if as_json:
        click.echo(json.dumps({
            "ok": overall_ok, "repo": str(repo_dir),
            "fixes": [{"action": a, "ok": o, "detail": d} for a, o, d in actions],
            "checks": [r.to_dict() for r in results]}, indent=2))
    else:
        width = max(len(r.name) for r in results)
        for r in results:
            status = "WARN" if (r.ok and r.warn) else ("OK  " if r.ok else "FAIL")
            click.echo(f"  [{status}] {r.name.ljust(width)}  {r.detail}")
        click.echo()
        for label, ok_, detail in actions:
            click.echo(f"  [{'FIXED' if ok_ else 'FAILED'}] {label} — {detail}")
        if actions:
            click.echo()
        if overall_ok:
            click.echo(f"All checks passed — agent at {repo_dir} is ready on this machine.")
        else:
            click.echo("Some checks failed — fix lines above (see also `canopy provision --check`).")
    if not overall_ok:
        raise SystemExit(1)


@agent.command("use")
@click.argument("slug")
@click.option("--dir", "directory", default=".", type=click.Path(file_okay=False),
              help="The project whose Claude Code sessions use the agent (default: here).")
@click.option("--json", "as_json", is_flag=True, help="Print the steps as JSON.")
def agent_use(slug, directory, as_json):
    """Use agent SLUG yourself, in Claude Code on this computer — no runner needed.

    \b
    Two ways to run an agent:
      1. Use it yourself in Claude Code: run this once on your computer.
      2. Let it run on its own: a runner — your Mac's runner app (set it
         Private: it does only your work) or a fleet cloud box.

    \b
    This sets up way 1 and is safe to re-run. It checks, and fixes where it can:
    your canopy-web sign-in, this computer's registration for local use, its
    device key, this device's access to the agent's canopy vault, your licence
    to use the agent, the agent's plugin, $CANOPY_AGENT for --dir, and the
    agent's env. Each step says ok / done / NEEDS YOU (with the exact URL or
    person). It ends with the line to start the agent, or what is left for you.
    Exits 1 while anything NEEDS YOU."""
    from orchestrator import agent_use as au

    rep = au.run(slug, directory, au.real_deps())
    click.echo(json.dumps(rep.as_dict(), indent=1) if as_json else rep.render())
    if rep.needs:
        raise SystemExit(1)


@agent.command("op-token")
@click.option("--slug", envvar="CANOPY_AGENT", required=True,
              help="Agent slug. Default: $CANOPY_AGENT (set by every agent repo's settings.json).")
def agent_op_token(slug):
    """Print THIS agent's own 1Password service-account key, for `OP_SERVICE_ACCOUNT_TOKEN`.

    The laptop twin of the cloud runner's `_agent_op_token`: the same canopy-web route
    (`/credentials/resolve`), the same key — scoped to the agent's own vault — so `op` on an
    emdash/laptop session never touches the 1Password desktop app (which locks, and then
    `op` hangs on an unlock prompt nobody is there to answer). Authorized as the OPERATOR:
    canopy-web hands the key only to a caller who pairs a live runner this agent routes to.

    Prints the key alone on stdout, for `OP_SERVICE_ACCOUNT_TOKEN="$(canopy agent op-token)"`.
    Exit 1 with the reason on stderr when there is none — never an empty success. Exit 3
    when the agent keeps its secrets in canopy vault: it HAS no 1Password key, by design
    (the SessionStart hook then just syncs its env from canopy vault).
    """
    from orchestrator import cred_resolve
    from orchestrator.agent_bootstrap import _operator_token
    from orchestrator.cred_cli import fetch_access
    access = fetch_access(slug)
    if not access.get("error") and access.get("credential_source") == "canopy-web":
        click.echo(f"{slug} keeps its secrets in canopy vault — there is no 1Password key "
                   f"(`canopy cred sync --agent {slug}` keeps its env current)", err=True)
        raise SystemExit(OP_TOKEN_CANOPY_VAULT)
    try:
        body = cred_resolve.resolve(slug, token=_operator_token()) or {}
    except Exception as e:  # noqa: BLE001 — report, don't traceback
        raise click.ClickException(f"could not resolve {slug}'s 1Password key: {str(e).splitlines()[0][:300]}")
    token = str(body.get("op_sa_token") or "")
    if not token:
        raise click.ClickException(
            f"canopy-web has no 1Password key for {slug} — it is a LEGACY 1Password agent; an "
            f"admin moves it to canopy vault with `canopy vault import --agent {slug} --zk --switch`")
    click.echo(token)


#: `canopy agent op-token`'s exit code for an agent whose secrets live in canopy vault.
OP_TOKEN_CANOPY_VAULT = 3


def delegated_chrome_sales_home(slug: str) -> Path:
    """Where chrome-sales reads Salesforce creds in this agent's sessions — the same
    path the cloud runner stages to (canopy-web#1291), so chrome-sales has one rule."""
    return Path.home() / ".canopy" / "delegated" / slug / "chrome-sales"


@agent.command("stage-delegated")
@click.option("--slug", envvar="CANOPY_AGENT", required=True,
              help="Agent slug. Default: $CANOPY_AGENT.")
def agent_stage_delegated(slug):
    """Stage the credentials this agent BORROWS onto this machine.

    Today: the Salesforce identity lent to it (canopy-web#1291 — agents act in
    Salesforce with delegated access to Eva's credential), written to
    ~/.canopy/delegated/<slug>/chrome-sales/.sf-creds.json (0600). When canopy-web
    says it borrows none, a stale file is REMOVED: a withdrawn loan must stop
    acting here too. The laptop twin of the cloud runner's `_stage_salesforce`;
    same route (`/credentials/resolve`, operator-authorized), same path.

    Prints one line saying what it did; never the credential.
    """
    import os

    from orchestrator import cred_resolve
    from orchestrator.agent_bootstrap import _operator_token, is_agents_own_token
    token = _operator_token()
    if is_agents_own_token(slug, token):
        # No operator token on this machine — only the agent's own, which the route
        # refuses (canopy#945). Skip without touching a copy staged by someone who could.
        click.echo(f"salesforce: skipped — the only canopy-web token here is {slug}'s own, "
                   f"and borrowed credentials are released to the operator; run "
                   f"/canopy:canopy-web-pat-mint as the person who runs this machine")
        return
    try:
        body = cred_resolve.resolve(slug, token=token) or {}
    except Exception as e:  # noqa: BLE001 — report, don't traceback
        raise click.ClickException(
            f"could not resolve {slug}'s delegated credentials: {str(e).splitlines()[0][:300]}")
    home = delegated_chrome_sales_home(slug)
    target = home / ".sf-creds.json"
    creds = str(body.get("salesforce_creds") or "")
    if not creds:
        existed = target.exists()
        target.unlink(missing_ok=True)
        click.echo(f"salesforce: {slug} borrows no Salesforce identity"
                   + (" — removed the stale staged copy" if existed else ""))
        return
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = target.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(creds)
    os.replace(tmp, target)
    click.echo(f"salesforce: staged {slug}'s borrowed Salesforce identity at {target}")


@agent.command("env")
@click.option("--plugin", required=True,
              help="The agent plugin's name, as installed (`<plugin>@canopy-agents`).")
@click.option("--template", type=click.Path(dir_okay=False), default=".env.tpl",
              show_default=True, help="The agent's .env.tpl (refs op://${AGENT_VAULT}/…).")
@click.option("--out", default=None,
              help="Where the filled env goes. Default: ~/.<plugin>/.env. The binding is "
                   "recorded beside it (canopy-instance.json).")
@click.option("--agent", "agent_slug", default="",
              help="The instance (canopy-web agent slug) to bind to, when you can see several.")
@click.option("--vault", default="",
              help="LEGACY: a 1Password vault to fill from instead of the agent's own secrets.")
@click.option("--force", is_flag=True,
              help="Re-bind this install to a different instance than the one recorded.")
@click.option("--print", "print_only", is_flag=True,
              help="Print the resolved instance and vault as JSON; inject and record nothing.")
def agent_env(plugin, template, out, agent_slug, vault, force, print_only):
    """Bind this install to ONE agent instance and fill its .env from that instance's vault.

    A plugin is code; an instance is canopy-web's Agent record (identity, workspace,
    where its secrets live). Resolves the instance of PLUGIN you can see (--agent picks
    one; the one this install is already bound to is kept; several stop with a re-run
    line each) and records the binding beside --out (switching instance needs --force).

    Secrets come from the instance's canopy vault (decrypted on this device). Only a
    LEGACY 1Password agent (or --vault) runs `op inject -i TEMPLATE -o OUT` with
    AGENT_VAULT and AGENT_SLUG set. Called by each agent's /<agent>:setup. Never
    prompts; never prints a secret. To use an agent yourself: `canopy agent use <slug>`.
    """
    from orchestrator import agent_instance as ai
    from orchestrator.workspace_cli import person_token

    token = person_token()
    if not token:
        raise click.ClickException(
            "not signed in to canopy-web — run /canopy:canopy-web-pat-mint, then re-run.")
    out_path = Path(out).expanduser() if out else Path.home() / f".{plugin}" / ".env"
    res = ai.run(plugin, template=Path(template) if template else None, out=out_path,
                 agent=agent_slug, vault=vault, force=force, print_only=print_only, token=token)
    inst = res.instance
    if print_only:
        _emit({"plugin": inst.plugin, "agent": inst.agent, "workspace": inst.workspace,
               "vault": res.vault, "vault_source": res.vault_source, "why": res.why,
               "binding": str(res.binding), "out": str(out_path), "notes": res.notes})
        return
    click.echo(f"instance: {inst.label()} — {res.why}")
    if res.vault_source == "canopy vault":
        click.echo("secrets:  canopy vault")
        for n in res.notes:
            click.echo(f"note:     {n}")
        click.echo(f"env:      {out_path} ({res.lines} vars) from canopy vault")
        click.echo(f"bound:    {res.binding}")
        return
    click.echo(f"vault:    {res.vault} (from {res.vault_source}) — 1Password, LEGACY: an admin "
               f"moves it with `canopy vault import --agent {inst.agent} --zk --switch`")
    for n in res.notes:
        click.echo(f"note:     {n}")
    click.echo(f"env:      {out_path} ({res.lines} vars) from {template}, AGENT_VAULT={res.vault}")
    click.echo(f"bound:    {res.binding}")


@agent.command("vault")
@click.option("--slug", required=True, help="The agent (canopy-web slug).")
@click.option("--workspace", default="",
              help="Its canopy-web workspace (default: the one canopy-web records for it).")
@click.option("--vault", default="",
              help="The vault to create or use, for an agent with none recorded (required then).")
@click.option("--repo", type=click.Path(exists=True, file_okay=False), default=None,
              help="The agent's checkout, to read .env.tpl from (default: its repo on GitHub).")
@click.option("--check", is_flag=True,
              help="Report what is missing; create, mint, prompt and write nothing.")
@click.option("--rotate-key", is_flag=True,
              help="Mint and store a NEW read-only key even if one is stored.")
@click.option("--rotate-pat", is_flag=True,
              help="Mint a NEW canopy token for the agent even if its vault holds one.")
@click.option("--no-prompt", is_flag=True,
              help="Never ask for values (they are reported as still empty).")
@click.option("--op-account", default="dimagi.1password.com", show_default=True)
@click.option("--key-expires-in", default="90d", show_default=True,
              help="Lifetime of a newly minted runner key (op duration: 90d, 12w, 720h), "
                   "or `never`. Rotate before it lapses with --rotate-key.")
def agent_vault(slug, workspace, vault, repo, check, rotate_key, rotate_pat, no_prompt,
                op_account, key_expires_in):
    """Give an agent its 1Password vault, filled, and the read-only key its runners use.

    Run by a PERSON with 1Password rights who is the agent's owner or admin on canopy-web.
    Creates the vault if missing and makes the agent's owner its admin; for each
    op://${AGENT_VAULT}/… reference in the agent's .env.tpl, creates the item and fills it —
    MINTS what the system can (its own canopy-web token), points at the control for what
    another flow produces (its Gmail token: Connect Google mailbox), and ASKS, with hidden
    input, only for what a person alone knows (an outside service's key). Then mints a
    read-only service account for the vault (expiring after --key-expires-in, default 90
    days) and stores it on canopy-web.
    Safe to re-run; prints no secret. See orchestrator/agent_vault.py.
    """
    import sys

    from orchestrator import agent_vault as av
    from orchestrator.workspace_cli import person_token

    token = person_token()
    if not token:
        raise click.ClickException(
            "not signed in to canopy-web — run /canopy:canopy-web-pat-mint, then re-run.")
    ask = None if (check or no_prompt or not sys.stdin.isatty()) else av.hidden_prompt
    try:
        rep = av.run(slug, token=token, workspace=workspace, vault=vault,
                     repo=Path(repo) if repo else None, check=check, rotate_key=rotate_key,
                     rotate_pat=rotate_pat, op=av.Op(op_account), ask=ask,
                     key_expires_in=key_expires_in)
    except av.VaultError as e:
        raise click.ClickException(str(e))
    click.echo(av.render(rep))
    if rep.missing or (check and rep.key != "stored"):
        sys.exit(1)


def _prepare_purpose(check) -> str:
    """`--check WHY`, or a terminal (a person checking by hand) → a labelled verification
    read; otherwise "" (the box's own refresh, which the runner and its timer run)."""
    import sys

    from orchestrator.agent_prepare import verification_purpose

    if check is not None:
        return verification_purpose(check)
    try:
        if sys.stdin.isatty() and not os.environ.get("CANOPY_TURN_ID"):
            return verification_purpose("interactive run")
    except Exception:  # noqa: BLE001
        pass
    return ""


@agent.command("prepare")
@click.option("--slug", "slugs", multiple=True, required=True, help="Agent slug (repeatable).")
@click.option("--runner", "runner_mode", is_flag=True,
              help="Runner mode: every credential from canopy-web (the runner's token, "
                   "CANOPY_TOKEN), the agent cloned under --agent-root. What a runner calls.")
@click.option("--agent-root", default=lambda: os.environ.get("AGENT_ROOT", "/opt/agents"),
              show_default="$AGENT_ROOT or /opt/agents", help="Where agents are cloned (runner).")
@click.option("--repo-url", default="", help="Clone URL (default: canopy-web's record).")
@click.option("--credentials-only", is_flag=True,
              help="Gmail client + token + verify + report only — no clone or re-provision.")
@click.option("--env-only", is_flag=True, help="Only re-inject ~/.<slug>/.env (a claim's refresh).")
@click.option("--no-report", is_flag=True, help="Do not POST the readiness report.")
@click.option("--dry-run", is_flag=True, help="Say what would happen; change nothing.")
@click.option("--json", "as_json", is_flag=True, help="One JSON report per agent.")
@click.option("--check", "check", default=None, metavar="WHY",
              help="A verification run, not the box's refresh: reads say `check: WHY (by "
                   "user@host)`, and the owner's read-outside-a-turn alert names it as that "
                   "check. Implied (as 'interactive run') when run from a terminal.")
@click.option("--no-secret-files", "no_secret_files", is_flag=True, default=None,
              help="Runner mode: resolve and check credentials in memory and write no "
                   "~/.<slug>/.env, <clone>/.env or .gws-sa-key.json copy. Implied by "
                   "$CANOPY_TURN_ENV_ROOT (the runner then writes each turn's env with "
                   "`canopy cred env --runner --out`).")
def agent_prepare(slugs, runner_mode, agent_root, repo_url, credentials_only, env_only,
                  no_report, dry_run, as_json, check, no_secret_files):
    """Make agents runnable on THIS machine — the one implementation every runner calls.

    Runner mode (`--runner`): credentials from canopy-web's /credentials/resolve with the
    runner's token; clone/pull, .env via op inject with the agent's own key, its plugin and
    required plugins (node deps, key files), bin/<slug>-setup, the Gmail token (its own
    client, newest of vault and canopy-web), a real mailbox call, then a readiness report.
    Without --runner it is `canopy agent bootstrap` (a person's laptop). See
    orchestrator/agent_prepare.py.
    """
    import sys

    if not runner_mode:
        ctx = click.get_current_context()
        return ctx.invoke(agent_bootstrap, slugs=slugs, repo=None, dry_run=dry_run,
                          op_account="dimagi.1password.com")
    from orchestrator import canopy_web
    from orchestrator.agent_prepare import RunnerPreparer

    base = os.environ.get("CANOPY_BASE_URL", "").strip() or canopy_web.resolve_base_url(None)
    token = os.environ.get("CANOPY_TOKEN", "").strip() or canopy_web.resolve_token(None)
    prep = RunnerPreparer(base_url=base, token=token, agent_root=Path(agent_root),
                          runner_name=os.environ.get("RUNNER_NAME", ""), dry_run=dry_run,
                          purpose=_prepare_purpose(check),
                          no_secret_files=True if no_secret_files else None,
                          echo=(lambda _m: None) if as_json else click.echo)
    bad = False
    for slug in slugs:
        rep = prep.prepare(slug, repo_url=repo_url, credentials_only=credentials_only,
                           env_only=env_only, report=not (no_report or env_only))
        bad = bad or not rep.ok
        if as_json:
            click.echo(json.dumps({"slug": slug, "ok": rep.ok, **rep.payload(prep.runner_name),
                                   "env": rep.env, "plugins": rep.plugins, "token": rep.token,
                                   "gmail": rep.gmail, "notes": rep.notes}))
        else:
            mail = ("email canopy-hosted" if rep.gmail == "canopy-hosted" else f"gmail {rep.gmail}")
            click.echo(f"{slug}: {'OK' if rep.ok else 'PROBLEMS'} — env {rep.env}, plugins "
                       f"{rep.plugins}, token {rep.token}, {mail}, turn {rep.turn}")
            for n in rep.notes:
                click.echo(f"  {n}")
    sys.exit(1 if bad else 0)


@agent.command("bootstrap")
@click.option("--slug", "slugs", multiple=True,
              help="Agent slug to bootstrap (repeatable). Default: every agent repo discovered "
                   "on this machine (the same discovery as `agent doctor --all`).")
@click.option("--repo", type=click.Path(exists=True, file_okay=False),
              help="Agent repo root, for a single agent not at the default location.")
@click.option("--dry-run", is_flag=True,
              help="Show what would happen. Reads token sources and the plugin/gog state, "
                   "but installs, injects, imports and writes nothing, and skips the gmail call.")
@click.option("--op-account", default="dimagi.1password.com", show_default=True,
              help="1Password account for `op read` / `op inject`.")
def agent_bootstrap(slugs, repo, dry_run, op_account):
    """Make THIS machine ready to run agents — the laptop twin of the cloud runner's
    bootstrap_agents.sh, same rules.

    Per agent: install its plugin + config/agent.json required_plugins; `op inject` its
    .env.tpl into ~/.<slug>/.env; take the NEWEST gog token (canopy-web vs
    op://<vault>/gog-token, by the token's own created_at — <vault> is the instance's recorded
    op_vault, never derived from the slug); materialize the OAuth client
    the TOKEN names; import it (skipped if gog already holds it) and map the account; verify
    with one real gmail call and check the client turns ask for against the token's.
    Idempotent — re-running on a ready machine changes nothing. One agent failing never
    stops the others; exits non-zero if any agent has a problem.
    """
    from orchestrator.agent_bootstrap import Bootstrapper, render_table
    from orchestrator.agent_email import AgentEmailError, find_agent_repo

    targets: list[tuple[str, Path | None]] = []
    if repo:
        from orchestrator.agent_web import AgentWebError, resolve_identity
        try:
            targets.append((resolve_identity(Path(repo))["slug"], Path(repo)))
        except AgentWebError as e:
            raise click.ClickException(str(e))
    elif slugs:
        for s in slugs:
            try:
                targets.append((s, find_agent_repo(s)))
            except AgentEmailError:
                targets.append((s, None))
    else:
        from orchestrator.agent_web import AgentWebError, resolve_identity
        from orchestrator.fleet_align import discover_agents
        # Key on the agent's IDENTITY (plugin.json name), not the directory: a second
        # checkout (`ace-2`) is the same agent, and bootstrapping it as "ace-2" would look up
        # a vault and a canopy-web agent that don't exist. Prefer the checkout named for it.
        by_slug: dict[str, Path] = {}
        for a in sorted(discover_agents(), key=lambda x: (x.path.name, str(x.path))):
            try:
                ident = resolve_identity(a.path)["slug"]
            except (AgentWebError, OSError, ValueError):
                ident = a.slug
            if ident not in by_slug or a.path.name == ident:
                by_slug[ident] = a.path
        targets = sorted(by_slug.items())
        if not targets:
            raise click.ClickException(
                "no agent repos discovered on this machine — pass --slug <x> or --repo <dir>")

    boot = Bootstrapper(dry_run=dry_run, op_account=op_account, echo=click.echo)
    click.echo(f"{'DRY RUN — ' if dry_run else ''}bootstrapping "
               f"{', '.join(s for s, _ in targets)}")
    reports = []
    for s, path in targets:
        click.echo(f"  ... {s}")
        reports.append(boot.bootstrap_one(s, path))
    click.echo()
    click.echo(render_table(reports))
    bad = [r.slug for r in reports if not r.ok]
    click.echo()
    click.echo(f"{len(bad)} agent(s) need attention: {', '.join(bad)}" if bad
               else f"All {len(reports)} agent(s) bootstrapped"
                    f"{' (dry run — nothing changed)' if dry_run else ''}.")
    if bad:
        raise SystemExit(1)


# The statuses a board drain actually wants: everything not yet resolved. `normalize_task_status`
# maps the whole vocabulary onto six tokens (canopy-web's Linear categories), and these four
# are the open ones: proposed, accepted-not-now, accepted-next-up, under way.
OPEN_TASK_STATUSES = ("suggested", "backlog", "todo", "in_progress")


@agent.command("tasks")
@click.option("--slug", required=True)
@click.option("--open", "open_only", is_flag=True,
              help=f"Only unresolved tasks ({', '.join(OPEN_TASK_STATUSES)}) — the turn-start drain.")
@click.option("--status", "statuses", multiple=True,
              help="Only tasks with this status (repeatable). Accepts human spellings "
                   '("in progress") as well as canonical tokens ("in_progress").')
def agent_tasks(slug, open_only, statuses):
    """List the agent's board tasks (JSON) — e.g. to compute the next ext_id.

    Unfiltered by default, because computing the next ext_id needs the FULL set including
    resolved tasks. Pass `--open` for the turn-start board drain, which wants the opposite:
    a board's signal is its handful of unresolved tasks, while its payload grows without
    bound as tasks close (canopy#516).
    """
    if open_only and statuses:
        raise click.ClickException("--open and --status are alternatives; pass one or the other.")
    wanted = set(OPEN_TASK_STATUSES) if open_only else {
        normalize_task_status(s) for s in statuses
    }
    try:
        tasks = _client(slug).list_tasks()
        if wanted:
            tasks = [t for t in tasks if normalize_task_status(t.get("status")) in wanted]
        _emit(tasks)
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))


def resolve_project_ref(client, ref):
    """A `--project` value → the board's `P<N>` ext_id.

    Accepts the ext_id or the project's NAME, because the name is what an agent
    has in hand: it is the Drive folder it just worked in, while `P3` lives only
    on canopy-web.

    An unknown value RAISES. The server deliberately does not 404 on one — it
    keeps the task and files it nowhere, which is right for the API (a typo must
    not cost an agent the work it just recorded) and wrong for a CLI, where it
    would report success and leave the task unfiled. Failing here is the only
    place that difference can be seen.
    """
    raw = str(ref or "").strip()
    projects = client.list_projects()
    def ref_of(project):
        # A project made outside an agent's route carries no `P<N>`: name it by its
        # workspace address, which every agent route also accepts.
        return project.get("ext_id") or project.get("address") or project.get("handle")

    for project in projects:
        if raw.casefold() in {str(project.get(k) or "").strip().casefold()
                              for k in ("ext_id", "handle", "address")} - {""}:
            return ref_of(project)

    named = [p for p in projects
             if str(p.get("name") or "").strip().casefold() == raw.casefold()]
    if len(named) == 1:
        return ref_of(named[0])
    if len(named) > 1:
        raise click.ClickException(
            f"{raw!r} is the name of {len(named)} projects on this board — "
            f"pass the P<N> ext_id instead ({', '.join(ref_of(p) for p in named)})."
        )

    known = ", ".join(f"{ref_of(p)} {p['name']}" for p in projects) or "none yet"
    raise click.ClickException(
        f"no project {raw!r} on this board — pass a P<N> ext_id or the exact name. "
        f"Nothing was written. Known: {known}. "
        f"(`canopy agent project-add --slug … --name …` creates one.)"
    )


@agent.command("projects")
@click.option("--slug", required=True)
@click.option("--active", "active_only", is_flag=True,
              help="Only projects still running — the default reading of the board.")
def agent_projects(slug, active_only):
    """List the agent's projects (JSON).

    A project is the piece of work a `Projects/<name>` Drive folder holds. canopy
    keeps the state the folder cannot state — what is open, what is parked on a
    person, whether it is still running; Drive keeps the files.
    """
    try:
        projects = _client(slug).list_projects()
        if active_only:
            projects = [p for p in projects if (p.get("status") or "active") == "active"]
        _emit(projects)
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))


@agent.command("project-add")
@click.option("--slug", required=True)
@click.option("--name", required=True,
              help="What the work IS — use the Drive folder's own name, so the two "
                   "cannot drift apart. Max 200 chars.")
@click.option("--outcome", default="", help="What DONE looks like, in one line.")
@click.option("--drive-folder-url", default="",
              help="The `Projects/<name>` folder this project is the state of. Pass it "
                   "at creation: a project whose folder nobody can find is a second "
                   "place to look rather than one place to look.")
@click.option("--drive-folder-id", default="")
@click.option("--no-folder", is_flag=True,
              help="Do not find-or-create Projects/<name> in Drive (the default links one).")
@click.option("--repo", "repo_slug", default="", help="owner/name, if the work has one.")
@click.option("--owner", "owner_note", default="",
              help="The human who owns the outcome. Max 200 chars.")
@click.option("--notes", default="")
@click.option("--links", default="", help='"label|url, label2|url2" (bare urls OK).')
def agent_project_add(slug, name, outcome, drive_folder_url, drive_folder_id, no_folder,
                      repo_slug, owner_note, notes, links):
    """Create ONE project (auto-assigns the next P<N>) and link its Drive folder.

    One project per real piece of work — the same rule the Drive layout already
    states. A project per task produces a directory of single-task projects, which
    tells you less than the task list did.

    Without --drive-folder-url/--drive-folder-id, `<agent root>/Projects/<name>` is
    found or created as the agent and linked (see `project-folder`). A Drive failure
    does not undo the project; the output's `folder.error` says what to run.
    """
    try:
        client = _client(slug)
        made = client.create_project(
            name=name.strip(), outcome=outcome.strip(), owner_note=owner_note.strip(),
            drive_folder_id=drive_folder_id.strip(),
            drive_folder_url=drive_folder_url.strip(),
            repo_slug=repo_slug.strip(), notes=notes.strip(),
            links=parse_task_links(links),
        )
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))
    if not no_folder and not (drive_folder_url.strip() or drive_folder_id.strip()):
        made["folder"] = _ensure_folder(client, slug, made)
    _emit(made)


def _ensure_folder(client, slug, project, *, dry_run=False):
    """`project_folder.ensure_project_folder` as the agent, failures returned not raised —
    a project or a backfill row must not be lost to one Drive error."""
    from orchestrator.agent_gdoc import AgentGdocError, _gdoc_identity_from_opts
    from orchestrator.project_folder import ensure_project_folder

    try:
        ident = _gdoc_identity_from_opts(None, slug, None, None)
        return ensure_project_folder(client, ident, project, dry_run=dry_run)
    except (AgentGdocError, CanopyError, RuntimeError, ValueError) as e:
        sys.stderr.write(
            f"NOTE: {project.get('ext_id')} has no Drive folder linked: {str(e)[:200]}\n"
            f"  Fix the cause, then: canopy agent project-folder --slug {slug} "
            f"--project {project.get('ext_id')}\n")
        return {"ext_id": project.get("ext_id"), "error": str(e)[:300]}


@agent.command("project-folder")
@click.option("--slug", required=True)
@click.option("--project", "ref", default=None, metavar="EXT_ID_OR_NAME",
              help="The board's P<N> ext_id, or the project's exact name.")
@click.option("--all", "all_active", is_flag=True,
              help="Every ACTIVE project with no folder linked (the backfill).")
@click.option("--dry-run", is_flag=True, help="Report what would be linked; write nothing.")
def agent_project_folder(slug, ref, all_active, dry_run):
    """Make a project's Drive folder a property of the project (JSON).

    Finds or creates `<agent root>/Projects/<project name>` AS THE AGENT and writes its id
    and url onto the canopy-web project, so any session reading the project, in any repo,
    gets the folder. Idempotent: a folder the project already links wins. Works from any
    directory; the agent's identity and Drive root come from --slug.
    """
    if bool(ref) == bool(all_active):
        raise click.ClickException("pass --project <P-ref|name> or --all, not both")
    try:
        client = _client(slug)
        if ref:
            targets = [client.get_project(resolve_project_ref(client, ref))]
        else:
            targets = [p for p in client.list_projects()
                       if (p.get("status") or "active") == "active"
                       and not str(p.get("drive_folder_id") or "").strip()]
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))
    results = [_ensure_folder(client, slug, p, dry_run=dry_run) for p in targets]
    _emit(results[0] if ref else results)
    if any("error" in r for r in results):
        sys.exit(1)


@agent.command("project-set")
@click.option("--slug", required=True)
@click.option("--project", "ref", required=True, metavar="EXT_ID_OR_NAME",
              help="The board's P<N> ext_id, or the project's exact name.")
@click.option("--status", default=None,
              help="active / done / archived. Closing a project KEEPS its tasks — the "
                   "history of what the work was is the point of a finished one.")
@click.option("--name", default=None)
@click.option("--outcome", default=None)
@click.option("--drive-folder-url", default=None)
@click.option("--drive-folder-id", default=None)
@click.option("--repo", "repo_slug", default=None)
@click.option("--owner", "owner_note", default=None)
@click.option("--notes", default=None)
@click.option("--links", default=None, help='REPLACE the links: "label|url, …". "" clears them.')
@click.option("--append-link", multiple=True,
              help='ADD one link, keeping the existing ones: "label|url". Repeat it to add '
                   "several. This is where a deliverable goes — the doc, deck or PR the "
                   "project produced. A url already on the project is not duplicated.")
def agent_project_set(slug, ref, links, append_link, **fields):
    """Patch a project — close it, point it at its Drive folder, restate the outcome,
    attach a deliverable (--append-link)."""
    append_link = list(append_link or ()) or None  # multiple=True: () when not passed
    if links is not None and append_link is not None:
        raise click.ClickException("pass --links (replace) or --append-link (add), not both")
    try:
        client = _client(slug)
        ext_id = resolve_project_ref(client, ref)
        if links is not None:
            fields["links"] = parse_task_links(links)
        elif append_link is not None:
            # Read-modify-write: the PATCH replaces `links` wholesale.
            current = list(client.get_project(ext_id).get("links") or [])
            fields["links"] = _merge_links(current, append_link)
        _emit(client.patch_project(ext_id, **fields))
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))


@agent.command("project-audit")
@click.option("--slug", required=True)
@click.option("--repo", default=None, type=click.Path(file_okay=False),
              help="The agent's repo (resolves its Drive identity, as `gdoc publish` does). "
                   "Default: found from --slug.")
@click.option("--recent-days", default=2, show_default=True, type=int,
              help="Window for check (e): open tasks touched this recently need a turn record.")
@click.option("--json", "as_json", is_flag=True, help="Emit the full result as JSON.")
def agent_project_audit(slug, repo, recent_days, as_json):
    """Read-only audit: does the board's project model match the agent's actual work?

    Findings: (a) open tasks with no project that look like project work, (b) active
    projects with no Drive folder, (d) project/folder name drift, (e) open tasks touched in
    the last --recent-days with no `canopy agent turn` record, (f) open tasks whose every
    referenced GitHub issue/PR is closed or merged. (c) — Projects/ folders with
    no active project — is informational only: Drive is the archive and outlives the board.
    Ends with the `projects: …` close-out line turn.md Step 4 requires. ALWAYS exits 0.
    """
    from datetime import datetime, timezone

    from orchestrator import project_audit as pa

    try:
        client = _client(slug)
        tasks, projects, turns = client.list_tasks(), client.list_projects(), client.list_turns()
    except (CanopyError, RuntimeError, OSError) as e:
        line = f"projects: audit failed — canopy-web unreadable ({str(e)[:160]})"
        _emit({"slug": slug, "error": str(e), "closeout": line}) if as_json else click.echo(line)
        return
    folders, note = pa.list_project_folders(repo, slug)
    gh_states, gh_note = pa.fetch_github_states(pa.open_task_github_refs(tasks))
    result = pa.audit(slug=slug, tasks=tasks, projects=projects, turns=turns,
                      folders=folders, drive_note=note,
                      now=datetime.now(timezone.utc), recent_days=recent_days,
                      github_states=gh_states, github_note=gh_note)
    if as_json:
        _emit(result)
    else:
        click.echo(pa.render(result))


@agent.command("handoff")
@click.option("--from", "from_slug", required=True, help="The agent giving the project up.")
@click.option("--to", "to_slug", required=True, help="The agent taking it over.")
@click.option("--name", required=True,
              help="The project's name on the RECEIVING board — use the name of the "
                   "Projects/<name> folder it will live in, as for project-add.")
@click.option("--ref", "refs", multiple=True, required=True,
              help="What the work is known by: a Gmail thread id, a Doc/folder id, a URL. "
                   "Repeatable. Used to FIND the source sessions; URLs also become links.")
@click.option("--from-project", default="",
              help="The project's P<N> or name on the SOURCE board, to close it there.")
@click.option("--outcome", default="")
@click.option("--owner", "owner_note", default="")
@click.option("--drive-folder-url", default="",
              help="The folder AFTER the move (under the receiver's Projects root).")
@click.option("--note", default="", help="One line on why it moved / what the receiver is to do.")
@click.option("--exclude-session", default="",
              help="Your own session id, so the handoff turn does not list itself.")
@click.option("--dry-run", is_flag=True, help="Find the sessions and print the plan; write nothing.")
def agent_handoff(from_slug, to_slug, name, refs, from_project, outcome, owner_note,
                  drive_folder_url, note, exclude_session, dry_run):
    """Hand a project from one agent to another (run by the RECEIVING agent).

    Does the parts that are the same for every handoff and prints the rest:

    \b
    1. FINDS the source agent's sessions that touched the refs (read-only) —
       the context the artifacts do not carry. Read them before acting.
    2. OPENS the project on the receiving board, its notes naming where it came
       from and which sessions to read, its links carrying the refs.
    3. CLOSES it on the source board (status archived, note "handed off to …")
       when --from-project is given. A source board this PAT cannot see is
       reported, not fatal — the step is printed for a human.

    The Drive folder move is NOT done here: who may move it depends on who owns
    the files (often the source agent's service account, which the receiver
    cannot act as), so it is a printed step. See agent-core/handoff.md.
    """
    from orchestrator.agent_handoff import find_sessions, handoff_notes

    sessions = find_sessions(refs, from_slug, exclude_session=exclude_session)
    notes = handoff_notes(from_slug, to_slug, sessions, note)
    url_refs = [r for r in refs if r.startswith("http")]
    links = ", ".join(f"ref|{u}" for u in url_refs)
    result = {"from": from_slug, "to": to_slug, "name": name, "sessions": sessions,
              "notes": notes, "dry_run": dry_run}
    if dry_run:
        _emit(result)
        return
    try:
        result["project"] = _client(to_slug).create_project(
            name=name.strip(), outcome=outcome.strip(), owner_note=owner_note.strip(),
            drive_folder_id="", drive_folder_url=drive_folder_url.strip(), repo_slug="",
            notes=notes, links=parse_task_links(links),
        )
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(f"could not open the project on {to_slug}'s board: {e}")
    if from_project:
        try:
            src = _client(from_slug)
            ext_id = resolve_project_ref(src, from_project)
            result["source_project"] = src.patch_project(
                ext_id, status="archived",
                notes=f"Handed off to {to_slug} as {result['project'].get('ext_id', name)!r}.")
        except (CanopyError, RuntimeError, click.ClickException) as e:
            msg = getattr(e, "message", None) or str(e)
            result["source_project"] = {"error": msg, "todo": (
                f"close {from_project!r} on {from_slug}'s board by hand: "
                f"canopy agent project-set --slug {from_slug} --project {from_project!r} "
                f"--status archived --notes 'Handed off to {to_slug}'")}
    else:
        result["source_project"] = {"todo": f"no --from-project given — if {from_slug} "
                                             "tracked this on its board, close it there."}
    _emit(result)


@agent.command("syncs")
@click.option("--slug", required=True)
@click.option("--limit", type=int, default=None, help="Cap the number returned (newest first).")
def agent_syncs(slug, limit):
    """List past manager syncs (JSON, newest first) — the manager-sync window is
    the latest sync's period_end → today, so the window state lives here."""
    try:
        _emit(_client(slug).list_syncs(limit=limit))
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))


@agent.command("sync-delete")
@click.option("--slug", required=True)
@click.option("--id", "sync_id", type=int, required=True, help="Sync id from `agent syncs`.")
def agent_sync_delete(slug, sync_id):
    """Delete ONE manager sync by id.

    `agent sync` upserts per (period, source), so re-posting corrects a sync for the
    SAME window — use this only for one filed under the WRONG period, or a stray row.
    """
    try:
        _client(slug).delete_sync(sync_id)
        _emit({"deleted": sync_id, "slug": slug})
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))


@agent.command("applied")
@click.option("--slug", required=True)
@click.option("--id", "action_id", type=int, required=True,
              help="The action's id, from `agent actions`.")
@click.option("--note", default="", help="What you did about it.")
def agent_applied(slug, action_id, note):
    """Mark a pending action applied — you carried it out."""
    try:
        _emit(_client(slug).mark_action_applied(action_id, result_note=note))
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))


def _no_such_task(ref):
    return click.ClickException(
        f"no task {ref!r} on this board — pass its T<N> ext_id "
        f"(`canopy agent tasks --slug …` lists them)"
    )


@agent.command("set")
@click.option("--slug", required=True)
@click.option("--task-id", required=True, metavar="EXT_ID",
              help="The board's T<N> ext_id, as shown on the card.")
@click.option("--title", default=None,
              help="Rewrite the card's headline (max 300 chars). Use when the title states "
                   "something that turned out to be WRONG — a corrected note under a false "
                   "headline still reads as false at a glance, because the title is all the "
                   "board shows.")
@click.option("--rationale", default=None)
@click.option("--source-url", default=None)
@click.option("--plan", default=None)
@click.option("--status", default=None)
@click.option("--assigned", default=None, help="Who the next action waits on. Max 120 chars.")
@click.option("--next-action", default=None,
              help="The single concrete next step, verb-first. Max 300 chars — over-length "
                   "is REJECTED, never truncated.")
@click.option("--owner", default=None, help="The human who owns the outcome. Max 120 chars.")
@click.option("--notes", default=None,
              help="REPLACE the card's notes wholesale. To add a turn's log without "
                   "restating the history, use --append-notes.")
@click.option("--append-notes", default=None,
              help="ADD text to the end of the card's notes, keeping what is there "
                   "(separated by a blank line). The common case — a turn logs what it did.")
@click.option("--score", default=None, help="Self-grade captured at completion — e.g. A-, B+, 4/5.")
@click.option("--review", default=None, help="One-line self-review captured when the task was done.")
@click.option("--links", default=None,
              help='REPLACE the card\'s links: "label|url, label2|url2" (bare urls OK). '
                   "Pass \"\" to clear them. To add one without restating the set, use "
                   "--append-link.")
@click.option("--append-link", multiple=True,
              help='ADD one link, keeping the existing ones: "label|url". Repeat it to add '
                   "several. The common case — a turn attaches the artifact it just "
                   "produced. A url already on the card is not duplicated.")
@click.option("--project", "project", default=None, metavar="EXT_ID_OR_NAME",
              help="File this task into a project (P<N> ext_id or its exact name). Pass \"\" "
                   "to take it out of one; omitting it leaves the filing alone.")
@click.option("--parent", "parent", default=None, metavar="T<N>",
              help="Make this a subtask of T<N> (same project; at most three levels). Pass \"\" "
                   "to make it top-level again; omitting it leaves it alone.")
@click.option("--append-quote", default=None,
              help="ADD a person's VERBATIM words to the task's human why (kept apart from "
                   "--rationale, which is yours). Needs --quote-by. Never a paraphrase.")
@click.option("--quote-by", default="", metavar='"Name <email>"',
              help="Who said the --quote / --append-quote: \"Name <email>\", a bare email or a name.")
@click.option("--quote-url", default="", help="Where they said it: the thread, doc comment or session.")
@click.option("--quote-at", default="", help="When they said it (ISO date or datetime).")
def agent_set(slug, task_id, links, append_link, append_notes, project, parent, append_quote,
              quote_by, quote_url, quote_at, **fields):
    """Patch a task (store rationale/source/plan/status/score/review/links/…).

    Score a task WHEN you mark it done (--status done --score --review) so a manager
    sync reads the completion grade instead of re-grading later."""
    append_link = list(append_link or ()) or None  # multiple=True: () when not passed
    if links is not None and append_link is not None:
        raise click.ClickException("pass --links (replace) or --append-link (add), not both")
    if fields.get("notes") is not None and append_notes is not None:
        raise click.ClickException("pass --notes (replace) or --append-notes (add), not both")
    # Same caps, same rejection, as `agent add` — checked before the network call so the
    # caller gets the field name and the overage instead of the server's 422.
    for name, value in list(fields.items()):
        if value is not None and name in TASK_FIELD_LIMITS:
            fields[name] = check_task_field(name, value)
    if fields.get("status") is not None:
        fields["status"] = check_task_status(fields["status"])
    try:
        client = _client(slug)
        # The ext_id IS the address — no board read just to resolve it.
        task_id = str(task_id).strip()
        if project is not None:
            # "" is a deliberate un-filing, so it skips resolution; anything else
            # has to name a project that exists.
            fields["project"] = resolve_project_ref(client, project) if project.strip() else ""
        if parent is not None:
            fields["parent"] = parent.strip()
        if append_quote is not None:
            entry = human_why_entry(append_quote, quote_by, quote_url, quote_at)
            current = list(client.get_task(task_id).get("human_why") or [])
            fields["human_why"] = current + [entry]
        if links is not None:
            fields["links"] = parse_task_links(links)
        elif append_link is not None:
            fields["links"] = _appended_links(client, task_id, append_link)
        if append_notes is not None:
            fields["notes"] = _appended_notes(client, task_id, append_notes)
        _emit(client.patch_task(task_id, **fields))
    except CanopyError as e:
        if f"/tasks/{task_id}/ -> 404" in str(e):
            raise _no_such_task(task_id)
        raise click.ClickException(str(e))
    except RuntimeError as e:
        raise click.ClickException(str(e))


def _task_by_ref(client, ref):
    """The task with ext_id `ref` from the board listing, or {} when there is none."""
    want = str(ref).strip().casefold()
    return next((t for t in client.list_tasks()
                 if str(t.get("ext_id") or "").strip().casefold() == want), {})


def _appended_links(client, task_id, spec):
    """Existing links + the parsed `spec`, de-duplicated on url (first label wins).

    Read-modify-write: the board's PATCH replaces `links` wholesale, so appending has
    to start from what is already on the card. Without this, the only way to add one
    link is to restate every link the card already had.
    """
    return _merge_links(list(_task_by_ref(client, task_id).get("links") or []), spec)


def _merge_links(current, spec):
    """`current` links + the parsed `spec` (one string, or a list from a repeated
    --append-link), de-duplicated on url (first label wins). Shared by a task's and a
    project's --append-link.

    `spec` became a list on 2026-10-22: the option took ONE value, so a turn passing
    `--append-link A --append-link B` got exit 0 and only B — click keeps the last
    value of a repeated single option, without a word."""
    specs = [spec] if isinstance(spec, str) else list(spec)
    seen = {str(l.get("url") or "").strip() for l in current}
    for link in (link for s in specs for link in parse_task_links(s)):
        if link["url"] not in seen:
            current.append(link)
            seen.add(link["url"])
    return current


def _appended_notes(client, task_id, text):
    """The card's existing notes + `text`, separated by a blank line.

    Read-modify-write for the same reason as `_appended_links`: the PATCH replaces
    `notes` wholesale, and a multi-turn task's notes ARE its history. The only append
    path used to be reading the card, concatenating by hand and passing the whole thing
    back through --notes. Passing just the new entry there instead succeeds and silently
    deletes every earlier turn's log.
    """
    current = (_task_by_ref(client, task_id).get("notes") or "").rstrip()
    addition = text.strip()
    if not addition:
        raise click.ClickException("--append-notes needs non-empty text")
    return f"{current}\n\n{addition}" if current else addition


TASK_STATUSES = ("suggested", "backlog", "todo", "in_progress", "done", "declined")


def _task_status_token(s):
    """The board token a human spelling names, or None when it names none of them.

    "blocked"/"waiting" are not a status — waiting on a person is expressed by `assigned`
    being that person; such items are still in progress on the outcome.
    """
    s = (s or "").strip().lower().replace("-", " ").replace("_", " ")
    if s in ("done", "complete", "completed", "shipped", "closed"):
        return "done"
    if s in ("declined", "rejected", "dropped", "wontfix", "won't do", "cancelled", "canceled"):
        return "declined"
    if s in ("in progress", "doing", "wip", "active", "started", "ongoing",
             "blocked", "waiting", "on hold", "hold", "stuck"):
        return "in_progress"
    if s in ("todo", "to do", "next", "up next", "accepted"):
        return "todo"
    if s in ("backlog", "later", "not now", "someday"):
        return "backlog"
    if s in ("suggested", "proposed", "triage"):
        return "suggested"
    return None


def normalize_task_status(s):
    """Human text ("In progress") AND canonical tokens ("in_progress") → the board's vocabulary.

    LENIENT — for READING statuses (filters, cards already on the board): anything
    unrecognised reads as "suggested". Never use it on a value you are about to WRITE;
    that is `check_task_status`.
    """
    return _task_status_token(s) or "suggested"


def check_task_status(value):
    """The board token for `value`, or raise `ClickException` — the strict twin for WRITES.

    The lenient fallback above is wrong on a write path: an unrecognised status became
    "suggested", so `agent set --status <typo>` demoted a live in-progress card to an
    unaccepted proposal and reported success. The server coerces the same way, so the
    raw value was no safer (dimagi-internal/canopy#659). Same contract as
    `check_task_field`: reject, write nothing, name the valid values.
    """
    token = _task_status_token(value)
    if token is None:
        raise click.ClickException(
            f"--status {value!r} is not a board status. Use one of: "
            f"{', '.join(TASK_STATUSES)} (human synonyms like 'blocked' → in_progress "
            f"are accepted). Nothing was written."
        )
    return token


# The board's per-field caps, MIRRORING canopy-web `apps/agents/schemas.py`
# (AgentTaskIn / AgentTaskPatch). The server is authoritative; this table exists so the
# CLI can reject an over-length field with a message naming the knob, instead of either
# discarding the tail locally or relaying a `string_too_long` 422 the caller must decode.
# Keep it in sync when the schema moves.
TASK_FIELD_LIMITS = {
    "ext_id": 64,
    "title": 300,
    "next_action": 300,
    "owner": 120,
    "assigned": 120,
    "confidence": 10,
    "score": 8,
    "source_url": 500,
    "origin": 32,
    "link label": 200,
    "link url": 500,
}

# Which CLI knob writes each field, so the error names what to shorten.
_TASK_FIELD_OPTION = {
    "ext_id": "--ext-id",
    "title": "--title",
    "next_action": "--next-action",
    "owner": "--owner",
    "assigned": "--assigned",
    "confidence": "--confidence",
    "score": "--score",
    "source_url": "--source-url",
    "link label": "--links / --append-link (the label before the `|`)",
    "link url": "--links / --append-link (the url after the `|`)",
}


def check_task_field(name, value):
    """Return `value` stripped, or raise `ClickException` if it exceeds the board's cap.

    Reject rather than truncate. A silently-shortened `next_action` still reads as
    complete on the kanban card — no ellipsis, nothing errored — so the agent that wrote
    it believes the whole instruction is recorded and the next turn acts on one whose
    final clause (often the actual constraint) is gone. Being told to shorten costs one
    retry; losing the tail of an instruction is undetectable and permanent.

    This is also what makes the writers agree: `agent add` used to truncate here while
    `agent set` passed the text through to a 422 — same cap, opposite failure modes
    (dimagi-internal/canopy#510).
    """
    limit = TASK_FIELD_LIMITS.get(name)
    text = (value or "").strip()
    if limit is None or len(text) <= limit:
        return text
    option = _TASK_FIELD_OPTION.get(name, f"--{name.replace('_', '-')}")
    raise click.ClickException(
        f"{option} is {len(text)} characters; the board caps {name} at {limit}. "
        f"Cut {len(text) - limit}. Nothing was written — the field was rejected, "
        f"not truncated."
    )


def preview_for_card(text, limit):
    """First `limit` chars of `text`, explicitly marked when it was cut.

    Unlike the capped fields above, shortening is CORRECT here: the card's notes are a
    preview of a dispatch brief the agent receives in full through the turn payload.
    What was wrong was cutting it invisibly — an unmarked truncation reads as the whole
    brief. The marker is the difference between "a short brief" and "a brief whose tail
    is missing".
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n\n[… truncated — the agent received the full brief]"


def _split_task_links(cell):
    """Split a `--links` cell into one string per link, keeping commas that aren't separators.

    Each comma-separated fragment either STARTS a link — it contains `|` or begins with
    `http` — or continues a neighbour:

    - a fragment with no whitespace at all — not even after the comma — that follows a
      URL is part of that URL (a query string like `?ids=1,2`). URLs carry no spaces,
      so `url, Bad=url` is still a separator followed by a bad part, and still raises;
    - any other fragment is the front of the NEXT link's label (`A (x, y)|url`).

    Where the grammar is ambiguous (`a|https://x,b,c|https://y`) the URL wins: the
    whitespace-free `b` stays on the first URL. A broken URL is a dead link; a label
    missing a comma is merely less pretty.

    A label with no `label|url` after it to join is returned whole as its own part, so
    `parse_task_links` still raises on it, naming the full label — the #579 guard is
    unchanged.
    """
    parts, pending = [], []
    for frag in cell.split(","):
        s = frag.strip()
        if "|" in s or s.startswith("http"):
            if pending and "|" in s:
                frag = ",".join(pending + [frag])
            elif pending:
                parts.append(",".join(pending))
            pending = []
            parts.append(frag)
        elif not s and not pending:
            continue
        elif (not pending and not any(c.isspace() for c in frag)
              and parts and parts[-1].split("|", 1)[-1].strip().startswith("http")):
            parts[-1] += "," + frag
        else:
            pending.append(frag)
    if pending:
        parts.append(",".join(pending))
    return parts


def parse_quote_by(spec: str) -> dict:
    """`"Jonathan Jackson <jj@dimagi.com>"` -> {name, email}; a bare address or a bare
    name fills the one it is."""
    import re

    spec = (spec or "").strip()
    m = re.fullmatch(r"(.*?)\s*<\s*([^<>\s]+@[^<>\s]+)\s*>", spec)
    if m:
        return {"name": m.group(1).strip().strip('"'), "email": m.group(2).lower()}
    if re.fullmatch(r"[^\s@]+@[^\s@]+", spec):
        return {"name": "", "email": spec.lower()}
    return {"name": spec, "email": ""}


def human_why_entry(quote: str, by: str, url: str = "", at: str = "") -> dict:
    """One `human_why` entry for the API: the person's words, verbatim, and who/where/when."""
    quote = (quote or "").strip()
    if not quote:
        raise click.ClickException("the quote is empty — record their words, or leave it off")
    who = parse_quote_by(by)
    if not who["name"] and not who["email"]:
        raise click.ClickException("--quote-by is required: whose words are these? "
                                   "(\"Name <email>\", an email, or a name)")
    entry = {"quote": quote, "who": who, "source_url": (url or "").strip()}
    if (at or "").strip():
        # A bare date is a day, not a moment: noon UTC is that same date in every
        # timezone the board renders in (midnight UTC showed as the day BEFORE in the US).
        entry["said_at"] = at.strip() if "T" in at else f"{at.strip()}T12:00:00Z"
    return entry


def parse_task_links(cell):
    """`"label|url, label2|url2"` → [{label, url}, …]; bare http urls get label "link".

    Over-length parts raise (see `check_task_field`): a URL cut to fit is a dead link,
    which is a worse outcome than being asked to shorten it.

    An unparseable part RAISES rather than being skipped. It used to fall off the end of
    the if/elif and vanish, so `--append-link "label=url"` — the wrong separator, and the
    one people reach for first — wrote nothing, returned the patched task as JSON, and
    exited 0. A write that reports success and stores nothing is the worst shape a CLI
    has: the caller has no reason to re-read the card, so the link is simply missing
    later, with no error anywhere to explain it. (2026-09-01, hal: two links onto a board
    task, both silently dropped; caught only because the card was re-read for an unrelated
    reason.)

    Commas inside a label or a URL are kept. A naive `split(",")` cut
    `"Slide deck (Dimagi brand, canonical)|https://…"` in half and rejected the first
    half as an unparseable link, so the only way to write that label was to drop its
    commas (2026-09-27 and 2026-09-30, eva — both retried without commas). See
    `_split_task_links` for how a comma is assigned to a label, a URL, or a separator.
    """
    out = []
    for part in _split_task_links(cell or ""):
        part = part.strip()
        if not part:
            continue
        if "|" in part:
            label, url = part.split("|", 1)
            out.append({"label": check_task_field("link label", label),
                        "url": check_task_field("link url", url)})
        elif part.startswith("http"):
            out.append({"label": "link", "url": check_task_field("link url", part)})
        else:
            raise click.ClickException(
                f"cannot parse link {part!r}: expected \"label|url\" (a pipe, not '=' or ':') "
                "or a bare http(s) url. Nothing was written."
            )
    return out


def next_task_ext_id(tasks):
    """Next free T<N> given the board's current tasks, so adds don't collide."""
    import re

    mx = 0
    for t in tasks or []:
        m = re.match(r"^T(\d+)$", str(t.get("ext_id") or "").strip())
        if m:
            mx = max(mx, int(m.group(1)))
    return f"T{mx + 1}"


@agent.command("add")
@click.option("--slug", required=True)
@click.option("--title", required=True, help="The card's one-line headline (max 300 chars).")
@click.option("--ext-id", default=None,
              help="Stable id (default: next free T<N> from the board). Max 64 chars.")
@click.option("--next-action", default="",
              help="The single concrete next step, verb-first. Max 300 chars — over-length "
                   "is REJECTED, never truncated, so a card never shows a half instruction.")
@click.option("--status", default="suggested",
              help="suggested (default) / backlog / todo / in_progress / done / declined — human synonyms accepted.")
@click.option("--owner", default="",
              help="The human stakeholder who owns the outcome — never the agent. Max 120 chars.")
@click.option("--assigned", default="",
              help="Who the next action waits on (the agent, or a person). Max 120 chars.")
@click.option("--confidence", default="", help="high / low, for suggested items.")
@click.option("--due", default=None, help="YYYY-MM-DD.")
@click.option("--links", default="", help='"label|url, label2|url2" (bare urls OK).')
@click.option("--notes", default="")
@click.option("--project", default="", metavar="EXT_ID_OR_NAME",
              help="File the task into a project (P<N> ext_id or its exact name) — the "
                   "same work its `Projects/<name>` Drive folder holds. Leave it off for "
                   "a genuine one-off.")
@click.option("--parent", default="", metavar="T<N>",
              help="Create it as a subtask of T<N>: it joins T<N>'s project (subtasks, not "
                   "subprojects — one board per product area). At most three levels deep.")
@click.option("--quote", default="",
              help="The person's VERBATIM words this task answers — its human why, shown above "
                   "your rationale. Needs --quote-by. Leave it off rather than paraphrase.")
@click.option("--quote-by", default="", metavar='"Name <email>"',
              help="Who said the --quote / --append-quote: \"Name <email>\", a bare email or a name.")
@click.option("--quote-url", default="", help="Where they said it: the thread, doc comment or session.")
@click.option("--quote-at", default="", help="When they said it (ISO date or datetime).")
def agent_add(slug, title, ext_id, next_action, status, owner, assigned, confidence, due,
              links, notes, project, parent, quote, quote_by, quote_url, quote_at):
    """Create ONE task on the board (auto-assigns the next T<N>).

    Safe to repeat: the create is keyed `<slug>:<ext_id>`, so re-running it hands back
    the task already made instead of a duplicate — and does NOT change it (`agent set`)."""
    import re

    conf = confidence.strip().lower()
    due = (due or "").strip()
    # Validate before opening a client: an over-length field should cost no round-trip.
    title = check_task_field("title", title)
    next_action = check_task_field("next_action", next_action)
    owner = check_task_field("owner", owner)
    assigned = check_task_field("assigned", assigned)
    ext_id = check_task_field("ext_id", ext_id) if ext_id else ext_id
    status = check_task_status(status)
    task_links = parse_task_links(links)
    human_why = [human_why_entry(quote, quote_by, quote_url, quote_at)] if quote.strip() else []
    try:
        client = _client(slug)
        # Resolved BEFORE the task is built: an unknown project must cost nothing,
        # not leave a task filed nowhere with a success on stdout.
        filing = resolve_project_ref(client, project) if project.strip() else ""
        task = {
            "ext_id": ext_id or next_task_ext_id(client.list_tasks()),
            "project": filing,
            "title": title,
            "next_action": next_action,
            "status": status,
            "owner": owner,
            "assigned": assigned,
            "confidence": conf if conf in ("high", "low") else "",
            "due": due if re.match(r"^\d{4}-\d{2}-\d{2}$", due) else None,
            "links": task_links,
            "notes": notes.strip(),
            "origin": "task-tracker",
        }
        if parent.strip():
            task["parent"] = parent.strip()
        if human_why:
            task["human_why"] = human_why
        created = client.create_tasks([task])
        _emit({"added": (created[0].get("ext_id") if created else None) or task["ext_id"],
               "result": created})
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))


def turn_mode_from_envelope(caller_path) -> dict | None:
    """THIS turn's mode from the caller envelope, or None to use the agent's.

    canopy-web decides the mode per turn when it is claimed — a routing rule such
    as "email from beth -> auto" overrides the agent-wide switch for its own work
    — and writes the decision into the envelope as `turn_mode: {mode, basis}`.
    None whenever there is nothing trustworthy to read: no file (a turn started
    by hand), an unreadable one, or an envelope from a canopy-web that predates
    per-turn modes. The caller then falls back to the agent-wide read, which is
    what it did before this existed.
    """
    if not caller_path:
        return None
    try:
        env = json.loads(Path(caller_path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    tm = env.get("turn_mode") if isinstance(env, dict) else None
    if not isinstance(tm, dict) or tm.get("mode") not in ("manual", "auto"):
        return None
    from orchestrator.caller import normalize_profile, normalize_relationship

    out = {"turn_mode": tm["mode"], "basis": str(tm.get("basis") or ""), "source": "turn"}
    # Who asked and what they reach, in envelope VERSION 2 words whatever the
    # envelope's version (`caller` → `contact`, `restricted` → `confined`), so the
    # turn opening can state them; terms: canopy-web docs/architecture/access.md.
    if env.get("relationship") is not None:
        out["relationship"] = normalize_relationship(env.get("relationship"))
    if env.get("profile") is not None:
        out["profile"] = normalize_profile(env.get("profile"))
    return out


@agent.command("mode")
@click.option("--slug", required=True)
@click.option("--caller", "caller_path", default="",
              help="The caller envelope the runner passed as `--caller <path>`. When it "
                   "carries this turn's mode, that wins over the agent-wide switch.")
def agent_mode(slug, caller_path):
    """Print the turn mode — {"slug", "turn_mode": "manual"|"auto", "basis", "source"}.

    With `--caller`, THIS turn's mode as canopy-web decided it at claim
    (`source: "turn"`, `basis` saying which routing rule or "agent"), plus who asked
    (`relationship`: owner | admin | member | contact | system) and what they reach
    (`profile`: full | confined) in envelope VERSION 2 words whatever the envelope's
    version (canopy-web docs/architecture/access.md). Otherwise,
    or when the envelope carries no mode, the agent-wide switch (`source:
    "agent"`) — board-side state flipped from the agent's Settings on canopy-web,
    never a repo file. The turn procedure reads this at preflight; if the call
    fails, the turn runs MANUAL (fail safe) and says so.
    """
    from_turn = turn_mode_from_envelope(caller_path)
    if from_turn is not None:
        _emit({"slug": slug, **from_turn})
        return
    try:
        _emit({"slug": slug, "turn_mode": _client(slug).turn_mode(),
               "basis": "agent", "source": "agent"})
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))


@agent.command("health")
@click.option("--slug", default="", help="One agent; omit to sweep the whole registered fleet.")
@click.option("--stale-needs-you-days", default=7.0, show_default=True, type=float,
              help="Flag needs-you items older than this")
@click.option("--stale-inbox-days", default=3.0, show_default=True, type=float,
              help="Flag unread inbox threads older than this")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def agent_health(slug, stale_needs_you_days, stale_inbox_days, as_json):
    """Work-state readiness for an agent's NEXT turn (or the whole fleet).

    The complement of `canopy agent doctor`: doctor asks "can this machine run
    the agent" (setup); health asks "is the agent's workload in a healthy state"
    — stale needs-you items on the board, stuck/failed harness turns, and unread
    inbox junk that would pollute inbox-triage. Turn recency is reported as info
    only (turn packaging is manual — never a readiness flag). Read-only; emits
    facts + deterministic junk SIGNALS (verdicts are the caller's job).
    Exits non-zero if any probed agent is not ready.
    """
    from orchestrator.agent_health import run_agent_health

    try:
        out = run_agent_health(slug or None,
                               stale_needs_you_days=stale_needs_you_days,
                               stale_inbox_days=stale_inbox_days)
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))

    if as_json:
        click.echo(json.dumps(out, indent=2))
    else:
        for a in out["agents"]:
            mark = "OK  " if a["ready"] else "FLAG"
            flags = ", ".join(a["flags"]) or "-"
            n_unread = len(a["inbox"]["unread"])
            n_junky = sum(1 for u in a["inbox"]["unread"] if u["junk_signals"])
            age = a["board"]["turn_age_days"]
            last_turn = "never" if age is None else f"{age}d ago"
            click.echo(f"[{mark}] {a['agent']:<8} flags: {flags}")
            click.echo(f"        last turn: {last_turn}  •  "
                       f"needs-you: {len(a['board']['needs_you'])} "
                       f"({sum(1 for i in a['board']['needs_you'] if i['stale'])} stale)  •  "
                       + ("inbox: hosted by canopy-web" if a["inbox"].get("hosted") else
                          f"unread: {n_unread} ({n_junky} junk-signaled)")
                       + (f"  •  inbox error: {a['inbox']['error']}" if a["inbox"]["error"] else ""))
            for line in a.get("keyring") or []:
                click.echo(f"        {line}")
        click.echo()
        n_bad = sum(1 for a in out["agents"] if not a["ready"])
        click.echo(f"All {len(out['agents'])} agent(s) ready for their next turn."
                   if out["ok"] else f"{n_bad}/{len(out['agents'])} agent(s) flagged — details above.")
    if not out["ok"]:
        raise SystemExit(1)


@agent.command("coverage")
@click.option("--slug", default="", help="One agent; omit to sweep the whole registered fleet.")
@click.option("--window-days", default=30, show_default=True, type=int,
              help="Transcript corpus window")
@click.option("--burst-gap-days", default=2, show_default=True, type=int,
              help="A gap of >= this many days splits one burst from the next")
@click.option("--min-bursts", default=2, show_default=True, type=int,
              help="Judge a skill only after it has lived through this many bursts")
@click.option("--decay-bursts", default=1, show_default=True, type=int,
              help="Silent for this many latest bursts (after firing) = decayed")
@click.option("--min-transcripts", default=3, show_default=True, type=int,
              help="Below this, negative claims degrade to insufficient_evidence")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def agent_coverage(slug, window_days, burst_gap_days, min_bursts, decay_bursts,
                   min_transcripts, as_json):
    """Bring-up coverage: how much of an agent's promised surface is LIVE yet.

    The longitudinal complement of `canopy agent health` (which asks whether the
    agent is ready for its NEXT turn). This asks what was declared and never fired,
    and what fired once and stopped. Opportunity is counted in BURSTS of activity,
    not wall-clock days -- the fleet works in short bursts, so a day-based gate
    hides the interesting skills. Read-only; emits facts + deterministic buckets
    (WHY a skill never fired is the caller's judgment, never this command's).
    """
    from orchestrator.agent_coverage import run_agent_coverage

    try:
        out = run_agent_coverage(slug or None, window_days=window_days,
                                 burst_gap_days=burst_gap_days, min_bursts=min_bursts,
                                 decay_bursts=decay_bursts,
                                 min_transcripts=min_transcripts)
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))

    if as_json:
        click.echo(json.dumps(out, indent=2))
        return

    for a in out["agents"]:
        if a.get("error"):
            click.echo(f"[ERR ] {a['agent']:<8} {a['error']}")
            continue
        buckets = {}
        for s in a["skills"]:
            buckets.setdefault(s["bucket"], []).append(s["name"])
        n_bursts = len(a["bursts"])
        corpus = a["corpus"]
        click.echo(f"\n=== {a['agent']} === {n_bursts} bursts / "
                   f"{corpus['transcripts']} transcripts in {a['window_days']}d"
                   + ("" if corpus["adequate"] else "  [thin corpus: negatives suppressed]"))
        if not a["persona"]["present"]:
            click.echo(f"  no persona.md ({len(a['skills'])} skills declared)")
        # Lead with decayed -- the sharpest bring-up signal.
        for bucket in ("decayed", "never_live", "live", "no_opportunity",
                       "insufficient_evidence", "sub_skill"):
            names = buckets.get(bucket)
            if names:
                click.echo(f"  {bucket:<22} {len(names):>3}  {', '.join(names[:6])}"
                           + (" …" if len(names) > 6 else ""))


@agent.command("dispatch")
@click.option("--slug", required=True, help="Agent to send (ace|ada|echo|eva|hal).")
@click.option("--title", default="",
              help="Board-task title — what the work IS, one line. Max 300 chars.")
@click.option("--prompt", default="", help="The brief the agent receives. Omit for a board drain.")
@click.option("--prompt-file", type=click.Path(exists=True, dir_okay=False), default=None,
              help="Read the brief from a file (for briefs too long to quote on a shell line).")
@click.option("--task", "task_ext_id", default=None,
              help="Attach to an EXISTING board task instead of creating one.")
@click.option("--no-task", is_flag=True, help="Dispatch without touching the board.")
@click.option("--links", default="", help='Evidence for the task: "label|url, label2|url2".')
@click.option("--next-action", default="",
              help="The single concrete next step, verb-first. Max 300 chars — over-length "
                   "is REJECTED, never truncated.")
@click.option("--idempotency-key", default=None,
              help="Override the derived key — (agent, title, day[, runner][, mode]), or "
                   "with no --title a hash of the whole prompt in the title's place, so a "
                   "different prompt is a different dispatch and an identical retry dedupes. "
                   "Pass a fresh key to deliberately re-dispatch the same work.")
@click.option("--runner", "runner_ref", default="",
              help="Pin the turn to ONE runner, by name (e.g. jj-mbp-cdp) or id — for work "
                   "only that box can do. Only it may claim the turn. Refused if the runner "
                   "is unknown/ambiguous/retired or does not list the agent in "
                   "capabilities.agents; see --queue-if-not-ready for paused/offline.")
@click.option("--queue-if-not-ready", is_flag=True,
              help="With --runner: enqueue even though the runner is paused/stale/offline. "
                   "The server holds a pinned turn QUEUED (it never expires) and the runner "
                   "claims it once back online. Without this flag such a pin is refused.")
@click.option("--mode", "turn_mode", type=click.Choice(["auto", "manual"]), default=None,
              help="Run THIS turn in auto or manual mode, above every routing rule and the "
                   "agent's own switch. manual: anyone who may dispatch. auto: only the "
                   "agent's owner or an admin (workspace owners included), on a session or "
                   "your own PAT — canopy-web refuses anyone else (403). Omit to let the "
                   "agent's rules decide.")
@click.option("--as", "acts_as", type=click.Choice(["editor", "viewer"]), default=None,
              help="Run THIS turn as a lower role than yours: whether it may start, its "
                   "mode, its access and its credential reads are all judged as an editor "
                   "or a viewer. Only lowers, so anyone may ask; it ends with the turn. How "
                   "to run the non-admin path for real.")
@click.option("--on-behalf-of", "on_behalf_of", default="",
              help="In an agent's turn: send this as the agent acting FOR the person who "
                   "asked — their email, or 'caller' for this turn's verified asker — so "
                   "canopy-web judges the runner pin and --mode auto on THEIR access, not "
                   "the agent's. Checked against the turn this session's login serves; a "
                   "wrong person, a stored PAT or a finished turn is refused. Omit to act "
                   "as the agent itself.")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def agent_dispatch(slug, title, prompt, prompt_file, task_ext_id, no_task, links,
                   next_action, idempotency_key, runner_ref, queue_if_not_ready, turn_mode,
                   acts_as, on_behalf_of, as_json):
    """Hand work to an agent to do UNATTENDED: record it on the agent's board, then a
    runner session does it.

    \b
    To work with an agent yourself — ask it questions, follow up, review what it
    says — do not dispatch: run `canopy agent use <slug>` once, then ask in your
    own Claude Code session. Dispatch starts a fresh session on a runner each time,
    with no memory of the last one; to follow up on one, use
    `canopy agent reply <turn-id> --prompt ...` with the turn id this prints.

    The one-shot counterpart to a schedule: schedules are for recurring work, this is
    "go do this now". The runner claims the turn and spawns a visible emdash session
    you can watch and interrupt.

    Reports the result as LAUNCHED (unverified) — a harness turn flips to `done` within
    seconds carrying "created session '<name>'", which is the runner finishing, not the
    agent's work succeeding. Verify with `canopy agent turns --slug <agent>`.

    --runner pins the turn to one box (default: any runner serving the agent). The pin
    is checked BEFORE the board is touched, so a refused pin leaves nothing behind.

    --mode auto|manual chooses this turn's mode explicitly (default: the agent's routing
    rules, then its own switch). auto is for the agent's owner/admins only.
    """
    import datetime as _dt

    from orchestrator import canopy_web
    from orchestrator.provenance import on_behalf_headers
    from orchestrator.agent_dispatch import (
        RUNNERS_PATH,
        DispatchError,
        TURNS_PATH,
        build_turn_payload,
        check_runner_pin,
        derive_idempotency_key,
        github_links_in,
        local_agent_slug,
        reply_hint,
        resolve_runner,
        summarize_turn,
    )

    if prompt_file:
        prompt = Path(prompt_file).read_text(encoding="utf-8")
    make_task = not no_task and not task_ext_id
    if make_task and not title.strip():
        raise click.ClickException(
            "--title is required to create the board task (or pass --no-task / --task <ext_id>)")
    # Validate the card's fields before dispatching: a rejected board write after the
    # runner was triggered would leave a live turn with no task to find.
    title = check_task_field("title", title)
    next_action = check_task_field("next_action", next_action or "Work this dispatch")
    task_links = parse_task_links(links)
    task_links += github_links_in(prompt, task_links)
    if queue_if_not_ready and not runner_ref.strip():
        raise click.UsageError("--queue-if-not-ready only applies with --runner")
    try:  # before the board: a turn with no person to act for must not leave a card
        behalf = on_behalf_headers(on_behalf_of)
    except RuntimeError as e:
        raise click.ClickException(str(e))

    # Resolve and vet the pin FIRST: a refusal after the board write would leave a card
    # for work that was never sent anywhere.
    pinned = None
    pin_notes: list[str] = []
    if runner_ref.strip():
        try:
            rows = canopy_web.call("GET", RUNNERS_PATH) or []
            if isinstance(rows, dict):
                rows = rows.get("items") or rows.get("results") or []
            pinned = resolve_runner(rows, runner_ref)
        except DispatchError as e:
            raise click.ClickException(str(e))
        except (CanopyError, RuntimeError) as e:
            raise click.ClickException(f"could not list runners to resolve --runner: {e}")
        problems, not_ready, warnings = check_runner_pin(pinned, slug)
        if problems:
            raise click.ClickException(
                "refusing to pin: " + "; ".join(problems) + ". Nothing was dispatched.")
        if not_ready and not queue_if_not_ready:
            raise click.ClickException(
                "refusing to pin: " + "; ".join(not_ready)
                + ". Pass --queue-if-not-ready to enqueue it anyway. Nothing was dispatched.")
        pin_notes = not_ready + warnings
        if not as_json:  # JSON carries them in `runner.warnings` instead
            for note in pin_notes:
                click.echo(f"warning: {note}", err=True)

    day = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    key = idempotency_key or derive_idempotency_key(
        slug, title, day, runner_id=str(pinned["id"]) if pinned else "",
        mode=(turn_mode or "") + (f"+as-{acts_as}" if acts_as else ""), prompt=prompt)

    try:
        client = _client(slug)
        # Board first: the agent must find the task already there when it arrives.
        if make_task:
            task_ext_id = next_task_ext_id(client.list_tasks())
            client.create_tasks([{
                "ext_id": task_ext_id,
                "title": title,
                "next_action": next_action,
                "status": "in_progress",
                "assigned": slug,
                "links": task_links,
                "notes": preview_for_card(prompt, 2000),
                "origin": "dispatch",
            }])

        # No agent repo underfoot means a person is dispatching from their own Claude Code:
        # name them, or the footer cannot say who sent the work.
        person = "" if local_agent_slug() else _dispatching_person(canopy_web)
        payload = build_turn_payload(slug, prompt=prompt, idempotency_key=key,
                                     task_ext_id=task_ext_id,
                                     runner_id=str(pinned["id"]) if pinned else None,
                                     turn_mode=turn_mode, person=person, acts_as=acts_as)
        turn = canopy_web.call("POST", TURNS_PATH, payload, headers=behalf or None)
    except DispatchError as e:
        raise click.ClickException(str(e))
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))

    summary = summarize_turn(turn)
    # What the server RECORDED, not what we asked for: an older canopy-web ignores the
    # field, and saying "auto" when it was dropped is the exact falsehood to avoid.
    turn = turn or {}
    mode_out = {"requested": turn_mode,
                "recorded": turn.get("requested_turn_mode") if "requested_turn_mode" in turn
                else None}
    # Who the server RECORDED this was done for (never what we sent): None when acting
    # as the agent itself, or on a canopy-web that does not know the header.
    behalf_out = (turn.get("origin_ref") or {}).get("on_behalf_of") if behalf else None
    runner_out = None
    if pinned:
        runner_out = {"id": pinned.get("id"), "name": pinned.get("name"),
                      "status": pinned.get("status"), "warnings": pin_notes}
    if as_json:
        _emit({"task_ext_id": task_ext_id, "idempotency_key": key, "turn": summary,
               "runner": runner_out, "mode": mode_out, "on_behalf_of": behalf_out})
        _note_local_use(slug, canopy_web)  # stderr: stdout stays pure JSON
        return

    click.echo(f"Agent:  {slug}")
    if behalf_out:
        click.echo(f"For:    {behalf_out.get('email')}  (on their access: "
                   f"{', '.join(behalf_out.get('decided') or []) or 'nothing it changed'})")
    if task_ext_id:
        click.echo(f"Task:   {task_ext_id}  {title.strip()}")
    if pinned:
        click.echo(f"Runner: {pinned.get('name')}  (pinned — only it may claim this turn; "
                   f"now {pinned.get('status')})")
    else:
        click.echo("Runner: (not pinned — the agent's runner order decides, which may be a "
                   "teammate's laptop; `canopy agent turns --slug "
                   f"{slug}` shows which one claimed it)")
    if turn_mode:
        rec = mode_out["recorded"]
        if rec == turn_mode:
            click.echo(f"Mode:   {turn_mode}  (requested for this turn — outranks the agent's rules)")
        elif rec is None:
            click.echo(f"Mode:   {turn_mode} requested, but the server did not report recording it "
                       "(older canopy-web?) — the agent's rules will decide", err=True)
        else:
            click.echo(f"Mode:   requested {turn_mode}, server recorded {rec!r}", err=True)
    else:
        click.echo("Mode:   (not requested — the agent's rules decide at claim)")
    click.echo(f"Turn:   {summary['id']}  →  {summary['headline']}")
    if summary.get("canopy_session_url"):
        click.echo(f"Session: {summary['canopy_session_url']}")
    click.echo("")
    click.echo("This is a LAUNCH, not a result — the agent may not have read the brief yet.")
    click.echo(f"  verify:  canopy agent turns --slug {slug}  (shows each turn's session link)")
    if summary.get("id"):
        click.echo(reply_hint(summary["id"]))
    _note_local_use(slug, canopy_web)


def _note_local_use(slug, canopy_web) -> None:
    """One stderr line steering a PERSON without local use of `slug` to `canopy agent use`
    (canopy-web#1955). Silent in an agent's turn; never fails or blocks the dispatch."""
    from orchestrator.agent_dispatch import local_use_note_for

    try:
        note = local_use_note_for(slug, canopy_web.call)
    except Exception:  # noqa: BLE001 — a hint is never worth failing a dispatch over
        note = ""
    if note:
        click.echo(note, err=True)


def _dispatching_person(canopy_web) -> str:
    """The canopy identity a person dispatches as, '' if it cannot be read. Never blocks."""
    try:
        me = canopy_web.call("GET", canopy_web.ME_PATH) or {}
    except Exception:  # noqa: BLE001 — a label is not worth failing a dispatch over
        return ""
    if not isinstance(me, dict):
        return ""
    return str(me.get("email") or me.get("username") or "").strip()


@agent.command("reply")
@click.argument("session")
@click.option("--prompt", default="", help="The follow-up to send into the session.")
@click.option("--prompt-file", type=click.Path(exists=True, dir_okay=False), default=None,
              help="Read the follow-up from a file.")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def agent_reply(session, prompt, prompt_file, as_json):
    """Send a follow-up into an EXISTING canopy session instead of starting a new one.

    SESSION is the turn id `canopy agent dispatch` printed, the canopy session's id, or
    its canopy-web link (the id is the UUID in the URL). A turn id is followed to the
    session that turn ran in; while the turn is still queued there is none yet, and this
    says so. The session keeps its memory of the earlier work, so a follow-up does not
    redo it — unlike a second `canopy agent dispatch`, which starts from nothing.
    Uses canopy-web's own send route (`POST /api/canopy-sessions/<id>/send`, the one
    canopy's chat and `canopy_sessions_send` use); you need write access to the session.
    """
    from orchestrator import canopy_web
    from orchestrator.agent_dispatch import DispatchError, reply_send_path

    if prompt_file:
        prompt = Path(prompt_file).read_text(encoding="utf-8")
    if not prompt.strip():
        raise click.UsageError("nothing to send: pass --prompt or --prompt-file")
    try:
        path = reply_send_path(session, canopy_web.call)
        out = canopy_web.call("POST", path, {"text": prompt}) or {}
    except DispatchError as e:
        raise click.ClickException(str(e))
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))
    out = out if isinstance(out, dict) else {}
    session_id = path.split("/")[3]
    turn_id = out.get("turn_id")
    if as_json:
        _emit({"session": session_id, "turn_id": turn_id,
               "message_id": (out.get("message") or {}).get("id")})
        return
    click.echo(f"Session: {session_id}")
    if turn_id:
        click.echo(f"Turn:    {turn_id}  (queued into that session — it keeps its earlier context)")
    else:
        click.echo("Turn:    (no new turn reported — the message may ride one already running there)")
    click.echo("This is a SEND, not a reply — read the answer on the session's canopy-web page.")


@agent.command("landscape")
@click.option("--slug", required=True, help="The conductor agent reading its tree (decides 'may run').")
@click.option("--workspace", default=None,
              help="Workspace whose tree to read (default: the root of --slug's own organisation).")
@click.option("--groups-file", type=click.Path(exists=True, dir_okay=False), default=None,
              help='JSON {"work_groups": [{"name", "keywords": [...]}, …]} — adds who each group needs.')
@click.option("--members", "members_only", is_flag=True, help="Print only the member list (needs --groups-file).")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def agent_landscape(slug, workspace, groups_file, members_only, as_json):
    """The agent landscape of ONE workspace tree: every agent in the workspace and its
    sub-workspaces, its owner, whether --slug may run it, its skills and recent turns
    (canopy-web agent-topology; admin and above). Any workspace's conductor agent uses the
    same command for its own tree. With --groups-file, also who each work group needs: an
    agent joins with >=2 skills NAMED for the group and only if --slug may run it."""
    from orchestrator import canopy_web
    from orchestrator import agent_landscape as L

    try:
        root = workspace or L.default_root(canopy_web.call, slug)
        land = L.fetch(canopy_web.call, slug, root)
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))
    sel = None
    if groups_file:
        groups = json.loads(Path(groups_file).read_text(encoding="utf-8")).get("work_groups") or []
        sel = L.select_members(land["agents"], groups, slug)
    if members_only:
        if sel is None:
            raise click.UsageError("--members needs --groups-file")
        click.echo(",".join(sel["members"]))
        return
    if as_json:
        _emit({**land, "selection": sel})
        return
    click.echo(L.render(land, slug, sel))


@agent.command("turns")
@click.option("--slug", required=True)
@click.option("--limit", default=10, type=int)
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def agent_turns(slug, limit, as_json):
    """Recent harness turns for an agent — how you check what a dispatch actually did.

    Statuses are reported through the same honest lens as `dispatch`: a `done` launch
    turn is `launched`, never `complete`. A turn whose runner has opened its canopy
    session shows that session's link; `canopy agent reply <turn-id>` follows up in it.
    """
    from orchestrator import canopy_web
    from orchestrator.agent_dispatch import summarize_turn

    try:
        rows = canopy_web.call("GET", f"/api/harness/turns/?agent={slug}") or []
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))
    if isinstance(rows, dict):
        rows = rows.get("items") or rows.get("results") or []
    rows = rows[:limit]

    if as_json:
        _emit([{**summarize_turn(t), "created_at": t.get("created_at"),
                "prompt": t.get("prompt")} for t in rows])
        return
    if not rows:
        click.echo(f"no harness turns for {slug}")
        return
    for t in rows:
        s = summarize_turn(t)
        click.echo(f"{str(t.get('created_at'))[:16]}  {s['state']:<9} {s['id']}")
        click.echo(f"    {s['headline']}")
        if s["canopy_session_id"]:
            click.echo(f"    session: {s['canopy_session_url'] or s['canopy_session_id']}"
                       f"  (follow up: canopy agent reply {s['id']} --prompt ...)")
        first = (str(t.get("prompt") or "").strip().splitlines() or [""])[0]
        if first:
            click.echo(f"    prompt: {first[:110]}")


@agent.command("session-token", hidden=True)
@click.option("--slug", required=True, help="The agent this session runs as.")
def agent_session_token_cmd(slug):
    """Print the login canopy-web issued for THIS laptop session as agent SLUG (tied to the
    session its turn opened; canopy-web spec 2026-10-21-turn-scoped-agent-token-design.md),
    or nothing. For the canopy-web MCP headers helper, which connects once per session and
    so needs a login that lasts it. Refuses to print to a terminal: it is a credential."""
    import sys

    from orchestrator import turn_agent_token

    if sys.stdout.isatty():
        raise click.ClickException("this prints a credential: pipe it to the program that needs it")
    token = turn_agent_token.resolve(slug, session_only=True)
    if token:
        click.echo(token)
