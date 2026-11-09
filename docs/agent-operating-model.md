# The agent operating model

How a canopy agent is built, what it is made of, how the parts connect, and the rules every agent
runs under. The code and `agent-core/` hold the details; this page holds the shape and the
invariants the code does not state in one place. Words (plugin, instance, runner, turn mode) are
defined in `docs/GLOSSARY.md`.

## What an agent is

An agent is two things joined by a slug:

- **A plugin**: its own git repo holding only what is specific to it (persona, domain skills, its
  rails, its `bin/` capabilities), installed into Claude Code at user scope from the catalog.
- **An instance**: canopy-web's `Agent` record in one workspace (identity, board, vault, access,
  runner settings). A local install is bound to exactly one instance
  (`src/orchestrator/agent_instance.py::check_rebind`: switching needs `--force`).

`canopy create-agent` stamps a new repo from `packages/canopy_agent_factory/canopy_agent_factory/_factory.py`;
`templates()` there is the table of every file a stamped agent carries, and the baseline
`src/orchestrator/fleet_align.py` diffs real agents against. Edit templates in that file, not in an
agent.

## How the parts connect

| Lives in the agent's repo | Lives in canopy (plugin + `canopy` CLI) |
|---|---|
| `persona.md`: identity, mandate, voice, stakeholders | The turn procedure, `plugins/canopy/agent-core/turn.md`, and the other fleet procedures in `agent-core/` |
| `skills/turn/SKILL.md`: a stub that binds `agent-core/turn.md` to the agent's name, slug and mailbox | The gating engine and its registration, `agent-core/gating_guard.py`, and the fleet baseline rails, `agent-core/gating-baseline.json` |
| `config/gating.json`: the agent's own `deny` rails and its `channels` mounts | Channel adapters (`src/orchestrator/agent_email.py`; the agent's `bin/<slug>-email` is a shim over it), the canopy-web client (`src/orchestrator/canopy_web.py`), publishing engines |
| Domain skills, `bin/` capabilities, `config/agent.json`, `CANOPY_AGENT` in `.claude/settings.json` `env` | The self-improvement lens `src/orchestrator/agent_review.py`, `create-agent`, cross-agent skills |

The rule: **logic, adapters and cross-agent skills are canopy's; identity, rules, secrets and
domain skills are the agent's.** If a file would be byte-identical in every agent repo it belongs in
canopy, so a fix reaches the fleet through `/canopy:update` instead of N pull requests
(`agent-core/agent-runtime.md` § Sibling rule). Do not copy the engine into an agent: a copy stops
receiving fixes and its rails silently stop working. `canopy create-agent` stamps the stubs, never
the engine.

A turn is one unit of agent work. A schedule, an email, a dispatch, a board task or a person
triggers it; a runner or someone's own computer executes it. `agent-core/turn.md` sets its order:
Step 1 preflight, Step 2 process inbound one counterpart at a time, Step 3 skill self-check, Step 4
close. The agent's stub says "re-read this file every turn"; the file is the checklist. Who may steer
the agent, and whether outbound actions wait for a person, come from the turn's caller envelope
(`agent-core/turn.md` § Turn mode); access words are defined in canopy-web's `docs/architecture/access.md`.

canopy-web is the fleet's web surface: each agent has a workspace at `/agents/<slug>` with a kanban
board (the second trigger surface: people queue work without a keyboard; `agent-core/task-tracker.md`),
its turn history, its vault and its access list. `src/orchestrator/agent_web.py` registers an agent
repo's catalog there.

Channels: an agent's primary channel is its mailbox. `config/gating.json` `channels` mounts the
baseline rails for each channel the agent uses (`gating-baseline.json` has one block per channel).
A new channel is one adapter in canopy plus a mount in the agents that want it.

The self-improvement loop is canopy's, run from outside the agent: sessions and turns are read
(`src/orchestrator/scanner.py`, `transcripts.py`), observed, proposed and shipped as a PR to the
skill, hook or `CLAUDE.md` that holds the lesson (`src/orchestrator/pipeline.py`;
`agent_review.py::run_review` aims it at one agent's turns; `docs/architecture/session-analysis.md`
says which command to use). Inside a turn, Step 3 of `agent-core/turn.md` is the agent's own
version: it must say whether it made or should have made a skill, and fix friction where it lives.
What improves is the scaffolding (skills, hooks, rails, `CLAUDE.md`), never the model.

## Invariants

- **A hard rule is a rail, not a sentence.** Prose in `CLAUDE.md` is followed by a model choosing to
  comply and fails under load; a deny rail makes the wrong path impossible and names the right one.
  Rails are regexes over the tool call, so they stop honest mistakes, not a determined bypass
  (`agent-core/gating_guard.py` docstring). Add an outbound action and you add a rail pointing at its
  one sanctioned path.
