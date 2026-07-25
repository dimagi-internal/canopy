"""CLI entry point for the orchestrator."""

from pathlib import Path
import click
from orchestrator.paths import CANOPY_DIR, ensure_canopy_dir
from orchestrator.capture import read_session_log, group_by_session, classify_sessions
from orchestrator.pipeline import run_cycle, CycleConfig
from orchestrator.server import run_server
from orchestrator.agent_cli import agent as agent_group
from orchestrator.agent_email import email_group
from orchestrator.agent_gdoc import gdoc_group
from orchestrator.eval_cli import eval_group


def _skill_hint(name: str) -> str | None:
    """If an unknown subcommand is actually an installed skill, say how to invoke it.

    Agents chronically conflate canopy's two surfaces — the deterministic CLI
    (`canopy agent-review ...`) and the Claude Code skills (`canopy:session-review`,
    invoked via the Skill tool). Hal ran `canopy session-review` and got a bare
    "No such command"; this makes the error itself teach the distinction.
    """
    try:
        from orchestrator.skill_catalog import build_catalog
        matches = [e for e in build_catalog() if e.get("name") == name]
    except Exception:
        return None
    if not matches:
        return None
    qualified = matches[0].get("qualified") or name
    return (
        f"Hint: {name!r} is a Claude Code skill, not a canopy CLI subcommand. "
        f"Invoke it via the Skill tool as {qualified!r} (slash command /{qualified}). "
        "The canopy CLI only exposes deterministic subcommands — see `canopy --help`."
    )


class SkillHintGroup(click.Group):
    """Click group whose unknown-command error checks the installed skill catalog."""

    def resolve_command(self, ctx, args):
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError as err:
            hint = _skill_hint(args[0]) if args else None
            if hint:
                raise click.UsageError(err.format_message() + "\n" + hint, ctx=ctx)
            raise


@click.group(cls=SkillHintGroup)
def main():
    """Canopy — self-improving MCP orchestration."""


main.add_command(agent_group)
main.add_command(email_group)
main.add_command(gdoc_group)
main.add_command(eval_group)


@main.group()
def sessions():
    """Session log commands."""


@sessions.command("list")
@click.option("--hours", default=24, type=int, help="Only show sessions with activity in the last N hours")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON (for skill consumption)")
@click.option("--project", default=None,
              help="Filter to sessions whose resolved GitHub repo ends with /<name>. "
                   "Uses repo-map (incl. emdash worktree path inference). "
                   "Example: --project ace matches jjackson/ace but NOT jjackson/ace-web.")
@click.option("--min-turns", default=0, type=int,
              help="Drop sessions with fewer than N user messages — useful for "
                   "filtering trivial/empty sessions out of session-review batches "
                   "where each analyzed session costs an LLM call.")
def sessions_list(hours, as_json, project, min_turns):
    """List recent sessions grouped by project."""
    import json as json_mod
    from datetime import datetime, timezone, timedelta

    from orchestrator.scanner import scan_all_transcripts
    from orchestrator.repo_map import load_repo_map
    from orchestrator.labels import load_labels

    projects_dir = Path.home() / ".claude" / "projects"
    state_dir = ensure_canopy_dir()
    repo_map = load_repo_map(state_dir / "repo-map.json")
    labels = load_labels(state_dir / "labels.yaml")

    all_sessions = scan_all_transcripts(projects_dir, repo_map, labels)

    # Filter by recency
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    cutoff_iso = cutoff.isoformat()
    recent = [s for s in all_sessions if s.get("last_ts") and s["last_ts"] >= cutoff_iso]

    # Filter by project (precise: repo must end with /<name>; never substring-matches).
    # Uses scanner's resolved `repo` field which honors emdash worktree inference,
    # so deleted-worktree sessions still get classified correctly.
    if project:
        suffix = f"/{project}"
        recent = [s for s in recent if (s.get("repo") or "").endswith(suffix)]

    # Drop trivial sessions before they reach LLM-driven analysis. A 2026-05-02
    # session-review run wasted ~2 min on 4 sessions that returned 0 observations
    # because they were too short to contain meaningful friction signal. Use
    # --min-turns ~5 to skip these in batch contexts.
    if min_turns > 0:
        recent = [s for s in recent if s.get("user_msgs", 0) >= min_turns]

    recent.sort(key=lambda s: s["last_ts"], reverse=True)

    if as_json:
        click.echo(json_mod.dumps(recent, indent=2, default=str))
        return

    if not recent:
        click.echo(f"No sessions with activity in the last {hours} hours.")
        return

    click.echo(f"Sessions with activity in the last {hours} hours:\n")
    for i, s in enumerate(recent, 1):
        project = s.get("repo") or s["project_key"]
        ts = s["last_ts"][:16].replace("T", " ")
        msg = s["first_msg"][:50] + ("..." if len(s["first_msg"]) > 50 else "")
        click.echo(f"  {i:>3}  [{ts}]  {project:<30}  \"{msg}\"  ({s['user_msgs']} msgs)")


@sessions.command("status")
def sessions_status():
    """Show session log status."""
    log_file = CANOPY_DIR / "session-log.jsonl"
    entries = read_session_log(log_file)
    if not entries:
        click.echo("No session log entries found.")
        return

    groups = group_by_session(entries)
    classified = classify_sessions(groups)

    click.echo(f"Session log: {len(entries)} entries, {len(groups)} sessions")
    click.echo(f"  Multi-server sessions: {len(classified['multi_server'])}")
    click.echo(f"  Single-server sessions: {len(classified['single_server'])}")

    latest = entries[-1]
    click.echo(f"  Latest: {latest['ts']} — {latest['server']}.{latest['tool']}")


@main.command("improve")
@click.option("--observe-only", is_flag=True, help="Analyze transcripts but don't propose or implement")
@click.option("--dry-run", is_flag=True, help="Analyze and propose but don't implement")
@click.option("--model", default="sonnet", help="Model to use for analysis/proposals")
def improve(observe_only, dry_run, model):
    """Run an improvement cycle — analyze sessions, propose and implement improvements."""
    state_dir = ensure_canopy_dir()

    config = CycleConfig(
        observe_only=observe_only,
        dry_run=dry_run,
        model=model,
    )

    click.echo("Starting improvement cycle...")
    if observe_only:
        click.echo("  Mode: observe-only (no proposals or implementation)")
    elif dry_run:
        click.echo("  Mode: dry-run (no implementation)")

    result = run_cycle(
        state_dir=state_dir,
        config=config,
    )

    _print_cycle_result(result)


@main.command("analyze")
@click.argument("transcript", type=click.Path(exists=True))
@click.option("--propose", is_flag=True, help="Also generate proposals from observations")
@click.option("--model", default="sonnet", help="Model to use")
@click.option("--budget", default=1.0, type=float, help="Max USD per claude -p call")
def analyze_cmd(transcript, propose, model, budget):
    """Analyze a specific transcript file for observations and proposals."""
    import sys
    import yaml
    from orchestrator.analyzer import analyze_transcript
    from orchestrator.proposer import generate_proposals
    from orchestrator.observations import create_observation, save_observation
    from orchestrator.proposals import create_proposal, save_proposal

    # STATUS sentinel: emitted unconditionally before any heavy work, flushed
    # immediately. Lets background-bash callers distinguish "process never
    # produced output" (0-byte file = something killed the command before it
    # started, e.g. uv venv contention under parallel launches) from
    # "process started, ran the LLM call, produced no observations". Surfaced
    # by a session-review run that fanned out 10 parallel `canopy analyze`
    # background tasks; 9 produced 0 bytes and the 1 that ran returned
    # "No observations" on a session that had 9 findings on a sequential
    # rerun. Without this sentinel the failure was invisible.
    click.echo(f"STATUS: STARTED analyze {transcript}")
    sys.stdout.flush()

    state_dir = ensure_canopy_dir()

    click.echo(f"Analyzing: {transcript}")
    click.echo()

    try:
        observations = analyze_transcript(
            Path(transcript), model=model, max_budget_usd=budget,
        )
    except Exception as e:
        click.echo(f"STATUS: FAILED analyze-raised {type(e).__name__}: {e}")
        sys.stdout.flush()
        raise

    if not observations:
        click.echo("No observations found.")
        click.echo("STATUS: DONE 0-observations")
        sys.stdout.flush()
        return

    click.echo(f"Found {len(observations)} observations:")
    click.echo()

    # Save and display
    obs_dir = state_dir / "observations"
    saved = []
    for obs_data in observations:
        obs = create_observation(
            obs_type=obs_data.get("type", "gap"),
            description=obs_data.get("description", ""),
            severity=obs_data.get("severity", "medium"),
            session_id=Path(transcript).stem,
            related_servers=obs_data.get("related_servers", []),
            lifecycle_stage=obs_data.get("lifecycle_stage"),
        )
        save_observation(obs, obs_dir)
        saved.append(obs)
        click.echo(f"  [{obs['severity']}] {obs['description']}")

    if not propose:
        click.echo(f"STATUS: DONE {len(saved)}-observations no-propose")
        sys.stdout.flush()
        return

    click.echo()
    click.echo("Generating proposals...")
    click.echo()

    from orchestrator.skill_catalog import build_catalog, format_for_prompt
    catalog = build_catalog()
    catalog_text = format_for_prompt(catalog)

    try:
        proposals_raw = generate_proposals(
            saved, model=model, max_budget_usd=budget,
            skill_catalog=catalog_text,
        )
    except Exception as e:
        click.echo(f"STATUS: FAILED propose-raised {type(e).__name__}: {e}")
        sys.stdout.flush()
        raise

    if not proposals_raw:
        # Distinguish "proposer ran cleanly with nothing to suggest" from "the
        # proposer's claude -p call broke" — the agent's salvage logic
        # (session-review.md Step 5b) needs to know which case it is so it
        # can hand-craft proposals from the saved observations rather than
        # silently dropping them. The actual failure detail was already
        # printed to stderr by generate_proposals().
        click.echo(
            "WARN: proposer returned no proposals — see stderr for whether "
            "it was a parse error or genuinely no-suggestions. "
            f"The {len(saved)} observation(s) above are saved and can be "
            "promoted to findings by hand if proposer parse-errored."
        )
        click.echo(f"STATUS: DONE {len(saved)}-observations 0-proposals proposer-empty")
        sys.stdout.flush()
        return

    # Validate proposals against the skill catalog (drop duplicates)
    proposals_raw, dropped = _validate_proposals(proposals_raw, catalog)

    if dropped:
        click.echo(f"Dropped {len(dropped)} duplicate/redundant proposal(s):")
        for d in dropped:
            reason = d.get("_dropped", "unknown reason")
            preview = (d.get("action") or "").replace("\n", " ")[:80]
            click.echo(f"  [dropped: {reason}]")
            click.echo(f"    {preview}")
        click.echo()

    proposals_dir = state_dir / "proposals"
    click.echo(f"Generated {len(proposals_raw)} proposals:")
    click.echo()
    for i, p_data in enumerate(proposals_raw, 1):
        proposal = create_proposal(
            proposal_type=p_data.get("type", "new_tool"),
            action=p_data.get("action", ""),
            target_repo=p_data.get("target_repo", ""),
            ownership=p_data.get("ownership", "self"),
            motivation=p_data.get("motivation", ""),
            observation_id=p_data.get("observation_id", ""),
            complexity=p_data.get("complexity", "medium"),
            verification=p_data.get("verification"),
        )
        save_proposal(proposal, proposals_dir)
        v = proposal.get("verification", {})
        target = p_data.get("target_system", "project")
        confidence = v.get("confidence", "?")

        click.echo(f"  {i}. [{confidence}] [{target}] {proposal['type']}")
        click.echo(f"     {proposal['action']}")
        click.echo(f"     Target: {proposal['target_repo']}")
        click.echo(f"     Motivation: {proposal['motivation'][:120]}")
        if v.get("test_description"):
            click.echo(f"     Verify: {v['test_description']}")
        if v.get("expected_outcome"):
            click.echo(f"     Expected: {v['expected_outcome'][:120]}")
        click.echo()

    click.echo(f"STATUS: DONE {len(saved)}-observations {len(proposals_raw)}-proposals")
    sys.stdout.flush()


