"""`canopy agent …` — thin CLI over AgentClient for shell-driven agents."""
import json
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
                   "Setting it on an already-registered agent MOVES it. Omit to leave "
                   "placement alone — which means a NEW agent lands in the default "
                   "workspace, where every @dimagi.com address is auto-admitted as an "
                   "editor and can therefore delete it.")
def agent_register(slug, name, email, description, persona, avatar_url, workspace):
    """Upsert agent identity."""
    try:
        c = _client(slug, name=name, email=email, description=description,
                    persona=persona, avatar_url=avatar_url, workspace=workspace)
        _emit(c.register())
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


@agent.command("work")
@click.option("--slug", required=True)
@click.option("--json", "json_file", required=True, type=click.Path(exists=True),
              help="JSON file: [{title,kind,url,description,tags,source}]")
def agent_work(slug, json_file):
    """Upsert work products from a JSON file."""
    try:
        items = json.load(open(json_file))
        _emit(_client(slug).put_work_products(items))
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
            items = json.load(open(json_file))
        else:
            raise click.ClickException("pass --from-repo or --json")
        _emit(_client(slug).put_skills(items))
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))


@agent.command("tasks-sync")
@click.option("--slug", required=True)
@click.option("--json", "json_file", required=True, type=click.Path(exists=True),
              help="JSON file: [{ext_id,title,next_action,status,owner,assigned,…}]")
def agent_tasks_sync(slug, json_file):
    """Non-destructive task upsert from a JSON file."""
    try:
        tasks = json.load(open(json_file))
        _emit(_client(slug).sync_tasks(tasks))
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))


@agent.command("commands")
@click.option("--slug", required=True)
def agent_commands(slug):
    """List board actions queued for the agent (drain on a turn)."""
    try:
        cmds = _client(slug).pending_commands()
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))
    if not cmds:
        click.echo("no queued commands")
        return
    for c in cmds:
        click.echo(f"  #{c.id} {c.kind} -> {c.task_title or '(no task)'}  [{c.created_by}]  {c.payload or ''}")


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
            status = "OK  " if r.ok else "FAIL"
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


# The statuses a board drain actually wants: everything not yet resolved. `normalize_task_status`
# maps the whole vocabulary onto four tokens, and the two below are the un-resolved pair.
OPEN_TASK_STATUSES = ("suggested", "in_progress")


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


@agent.command("apply")
@click.option("--slug", required=True)
@click.option("--id", "cmd_id", type=int, required=True)
@click.option("--note", default="")
def agent_apply(slug, cmd_id, note):
    """Mark a queued command applied."""
    try:
        _emit(_client(slug).apply_command(cmd_id, result_note=note))
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))


def resolve_task_id(client, task_id):
    """Accept either the numeric DB id or the board's own `T<N>` ext_id.

    The board only ever SHOWS you the ext_id — `agent add` reports it, the kanban card
    is labelled with it, and `agent turn --task` / `agent dispatch --task` both take it.
    The numeric id appears nowhere except a raw `agent tasks` dump, so requiring it here
    cost one failed call plus a JSON grep every time an agent patched a task.
    """
    raw = str(task_id).strip()
    if raw.isdigit():
        return int(raw)

    for task in client.list_tasks():
        if str(task.get("ext_id") or "").strip().casefold() == raw.casefold():
            return task["id"]

    raise click.ClickException(
        f"no task {raw!r} on this board — pass a T<N> ext_id or a numeric id "
        f"(`canopy agent tasks --slug …` lists both)"
    )


@agent.command("set")
@click.option("--slug", required=True)
@click.option("--task-id", required=True, metavar="ID_OR_EXT_ID",
              help="The board's T<N> ext_id (as shown on the card) or the numeric id.")
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
@click.option("--notes", default=None)
@click.option("--score", default=None, help="Self-grade captured at completion — e.g. A-, B+, 4/5.")
@click.option("--review", default=None, help="One-line self-review captured when the task was done.")
@click.option("--links", default=None,
              help='REPLACE the card\'s links: "label|url, label2|url2" (bare urls OK). '
                   "Pass \"\" to clear them. To add one without restating the set, use "
                   "--append-link.")
