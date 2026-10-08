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
@click.option("--agent-repo", "agent_repos", multiple=True,
              type=click.Path(exists=True, file_okay=False),
              help="agent repo whose .claude/settings.json gets the prompt hook (repeatable)")
@click.option("--user", "user", is_flag=True,
              help="your own ~/.claude/settings.json — a personal preference for every session "
                   "on this macOS account")
@click.option("--check", is_flag=True, help="exit 1 if any target is stale; write nothing")
@click.option("--repo", default=None, help="canopy checkout to read examples from")
def stamp_cmd(agent_repos, user, check, repo):
    """Opt-in scope: stamp the rendered prompt hook into each agent repo's
    .claude/settings.json and/or your own ~/.claude/settings.json (--user), replacing any
    hooks/decide_guard.py loader hook. The plugin itself carries no decide-guard."""
    if not agent_repos and not user:
        raise click.ClickException("name at least one --agent-repo, or --user")
    _, examples = _paths(repo)
    targets = []
    for agent in agent_repos:
        settings = Path(agent) / ".claude" / "settings.json"
        if not settings.exists():
            raise click.ClickException(f"{settings} not found")
        targets.append(settings)
    if user:
        targets.append(dg.user_settings_path())
    stale = []
    for settings in targets:
        if check:
            if not settings.exists() or not dg.settings_in_sync(settings, examples):
                stale.append(str(settings))
            continue
        changed = dg.stamp_settings(settings, examples)
        click.echo(f"{'stamped' if changed else 'unchanged'}: {settings}")
    if stale:
        raise click.ClickException("stale (re-run without --check): " + ", ".join(stale))
    if check:
        click.echo(f"in sync: {len(targets)} target(s)")


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
        click.echo("opt-in scope: ship this, then re-stamp — `canopy decide-guard stamp "
                   "--agent-repo <repo>` in each agent repo that carries it (and ship those), "
                   "and `--user` on each account that opted in")