@main.command("serve")
@click.option("--port", default=8484, type=int, help="Port to serve on")
def serve(port):
    """Start the transcript browser web UI."""
    state_dir = ensure_canopy_dir()
    projects_dir = Path.home() / ".claude" / "projects"

    run_server(
        projects_dir=projects_dir,
        state_dir=state_dir,
        port=port,
    )


@main.command("brief")
@click.option("--model", default="sonnet", help="Model to use for brief generation")
@click.option("--budget", default=1.0, type=float, help="Max USD per claude -p call")
def brief(model, budget):
    """Generate a strategic brief from recent activity."""
    from orchestrator.briefing import generate_brief

    state_dir = ensure_canopy_dir()

    click.echo(generate_brief(
        state_dir=state_dir,
        model=model,
        max_budget_usd=budget,
    ))


@main.group("shareout")
def shareout_group():
    """Team work-briefing commands — gather a date range and post to canopy-web."""


@shareout_group.command("gather")
@click.option("--from", "from_date", default=None, help="Start date YYYY-MM-DD")
@click.option("--to", "to_date", default=None, help="End date YYYY-MM-DD")
@click.option("--days", default=None, type=int, help="Last N full days ending yesterday")
@click.option("--project", default=None,
              help="Filter to sessions whose resolved repo ends with /<name>")
@click.option("--author", default="@me", help="GitHub PR author (default: @me)")
@click.option("--api-url", default=None,
              help="canopy-web base URL (used to find the last shareout for the default window)")
@click.option("--json-out", "json_out", default=None,
              help="Write corpus JSON to this path instead of stdout")
def shareout_gather(from_date, to_date, days, project, author, api_url, json_out):
    """Gather sessions + the author's PRs per project for a date range.

    With no --from/--to/--days, the window runs from the end of the most recent
    existing shareout up to today (the gap since the last one). If no shareout
    exists yet — or canopy-web is unreachable — it falls back to yesterday.
    """
    import datetime as dt
    import json as json_mod

    from orchestrator import canopy_web
    from orchestrator import shareout as shareout_mod
    from orchestrator.repo_map import load_repo_map
    from orchestrator.labels import load_labels

    projects_dir = Path.home() / ".claude" / "projects"
    state_dir = ensure_canopy_dir()
    repo_map = load_repo_map(state_dir / "repo-map.json")
    labels = load_labels(state_dir / "labels.yaml")

    if not (from_date or to_date or days):
        # Incremental default: continue from where the last shareout left off.
        api = canopy_web.resolve_base_url(api_url)
        token = shareout_mod.resolve_pat()
        latest_end = shareout_mod.fetch_latest_period_end(api, token) if token else None
        start, end = shareout_mod.resolve_default_range(latest_end)
    else:
        start, end = shareout_mod.resolve_range(from_date, to_date, days)
    corpus = shareout_mod.gather(
        projects_dir=projects_dir,
        repo_map=repo_map,
        labels=labels,
        start=start,
        end=end,
        author=author,
        project_filter=project,
    )
    out = json_mod.dumps(corpus, indent=2, default=str)
    if json_out:
        Path(json_out).write_text(out)
        click.echo(f"Wrote corpus for {start}..{end} "
                   f"({len(corpus['projects'])} projects) to {json_out}")
    else:
        click.echo(out)


@shareout_group.command("post")
@click.argument("authoring_json", type=click.Path(exists=True))
@click.option("--corpus", "corpus_json", default=None, type=click.Path(exists=True),
              help="gather corpus JSON — auto-fills each project's all_prs (full PR list)")
@click.option("--source", "source_override", default=None,
              help="Override the source tag. Reuse a prior run's source to REPLACE its "
                   "rows (idempotency is keyed on project+period+source) instead of "
                   "appending a fresh timestamped set.")
@click.option("--api-url", default=None,
              help="canopy-web base URL (default: $CANOPY_WEB_API_URL or prod)")
@click.option("--produced-by-agent", "produced_by_agent", default=None,
              help="Agent slug (e.g. 'eva') when an agent runs this on the user's behalf. "
                   "The report stays attributed to the author; this records the producer for "
                   "a subtle byline. Auto-detected from $CANOPY_AGENT_SLUG / config/agent.json "
                   "when omitted.")
def shareout_post(authoring_json, corpus_json, source_override, api_url, produced_by_agent):
    """Post an authored briefings doc to the canopy-web /shareouts feed."""
    import datetime as dt
    import json as json_mod

    from orchestrator import canopy_web
    from orchestrator import shareout as shareout_mod

    api = canopy_web.resolve_base_url(api_url)
    token = shareout_mod.resolve_pat()
    if not token:
        raise click.ClickException(
            "no canopy-web PAT — run /canopy:canopy-web-pat-mint or set CANOPY_WEB_PAT"
        )

    # Explicit flag wins; otherwise best-effort detect the producing agent.
    agent = produced_by_agent if produced_by_agent is not None else shareout_mod.detect_agent_slug()

    authoring = json_mod.loads(Path(authoring_json).read_text())
    if corpus_json:
        corpus = json_mod.loads(Path(corpus_json).read_text())
        shareout_mod.fill_all_prs_from_corpus(authoring, corpus)
    source = source_override or f"canopy:shareout@{dt.datetime.now(dt.timezone.utc).isoformat()}"
    payload = shareout_mod.build_post_payload(authoring, source=source, produced_by_agent=agent)

    status, body = shareout_mod.post(payload, api, token)
    if status not in (200, 201):
        raise click.ClickException(f"post failed ({status}): {body}")
    click.echo(f"Posted {body.get('created', 0)} briefing(s) "
               f"(replaced {body.get('replaced', 0)}, skipped {body.get('skipped', 0)})"
               + (f" — produced by {agent}." if agent else "."))
    click.echo(f"View: {shareout_mod.feed_url(api)}")


@shareout_group.command("clear")
@click.option("--source", default=None, help="Delete shareouts with this exact source tag")
@click.option("--project", default=None, help="Delete shareouts for this project slug")
@click.option("--from", "date_from", default=None, help="period_end on/after YYYY-MM-DD")
@click.option("--to", "date_to", default=None, help="period_start on/before YYYY-MM-DD")
@click.option("--all", "clear_all", is_flag=True, help="Required to delete with no filters")
@click.option("--api-url", default=None, help="canopy-web base URL")
def shareout_clear(source, project, date_from, date_to, clear_all, api_url):
    """Delete shareouts from the canopy-web feed (filters AND-combine)."""
    from orchestrator import canopy_web
    from orchestrator import shareout as shareout_mod

    filters = {k: v for k, v in {
        "source": source, "project": project, "date_from": date_from, "date_to": date_to,
    }.items() if v}
    if not filters and not clear_all:
        raise click.ClickException("refusing to clear ALL shareouts without --all")

    api = canopy_web.resolve_base_url(api_url)
    token = shareout_mod.resolve_pat()
    if not token:
        raise click.ClickException("no canopy-web PAT — run /canopy:canopy-web-pat-mint")
    status, body = shareout_mod.clear(filters, api, token)
    if status != 200:
        raise click.ClickException(f"clear failed ({status}): {body}")
    click.echo(f"Cleared {body.get('cleared', 0)} shareout row(s).")


@main.group("test-audit")
def test_audit():
    """Test-audit tools: build a corpus for the agent to judge, then apply verdicts."""


@test_audit.command("collect")
@click.argument("repo", type=click.Path(exists=True, file_okay=False, path_type=Path),
                default=".")
@click.option("--no-run", is_flag=True,
              help="Skip the test runner; static analysis only.")
@click.option("--reruns", type=int, default=0,
              help="Run the suite N extra times for flake detection (default: 0).")
@click.option("--framework", type=click.Choice(["auto", "pytest", "vitest"]),
              default="auto", show_default=True,
              help="Override framework detection.")
@click.option("--source-roots", "source_roots", default=None,
              help="Comma-separated list of repo-relative source directories "
                   "to scan for the module inventory (e.g. 'lib,mcp'). "
                   "Defaults to the framework's conventional layout.")
def test_audit_collect(repo, no_run, reruns, framework, source_roots):
    """Build the audit corpus (test inventory + source + runtime) for an agent to read."""
    from orchestrator.test_audit import collect_corpus

    fw = None if framework == "auto" else framework
    roots = [r.strip() for r in source_roots.split(",")] if source_roots else None
    if roots:
        roots = [r for r in roots if r]
    result = collect_corpus(Path(repo), run_tests=not no_run, reruns=reruns,
                            framework=fw, source_roots=roots)
    click.echo(f"corpus: {result.corpus_path}")
    click.echo(f"stamp_dir: {result.stamp_dir}")
    click.echo(f"framework: {result.framework}")
    click.echo(f"test_count: {result.test_count}")
    click.echo(f"ran_tests: {result.ran_tests}")