@click.option("--append-link", default=None,
              help='ADD one link, keeping the existing ones: "label|url". The common case — '
                   "a turn attaches the artifact it just produced. A url already on the card "
                   "is not duplicated.")
def agent_set(slug, task_id, links, append_link, **fields):
    """Patch a task (store rationale/source/plan/status/score/review/links/…).

    Score a task WHEN you mark it done (--status done --score --review) so a manager
    sync reads the completion grade instead of re-grading later."""
    if links is not None and append_link is not None:
        raise click.ClickException("pass --links (replace) or --append-link (add), not both")
    # Same caps, same rejection, as `agent add` — checked before the network call so the
    # caller gets the field name and the overage instead of the server's 422.
    for name, value in list(fields.items()):
        if value is not None and name in TASK_FIELD_LIMITS:
            fields[name] = check_task_field(name, value)
    try:
        client = _client(slug)
        task_id = resolve_task_id(client, task_id)
        if links is not None:
            fields["links"] = parse_task_links(links)
        elif append_link is not None:
            fields["links"] = _appended_links(client, task_id, append_link)
        _emit(client.patch_task(task_id, **fields))
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))


def _appended_links(client, task_id, spec):
    """Existing links + the parsed `spec`, de-duplicated on url (first label wins).

    Read-modify-write: the board's PATCH replaces `links` wholesale, so appending has
    to start from what is already on the card. Without this, the only way to add one
    link is to restate every link the card already had.
    """
    current = []
    for task in client.list_tasks():
        if task.get("id") == task_id:
            current = list(task.get("links") or [])
            break
    seen = {str(l.get("url") or "").strip() for l in current}
    for link in parse_task_links(spec):
        if link["url"] not in seen:
            current.append(link)
            seen.add(link["url"])
    return current


