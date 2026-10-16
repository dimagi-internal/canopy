---
name: create-agent
description: >
  Scaffold a new Claude Code agent from the canopy operating model — a turn-driven persona with
  reads-free / writes-gated guarding (a config-driven PreToolUse hook), a `turn` orchestrator,
  self-review, and a canopy-web-ready layout, grounded in the primitives proven by echo. Use when
  asked to "create an agent", "scaffold a new agent", "start a new agent like echo", or
  "spin up agent <name>". Generates the agent in its OWN git repo; you then fill in persona +
  domain skills. See docs/agent-operating-model.md.
---

# Create Agent — stamp out a new agent from the operating model

This scaffolds a **new agent in its own git repo** that leverages the common canopy operating
model. Each agent is independently shippable and team-runnable; the shared framework is what
lets a fleet improve together. Read `docs/agent-operating-model.md` (§1 primitives, §4a topology,
§6.6 gating) if you want the why.

## Step 1 — Gather the spec
You need, at minimum, a **slug** (lowercase id, e.g. `sales`) and a **mandate** (one line). Ask
the human for any you don't have; infer sensible defaults for the rest:
- `slug` — 2-31 chars, lowercase letters/digits/hyphen, starts with a letter, not a Claude Code
  built-in name (`doctor`, `config`, `model`, `review`, …). The factory rejects bad/colliding slugs.
- `--name` — display name (defaults to the slug, title-cased).
- `--mandate` — the one-line mission. **Required.**
- `--mailbox` — the agent's primary channel address (e.g. `sales@dimagi-ai.com`), if it has one.
- `--stakeholders` — who it serves.