@test_audit.command("apply")
@click.argument("stamp_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--repo", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None,
              help="Repo root (default: infer from stamp_dir).")
@click.option("--aggressive", is_flag=True,
              help="Apply prunes with score 4-6 too. Default: only score 0-3.")
@click.option("--dry-run", is_flag=True,
              help="Plan changes but don't write or open a PR.")
@click.option("--framework", type=click.Choice(["auto", "pytest", "vitest"]),
              default="auto", show_default=True,
              help="Override framework detection (default: read from corpus.yaml).")
def test_audit_apply(stamp_dir, repo, aggressive, dry_run, framework):
    """Read <stamp_dir>/verdicts.yaml and apply (delete/skip) + open a PR."""
    from orchestrator.test_audit import apply_audit, render_apply_summary

    fw = None if framework == "auto" else framework
    result = apply_audit(Path(stamp_dir), repo=Path(repo) if repo else None,
                         aggressive=aggressive, dry_run=dry_run, framework=fw)
    click.echo(render_apply_summary(result))


@main.command("doctor")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def doctor_cmd(as_json):
    """Diagnose canopy plugin health.

    Runs read-only checks — hook registration, session log, repo map,
    workbench token, plugin version. Exits non-zero if any check fails so it
    can gate CI.
    """
    import json as json_mod
    from orchestrator.doctor import run_doctor

    results, overall_ok = run_doctor()

    if as_json:
        click.echo(json_mod.dumps(
            {
                "ok": overall_ok,
                "checks": [r.to_dict() for r in results],
            },
            indent=2,
        ))
    else:
        width = max(len(r.name) for r in results)
        for r in results:
            status = "OK  " if r.ok else "FAIL"
            click.echo(f"  [{status}] {r.name.ljust(width)}  {r.detail}")
        click.echo()
        if overall_ok:
            click.echo("All checks passed — canopy is healthy.")
        else:
            click.echo("Some checks failed — see details above.")

    if not overall_ok:
        raise click.exceptions.Exit(1)


@main.command("patterns")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def patterns_cmd(as_json):
    """Show cross-session friction patterns."""
    import json as json_mod
    from orchestrator.patterns import detect_patterns

    state_dir = ensure_canopy_dir()
    obs_dir = state_dir / "observations"

    results = detect_patterns(obs_dir)

    if as_json:
        click.echo(json_mod.dumps(results, indent=2, default=str))
        return

    if not results:
        click.echo("No patterns detected. Run `canopy improve` to analyze sessions first.")
        return

    recurring = [p for p in results if p["type"] == "recurring_issue"]
    hotspots = [p for p in results if p["type"] == "project_hotspot"]

    if recurring:
        click.echo("Recurring Issues:\n")
        for p in recurring:
            servers = ", ".join(p["related_servers"]) if p["related_servers"] else "general"
            click.echo(f"  [{p['severity']}] {p['issue_type']} — {servers}")
            click.echo(f"    Seen {p['total_frequency']}x across {p['unique_sessions']} sessions ({p['observation_count']} observations)")
            for desc in p.get("descriptions", [])[:2]:
                click.echo(f"    - {desc[:80]}")
            click.echo()

    if hotspots:
        click.echo("Project Hotspots:\n")
        for p in hotspots:
            high = f" ({p['high_severity_count']} high)" if p["high_severity_count"] else ""
            click.echo(f"  {p['server']}: {p['issue_count']} issues{high}")
        click.echo()


@main.command("verify-findings")
@click.argument("id_prefixes", nargs=-1)
@click.option("--all-pending", is_flag=True,
              help="Verify every proposal whose status is currently `pending`.")
@click.option("--json-output", "as_json", is_flag=True,
              help="Emit JSON for skill consumption (no triage table).")
@click.option("--model", default="sonnet",
              help="Model for the verdict LLM call.")
@click.option("--budget", default=0.50, type=float,
              help="Max USD per claude -p call.")
def verify_findings_cmd(id_prefixes, all_pending, as_json, model, budget):
    """Re-verify session-review proposals against the current state of their target repos.

    Drops proposals whose fix already shipped (flips status to `obsolete`)
    and surfaces a triage table with one verdict per proposal:
    shipped / partial / open / unverifiable. Caller passes proposal-id
    prefixes (8+ chars each) or --all-pending.
    """
    import json as json_mod
    from orchestrator.verify_findings import verify

    if not id_prefixes and not all_pending:
        raise click.UsageError(
            "pass at least one proposal-id prefix, or --all-pending."
        )

    result = verify(
        id_prefixes=list(id_prefixes) if id_prefixes else None,
        all_pending=all_pending,
        model=model,
        max_budget_usd=budget,
    )

    if as_json:
        # Drop internal _path before emitting — callers don't need it.
        for v in result["verdicts"]:
            v.pop("_path", None)
        click.echo(json_mod.dumps(result, indent=2, default=str))
        return

    summary = result["summary"]
    if not result["verdicts"]:
        click.echo("No proposals matched. Pass id-prefixes or --all-pending.")
        return

    click.echo(
        f"verify-findings: {summary['shipped']} shipped · "
        f"{summary['partial']} partial · {summary['open']} open · "
        f"{summary['unverifiable']} unverifiable "
        f"(of {summary['total']})"
    )
    click.echo()

    # Triage table
    click.echo(
        f"{'status':<14} {'id':<14} {'evidence':<60}"
    )
    click.echo("-" * 90)
    for v in result["verdicts"]:
        verdict = (v.get("verdict") or "")[:13]
        pid = (v.get("id") or "")[:13]
        evidence = (v.get("evidence") or "")[:60]
        click.echo(f"{verdict:<14} {pid:<14} {evidence:<60}")


@main.command("portfolio-discover")
@click.option("--max-age-days", default=30, type=int,
              help="How recent the latest commit must be to count as 'active'")
@click.option("--json-output", "as_json", is_flag=True,
              help="Emit JSON for skill consumption")
@click.option("--api-url", default=None,
              help="Override the canopy-web API base URL (default: $CANOPY_WEB_API_URL or the prod URL)")
def portfolio_discover_cmd(max_age_days, as_json, api_url):
    """List local emdash repos with recent activity that aren't yet curated on canopy-web.

    Closes the gap where a freshly-created project (e.g. expense-helper) stays
    invisible to the canopy portfolio feed until manually registered. Scans
    ~/emdash/{worktrees,repositories} and ~/emdash-projects, asks canopy-web
    which slugs are already curated, and prints the difference.
    """
    import json as json_mod
    from orchestrator import canopy_web
    from orchestrator.portfolio_discover import (
        discover_active_repos, fetch_curated_slugs, diff_against_curated,
    )

    active = discover_active_repos(max_age_days=max_age_days)

    base_url = canopy_web.resolve_base_url(api_url)
    token_file = Path.home() / ".claude" / "canopy" / "workbench-token"
    curated: set = set()
    curated_reachable = False
    if token_file.exists() and token_file.read_text().strip():
        curated = fetch_curated_slugs(base_url, token_file.read_text().strip())
        curated_reachable = bool(curated) or True  # we tried; empty means none curated, not unreachable
        # but treat URLError → empty set as "unreachable"; the fetch helper
        # returns set() for both "no projects" and "couldn't reach" — accept the
        # ambiguity, the user gets useful output either way.

    candidates = diff_against_curated(active, curated)

    if as_json:
        click.echo(json_mod.dumps(
            {
                "active_count": len(active),
                "curated_count": len(curated),
                "candidates": candidates,
            },
            indent=2,
        ))
        return

    if not active:
        click.echo("No active git repos found under emdash roots.")
        return

    click.echo(
        f"Found {len(active)} repos with commits in the last {max_age_days} days; "
        f"{len(curated)} are already curated on canopy-web."
    )
    if not candidates:
        click.echo("All active repos are already curated. Nothing to register.")
        return

    click.echo()
    click.echo(f"Candidates not yet curated ({len(candidates)}):")
    for c in candidates:
        ts_short = c["last_commit"][:10]
        click.echo(f"  [{ts_short}]  {c['slug']:30}  {c['path']}")
    click.echo()
    click.echo(f"Register a project at: {base_url.rstrip('/')}/admin/")


@main.group()
def version():
    """VERSION coordination across worktrees."""


@version.command("verify")
@click.option("--repo", default=None, type=click.Path(exists=True, file_okay=False),
              help="Repo root (defaults to current working directory)")
def version_verify(repo):
    """Verify VERSION and plugin.json agree on the current version."""
    from orchestrator.version_bump import verify
    repo_root = Path(repo) if repo else Path.cwd()
    try:
        matches, v, p = verify(repo_root)
    except FileNotFoundError as e:
        raise click.ClickException(str(e))
    if matches:
        click.echo(f"OK: VERSION and plugin.json both at v{v}")
    else:
        click.echo(f"MISMATCH: VERSION={v}  plugin.json={p}", err=True)
        raise click.exceptions.Exit(1)


@version.command("verify-bump")
@click.option("--repo", default=None, type=click.Path(exists=True, file_okay=False),
              help="Repo root (defaults to current working directory)")
@click.option("--base-ref", default="origin/main",
              help="Base ref to compare against (default: origin/main)")
def version_verify_bump(repo, base_ref):
    """Fail if plugins/canopy/ changed without a VERSION bump on this branch.

    The check that catches the discipline failure CLAUDE.md flags as the #1
    canopy mistake — merging plugin asset changes without bumping VERSION,
    which makes `/canopy:update` silently report UP_TO_DATE and leaves
    every existing session stuck on the old cache.
    """
    from orchestrator.version_bump import verify_bump_when_plugin_changed

    repo_root = Path(repo) if repo else Path.cwd()
    try:
        result = verify_bump_when_plugin_changed(repo_root, base_ref=base_ref)
    except FileNotFoundError as e:
        raise click.ClickException(str(e))

    click.echo(result["reason"])
    if result["plugin_files_changed"]:
        click.echo(f"\nPlugin files changed on this branch ({len(result['plugin_files_changed'])}):")
        for p in result["plugin_files_changed"]:
            click.echo(f"  {p}")
    if result["main_version"] and result["local_version"]:
        click.echo(f"\nbase {result['base_ref']} VERSION: {result['main_version']}")
        click.echo(f"branch HEAD VERSION:    {result['local_version']}")
    if result["ok"]:
        return
    raise click.exceptions.Exit(1)


