# Agent setup — one path per stage, shared by canopy and canopy-web

**Date:** 2026-10-15 · **Why:** Lex (Lilianna's agent) was created in the browser and stalled at
the vault: no step named it, the vault bootstrap was a bash script that scaffolded four EMPTY
items, the key was minted by a second tool with different permissions, and the agent's own
canopy token could only be minted on the server. Jonathan: *"maximally DRY the
creation/bootstrapping/validation of the agent setup between canopy and canopy-web."*

The rule: **each stage has exactly one implementation; every door calls it.** canopy-web is
the source of truth for state (what exists, what a box reported); canopy holds the logic that
needs a person's machine (1Password rights, a terminal); a box runs the same code a laptop
does.

| Stage | The one implementation | Doors that call it |
|---|---|---|
| Create the repo | `canopy_agent_factory` (templates + push) | canopy-web New-agent page; `/canopy:create-agent` |
| What's left to do | canopy-web `apps/agents/setup.py` (`GET /api/agents/<slug>/setup`) | Settings page, create wizard, `/canopy:create-agent` (MCP `agent_setup`) |
| Vault + its values + key | `canopy agent vault` (`src/orchestrator/agent_vault.py`) | the checklist's Vault step; `/canopy:create-agent`; Ada's `bin/ada-vault-provision` (a shim) |
| The agent's canopy identity | canopy-web `POST /api/agents/<slug>/canopy-token` | `canopy agent vault` only (MCP twin deny-railed) |
| Make a machine ready | `canopy agent bootstrap` (laptop) · `runner/ec2/bootstrap_agents.sh` (box) | — **phase 2 makes the box call the canopy one** |
| Is it working | `canopy agent doctor` (repo) · canopy-web `readiness` (box) | — **phase 3 makes doctor read the server's checks** |

## What a person types vs what the system produces

Each `op://${AGENT_VAULT}/<item>/<field>` in an agent's `.env.tpl` has a kind (`KINDS` in
`agent_vault.py` — the one table): **minted** (`canopy-pat`, by canopy-web), **linked**
(`gog-token`: Connect Google mailbox), **shared** (`gog-oauth-client`: the workspace's shared
vault), **unused** (`claude-oauth-token`: runner logins travel box to box), or **asked** — only
values a person alone has, like an outside service's key. Adding a value the fleet can produce
means adding it to `KINDS`, so no one is ever asked for it.

## Phases

1. **Vault (shipped 2026-10-15).** canopy-web: canopy-token route, Vault checklist step
   (`where: terminal`), stale `env_ok` cleared and boxes re-asked on a vault change,
   `deploy/secrets/bootstrap_1password.sh` deleted. canopy: `canopy agent vault`, the scaffold's
   live `CANOPY_WEB_PAT` line, baseline rails for the raw paths. ada: `bin/ada-vault-provision`
   → shim.
2. **One machine bootstrap.** `bootstrap_agents.sh` keeps box-level setup (packages, gog's file
   keyring) and calls `canopy agent bootstrap --service-account` per agent, which reads the
   agent's key from canopy-web instead of a person's 1Password. Drilled on a standby box first.
3. **One validation story.** `canopy agent doctor` reads `agent_setup` + `readiness` for every
   server-side fact and keeps only repo-local checks (rails fire, hooks, plugins), so the
   checklist and the doctor cannot disagree.