## Step 2 — Generate
Run the factory (it creates the repo, does `git init` + an initial commit by default):
```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
uv run --project "$CANOPY_ROOT" canopy create-agent <slug> \
  --name "<Display Name>" \
  --mandate "<one-line mission>" \
  --mailbox "<address>" \
  --stakeholders "<who it serves>" \
  --into <path>            # default: ./<slug>
```
This writes ~15 files: `persona.md`, `CLAUDE.md`, the `turn` + `self-review` skills,
`config/gating.json` (the reads-free / writes-gated rails, shipped deny-rails-only with an empty
`approve` list — enforced by canopy's own session gating hook, so the agent registers NO hook of its
own; canopy#849), `.claude/settings.json` (sets `CANOPY_AGENT=<slug>`, which tells canopy whose
session it is),
`bin/<slug>-email` (thin shim over the shared `canopy email` engine; raw `gog gmail send` is
deny-railed out of the box), `config/agent.json` (identity: mailbox + `gog_client`),
`.env.tpl` (the fleet-standard secrets template — see agent-core/agent-runtime.md;
**never write a real `op://<vault>/<item>/<field>` in a comment**, `op inject` resolves those
too — only the angle-bracket placeholder; refs read `op://${AGENT_VAULT}/<item>/<field>` and
never name a vault), `skills/setup/SKILL.md` (`/<slug>:setup` — binds the install to ONE canopy-web
instance and fills `~/.<slug>/.env` from that instance's recorded vault via `canopy agent env`),
`config/allowlist.txt`, and the plugin manifest.
`config/allowlist.txt` is a **legacy fallback**: `canopy caller tier` consults it only while the
agent has no declared interface on canopy-web. Once an interface is published, canopy-web
(interface + workspace membership) decides who may steer the agent and the file is not read —
grant people on canopy-web, not there.
Report the path and file count back.

**Or start it in the browser.** `https://canopy.dimagi.com/w/<workspace>/agents/new` does all of
Step 2 *and* registration in one go: it renders the same `canopy-agent-factory` scaffold, pushes it
with the owner's fine-grained GitHub token, registers the agent (the creator becomes its owner) and
saves that token as the agent's GitHub delegation. Pointed at a repo that already holds an earlier
attempt at this agent (its `plugin.json` names the slug) or only GitHub starter files, it adds the
missing template files and overwrites nothing. A repo started there is continued here: clone it and
pick up at Step 3.

## Step 3 — Register (once)
Look the agent up first with the canopy-web MCP tool `get_agent` (`slug`):
- **It exists and its `repo_url` is this repo** (created in the browser, or registered on an earlier
  run) → do NOT re-register. Go to Step 4.
- **It exists with a different `repo_url`** → the slug is taken by another agent. Stop and ask the
  human; never overwrite it.
- **It does not exist** → push the repo to GitHub, then register from the repo root with
  `uv run --project "$CANOPY_ROOT" canopy agent-publish register` (resolve `CANOPY_ROOT` with the
  same two-liner as Step 2). If the human belongs to more than one workspace, add
  `--workspace <slug>` naming the agent's home — canopy-web will not guess it.

## Step 4 — Walk the setup checklist
canopy-web owns what is left to do. Call the MCP tool **`agent_setup`** (`slug`, plus `workspace`
if you belong to more than one organisation) and walk the human through every step whose `status`
is not done, in the order returned, using each step's own `detail` and `href`/`action`. Do not
restate or second-guess those steps here — the checklist is computed from canopy-web's own checks,
so re-call `agent_setup` after each fix to confirm it turned done, rather than judging it yourself.

A step with **`where: terminal`** (the vault: `canopy agent vault --slug <slug>`) needs the
human's OWN access — 1Password rights to create a vault and mint its key. Hand them its `command`
to run themselves (in this session, `! <command>`): it asks, with hidden input, for the few values
only they know, and that prompt needs their terminal. It mints the agent's canopy token and vault
key itself and prints neither.

The one step that happens in Claude Code is **`job`** (`where: claude-code`): the agent's first
real work. Its `command` starts that session; in it, apply the judgment below.

### The `job` step — what the factory and the checklist can't do
1. **`persona.md`** — voice, mandate detail, and what (if anything) is worth persisting as memory
   (per-counterpart facts only — behaviors become skills, not memories).
2. **First domain skill** — add `skills/<name>/SKILL.md` for the agent's actual job. Capability
   logic goes in a CLI/MCP tool; the skill orchestrates (keeps it portable). A skill is **already
   launchable** — Claude Code exposes it as the slash command `/<slug>:<name>` with no wrapper
   (custom commands were merged into skills; see https://code.claude.com/docs/en/skills). So do
   NOT hand-write a `commands/<name>.md` to make a skill launchable. Control invocation with
   frontmatter instead:
   - default (nothing set) → both a human (`/<slug>:<name>`) and Claude can invoke it. This is
     right for most entry-point skills.
   - `user-invocable: false` → Claude-only. Set this on **internal** skills — pipeline sub-steps
     and shared utilities that only make sense when another skill drives them (the scaffold ships
     `task-tracker` and `agent-turn-review` this way). Keeps them out of the `/` menu.
   - `disable-model-invocation: true` → human-only. Set this on side-effecting actions whose
     timing you must control (a `/deploy`-style command).
3. **Gating rules — rails, not gates** (operating model §1a revision): for EVERY outbound
   action the agent will take (send on a channel, public write), add a `deny` rail to
   `config/gating.json` that blocks the wrong path and NAMES the sanctioned one. Keep
   `approve` empty — a PreToolUse "ask" is a blocking modal that stalls autonomous work;
   approval lives procedurally in the turn checklist. **This is how you "force" the
   guardrail** — do not rely on prose in `CLAUDE.md`. Test a rule by piping a PreToolUse
   payload to canopy's engine as the agent's session would:
   `echo '{"tool_name":"Bash","tool_input":{"command":"gog gmail send --to x"}}' | CLAUDE_PROJECT_DIR=$PWD python3 "$CANOPY/agent-core/gating_guard.py" --session; echo $?`
   (exit 2 = blocked). Never register a PreToolUse gating hook in the agent itself: plugins
   install user-scope, so it would fire in every other agent's sessions too.

Secrets the domain skill needs go in `.env.tpl` (see the warnings in Step 2) and resolve with
`/<slug>:setup`. canopy-web can't see this machine, so before the first turn on a laptop also run
`uv run --project "$CANOPY_ROOT" canopy agent doctor --repo .` (and again on any new machine).

Ship repo changes per the agent repo's own convention (branch → PR → merge yourself; the PR is a
CI/record checkpoint, not a review gate). Outbound actions still need human approval at run time.

## Notes
- **Why its own repo:** independently shippable, team-runnable (a teammate clones one repo + runs
  setup), own secrets, own permission surface. Multi-operator = more people run the same repo —
  NOT one shared always-on instance (that topology bleeds contexts).
- **What the agent inherits later:** infra fixes (gating engine, channel adapters, canopy-web
  client) propagate via the shared kit version; domain-skill improvements arrive as canopy
  self-improvement PRs into the agent's repo.
- The factory is the standalone `canopy-agent-factory` package at
  `packages/canopy_agent_factory/` (published separately so a second consumer, e.g.
  canopy-web, can scaffold an agent repo without inheriting canopy's video-tooling deps);
  templates live there as the editable starting point every agent inherits.