@version.command("bump")
@click.option("--repo", default=None, type=click.Path(exists=True, file_okay=False),
              help="Repo root (defaults to current working directory)")
def version_bump(repo):
    """Bump VERSION + plugin.json by max(local, origin/main) + patch+1.

    Fetches origin/main first so a parallel worktree's bump is visible before
    deciding the next number. Solves the recurring "two worktrees both bumped
    to the same number" friction.
    """
    from orchestrator.version_bump import bump
    repo_root = Path(repo) if repo else Path.cwd()
    try:
        result = bump(repo_root)
    except (FileNotFoundError, ValueError) as e:
        raise click.ClickException(str(e))

    prev = result["previous_local"]
    origin = result["origin_main"]
    new = result["new_version"]
    origin_str = origin or "(unreachable)"
    click.echo(f"Bumped to v{new}")
    click.echo(f"  was: local=v{prev}  origin/main=v{origin_str}")
    click.echo(f"  wrote: {result['version_path']}")
    click.echo(f"  wrote: {result['plugin_json_path']}")
    mp_path = result.get("marketplace_json_path")
    mp_n = result.get("marketplace_json_replacements", 0)
    if mp_path and mp_n:
        click.echo(f"  wrote: {mp_path} ({mp_n} version field{'s' if mp_n != 1 else ''})")


@main.command("structure-drift")
@click.option("--repo", default=None, type=click.Path(exists=True, file_okay=False),
              help="Repo root to audit (defaults to the canopy checkout this CLI ships from)")
@click.option("--strict", is_flag=True,
              help="Exit non-zero if any finding exists (CI gate)")
@click.option("--per-skill-limit", default=None, type=int,
              help="Per-skill description char limit (default: 1024)")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def structure_drift_cmd(repo, strict, per_skill_limit, as_json):
    """Self-audit canopy's documented structural invariants in one pass.

    Checks the invariants canopy documents in CLAUDE.md:
      - command/skill collisions follow Pattern B (read SKILL.md from disk)
      - no command/skill/agent uses a reserved built-in slash-command name
      - VERSION == plugin.json version == marketplace.json version field(s)
      - no skill description exceeds the per-skill char budget

    Default: print findings and exit 0. With --strict, exit non-zero when any
    finding exists, so CI can gate on it.
    """
    import json as json_mod
    from orchestrator.structure_drift import run_structure_drift, DEFAULT_PER_SKILL_LIMIT

    repo_root = Path(repo) if repo else None
    psl = per_skill_limit if per_skill_limit is not None else DEFAULT_PER_SKILL_LIMIT

    report = run_structure_drift(repo_root=repo_root, per_skill_limit=psl)

    if as_json:
        click.echo(json_mod.dumps(report, indent=2, default=str))
    else:
        counts = report["counts"]
        if report["ok"]:
            click.echo("OK: no structure drift detected.")
        else:
            click.echo(
                f"DRIFT: {counts['total']} finding(s) — "
                f"{counts['error']} error, {counts['warning']} warning\n"
            )
            for inv, items in report["by_invariant"].items():
                click.echo(f"[{inv}]")
                for f in items:
                    click.echo(f"  {f['severity'].upper()}: {f['detail']}")
                click.echo()

    if strict and not report["ok"]:
        raise click.exceptions.Exit(1)


@main.group()
def skills():
    """Inspect installed skills (plugin + user)."""


@skills.command("list")
@click.option("--scope", type=click.Choice(["all", "plugin", "user"]), default="all")
@click.option("--source", default=None, help="Filter by plugin name (e.g. 'canopy', 'ace')")
@click.option("--search", default=None, help="Filter by substring in name or description")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def skills_list(scope, source, search, as_json):
    """List skills available across plugin and user scopes."""
    import json as json_mod
    from orchestrator.skill_catalog import build_catalog

    catalog = build_catalog()
    if scope != "all":
        catalog = [e for e in catalog if e["scope"] == scope]
    if source:
        catalog = [e for e in catalog if e["source"] == source]
    if search:
        s = search.lower()
        catalog = [e for e in catalog if s in e["name"].lower() or s in (e["description"] or "").lower()]

    if as_json:
        click.echo(json_mod.dumps(catalog, indent=2, default=str))
        return

    if not catalog:
        click.echo("No skills match the given filters.")
        return

    click.echo(f"{len(catalog)} skill(s):\n")
    for e in catalog:
        desc = (e.get("description") or "").replace("\n", " ")
        if len(desc) > 80:
            desc = desc[:77] + "..."
        version = e.get("installed_version")
        version_str = f" v{version}" if version else ""
        click.echo(f"  {e['qualified']:<45} [{e['scope']}]{version_str}")
        if desc:
            click.echo(f"    {desc}")


@skills.command("budget")
@click.option("--scope", type=click.Choice(["all", "plugin", "user"]), default="all")
@click.option("--source", default=None, help="Filter by plugin name (e.g. 'canopy', 'ace')")
@click.option(
    "--per-skill-limit",
    type=int,
    default=None,
    help="Per-skill description char limit (default: 1024)",
)
@click.option(
    "--aggregate-limit",
    type=int,
    default=None,
    help="Aggregate description char limit across all skills (default: 1500)",
)
@click.option(
    "--top",
    type=int,
    default=None,
    help="Show only the top N largest skills (default: all)",
)
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def skills_budget(scope, source, per_skill_limit, aggregate_limit, top, as_json):
    """Show description-size budget for installed skills.

    Reads each skill's frontmatter `description`, ranks by size, and prints
    an aggregate gauge against the configured cap. Useful for diagnosing the
    silent "N skills dropped" message Claude Code shows when the system
    prompt overflows.
    """
    import json as json_mod
    from orchestrator.skill_catalog import build_catalog
    from orchestrator.skill_budget import (
        DEFAULT_AGGREGATE_LIMIT,
        DEFAULT_PER_SKILL_LIMIT,
        rank,
    )

    psl = per_skill_limit if per_skill_limit is not None else DEFAULT_PER_SKILL_LIMIT
    al = aggregate_limit if aggregate_limit is not None else DEFAULT_AGGREGATE_LIMIT

    catalog = build_catalog()
    if scope != "all":
        catalog = [e for e in catalog if e["scope"] == scope]
    if source:
        catalog = [e for e in catalog if e["source"] == source]

    ranked = rank(catalog)
    if top is not None and top > 0:
        ranked = ranked[:top]

    aggregate_used = sum(min(e["description_size"], psl) for e in ranked)
    over_count = sum(1 for e in ranked if e["description_size"] > psl)

    if as_json:
        click.echo(json_mod.dumps({
            "totals": {
                "skills_total": len(ranked),
                "aggregate_used": aggregate_used,
                "aggregate_limit": al,
                "per_skill_limit": psl,
                "per_skill_over": over_count,
            },
            "skills": ranked,
        }, indent=2, default=str))
        return

    if not ranked:
        click.echo("No skills match the given filters.")
        return

    click.echo(f"{len(ranked)} skill(s) — per-skill cap {psl} chars, aggregate cap {al} chars\n")
    click.echo(f"  {'STATUS':<5}  {'SIZE':>5}  {'KIND':<8}  {'NAME':<48}")
    for e in ranked:
        click.echo(
            f"  {e['per_skill_status']:<5}  {e['description_size']:>5}  "
            f"{(e.get('kind') or 'skill'):<8}  {e['qualified']}"
        )

    pct = (aggregate_used / al * 100) if al else 0
    click.echo()
    click.echo(
        f"  AGGREGATE: {aggregate_used}/{al} chars ({pct:.0f}%)  •  "
        f"{over_count} over per-skill cap"
    )
    if aggregate_used > al:
        click.echo("  ⚠ aggregate exceeds cap — run `canopy skills dropped` to see which skills get dropped")


@skills.command("dropped")
@click.option("--scope", type=click.Choice(["all", "plugin", "user"]), default="all")
@click.option("--source", default=None, help="Filter by plugin name (e.g. 'canopy', 'ace')")
@click.option(
    "--per-skill-limit",
    type=int,
    default=None,
    help="Per-skill description char limit (default: 1024)",
)
@click.option(
    "--aggregate-limit",
    type=int,
    default=None,
    help="Aggregate description char limit across all skills (default: 1500)",
)
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def skills_dropped(scope, source, per_skill_limit, aggregate_limit, as_json):
    """Simulate which skills Claude Code drops under the aggregate cap.

    Sums per-skill (capped) description sizes in alphabetical-by-qualified
    order and flags any skill whose inclusion would push the running total
    over the aggregate cap.
    """
    import json as json_mod
    from orchestrator.skill_catalog import build_catalog
    from orchestrator.skill_budget import (
        DEFAULT_AGGREGATE_LIMIT,
        DEFAULT_PER_SKILL_LIMIT,
        simulate_drops,
    )

    psl = per_skill_limit if per_skill_limit is not None else DEFAULT_PER_SKILL_LIMIT
    al = aggregate_limit if aggregate_limit is not None else DEFAULT_AGGREGATE_LIMIT

    catalog = build_catalog()
    if scope != "all":
        catalog = [e for e in catalog if e["scope"] == scope]
    if source:
        catalog = [e for e in catalog if e["source"] == source]

    result = simulate_drops(catalog, per_skill_limit=psl, aggregate_limit=al)

    if as_json:
        click.echo(json_mod.dumps(result, indent=2, default=str))
        return

    totals = result["totals"]
    dropped = result["dropped"]
    click.echo(
        f"{totals['kept_count']} kept, {totals['dropped_count']} dropped — "
        f"aggregate {totals['aggregate_used']}/{totals['aggregate_limit']} chars used"
    )
    if not dropped:
        click.echo("\nNo skills dropped under the configured caps.")
        return
    click.echo(f"\nDropped ({len(dropped)}):")
    for e in dropped:
        click.echo(
            f"  {e['qualified']:<48} {e['description_size']:>5} chars  ({e.get('drop_reason', 'aggregate_limit_exceeded')})"
        )


