---
description: Lead a huddle as this agent — a team of agents syncs and proposes work; or run a project check-in's scheduled turn (--checkin-run)
argument-hint: "[--checkin-run <id>] [--members a,b,c] [--days N] [--type work]"
allowed-tools: [Bash, Read, Write]
---

# Huddle

Any agent can lead a huddle when told to. You run it as YOURSELF: your own identity, repo,
canopy-web token, turn mode and sanctioned send path, and every ACL canopy-web enforces applies
(a member you may not run is "not reached", never forced).

Arguments: `$ARGUMENTS` (`--group-run <id>` is the old name of `--checkin-run <id>`: treat it the
same.)

## 1. A team binding wins

If the agent repo you are running in has its own huddle binding (a `skills/huddle/SKILL.md`,
such as Ada's, which wraps the engine as `bin/ada-huddle`), **follow that binding instead,
passing the same arguments.** Stop reading here. Only an agent with no binding uses the core
directly, below.

```bash
ls skills/huddle/SKILL.md 2>/dev/null && echo "BINDING: follow skills/huddle/SKILL.md with: $ARGUMENTS"
```

## 2. With `--checkin-run <id>`: a project check-in's scheduled run

Read the run first (how to find `CANOPY_ROOT`: agent-core `huddle.md` § Running the engine):

```bash
uv run --project "$CANOPY_ROOT" canopy checkin material --run <id> --json
```

Its `checkin.format` (from the check-in's agents) decides:

- **`solo`** (the check-in's only agent is you): follow agent-core `checkin.md`. Read the
  material, work the project, propose the outputs as tasks in the project
  (`canopy checkin propose`), then `canopy checkin finish`.
- **`huddle`** (several agents): lead the huddle by agent-core `huddle.md`, planning with
  `canopy huddle plan --checkin-run <id> --repo <your repo> --out plan.json` (§ Check-in runs).
  The check-in's lead person is the principal; no email is sent. The outputs are proposed tasks
  in the project, and `file` closes the run.

## 3. Without `--checkin-run`: a huddle you were asked to lead

Follow agent-core `huddle.md` from step 0, with `--members` (required), `--days` (default 7) and
`--type` (default `work`) as given. You need a priorities brief from whoever asked (step 1);
without one the engine runs the pre-brief huddle and warns. Send the step-6 digest only through
your own sanctioned send path, under your turn mode.

Read the doc you follow from disk; do not run it from memory:

```bash
python3 -c "import json; d=json.load(open('$HOME/.claude/plugins/installed_plugins.json')); p=d['plugins']['canopy@canopy'][0]['installPath']; print(p + '/agent-core/huddle.md'); print(p + '/agent-core/checkin.md')"
```
