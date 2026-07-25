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
def agent_register(slug, name, email, description, persona, avatar_url):
    """Upsert agent identity."""
    try:
        c = _client(slug, name=name, email=email, description=description,
                    persona=persona, avatar_url=avatar_url)
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


@agent.command("tasks")
@click.option("--slug", required=True)
def agent_tasks(slug):
    """List the agent's board tasks (JSON) — e.g. to compute the next ext_id."""
    try:
        _emit(_client(slug).list_tasks())
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


@agent.command("set")
@click.option("--slug", required=True)
@click.option("--task-id", type=int, required=True)
@click.option("--rationale", default=None)
@click.option("--source-url", default=None)
@click.option("--plan", default=None)
@click.option("--status", default=None)
@click.option("--assigned", default=None)
@click.option("--next-action", default=None)
@click.option("--owner", default=None)
@click.option("--notes", default=None)
@click.option("--score", default=None, help="Self-grade captured at completion — e.g. A-, B+, 4/5.")
@click.option("--review", default=None, help="One-line self-review captured when the task was done.")
def agent_set(slug, task_id, **fields):
    """Patch a task (store rationale/source/plan/status/score/review/…).

    Score a task WHEN you mark it done (--status done --score --review) so a manager
    sync reads the completion grade instead of re-grading later."""
    try:
        _emit(_client(slug).patch_task(task_id, **fields))
    except (CanopyError, RuntimeError) as e:
        raise click.ClickException(str(e))


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


def parse_task_links(cell):
    """`"label|url, label2|url2"` → [{label, url}, …]; bare http urls get label "link"."""
    out = []
    for part in (cell or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "|" in part:
            label, url = part.split("|", 1)
            out.append({"label": label.strip()[:200], "url": url.strip()[:500]})
        elif part.startswith("http"):
            out.append({"label": "link", "url": part[:500]})
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
@click.option("--title", required=True)
@click.option("--ext-id", default=None, help="Stable id (default: next free T<N> from the board).")
@click.option("--next-action", default="", help="The single concrete next step, verb-first.")
@click.option("--status", default="suggested",
              help="suggested (default) / in_progress / done / declined — human synonyms accepted.")
@click.option("--owner", default="", help="The human stakeholder who owns the outcome — never the agent.")
@click.option("--assigned", default="", help="Who the next action waits on (the agent, or a person).")
@click.option("--confidence", default="", help="high / low, for suggested items.")
@click.option("--due", default=None, help="YYYY-MM-DD.")
@click.option("--links", default="", help='"label|url, label2|url2" (bare urls OK).')
@click.option("--notes", default="")
def agent_add(slug, title, ext_id, next_action, status, owner, assigned, confidence, due, links, notes):
    """Create ONE task on the board (upsert via tasks/sync; auto-assigns the next T<N>)."""
    import re

    conf = confidence.strip().lower()
    due = (due or "").strip()
    try:
        client = _client(slug)
        task = {
            "ext_id": (ext_id or next_task_ext_id(client.list_tasks()))[:64],
            "title": title.strip()[:300],
            "next_action": next_action.strip()[:300],
            "status": normalize_task_status(status),
            "owner": owner.strip()[:120],
            "assigned": assigned.strip()[:120],
            "confidence": conf if conf in ("high", "low") else "",
            "due": due if re.match(r"^\d{4}-\d{2}-\d{2}$", due) else None,
            "links": parse_task_links(links),
            "notes": notes.strip(),
            "source": "task-tracker",
        }
        result = client.sync_tasks([task])
        _emit({"added": task["ext_id"], "result": result})
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