@skills.command("find")
@click.argument("query", nargs=-1, required=True)
@click.option("--limit", default=5, type=int, help="Maximum matches to display")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def skills_find(query, limit, as_json):
    """Fuzzy-match installed skills by name and description.

    Useful for the "do we already have a skill for X?" check before proposing
    or building a new one.
    """
    import json as json_mod
    from orchestrator.skill_catalog import build_catalog, find_skills

    text = " ".join(query)
    catalog = build_catalog()
    matches = find_skills(text, catalog, limit=limit)

    if as_json:
        click.echo(json_mod.dumps(matches, indent=2, default=str))
        return

    if not matches:
        click.echo(f"No skills match '{text}'.")
        return

    click.echo(f"{len(matches)} match(es) for '{text}':\n")
    for e in matches:
        desc = (e.get("description") or "").replace("\n", " ")
        if len(desc) > 80:
            desc = desc[:77] + "..."
        click.echo(f"  {e['qualified']:<45} [{e['scope']}]")
        if desc:
            click.echo(f"    {desc}")
        click.echo(f"    {e['path']}")


@skills.command("overlap")
@click.argument("action_text", nargs=-1, required=True)
def skills_overlap(action_text):
    """Check whether a proposed skill action overlaps with an existing skill."""
    from orchestrator.skill_catalog import build_catalog, find_overlap, extract_candidate_names

    text = " ".join(action_text)
    catalog = build_catalog()
    match = find_overlap(text, catalog)
    if match:
        click.echo(f"OVERLAP: {match['qualified']} ({match['scope']})")
        if match.get("description"):
            click.echo(f"  {match['description']}")
        click.echo(f"  {match['path']}")
        raise click.exceptions.Exit(1)
    click.echo("No overlap detected.")
    candidates = extract_candidate_names(text)
    if candidates:
        click.echo(f"  Candidates examined: {', '.join(candidates[:8])}")


@main.group()
def observations():
    """Inspect saved observations."""


@observations.command("list")
@click.option("--type", "obs_type", default=None, help="Filter by observation type (friction, gap, etc.)")
@click.option("--status", default=None, help="Filter by status (pending, addressed, etc.)")
@click.option("--severity", default=None, help="Filter by severity (low, medium, high)")
@click.option("--limit", default=20, type=int, help="Maximum rows to display")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def observations_list(obs_type, status, severity, limit, as_json):
    """List observations with optional filters."""
    import json as json_mod
    from orchestrator.observations import list_observations

    state_dir = ensure_canopy_dir()
    obs_dir = state_dir / "observations"
    rows = list_observations(obs_dir, obs_type=obs_type, status=status)
    if severity:
        rows = [o for o in rows if o.get("severity") == severity]
    rows.sort(key=lambda o: (o.get("created", ""), o.get("frequency", 0)), reverse=True)
    rows = rows[:limit]

    if as_json:
        click.echo(json_mod.dumps(rows, indent=2, default=str))
        return

    if not rows:
        click.echo("No observations match the given filters.")
        return

    click.echo(f"{len(rows)} observation(s):\n")
    for o in rows:
        sev = o.get("severity", "?")
        otype = o.get("type", "?")
        st = o.get("status", "?")
        freq = o.get("frequency", 1)
        desc = (o.get("description") or "").replace("\n", " ")
        if len(desc) > 100:
            desc = desc[:97] + "..."
        click.echo(f"  {o['id']}  [{sev}] [{otype}] [{st}]  freq={freq}")
        click.echo(f"    {desc}")


@observations.command("show")
@click.argument("obs_id")
def observations_show(obs_id):
    """Show full YAML for a single observation by id (or id prefix)."""
    state_dir = ensure_canopy_dir()
    obs_dir = state_dir / "observations"
    matches = sorted(obs_dir.glob(f"{obs_id}*.yaml"))
    if not matches:
        raise click.ClickException(f"No observation found matching id '{obs_id}'")
    if len(matches) > 1:
        click.echo(f"Multiple matches for '{obs_id}':", err=True)
        for m in matches:
            click.echo(f"  {m.stem}", err=True)
        raise click.ClickException("Specify a more unique id prefix.")
    click.echo(matches[0].read_text())


@main.group()
def proposals():
    """Inspect saved proposals."""


@proposals.command("list")
@click.option("--status", default=None, help="Filter by status (pending, implemented, failed)")
@click.option("--complexity", default=None, help="Filter by complexity (low, medium, high)")
@click.option("--limit", default=20, type=int, help="Maximum rows to display")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def proposals_list(status, complexity, limit, as_json):
    """List proposals with optional filters."""
    import json as json_mod
    from orchestrator.proposals import list_proposals

    state_dir = ensure_canopy_dir()
    proposals_dir = state_dir / "proposals"
    rows = list_proposals(proposals_dir, status=status)
    if complexity:
        rows = [p for p in rows if p.get("complexity") == complexity]
    rows.sort(key=lambda p: p.get("created", ""), reverse=True)
    rows = rows[:limit]

    if as_json:
        click.echo(json_mod.dumps(rows, indent=2, default=str))
        return

    if not rows:
        click.echo("No proposals match the given filters.")
        return

    click.echo(f"{len(rows)} proposal(s):\n")
    for p in rows:
        ptype = p.get("type", "?")
        st = p.get("status", "?")
        cx = p.get("complexity", "?")
        conf = (p.get("verification") or {}).get("confidence", "?")
        action = (p.get("action") or "").replace("\n", " ")
        if len(action) > 100:
            action = action[:97] + "..."
        click.echo(f"  {p['id']}  [{st}] [{ptype}] complexity={cx} conf={conf}")
        click.echo(f"    {action}")
        target = p.get("target_repo")
        if target:
            click.echo(f"    target: {target}")


@proposals.command("show")
@click.argument("proposal_id")
def proposals_show(proposal_id):
    """Show full YAML for a single proposal by id (or id prefix)."""
    state_dir = ensure_canopy_dir()
    proposals_dir = state_dir / "proposals"
    matches = sorted(proposals_dir.glob(f"{proposal_id}*.yaml"))
    if not matches:
        raise click.ClickException(f"No proposal found matching id '{proposal_id}'")
    if len(matches) > 1:
        click.echo(f"Multiple matches for '{proposal_id}':", err=True)
        for m in matches:
            click.echo(f"  {m.stem}", err=True)
        raise click.ClickException("Specify a more unique id prefix.")
    click.echo(matches[0].read_text())


