# Authoring skills and commands

## Bash Positional Parameters
Bash code blocks in skill/command markdown files must NOT use `$1`, `$2`, etc.
for function arguments. Claude Code's slash-command argument expansion strips
these before the shell sees them, causing silent failures (empty strings).

**Workaround:** Move complex bash logic to standalone scripts in `bin/` or
`scripts/` and invoke them from the skill. Simple inline commands (no functions)
are fine.

## Command/Skill Name Collisions

If `commands/<name>.md` AND `skills/<name>/SKILL.md` both exist, the Skill tool
silently resolves `canopy:<name>` to the slash-command file — the actual
SKILL.md never lands in context, and the agent improvises from memory.

**Rule:** colliding commands MUST follow Pattern B — read SKILL.md from disk
explicitly before following it:

```bash
python3 -c "import json; d=json.load(open('$HOME/.claude/plugins/installed_plugins.json')); print(d['plugins']['canopy@canopy'][0]['installPath'] + '/skills/<name>/SKILL.md')"
```

Then `Read` that path and follow the SKILL.md exactly.

`tests/test_command_skill_collisions.py` enforces this — every colliding
command must reference `skills/<name>/SKILL.md` in its body. Adding a new
colliding command without Pattern B will fail CI.

## Built-in Command Namespace

Claude Code reserves a set of built-in slash command names (`/help`, `/clear`,
`/doctor`, `/config`, `/compact`, `/model`, `/fast`, `/login`, `/logout`,
`/agents`, `/mcp`, `/permissions`, etc.). Naming a plugin skill / command /
agent the same as a built-in causes a silent collision:

- `canopy:<name>` invocations route to the built-in handler, NOT your skill
- Worse, the entire skill description block can be truncated from the system
  prompt at session start

**Rule:** before naming a new skill/command/agent, check whether the bare name
already works as a slash command in a stock Claude Code session. If yes, pick a
different name. The canonical fix when a native built-in lands on top of an
existing plugin entry is to namespace-prefix it (e.g. `doctor` →
`canopy-doctor`).

`tests/test_builtin_command_collisions.py` enforces this — adding a new
plugin entry with a reserved name will fail CI. The reserved-set list lives
in that test; update it when Claude Code introduces new built-ins.

## Description Limits

Claude Code surfaces every installed skill's frontmatter `description` in the
system prompt at session start. Two budgets apply:

- **Per-skill cap**: Anthropic's authoring guidance recommends ≤1024 chars
  per description. Over the cap, Claude Code truncates.
- **Aggregate cap**: across all installed skills, the system prompt has a
  hard ceiling. Once it's hit, subsequent skills are dropped wholesale and
  Claude Code prints something like "N skills dropped" with no detail on
  which ones.

When a session reports dropped skills, run:

```bash
canopy skills budget               # ranked size table + aggregate gauge
canopy skills dropped              # simulated drop list under default caps
canopy skills dropped --aggregate-limit 30000   # try a different cap
```

The defaults (`--per-skill-limit 1024 --aggregate-limit 1500`) are
conservative — `--aggregate-limit` is the one to tune since the real
Claude Code ceiling is not publicly documented and shifts with releases.