- **Deny rails only; `approve` stays empty.** A PreToolUse "ask" is a blocking modal that stalls
  autonomous work and nags interactive sessions. Approval is procedural: the turn drafts the action
  and presents it (`agent-core/turn.md` Step 2) when the turn mode is `manual`. The factory's
  `_GATING_JSON` ships `"approve": []`, and `packages/canopy_agent_factory/tests/test_factory.py` pins it.
- **Reads are free; outbound follows the turn mode.** `manual` (the default) means a human approves
  anything that reaches a person or the public; `auto` means the agent self-reviews, acts within its
  rails and closes with a record for after-the-fact review. Turn mode is canopy-web state decided per
  turn, never repo config (`agent-core/turn.md` § Turn mode).
- **Agents register no gating hook; canopy registers one.** Agent plugins install at user scope, so a
  hook an agent registers fires in every session on the machine. canopy's plugin registers
  `gating_guard.py --session` (`plugins/canopy/hooks/hooks.json`), which finds the session's agent
  (`gating_guard.py::session_agent`: `$CANOPY_AGENT_SLUG`, then `$CANOPY_AGENT`, then the agent repo
  at or above the project dir, else nobody) and applies the baseline plus that agent's `deny` list.
  With nobody, only the channel-independent baseline rails apply. An agent's config can add rails and
  never remove a baseline one. `fleet_align.py::legacy_gating_hooks` and `canopy agent doctor` flag
  an agent that still registers its own hook.
- **The gating config is JSON and the engine is stdlib-only.** A PreToolUse hook runs under whatever
  `python3` is on PATH, often without PyYAML; the baseline is data read from the plugin directory, not
  an import of canopy (`gating_guard.py::load_baseline`). If the baseline is unreadable the hook fails
  closed for agents with channels mounted.
- **One counterpart, one memory scope per turn.** Never reason about two counterparts' threads
  together; it is what prevents cross-contamination (`agent-core/turn.md` Step 2; the factory's
  `_CLAUDE_MD` states it for every new agent).
- **Behaviour lives in skills and hooks, not memory.** A durable "always X / next time Y" goes into
  the skill that governs the task, in the same turn (`agent-core/turn.md` Step 3 item 4). Memory holds
  facts about people only, narrowly: canopy-web stores per-person facts and serves them as Human
  Context Protocol entries; the `caller_context` hook prints a short index of them beside every prompt
  (`plugins/canopy/hooks/caller_context.py`), and the agent records new ones in the session that has
  the context (`agent-core/turn.md` Step 4, `hcp_addPreference`). Keep memory behind the MCP/CLI
  boundary so it can be swapped.
- **Capability lives in CLIs and MCP tools; skills orchestrate.** A `SKILL.md` is declarative so the
  fleet stays portable across runtimes; logic belongs in `bin/` or `src/orchestrator/`.
- **No environment-varying value is committed to git.** Secrets and ids that differ between laptop and
  runner (folder, sheet, doc ids too) are in the agent's Canopy vault; nothing in an agent repo names
  one (`agent-core/agent-runtime.md`). `/<agent>:setup` runs `canopy agent env --plugin <plugin>`,
  which binds the install to one instance and fills `~/.<plugin>/.env` (0600) from its vault.
- **Everything the loop needs at runtime is tracked or globally resolved.** Turns often run in a fresh
  git worktree, which lacks gitignored files. So the env lives in the global home
  (`~/.<slug>/.env`, read by `bin/_env.py`), tools resolve from PATH or the plugin cache, and the
  gating hook uses `$CLAUDE_PROJECT_DIR`, which is the worktree root (`agent-core/agent-runtime.md`).
- **Reply quality is a fleet rule, not an agent's habit.** Answering each discrete ask, leading with
  what was done, deliverables as shared Google Docs with the draft shown inline, decide-then-show in
  one order, and recipients verified from the structured reader (reply-all) are stated once in
  `agent-core/turn.md` Step 2 and templated into every stamped agent's `turn` and `agent-turn-review`.
- **Borrow another project's capability, do not recreate it.** An agent declares the MCP servers it
  borrows in its own `.mcp.json` and names them in its `CLAUDE.md`. Reference a borrowed server by a
  PATH-resolved command, never a per-machine absolute path, and keep provider code private and creds
  per-operator (`docs/architecture/shared-gog-gdrive.md` § 5).

## Where to start reading

| To change | Read |
|---|---|
| What every new agent starts with | `_factory.py::templates`, then `packages/canopy_agent_factory/tests/test_factory.py` |
| The turn procedure | `plugins/canopy/agent-core/turn.md` |
| A rail or the session hook | `plugins/canopy/agent-core/gating_guard.py` (docstring first), `gating-baseline.json` |
| Where a config value or secret lives | `plugins/canopy/agent-core/agent-runtime.md` |
| Reviewing an agent's turns | `src/orchestrator/agent_review.py`, `docs/architecture/session-analysis.md` |
| Framework/product import direction | `src/orchestrator/TIERS.md` |