def _validate_proposals(
    proposals: list[dict],
    skill_catalog: list[dict] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Validate proposals against the skill catalog.

    Returns `(kept, dropped)`. Each dropped proposal has `_dropped` set to a
    human-readable reason; callers can surface this so users see what was
    filtered out and why.

    - Drop `new_skill` proposals that overlap with an existing skill
    """
    from orchestrator.skill_catalog import find_overlap

    catalog = skill_catalog or []

    kept: list[dict] = []
    dropped: list[dict] = []
    for p in proposals:
        action = p.get("action", "")
        ptype = p.get("type", "")

        # Drop new_skill proposals that overlap with an existing skill
        if ptype == "new_skill" and catalog:
            overlap = find_overlap(action, catalog)
            if overlap:
                p["_dropped"] = f"skill {overlap['qualified']} already exists"
                dropped.append(p)
                continue

        kept.append(p)

    return kept, dropped


def _print_cycle_result(result: dict) -> None:
    click.echo()
    click.echo(f"Transcripts analyzed: {result.get('transcripts_analyzed', 0)}")
    click.echo(f"Observations created: {result.get('observations_created', 0)}")
    click.echo(f"Observations merged:  {result.get('observations_merged', 0)}")
    click.echo(f"Proposals generated:  {result.get('proposals_generated', 0)}")
    click.echo()
    click.echo("Implementation is handled by /canopy:improve skill agents.")

    if result.get("errors"):
        click.echo()
        click.echo("Errors:")
        for err in result["errors"]:
            click.echo(f"  - {err}")


@main.command("create-agent")
@click.argument("slug")
@click.option("--name", default=None, help="Display name (default: derived from slug)")
@click.option("--mandate", required=True, help="One-line mission for the agent")
@click.option("--mailbox", default="", help="Primary channel address, e.g. name@dimagi-ai.com")
@click.option("--stakeholders", default="", help="Who the agent serves")
@click.option("--into", "target", default=None, type=click.Path(),
              help="Target directory (default: ./<slug>)")
@click.option("--force", is_flag=True, help="Scaffold into a non-empty directory")
@click.option("--git-init/--no-git-init", default=True,
              help="git init + initial commit in the new repo (default: on)")
def create_agent_cmd(slug, name, mandate, mailbox, stakeholders, target, force, git_init):
    """Scaffold a new Claude Code agent from the canopy operating model.

    Generates a self-contained agent repo (persona, the `turn` orchestrator, a reads-free /
    writes-gated gating hook, canopy-web-ready layout) grounded in the primitives proven by
    echo. See docs/agent-operating-model.md.
    """
    import subprocess
    from orchestrator.agent_factory import (
        AgentSpec, create_agent, normalize_slug, AgentFactoryError,
    )

    try:
        slug = normalize_slug(slug)
    except AgentFactoryError as e:
        raise click.ClickException(str(e))

    display = name or slug.replace("-", " ").title()
    dest = Path(target).expanduser() if target else Path.cwd() / slug
    spec = AgentSpec(
        slug=slug, display_name=display, mandate=mandate.strip(),
        mailbox=mailbox, stakeholders=stakeholders,
    )
    try:
        written = create_agent(spec, dest, force=force)
    except AgentFactoryError as e:
        raise click.ClickException(str(e))

    click.echo(f"Scaffolded {display} — {len(written)} files at {dest}")
    if git_init and not (dest / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=dest, check=False)
        subprocess.run(["git", "add", "-A"], cwd=dest, check=False)
        subprocess.run(
            ["git", "commit", "-q", "-m", f"scaffold {display} from canopy agent factory"],
            cwd=dest, check=False,
        )
        click.echo("Initialized git repo + initial commit.")
    click.echo()
    click.echo("Next:")
    click.echo("  1. Fill in persona.md (voice, mandate detail, memory scope).")
    click.echo("  2. Add domain skills under skills/<name>/SKILL.md.")
    click.echo("  3. Add outbound actions as approve/deny rules in config/gating.json.")
    click.echo("  4. Wire a channel adapter + setup/preflight for your secrets.")


@main.command("agent-review")
@click.argument("agent", required=False)
@click.option("--hours", default=168, type=int, help="Look back this many hours (default: 7 days)")
@click.option("--no-llm", is_flag=True, help="Deterministic friction signals only; skip claude -p")
@click.option("--no-verify", is_flag=True,
              help="Skip the source-verification gate (by default findings already shipped to "
                   "origin/main are dropped before they're returned)")
@click.option("--model", default="sonnet", help="Model for the synthesis pass")
@click.option("--max-budget-usd", default=2.0, type=float,
              help="USD cap for the claude -p synthesis pass (default: 2.0)")
@click.option("--timeout", "timeout", default=None, type=int,
              help="Seconds to allow the claude -p synthesis pass (default: 180). On timeout "
                   "the run returns no findings and a descriptive error — raise it for big corpora.")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
@click.option("--qualify-file", type=click.Path(exists=True, dir_okay=False), default=None,
              help="Validate a YAML findings file against the SP1 evidence-record schema "
                   "(qualify_findings) and print qualified/dropped; skips the review.")
def agent_review_cmd(agent, hours, no_llm, no_verify, model, max_budget_usd, timeout,
                     as_json, qualify_file):
    """Review an agent's recent TURNS for operating-model friction and recommend fixes.

    AGENT is a slug (e.g. `echo`) or a path to the agent repo. Build 2 of the agent operating
    model — points canopy's analyze→propose loop at an agent's own turns. See
    docs/agent-operating-model.md §4 Build 2.
    """
    import json as json_mod

    if qualify_file:
        import yaml
        from orchestrator.agent_review import qualify_findings

        try:
            with open(qualify_file) as fh:
                findings = yaml.safe_load(fh)
        except (yaml.YAMLError, OSError) as e:
            raise click.ClickException(f"could not read qualify-file: {e}")
        if not isinstance(findings, list):
            raise click.ClickException("qualify-file must contain a YAML list of findings")
        qualified, dropped = qualify_findings(findings)
        if as_json:
            click.echo(json_mod.dumps({"qualified": qualified, "dropped": dropped}, indent=2, default=str))
            return
        click.echo(f"Qualified ({len(qualified)}):")
        for f in qualified:
            title = f.get("title", "(untitled)") if isinstance(f, dict) else "(untitled)"
            click.echo(f"  ✓ {title}")
        click.echo(f"Dropped ({len(dropped)}):")
        for f in dropped:
            if isinstance(f, dict):
                title = f.get("title", "(untitled)")
                reason = f.get("_drop_reason", "")
                if "_raw" in f:
                    click.echo(f"  ✗ {f['_raw']} — {reason}")
                else:
                    click.echo(f"  ✗ {title} — {reason}")
            else:
                click.echo(f"  ✗ {f}")
        return

    if not agent:
        raise click.ClickException("provide an AGENT slug, or --qualify-file <path>")

    from orchestrator.agent_review import run_review, FRICTION_TYPES, SYNTHESIS_TIMEOUT

    result = run_review(agent, hours=hours, use_llm=not no_llm, verify=not no_verify,
                        model=model, max_budget_usd=max_budget_usd,
                        timeout=SYNTHESIS_TIMEOUT if timeout is None else timeout)

    if as_json:
        click.echo(json_mod.dumps(result, indent=2, default=str))
        return
    if result.get("error") and not result.get("turns"):
        raise click.ClickException(result["error"])

    click.echo(f"Agent: {result['agent']}  ({result['repo']})")
    click.echo(f"Turns reviewed (last {hours}h): {result['turns']}")
    # Deterministic signal rollup
    sig = result.get("signals", [])
    fails = sum(len(s["failures"]) for s in sig)
    blocks = sum(len(s["gating_blocks"]) for s in sig)
    auth = sum(len(s["auth_friction"]) for s in sig)
    gaps = {}
    for s in sig:
        for g in s["checklist_gaps"]:
            gaps[g] = gaps.get(g, 0) + 1
    corrections = [c for s in sig for c in s.get("human_corrections", [])]
    click.echo(f"  tool failures: {fails}  •  gating blocks: {blocks}  •  auth friction: {auth}"
               f"  •  human corrections: {len(corrections)}")
    if gaps:
        click.echo("  checklist gaps: " + ", ".join(f"{k}×{v}" for k, v in sorted(gaps.items())))
    if corrections:
        click.echo("  ⚑ HUMAN CORRECTIONS (highest signal — what Jonathan had to override):")
        for c in corrections:
            click.echo(f"      [{','.join(c['kinds'])}] {c['quote']}")

    findings = result.get("findings", [])
    if findings:
        click.echo(f"\nFindings ({len(findings)}):")
        for f in findings:
            if not isinstance(f, dict):
                continue
            conf = f.get("confidence", "?")
            click.echo(f"  [{f.get('friction_type','?')}/{conf}] {f.get('title','')}")
            if f.get("target"):
                click.echo(f"      fix: {f.get('fix_kind','?')} → {f['target']}")
            if f.get("recommendation"):
                click.echo(f"      {str(f['recommendation'])[:160]}")
    dropped = result.get("dropped_findings", [])
    if dropped:
        click.echo(f"\nDropped by source gate — already shipped to origin/main ({len(dropped)}):")
        for f in dropped:
            if not isinstance(f, dict):
                continue
            ev = (f.get("verification") or {}).get("evidence") or ""
            click.echo(f"  ✓ {f.get('title','')}")
            if ev:
                click.echo(f"      {str(ev)[:160]}")
    verify_error = result.get("verification_error")
    if verify_error and findings:
        click.echo(f"\n⚠  SOURCE GATE DID NOT RUN — the {len(findings)} finding(s) above are "
                   f"UNVERIFIED against current origin/main.\n   reason: {verify_error}\n"
                   "   Re-verify each finding against source before acting on it.")
    if result.get("error"):
        # A costed synthesis pass that yielded nothing must be LOUD, not a parenthetical:
        # a live cron read the quiet version as "no findings" and moved on.
        click.echo(f"\n⚠  SYNTHESIS PASS DID NOT COMPLETE — this run's findings are NOT a "
                   f"clean bill of health.\n   reason: {result['error']}")
    elif not findings and dropped:
        pass  # all findings were already-shipped; the drop list above says so
    elif not no_llm:
        click.echo("\nNo findings synthesized.")


@main.command("fleet-align")
@click.option("--repo", "extra_repos", multiple=True, help="Add an agent repo not under the default bases (repeatable)")
@click.option("--hours", default=336, type=int, help="Evidence look-back window (default: 14 days)")
@click.option("--no-evidence", is_flag=True, help="Skip the recent-session evidence search")
@click.option("--no-llm", is_flag=True, help="Deterministic findings only; skip the claude -p judgment pass")
@click.option("--model", default="sonnet", help="Model for the judgment pass")
@click.option("--json-output", "as_json", is_flag=True, help="Output findings + change briefs as JSON (for the apply agent)")
def fleet_align_cmd(extra_repos, hours, no_evidence, no_llm, model, as_json):
    """Cross-agent improvement spread — compare the factory-stamped agent fleet against the
    template and each other, and surface what to DISTRIBUTE (backport a better/newer version into
    laggards) or PROMOTE (lift a converged pattern into canopy). For each finding it then searches
    the laggards' RECENT SESSIONS for evidence the gap actually cost something, and (unless
    --no-llm) runs a judgment pass that confirms direction + weighs that evidence. The *spread*
    verb of the operating model; sibling to `agent-review` (which measures ONE agent).

    Read-only ANALYSIS. Applying a finding (the surgical edit + PR + merge) is dispatched to a
    Claude Code agent by the `fleet-align` skill — matching canopy's pipeline-stops-at-proposals
    architecture — not done by brittle programmatic splicing here. `--json-output` emits each
    distribute finding's change brief (target file + the template's reference text) for that agent.
    See docs/superpowers/specs/2026-07-03-fleet-align-design.md.
    """
    import json as json_mod
    from orchestrator import fleet_align as fa

    agents = fa.discover_agents(extra_repos=extra_repos)
    drift = fa.checkout_warnings(extra_repos=extra_repos)
    if not agents:
        for w in drift:
            click.echo(f"⚠ {w}")
        raise click.ClickException("No agent repos found (marker: skills/turn/SKILL.md). Pass --repo <dir>.")
    findings = fa.analyze(agents)
    unreadable = 0
    if not no_evidence:
        unreadable = fa.gather_evidence(findings, agents, hours=hours)
    if not no_llm:
        findings = fa.judge(findings, model=model)
    findings = fa.evidence_rank(findings)

    if as_json:
        rows = []
        for f in findings:
            row = f.as_dict()
            brief = fa.change_brief(f)
            if brief:
                row["change_brief"] = brief  # the AI apply agent's spec
            rows.append(row)
        click.echo(json_mod.dumps(
            {"fleet": [a.slug for a in agents], "unreadable_logins": unreadable,
             "checkout_warnings": drift, "findings": rows},
            indent=2, default=str,
        ))
        return
    for w in drift:
        click.echo(f"⚠ {w}")
    click.echo(fa.format_report(agents, findings))
    if unreadable:
        click.echo(f"\n⚠ half-blind: {unreadable} user login(s) unreadable — evidence may be incomplete.")


@main.group("harvest")
def harvest():
    """Architect/harvester corpus tools — cross-user, origin-anchored session assembly for Hal.

    Deterministic only: assembles the material; intent reconstruction + drift is the agent's job.
    See docs/agent-operating-model.md + canopy memory `harvester-architect`.
    """


@harvest.command("map")
@click.argument("initiative")
@click.option("--match", "match", default="", help="Comma-separated match terms (default: the initiative name)")
@click.option("--inputs-k", default=6, type=int, help="How many of your inputs to sample per session (non-full mode)")
@click.option("--full", "full", is_flag=True, help="RICH map: ALL your inputs untruncated + full final output per session (quality > token-cost)")
@click.option("--json-output", "as_json", is_flag=True)
def harvest_map(initiative, match, inputs_k, full, as_json):
    """Whole-arc MAP: a digest of EVERY matched session (cross-user). Read all of it to see the full
    arc, then drill into interesting ones with `canopy harvest strip <path>`. `--full` = lose no
    input signal (all your inputs untruncated) when you care more about thoroughness than tokens."""
    import json as json_mod
    from orchestrator.harvest import corpus_map

    terms = [t.strip() for t in match.split(",") if t.strip()]
    m = corpus_map(initiative, terms, inputs_k=inputs_k, full=full)
    if as_json:
        click.echo(json_mod.dumps(m, indent=2, default=str)); return

    sp = m["span"]
    click.echo(f"# MAP: {m['initiative']}  •  {m['total_sessions']} sessions  •  {m['confidence'].upper()}"
               f"  •  by user {m['by_user']}" + (f"  •  {sp['from']} → {sp['to']}" if sp else ""))
    if m["unreadable_users"]:
        click.echo(f"# ⚠ HALF-BLIND — unreadable: {', '.join(m['unreadable_users'])}")
    click.echo("# (read the whole arc below; then `canopy harvest strip <path>` the interesting ones)\n")
    for d in m["digests"]:
        click.echo(f"── [{d['user']}] {d['when']}  {d['project']}  ({d['turns']} turns)")
        click.echo(f"   path: {d['path']}")
        for i, inp in enumerate(d["inputs"]):
            click.echo(f"   {'intent' if i == 0 else 'then'}: {inp}")
        if d["final_output"]:
            click.echo(f"   ended: {d['final_output']}")
        click.echo("")


@harvest.command("strip")
@click.argument("session_path", type=click.Path(exists=True))
@click.option("--mode", type=click.Choice(["final", "full"]), default="final",
              help="final = last assistant block per turn (the output you saw); full = all assistant prose")
def harvest_strip(session_path, mode):
    """Drill into ONE session: print it stripped to (your inputs + assistant outputs), tool noise removed."""
    from orchestrator.harvest import strip_session
    click.echo(strip_session(session_path, mode=mode))


@harvest.command("intent-audit")
@click.argument("session_path", type=click.Path(exists=True))
@click.option("--no-llm", is_flag=True, help="Skip the LLM pass; just assemble material (no findings)")
@click.option("--model", default="sonnet", help="Model for the intent-fidelity audit pass")
@click.option("--max-budget-usd", default=2.0, type=float,
              help="USD cap for the claude -p audit pass (default: 2.0)")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def harvest_intent_audit(session_path, no_llm, model, max_budget_usd, as_json):
    """Intent-fidelity audit for ONE session: reconstruct what Jonathan was actually going for
    (in his own words) and judge whether the agent did that — not whether its output looks
    reasonable in isolation. Findings are validated against the SP1 evidence-record schema
    before they're trusted (see `orchestrator.harvest.intent_audit`)."""
    import json as json_mod
    from orchestrator.harvest import intent_audit

    out = intent_audit(session_path, use_llm=not no_llm, model=model, max_budget_usd=max_budget_usd)

    if as_json:
        click.echo(json_mod.dumps(out, indent=2, default=str))
        return

    if out["error"]:
        click.echo(f"⚠  intent audit did not run: {out['error']}")

    qualified, dropped = out["qualified"], out["dropped"]
    click.echo(f"Qualified ({len(qualified)}):")
    for f in qualified:
        title = f.get("title", "(untitled)") if isinstance(f, dict) else "(untitled)"
        click.echo(f"  ✓ {title}")
    click.echo(f"Dropped ({len(dropped)}):")
    for f in dropped:
        if isinstance(f, dict):
            title = f.get("title", "(untitled)")
            reason = f.get("_drop_reason", "")
            if "_raw" in f:
                click.echo(f"  ✗ {f['_raw']} — {reason}")
            else:
                click.echo(f"  ✗ {title} — {reason}")
        else:
            click.echo(f"  ✗ {f}")


