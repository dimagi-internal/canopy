# Agent setup — one path per stage, shared by canopy and canopy-web

**Rule:** every stage has one path, shared by the browser and the CLI.

The rule: **each stage has exactly one implementation; every entry point calls it.** canopy-web is
the source of truth for state (what exists, what a box reported); canopy holds the logic that
needs a person's machine (a terminal); a box runs the same code a laptop
does.

| Stage | The one implementation | Entry points that call it |
|---|---|---|
| Create the repo | `canopy_agent_factory` (templates + push) | canopy-web New-agent page; `/canopy:create-agent` |
| What's left to do | canopy-web `apps/agents/setup.py` (`GET /api/agents/<slug>/setup`) | Settings page, create wizard, `/canopy:create-agent` (MCP `agent_setup`) |
| Vault + its values | **canopy vault** (canopy-web; zero-knowledge: `canopy vault zk`, `vault_zk_cli.py`) | the agent's Settings → Credentials; the checklist's Vault step; `/canopy:create-agent` |
| A device's access to the vault | canopy-web's pending grants (`GET …/vault/zk`), granted with a holder's key | **Grant pending** on `/agents/<slug>`; `canopy vault zk grant --agent X`; `canopy agent use <slug>` says when one is missing |
| The agent's canopy identity | canopy-web `POST /api/agents/<slug>/canopy-login` (sealed into canopy vault, never shown) | Settings → Credentials → **Give it its canopy login** |
| Use an agent yourself, no runner | `canopy agent use <slug>` (`agent_use.py`) | a person on their own computer ([running-an-agent.md](../running-an-agent.md)) |
| Make an agent runnable here | `canopy agent prepare` (`agent_prepare.py`; person mode = `canopy agent bootstrap`) | cloud box bootstrap + each claim, laptop runner at claim — **phase 2 switches them over** |
| Is it working | fleet facts: canopy-web `agent_setup` (+ `readiness`); this machine: `canopy agent doctor` | the doctor's `canopy-web setup` check reads `agent_setup` |

## What a person types vs what the system produces

Each credential in an agent's Canopy vault has a kind: **minted** (`canopy-pat`, by canopy-web),
**linked** (`gog-token`: Connect Google mailbox), **shared** (`gog-oauth-client`: the workspace's
shared vault), or **asked** — only values a person alone has, like an outside service's key.

## Phases

1. **Vault (the Canopy vault is the only credential store).** canopy-web:
   canopy-token route, Vault checklist step, stale `env_ok` cleared and boxes re-asked on a
   vault change. canopy: baseline rails for the raw token paths.
2. **One agent preparation (in progress).** Not "port the bash": the batch-at-boot shape is
   itself wrong — the cloud runner had already grown per-claim patches (env refresh, lazy
   clone, pull) because a boot-time pass goes stale, and the laptop runner prepared nothing.
   So: `canopy agent prepare --slug <s> --runner` is the one per-agent, idempotent
   implementation (shipped first, unused); runners call it — the cloud box behind a per-box
   switch, drilled on a standby box, then the laptop runner at claim; then
   `bootstrap_agents.sh` shrinks to box provisioning (packages, gog's file keyring, the
   canopy plugin and CLI) and the unwired `canopy_runtime` reconciler is retired
   .
3. **One validation story.** `canopy agent doctor`'s `canopy-web setup`
   check reads `agent_setup` for every fleet fact (repo, GitHub, mailbox, vault, runner, job,
   first turn) instead of pinging the board; the doctor keeps only what is true of THIS
   machine (local auth, plugins, rails firing, the materialized `~/.<slug>/.env`: the env file exists and every var holds a value). Making the checklist trustworthy
   enough to read came first: its mailbox step now also accepts a box's `turn_ready`.
