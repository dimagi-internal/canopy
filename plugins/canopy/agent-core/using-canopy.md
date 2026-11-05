# Using canopy — read this before you answer a question about how canopy works

> **Fleet-canonical (canopy agent-core).** It ships with the plugin, so a fix reaches every agent
> through `/canopy:update`. An agent's own `CLAUDE.md` carries only a pointer to this file, never a
> copy of it: canopy moves fast, and a copy goes stale in the repo where nobody is looking.

## The rule

A question about how canopy works ("how do my kids get Bailey?", "what do I approve?", "does this
run on a runner?") is answered in three steps:

1. **Read the page** below that covers it.
2. **Verify the specific claim against something that cannot drift** before you assert it:
   - the CLI: `canopy <command> --help` (e.g. `canopy agent use --help`);
   - live state: `who_is_asking`, `canopy agent doctor`, the canopy-web MCP (roles, membership,
     vault members, the agent's record);
   - the code: `canopy` (`src/orchestrator/`) and canopy-web (`apps/`).
3. **Say which it was.** "Docs say X; `--help` confirms it" or "docs say X; the code says Y — the
   doc is stale, filed as <task>". An answer resting on docs alone is labelled as such and is never
   the recommended path.

Two things are not evidence: how **this** session happens to be running (a cloud runner with the
vault open tells you nothing about how a person's own Claude Code works), and your own earlier
answer. If a person pushes back, re-read the page; don't build a new architecture from tool names.
If your recommendation contradicts a page you have just read, say so and reconcile it before sending.

## The two things that get confused

- **Plugin vs instance.** A *plugin* is an agent's code (skills, rails, persona) installed from its
  GitHub repo. An *instance* is canopy-web's `Agent` record: identity, workspace, vault. A local
  install is bound to exactly ONE instance (`/<plugin>:setup`).
- **Two ways to run an agent, both normal.** *Use it yourself* in your own Claude Code
  (`canopy agent use <slug>`, then `/<slug>:turn`), or *let it run on its own* on a runner
  (schedule, email, dispatch). The marketplace and the canopy-web MCP are how you **set up** the
  first way; they are not an alternative to it. The canopy README says "Agents are used by
  installing their plugin, not remotely."

## Where the truth is

| Question | Read | Then verify with |
|---|---|---|
| How do I use / run an agent? Local vs runner | `docs/running-an-agent.md` | `canopy agent use --help`, `canopy agent runs-as` |
| How does a person get an agent's plugin? The `canopy-agents` catalog | canopy-web `docs/architecture/plugin-marketplace.md` | the catalog (`GET /plugins/marketplace.json`), `canopy agent use` output |
| Who can do what; licences; turn modes | canopy-web `docs/architecture/access.md`, `roles.md` | `who_is_asking`, `canopy agent read-mode get --slug <slug>`, `canopy agent sites list --slug <slug>` |
| Runners, schedules, routing | canopy-web `docs/architecture/runners.md` | `canopy agent doctor`, the runner list |
| Setting up / vaults / device grants | `docs/architecture/agent-setup-lifecycle.md` | `canopy agent doctor`, the agent's vault members |
| Words used here (plugin, instance, runner, local turn…) | `docs/GLOSSARY.md` | — |
| How this repo's docs are kept honest | `docs/documentation.md` | — |

Docs are on GitHub (`dimagi-internal/canopy`, `dimagi-internal/canopy-web`); canopy is public, so
`https://github.com/dimagi-internal/canopy/blob/main/docs/<page>` reads without a login. If a page
is missing, wrong, or you had to guess: say so in your answer and file it (a board task or a canopy
issue). That is how the docs improve.