@harvest.command("corpus")
@click.argument("initiative")
@click.option("--match", "match", default="", help="Comma-separated match terms (default: the initiative name)")
@click.option("--origin-k", default=6, type=int, help="How many earliest sessions (intent)")
@click.option("--recent-k", default=6, type=int, help="How many latest sessions (status/drift)")
@click.option("--json-output", "as_json", is_flag=True)
def harvest_corpus(initiative, match, origin_k, recent_k, as_json):
    """Assemble a cross-user, origin-anchored corpus for INITIATIVE (for Hal to read + judge)."""
    import json as json_mod
    from orchestrator.harvest import assemble_corpus, user_session_roots

    terms = [t.strip() for t in match.split(",") if t.strip()]
    corpus = assemble_corpus(initiative, terms, origin_k=origin_k, recent_k=recent_k)

    if as_json:
        click.echo(json_mod.dumps(corpus, indent=2, default=str))
        return

    roots = user_session_roots()
    click.echo(f"initiative: {corpus['initiative']}   confidence: {corpus['confidence'].upper()}")
    click.echo("  users seen: " + ", ".join(
        f"{r['user']}({'r' if r['readable'] else 'BLIND'})" for r in roots))
    if corpus["unreadable_users"]:
        click.echo("  ⚠ HALF-BLIND — unreadable: " + ", ".join(corpus["unreadable_users"]))
    sp = corpus["span"]
    click.echo(f"  sessions: {corpus['total_sessions']}  by user: {corpus['by_user']}"
               + (f"  span: {sp['from']} → {sp['to']}" if sp else ""))
    for label, key in (("ORIGIN (intent — read these for what you were going for)", "origin_sessions"),
                       ("RECENT (status/drift — did reality match intent?)", "recent_sessions")):
        click.echo(f"\n=== {label} ===")
        for s in corpus[key]:
            click.echo(f"  [{s['user']}] {s['when']}  {s['project']}")
            click.echo(f"     intent: {s['first_prompt'][:160]}")
            for m in s["human_messages"][:6]:
                click.echo(f"       · {m[:150]}")


@main.group("issue")
def issue():
    """Architect-routed GitHub issues with canopy.origin provenance.

    A ROUTE'd issue stays CLEAN + portable; the rich understanding (provenance, intent, evidence,
    POINTERS to the sessions the architect drilled) is a `canopy.origin/v1` record stored locally
    (and best-effort synced to canopy-web). Session transcripts stay local — recover them with
    `canopy harvest strip` only where they exist. See src/orchestrator/issue_origin.py.
    """


@issue.command("create")
def issue_create():
    """File a CLEAN GitHub issue + store its canopy.origin record. Reads a JSON record on STDIN:

    {"repo","title","mandate","done_when","initiative","ledger","created","confidence",
     "intent","evidence":[{"claim","session"}],
     "corpus":{"sessions_scanned","cross_user","drilled":[<local path>,...]}}
    """
    import json as json_mod
    import subprocess
    import sys as _sys
    from orchestrator import issue_origin as io

    data = json_mod.loads(_sys.stdin.read())
    repo = data["repo"]
    title = data["title"].strip()

    existing = io.find_existing_issue_number(repo, title)
    if existing is not None:
        click.echo(f"EXISTS: {repo}#{existing} already routes \"{title}\" — not duplicating.")
        return

    corpus = data.get("corpus") or {}
    rec = io.build_record(
        repo=repo, initiative=data.get("initiative", ""), ledger=data.get("ledger", ""),
        created=data["created"], confidence=data.get("confidence", "medium"),
        mandate=data.get("mandate", ""), done_when=data.get("done_when", ""),
        intent=data.get("intent", ""), evidence=data.get("evidence") or [],
        sessions_scanned=corpus.get("sessions_scanned", 0), cross_user=corpus.get("cross_user", False),
        drilled=corpus.get("drilled") or [],
    )
    rec["title"] = title

    created = subprocess.run(
        ["gh", "issue", "create", "-R", repo, "--title", f"[architect] {title}",
         "--body", io.clean_issue_body(rec)],
        capture_output=True, text=True,
    )
    if created.returncode != 0:
        click.echo(f"gh issue create FAILED: {created.stderr.strip()}", err=True)
        raise SystemExit(1)
    url = created.stdout.strip().splitlines()[-1]
    number = int(url.rsplit("/", 1)[-1])
    rec["number"] = number
    rec["issue"] = f"{repo}#{number}"

    # finalize the issue body now that we know the number (footer points back at the record)
    subprocess.run(["gh", "issue", "edit", str(number), "-R", repo, "--body", io.clean_issue_body(rec)],
                   capture_output=True, text=True)

    io.save_local(rec)
    ok, msg = io.web_sync(rec)
    click.echo(f"FILED: {url}")
    click.echo(f"  record: {io.record_path(repo, number)}  ·  {msg}")


@issue.command("context")
@click.argument("ref")
def issue_context(ref):
    """Hydrate the understanding behind an architect issue. REF = `owner/repo#number`.

    Reads the local record; falls back to canopy-web (/api/issues) if there's no local copy."""
    from orchestrator import issue_origin as io

    if "#" not in ref:
        raise click.ClickException("REF must be owner/repo#number")
    repo, num = ref.rsplit("#", 1)
    number = int(num)
    rec = io.load_local(repo, number) or io.web_fetch(repo, number)
    if rec is None:
        raise click.ClickException(
            f"No canopy.origin record for {ref} — not local and not on canopy-web.")
    click.echo(io.render_context(rec))


@issue.command("delete")
@click.argument("ref")
@click.option("--close-gh", is_flag=True, help="Also close the GitHub issue (gh issue close)")
def issue_delete(ref, close_gh):
    """Delete an architect issue's record (cleanup). REF = `owner/repo#number`.

    Removes the canopy-web record AND the local copy. With --close-gh, also closes the GitHub issue."""
    import subprocess
    from orchestrator import issue_origin as io

    if "#" not in ref:
        raise click.ClickException("REF must be owner/repo#number")
    repo, num = ref.rsplit("#", 1)
    number = int(num)
    web_ok, web_msg = io.web_delete(repo, number)
    local_ok = io.delete_local(repo, number)
    click.echo(f"record: {web_msg}; local {'removed' if local_ok else 'none'}")
    if close_gh:
        r = subprocess.run(["gh", "issue", "close", str(number), "-R", repo,
                            "-c", "Closed via canopy issue delete (architect cleanup)."],
                           capture_output=True, text=True)
        click.echo("gh issue closed" if r.returncode == 0 else f"gh close failed: {r.stderr.strip()}")


@main.group("agent-publish")
def agent_publish():
    """Publish an agent repo to its canopy-web workspace (/agents/<slug>).

    Run from an agent repo root (or pass --repo). Resolves identity from the repo's
    .claude-plugin/plugin.json + optional config/agent.json. Needs a canopy-web PAT
    (CANOPY_WEB_PAT or ~/.claude/canopy/workbench-token).
    """


def _agent_repo(repo):
    from pathlib import Path
    return Path(repo).expanduser() if repo else Path.cwd()