def normalize_task_status(s):
    """Human text ("In progress") AND canonical tokens ("in_progress") → the board's vocabulary.

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
    return "suggested"


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
    "source": 100,
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


def parse_task_links(cell):
    """`"label|url, label2|url2"` → [{label, url}, …]; bare http urls get label "link".

    Over-length parts raise (see `check_task_field`): a URL cut to fit is a dead link,
    which is a worse outcome than being asked to shorten it.
    """
    out = []
    for part in (cell or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "|" in part:
            label, url = part.split("|", 1)
            out.append({"label": check_task_field("link label", label),
                        "url": check_task_field("link url", url)})
        elif part.startswith("http"):
            out.append({"label": "link", "url": check_task_field("link url", part)})
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
              help="suggested (default) / in_progress / done / declined — human synonyms accepted.")
@click.option("--owner", default="",
              help="The human stakeholder who owns the outcome — never the agent. Max 120 chars.")
@click.option("--assigned", default="",
              help="Who the next action waits on (the agent, or a person). Max 120 chars.")
@click.option("--confidence", default="", help="high / low, for suggested items.")
@click.option("--due", default=None, help="YYYY-MM-DD.")
@click.option("--links", default="", help='"label|url, label2|url2" (bare urls OK).')
@click.option("--notes", default="")
def agent_add(slug, title, ext_id, next_action, status, owner, assigned, confidence, due, links, notes):
    """Create ONE task on the board (upsert via tasks/sync; auto-assigns the next T<N>)."""
    import re

    conf = confidence.strip().lower()
    due = (due or "").strip()
    # Validate before opening a client: an over-length field should cost no round-trip.
    title = check_task_field("title", title)
    next_action = check_task_field("next_action", next_action)
    owner = check_task_field("owner", owner)
    assigned = check_task_field("assigned", assigned)
    ext_id = check_task_field("ext_id", ext_id) if ext_id else ext_id
    task_links = parse_task_links(links)
    try:
        client = _client(slug)
        task = {
            "ext_id": ext_id or next_task_ext_id(client.list_tasks()),
            "title": title,
            "next_action": next_action,
            "status": normalize_task_status(status),
            "owner": owner,
            "assigned": assigned,
            "confidence": conf if conf in ("high", "low") else "",
            "due": due if re.match(r"^\d{4}-\d{2}-\d{2}$", due) else None,
            "links": task_links,
            "notes": notes.strip(),
            "source": "task-tracker",
        }
        result = client.sync_tasks([task])
        _emit({"added": task["ext_id"], "result": result})
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))


@agent.command("mode")
@click.option("--slug", required=True)
def agent_mode(slug):
    """Print the agent's turn mode — {"slug": ..., "turn_mode": "manual"|"auto"}.

    Board-side state (flipped from /agents/<slug> on canopy-web, or PATCH
    /api/agents/<slug>/turn-mode — never a repo file). The turn procedure reads
    this at preflight; if the call fails, the turn runs MANUAL (fail safe) and
    says so.
    """
    try:
        _emit({"slug": slug, "turn_mode": _client(slug).turn_mode()})
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
                       f"unread: {n_unread} ({n_junky} junk-signaled)"
                       + (f"  •  inbox error: {a['inbox']['error']}" if a["inbox"]["error"] else ""))
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
              help="Override the derived (agent, title, day) key — pass a fresh one to "
                   "deliberately re-dispatch the same work.")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def agent_dispatch(slug, title, prompt, prompt_file, task_ext_id, no_task, links,
                   next_action, idempotency_key, as_json):
    """Record work on an agent's board, then trigger a runner session to do it.

    The one-shot counterpart to a schedule: schedules are for recurring work, this is
    "go do this now". The runner claims the turn and spawns a visible emdash session
    you can watch and interrupt.

    Reports the result as LAUNCHED (unverified) — a harness turn flips to `done` within
    seconds carrying "created session '<name>'", which is the runner finishing, not the
    agent's work succeeding. Verify with `canopy agent turns --slug <agent>`.
    """
    import datetime as _dt

    from orchestrator import canopy_web
    from orchestrator.agent_dispatch import (
        DispatchError,
        TURNS_PATH,
        build_turn_payload,
        derive_idempotency_key,
        summarize_turn,
    )

    if prompt_file:
        prompt = Path(prompt_file).read_text()
    make_task = not no_task and not task_ext_id
    if make_task and not title.strip():
        raise click.ClickException(
            "--title is required to create the board task (or pass --no-task / --task <ext_id>)")
    # Validate the card's fields before dispatching: a rejected board write after the
    # runner was triggered would leave a live turn with no task to find.
    title = check_task_field("title", title)
    next_action = check_task_field("next_action", next_action or "Work this dispatch")
    task_links = parse_task_links(links)

    day = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    key = idempotency_key or derive_idempotency_key(slug, title or prompt[:80], day)

    try:
        client = _client(slug)
        # Board first: the agent must find the item already there when it arrives.
        if make_task:
            task_ext_id = next_task_ext_id(client.list_tasks())
            client.sync_tasks([{
                "ext_id": task_ext_id,
                "title": title,
                "next_action": next_action,
                "status": "in_progress",
                "assigned": slug,
                "links": task_links,
                "notes": preview_for_card(prompt, 2000),
                "source": "dispatch",
            }])

        payload = build_turn_payload(slug, prompt=prompt, idempotency_key=key,
                                     task_ext_id=task_ext_id)
        turn = canopy_web.call("POST", TURNS_PATH, payload)
    except DispatchError as e:
        raise click.ClickException(str(e))
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))

    summary = summarize_turn(turn)
    if as_json:
        _emit({"task_ext_id": task_ext_id, "idempotency_key": key, "turn": summary})
        return

    click.echo(f"Agent:  {slug}")
    if task_ext_id:
        click.echo(f"Task:   {task_ext_id}  {title.strip()}")
    click.echo(f"Turn:   {summary['id']}  →  {summary['headline']}")
    click.echo("")
    click.echo("This is a LAUNCH, not a result — the agent may not have read the brief yet.")
    click.echo(f"  verify:  canopy agent turns --slug {slug}")


@agent.command("turns")
@click.option("--slug", required=True)
@click.option("--limit", default=10, type=int)
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def agent_turns(slug, limit, as_json):
    """Recent harness turns for an agent — how you check what a dispatch actually did.

    Statuses are reported through the same honest lens as `dispatch`: a `done` launch
    turn is `launched`, never `complete`.
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
        first = (str(t.get("prompt") or "").strip().splitlines() or [""])[0]
        if first:
            click.echo(f"    prompt: {first[:110]}")
