---
description: Lead a huddle as this agent — a team of agents syncs and proposes work; or run a work group's scheduled turn (--group-run)
argument-hint: "[--group-run <id>] [--members a,b,c] [--days N] [--type work]"
allowed-tools: [Bash, Read, Write]
---

# Huddle

Any agent can lead a huddle when told to. You run it as YOURSELF: your own identity, repo,
canopy-web token, turn mode and sanctioned send path, and every ACL canopy-web enforces applies
(a member you may not run is "not reached", never forced).

Arguments: `$ARGUMENTS`

## 1. A team binding wins

If the agent repo you are running in has its own huddle binding (a `skills/huddle/SKILL.md`,
such as Ada's, which wraps the engine as `bin/ada-huddle`), **follow that binding instead,
passing the same arguments.** Stop reading here. Only an agent with no binding uses the core
directly, below.

```bash
ls skills/huddle/SKILL.md 2>/dev/null && echo "BINDING: follow skills/huddle/SKILL.md with: $ARGUMENTS"
```

## 2. With `--group-run <id>`: a work group's scheduled run

Read the run first (how to find `CANOPY_ROOT`: agent-core `huddle.md` § Running the engine):

```bash
uv run --project "$CANOPY_ROOT" canopy huddle-group material --run <id> --json
```

- **The group has ONE agent** (its `group.agents` is just you): follow agent-core
  `work-group.md`. Read the material, work the group's project, file the outputs as tasks in that
  project, then `canopy huddle-group finish`.
- **Several agents:** lead the huddle by agent-core `huddle.md`, planning with
  `canopy huddle plan --group-run <id> --repo <your repo> --out plan.json` (§ Work-group runs).
  The group's lead person is the principal; no email is sent. The outputs are proposed projects
  people act on in the huddle UI, and `file` closes the run.

## 3. Without `--group-run`: a huddle you were asked to lead

Follow agent-core `huddle.md` from step 0, with `--members` (required), `--days` (default 7) and
`--type` (default `work`) as given. You need a priorities brief from whoever asked (step 1);
without one the engine runs the pre-brief huddle and warns. Send the step-6 digest only through
your own sanctioned send path, under your turn mode.

Read the doc you follow from disk; do not run it from memory:

```bash
python3 -c "import json; d=json.load(open('$HOME/.claude/plugins/installed_plugins.json')); p=d['plugins']['canopy@canopy'][0]['installPath']; print(p + '/agent-core/huddle.md'); print(p + '/agent-core/work-group.md')"
```
