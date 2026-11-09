"""Guard against canopy plugin names colliding with Claude Code built-in slash commands.

Background: When a plugin command/skill/agent shares a name with a built-in
Claude Code slash command (e.g. `/doctor`, `/help`, `/clear`), Claude Code
silently routes the qualified `canopy:<name>` invocation to the built-in
handler — and worse, the entire skill description block can be dropped
from the system prompt at session start. This was discovered the hard way
when `canopy:doctor` collided with the native `/doctor` and 142 skill
descriptions vanished. The skill was renamed to `canopy:canopy-doctor`
in v0.2.79.

The reserved set below is conservative (names confirmed reserved by Claude
Code at the time of writing). When Claude Code adds a new built-in that
collides with an existing canopy plugin, the test fails with the rename
guidance, NOT with a silent prompt-truncation regression in production.

If a future Claude Code release introduces a new built-in slash command,
add it to `RESERVED_BUILTINS` here. If a canopy contributor introduces a
new skill/command/agent that lands on a reserved name, the test fails
with a rename suggestion before the change ships.
"""

from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent / "plugins" / "canopy"
COMMANDS_DIR = PLUGIN_ROOT / "commands"
SKILLS_DIR = PLUGIN_ROOT / "skills"
AGENTS_DIR = PLUGIN_ROOT / "agents"

# Claude Code built-in slash commands. Adding a plugin entry with one of
# these names triggers a silent routing collision and (historically) a
# system-prompt truncation that drops 100+ skill descriptions.
#
# Rule of thumb for adding to this set: if `/<name>` works in a stock
# Claude Code session before any plugins are installed, it belongs here.
RESERVED_BUILTINS: frozenset[str] = frozenset({
    "help",       # /help — built-in help
    "clear",      # /clear — clear conversation
    "doctor",     # /doctor — diagnose Claude Code install (collides with canopy:doctor)
    "config",     # /config — open settings
    "compact",    # /compact — manual context compaction
    "model",      # /model — switch model
    "fast",       # /fast — toggle fast mode
    "bug",        # /bug — file bug report
    "exit",       # /exit — exit session
    "quit",       # /quit — exit session
    "logout",     # /logout — sign out
    "login",      # /login — sign in
    "ide",        # /ide — IDE integration
    "vim",        # /vim — vim mode
    "release-notes",  # /release-notes — show release notes
    "cost",       # /cost — show session cost
    "memory",     # /memory — manage memory
    "agents",     # /agents — list agents
    "mcp",        # /mcp — manage MCP servers
    "permissions",  # /permissions — manage permissions
})


def _collect_plugin_names() -> dict[str, list[str]]:
    """Return {name: [kinds...]} for every plugin entry across skills/commands/agents."""
    names: dict[str, list[str]] = {}

    if SKILLS_DIR.exists():
        for skill_dir in SKILLS_DIR.iterdir():
            if skill_dir.is_dir() and (skill_dir / "SKILL.md").is_file():
                names.setdefault(skill_dir.name, []).append("skill")

    if COMMANDS_DIR.exists():
        for cmd in COMMANDS_DIR.glob("*.md"):
            names.setdefault(cmd.stem, []).append("command")

    if AGENTS_DIR.exists():
        for agent in AGENTS_DIR.glob("*.md"):
            names.setdefault(agent.stem, []).append("agent")

    return names


def test_no_plugin_name_collides_with_builtin():
    """No canopy plugin entry may share its name with a Claude Code built-in."""
    plugin_names = _collect_plugin_names()
    collisions = {n: kinds for n, kinds in plugin_names.items() if n in RESERVED_BUILTINS}
    assert not collisions, (
        "Plugin name collides with Claude Code built-in slash command. "
        "This causes silent system-prompt truncation (100+ skill descriptions "
        "can vanish). Rename the plugin entry — e.g. `doctor` → `canopy-doctor`. "
        f"Collisions: {collisions}. "
        f"Reserved built-ins: {sorted(RESERVED_BUILTINS)}"
    )


@pytest.mark.parametrize("reserved", sorted(RESERVED_BUILTINS))
def test_reserved_set_does_not_drift_from_plugin_inventory(reserved):
    """Sanity: reserved entries are lowercase hyphen-friendly slugs.

    Catches typos that would silently weaken the guard above.
    """
    assert reserved == reserved.lower(), f"reserved name `{reserved}` must be lowercase"
    assert all(c.isalnum() or c == "-" for c in reserved), (
        f"reserved name `{reserved}` contains unexpected characters"
    )
