"""`canopy decide-guard …` — maintain the fleet's decide-don't-offer Stop prompt hook.

The hook's prompt in `plugins/canopy/hooks/hooks.json` is GENERATED from
`plugins/canopy/agent-core/decide_guard_examples.jsonl`; these commands are the only
sanctioned way to change it. When `canopy agent-review` (or a human) finds an agent
handing back work it could have done — or a false block — add it as an example here.
"""
from __future__ import annotations

import json
from pathlib import Path

import click

from orchestrator import decide_guard_prompt as dg


def _root(repo: str | None) -> Path:
    """The canopy plugin root to edit: --repo, else the cwd's checkout, else this package's."""
    candidates = [Path(repo)] if repo else [Path.cwd(), *Path.cwd().parents]
    for base in candidates:
        plugin = base / "plugins" / "canopy"
        if (plugin / "agent-core" / dg.EXAMPLES_PATH.name).exists():
            return plugin
    if repo:
        raise click.ClickException(f"{repo} is not a canopy checkout (no {dg.EXAMPLES_PATH.name})")
    return dg.PLUGIN_ROOT


def _paths(repo):
    plugin = _root(repo)
    return plugin / "hooks" / "hooks.json", plugin / "agent-core" / dg.EXAMPLES_PATH.name


@click.group(name="decide-guard")
def decide_guard_group():
    """The decide-don't-offer Stop prompt hook (examples -> hooks.json)."""


@decide_guard_group.command("render")
@click.option("--repo", default=None, help="canopy checkout (default: the one containing cwd)")
def render_cmd(repo):
    """Make the plugin's hooks.json agree with the scope switch (PLUGIN_WIDE)."""
    hooks, examples = _paths(repo)
    changed = dg.sync_hooks_json(hooks, examples)
    click.echo(f"{'updated' if changed else 'unchanged'}: {hooks}")


@decide_guard_group.command("check")
@click.option("--repo", default=None)
def check_cmd(repo):
    """Exit 1 if hooks.json is out of sync with the examples file."""
    hooks, examples = _paths(repo)
    if not dg.in_sync(hooks, examples):
        raise click.ClickException("hooks.json is stale — run `canopy decide-guard render`")
    click.echo(f"in sync: {len(dg.load_examples(examples))} examples")


@decide_guard_group.command("stamp")
@click.option("--agent-repo", "agent_repos", multiple=True, hidden=True,
              type=click.Path(file_okay=False),
              help="RETIRED — an agent repo carries the hook to every runner owner")
@click.option("--user", "user", is_flag=True,
              help="your own ~/.claude/settings.json — opt in for every session on this "
                   "macOS account")
@click.option("--check", is_flag=True, help="exit 1 if your stamp is stale; write nothing")
@click.option("--repo", default=None, help="canopy checkout to read examples from")
def stamp_cmd(agent_repos, user, check, repo):
    """Opt in: stamp the rendered prompt hook into your own ~/.claude/settings.json.

    decide-guard is a PER-PERSON preference. Nothing else carries it: not the plugin, and
    not an agent repo — a repo's settings run for whoever runs the agent, and on the first
    runner not owned by Jonathan (stewari-mbp-cdp, Claude Code 2.1.220) the judge 400'd on
    every Stop and wedged the session (failure investigation 1, turn bb892462)."""
    if agent_repos:
        raise click.ClickException(
            "decide-guard is a per-person preference, not an agent's: an agent repo's "
            "settings run it for every person who runs that agent (failure investigation 1, "
            "turn bb892462). Opt yourself in with `canopy decide-guard stamp --user`; remove "
            "an agent repo's copy with `canopy decide-guard unstamp --agent-repo DIR`.")
    if not user:
        raise click.ClickException("opt in with --user (a per-person preference)")
    _, examples = _paths(repo)
    settings = dg.user_settings_path()
    if check:
        if not settings.exists() or not dg.settings_in_sync(settings, examples):
            raise click.ClickException(f"stale (re-run without --check): {settings}")
        click.echo(f"in sync: {settings}")
        return
    try:
        dg.require_claude_code_floor()
    except dg.ClaudeCodeTooOld as e:
        raise click.ClickException(str(e))
    changed = dg.stamp_settings(settings, examples)
    click.echo(f"{'stamped' if changed else 'unchanged'}: {settings}")


@decide_guard_group.command("unstamp")
@click.option("--agent-repo", "agent_repos", multiple=True,
              type=click.Path(exists=True, file_okay=False),
              help="agent repo to remove a (retired) decide-guard copy from (repeatable)")
@click.option("--user", "user", is_flag=True, help="opt out: your own ~/.claude/settings.json")
@click.option("--check", is_flag=True,
              help="exit 1 if any target still carries decide-guard; write nothing")
def unstamp_cmd(agent_repos, user, check):
    """Remove the decide-guard Stop hook (and any regex loader) — opt out, or retire an
    agent repo's copy. Every other hook in the file is kept."""
    if not agent_repos and not user:
        raise click.ClickException("name at least one --agent-repo, or --user")
    targets = [Path(a) / ".claude" / "settings.json" for a in agent_repos]
    if user:
        targets.append(dg.user_settings_path())
    carrying = []
    for settings in targets:
        if not settings.exists():
            continue
        if check:
            if dg.carries_decide_guard(settings):
                carrying.append(str(settings))
            continue
        changed = dg.unstamp_settings(settings)
        click.echo(f"{'removed' if changed else 'absent'}: {settings}")
    if carrying:
        raise click.ClickException("still carries decide-guard: " + ", ".join(carrying))
    if check:
        click.echo(f"clean: {len(targets)} target(s)")


@decide_guard_group.command("show")
@click.option("--repo", default=None)
def show_cmd(repo):
    """Print the rendered prompt."""
    _, examples = _paths(repo)
    click.echo(dg.render_prompt(dg.load_examples(examples)))


@decide_guard_group.command("add-example")
@click.option("--closing", required=True, help="the agent's closing message (verbatim)")
@click.option("--handback", required=True, type=click.Choice(["true", "false"]),
              help="true = it handed back a call it could have made (should block)")
@click.option("--why", required=True, help="one line: whose call it was and why")
@click.option("--in-prompt", is_flag=True,
              help="also render it into the judge's prompt (default: held-out eval set only)")
@click.option("--repo", default=None)
def add_example_cmd(closing, handback, why, in_prompt, repo):
    """Append a labelled example and re-render hooks.json. Bump the version and ship after."""
    hooks, examples = _paths(repo)
    try:
        row = dg.add_example(closing, handback == "true", why, examples, in_prompt=in_prompt)
    except ValueError as e:
        raise click.ClickException(str(e))
    dg.sync_hooks_json(hooks, examples)
    click.echo(json.dumps(row, ensure_ascii=False))
    if dg.PLUGIN_WIDE:
        click.echo(f"re-rendered {hooks} — now `uv run canopy version bump`, add a CHANGELOG line, and ship")
    else:
        click.echo("opt-in scope: ship this, then `canopy decide-guard stamp --user` on each "
                   "account that opted in")
