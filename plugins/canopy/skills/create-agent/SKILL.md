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
`config/gating.json` + `hooks/gating_guard.py` (the reads-free / writes-gated engine, shipped
deny-rails-only with an empty `approve` list), `.claude/settings.json` (wires the hook),
`bin/<slug>-email` (thin shim over the shared `canopy email` engine; raw `gog gmail send` is
deny-railed out of the box), `config/agent.json` (identity: mailbox + `gog_client`),
`config/secrets.yaml`, `config/allowlist.txt`, and the plugin manifest. Report the path and
file count back.

## Step 3 — Make it real (the part the factory can't do)
The scaffold is a skeleton. Walk the human through filling it in, in this order:
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
   payload to `hooks/gating_guard.py` (see the generated hook's docstring).
4. **Channel + setup** — email is already wired: mint the agent's own mailbox + gog OAuth
   client (named `<slug>`), `gog login <mailbox> --client <slug> --services gmail,drive,docs,sheets,forms,appscript`,
   verify with `uv run --project "$CANOPY_ROOT" canopy email preflight --repo .`, send via
   `bin/<slug>-email` (resolve `CANOPY_ROOT` with the same two-liner as Step 2 in each bash
   block). Then declare secrets in `config/secrets.yaml` and run
   `uv run --project "$CANOPY_ROOT" canopy provision`. Finish with
   `uv run --project "$CANOPY_ROOT" canopy agent doctor --repo .` — one command verifying
   identity, rails, manifest, gog
   auth, and canopy-web registration; it must be all-green before the agent's first turn
   (and re-run it on any NEW machine — it catches setup that only ever lived on the old one).

## Step 4 — Register & ship
- It's a Claude Code plugin: register it so the Skill tool can dispatch its skills (stronger than
  re-reading files — pair with the gating hook for real enforcement).
- Ship per the agent repo's own convention (branch → PR → merge yourself; the PR is a CI/record
  checkpoint, not a review gate). The runtime guardrail still stands: outbound actions need
  human approval at run time.

## Notes
- **Why its own repo:** independently shippable, team-runnable (a teammate clones one repo + runs
  setup), own secrets, own permission surface. Multi-operator = more people run the same repo —
  NOT one shared always-on instance (that topology bleeds contexts).
- **What the agent inherits later:** infra fixes (gating engine, channel adapters, canopy-web
  client) propagate via the shared kit version; domain-skill improvements arrive as canopy
  self-improvement PRs into the agent's repo.
- The factory is `src/orchestrator/agent_factory.py`; templates live there as the editable
  starting point every agent inherits.