@agent_publish.command("register")
@click.option("--repo", default=None, type=click.Path(), help="Agent repo (default: cwd)")
def agent_publish_register(repo):
    """Register (idempotent) the agent as a first-class canopy-web agent."""
    import json as json_mod
    from orchestrator.agent_web import register, AgentWebError
    try:
        click.echo(json_mod.dumps(register(_agent_repo(repo))))
    except AgentWebError as e:
        raise click.ClickException(str(e))


@agent_publish.command("skills")
@click.option("--repo", default=None, type=click.Path(), help="Agent repo (default: cwd)")
def agent_publish_skills(repo):
    """Mirror the agent's skills/*/SKILL.md into its canopy-web skill catalog."""
    import json as json_mod
    from orchestrator.agent_web import put_skills, register, AgentWebError
    try:
        register(_agent_repo(repo))            # ensure the agent exists first
        click.echo(json_mod.dumps(put_skills(_agent_repo(repo))))
    except AgentWebError as e:
        raise click.ClickException(str(e))


@agent_publish.command("sync")
@click.option("--repo", default=None, type=click.Path(), help="Agent repo (default: cwd)")
@click.option("--doc-url", required=True)
@click.option("--title", required=True)
@click.option("--summary", default="")
@click.option("--grades", default="{}", help='JSON, e.g. \'{"work":"B+","skills":"A-"}\'')
@click.option("--period-start", required=True)
@click.option("--period-end", required=True)
@click.option("--source", default="manager-sync")
def agent_publish_sync(repo, doc_url, title, summary, grades, period_start, period_end, source):
    """Post a sync (the gdoc is the body; grades + summary land on the feed card)."""
    import json as json_mod
    from orchestrator.agent_web import post_sync, register, AgentWebError
    try:
        register(_agent_repo(repo))
        out = post_sync(
            _agent_repo(repo), doc_url=doc_url, title=title, summary=summary,
            grades=json_mod.loads(grades), period_start=period_start,
            period_end=period_end, source=source,
        )
        click.echo(json_mod.dumps(out))
    except AgentWebError as e:
        raise click.ClickException(str(e))


@agent_publish.command("work")
@click.option("--repo", default=None, type=click.Path(), help="Agent repo (default: cwd)")
@click.argument("items_json", type=click.Path(exists=True))
def agent_publish_work(repo, items_json):
    """Push work products from a JSON file: [{title,kind,url,description,tags,source}]."""
    import json as json_mod
    from orchestrator.agent_web import push_work, register, AgentWebError
    try:
        register(_agent_repo(repo))
        items = json_mod.load(open(items_json))
        click.echo(json_mod.dumps(push_work(_agent_repo(repo), items)))
    except AgentWebError as e:
        raise click.ClickException(str(e))


@agent_publish.command("items")
@click.option("--repo", default=None, type=click.Path(), help="Agent repo (default: cwd)")
@click.argument("items_json", type=click.Path(exists=True))
def agent_publish_items(repo, items_json):
    """Post a review-items batch from a JSON file (must be a JSON list)."""
    import json as json_mod
    from orchestrator.agent_web import push_items, register, AgentWebError
    try:
        register(_agent_repo(repo))
        items = json_mod.load(open(items_json))
        if not isinstance(items, list):
            raise click.ClickException("items file must be a JSON list")
        click.echo(json_mod.dumps(push_items(_agent_repo(repo), items)))
    except AgentWebError as e:
        raise click.ClickException(str(e))


@main.group("openclaw-harvest")
def openclaw_harvest():
    """Bridge a live OpenClaw into the canopy fleet — snapshot, compare to its GitHub repo, and
    bootstrap a new agent or reconcile its latest skills/ideas in. OpenClaw content is read-safe;
    credential files are never pulled. See docs/agent-operating-model.md."""


@openclaw_harvest.command("snapshot")
@click.argument("host")
@click.option("--into", "into", required=True, type=click.Path(), help="Local dir to pull into")
@click.option("--root", default="~/.openclaw", help="OpenClaw root on the host")
def openclaw_snapshot(host, into, root):
    """rsync an OpenClaw's readable workspace (persona/skills/memory, NOT creds) from HOST."""
    from orchestrator.openclaw_harvest import snapshot_via_ssh, HarvestError
    from pathlib import Path
    try:
        pulled = snapshot_via_ssh(host, Path(into), openclaw_root=root)
    except HarvestError as e:
        raise click.ClickException(str(e))
    click.echo(f"Pulled {len(pulled)} file(s) to {into}")
    for p in pulled[:40]:
        click.echo(f"  {p}")


@openclaw_harvest.command("inventory")
@click.argument("snapshot_dir", type=click.Path(exists=True))
@click.option("--json-output", "as_json", is_flag=True)
def openclaw_inventory(snapshot_dir, as_json):
    """Inventory a local OpenClaw snapshot (persona, skills, memory)."""
    import json as json_mod
    from orchestrator.openclaw_harvest import inventory_snapshot, HarvestError
    try:
        inv = inventory_snapshot(snapshot_dir)
    except HarvestError as e:
        raise click.ClickException(str(e))
    if as_json:
        click.echo(json_mod.dumps(inv, indent=2, default=str)); return
    click.echo(f"persona: {'present' if inv['has_persona'] else 'none'}  •  "
               f"skills: {len(inv['skills'])}  •  memory files: {len(inv['memory'])}")
    for s in inv["skills"]:
        click.echo(f"  skill {s['key']:<24} {s['description'][:70]}")


@openclaw_harvest.command("compare")
@click.argument("snapshot_dir", type=click.Path(exists=True))
@click.argument("agent")
@click.option("--json-output", "as_json", is_flag=True)
def openclaw_compare(snapshot_dir, agent, as_json):
    """Compare an OpenClaw snapshot to AGENT's canopy repo (slug or path). Says bootstrap vs reconcile."""
    import json as json_mod
    from orchestrator.openclaw_harvest import inventory_snapshot, compare, HarvestError
    from orchestrator.agent_review import resolve_agent_repo
    try:
        inv = inventory_snapshot(snapshot_dir)
    except HarvestError as e:
        raise click.ClickException(str(e))
    repo = resolve_agent_repo(agent)
    result = compare(inv, repo)
    if as_json:
        click.echo(json_mod.dumps(result, indent=2, default=str)); return
    click.echo(f"recommendation: {result['recommendation'].upper()}")
    click.echo(f"  {result['summary']}")
    if result["only_in_openclaw"]:
        click.echo("  only on the OpenClaw: " + ", ".join(result["only_in_openclaw"]))


@openclaw_harvest.command("bootstrap")
@click.argument("snapshot_dir", type=click.Path(exists=True))
@click.argument("slug")
@click.option("--name", default=None)
@click.option("--mandate", required=True)
@click.option("--mailbox", default="")
@click.option("--into", default=None, type=click.Path(), help="Target dir (default: ./<slug>)")
@click.option("--force", is_flag=True)
def openclaw_bootstrap(snapshot_dir, slug, name, mandate, mailbox, into, force):
    """Scaffold a NEW canopy agent repo seeded from an OpenClaw snapshot (persona + ported skills)."""
    import json as json_mod
    from pathlib import Path
    from orchestrator.openclaw_harvest import inventory_snapshot, bootstrap_from_snapshot, HarvestError
    from orchestrator.agent_factory import normalize_slug, AgentFactoryError
    try:
        slug = normalize_slug(slug)
        inv = inventory_snapshot(snapshot_dir)
        dest = Path(into).expanduser() if into else Path.cwd() / slug
        out = bootstrap_from_snapshot(
            inv, slug=slug, display_name=(name or slug.replace("-", " ").title()),
            mandate=mandate.strip(), into=dest, mailbox=mailbox, force=force,
        )
    except (HarvestError, AgentFactoryError) as e:
        raise click.ClickException(str(e))
    click.echo(json_mod.dumps(out, indent=2))
    click.echo(f"\nSeeded {out['repo']} — ported {len(out['ported_skills'])} OpenClaw skill(s). "
               "Refine persona.md, then ship.")


@openclaw_harvest.command("reconcile")
@click.argument("snapshot_dir", type=click.Path(exists=True))
@click.argument("agent")
def openclaw_reconcile(snapshot_dir, agent):
    """Port OpenClaw skills missing from AGENT's existing repo into it (stage a PR)."""
    from orchestrator.openclaw_harvest import inventory_snapshot, port_new_skills, HarvestError
    from orchestrator.agent_review import resolve_agent_repo
    try:
        inv = inventory_snapshot(snapshot_dir)
    except HarvestError as e:
        raise click.ClickException(str(e))
    repo = resolve_agent_repo(agent)
    if not repo:
        raise click.ClickException(f"could not resolve agent repo for {agent!r}")
    ported = port_new_skills(inv, repo)
    click.echo(f"Ported {len(ported)} skill(s) into {repo}: {', '.join(ported) or '(none)'}")
    if ported:
        click.echo("Review the bodies, then branch → PR → merge in the agent repo.")


@main.command("provision")
@click.option("--repo", default=None, type=click.Path(), help="Agent/provider repo (default: cwd)")
@click.option("--check", is_flag=True, help="Validate 1Password refs + show targets, write nothing")
@click.option("--json-output", "as_json", is_flag=True)
def provision_cmd(repo, check, as_json):
    """Materialize an agent's secrets from 1Password per its config/secrets.yaml.

    Portable: anyone with `op` access + the right grants runs this on any machine (incl. emdash
    worktrees) to get the creds an agent needs — no hand-shuffling keys. See provision.py.
    """
    import json as json_mod
    from pathlib import Path
    from orchestrator.provision import provision, ProvisionError

    target = Path(repo).expanduser() if repo else Path.cwd()
    try:
        result = provision(target, check=check)
    except ProvisionError as e:
        raise click.ClickException(str(e))

    if as_json:
        click.echo(json_mod.dumps(result, indent=2))
        return
    verb = "would provision" if check else "provisioned"
    line = f"{result['provisioned']} {verb}, {result['skipped']} skipped"
    if result["errors"]:
        line += f", {len(result['errors'])} error(s)"
    click.echo(line)
    for r in result["results"]:
        tgt = f" → {r['target']}" if r.get("target") else ""
        mode = f"  ({r['mode']})" if r.get("mode") else ""
        click.echo(f"  [{r['status']}] {r['name']}{tgt}{mode}")
    if result["errors"]:
        for e in result["errors"]:
            click.echo(f"  ! {e}")
        raise click.ClickException("provisioning had errors")
