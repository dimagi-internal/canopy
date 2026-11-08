# Authoring skills and commands

## Bash positional parameters
Bash code blocks in skill/command markdown must NOT use `$1`, `$2`, etc. for function arguments. Claude
Code's slash-command argument expansion strips them before the shell sees them, so they silently become
empty strings. Move complex bash logic to a standalone script in `bin/` or `scripts/` and invoke it from
the skill; simple inline commands (no functions) are fine. `canopy structure-drift`
(`src/orchestrator/structure_drift.py`) audits this.

## Command/skill name collisions

If `commands/<name>.md` AND `skills/<name>/SKILL.md` both exist, the Skill tool silently resolves
`canopy:<name>` to the slash-command file. The SKILL.md never lands in context and the agent improvises
from memory.

**Rule:** a colliding command follows Pattern B: it reads SKILL.md from disk explicitly before following
it. Copy the shape from `plugins/canopy/commands/find-session.md` (resolve `installPath` from
`installed_plugins.json`, `Read` `<installPath>/skills/<name>/SKILL.md`, follow it exactly).
`tests/test_command_skill_collisions.py` fails CI for a colliding command whose body does not reference
`skills/<name>/SKILL.md`.

## Built-in command namespace

Claude Code reserves built-in slash-command names (`/help`, `/doctor`, `/config`, `/model`, ...). A plugin
skill, command or agent with the same name collides silently: `canopy:<name>` routes to the built-in
handler instead of your skill, and the skill description block can be truncated from the system prompt.

**Rule:** before naming a new skill/command/agent, check that the bare name is not already a slash command
in a stock Claude Code session; if it is, pick a different name (or namespace-prefix it, as
`canopy-doctor`). `tests/test_builtin_command_collisions.py` fails CI on a reserved name; its
`RESERVED_BUILTINS` is the list, and you update it when Claude Code adds built-ins.

## Description limits

Claude Code puts every installed skill's frontmatter `description` in the system prompt at session
start. A description over 1024 chars is truncated, and once the aggregate ceiling is hit, later skills are
dropped wholesale with no detail on which ones (the real ceiling is not published and shifts, so the
aggregate default is a calibration, not a fact). When a session reports dropped skills, run
`canopy skills budget` (ranked sizes) and `canopy skills dropped` (simulated drop list); the limits and
flags are in `src/orchestrator/skill_budget.py` and `--help`.
