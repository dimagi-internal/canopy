# Agent setup — one path per stage, shared by canopy and canopy-web

**Rule: each stage has exactly one implementation, and every entry point calls it** (the browser and the
CLI share it). canopy-web is the source of truth for state (what exists, what a box reported); canopy holds
the logic that needs a person's machine (a terminal); a box runs the same code a laptop does.

| Stage | The one implementation | Entry points that call it |
|---|---|---|
| Create the repo | `canopy_agent_factory` (templates + push) | canopy-web New-agent page; `/canopy:create-agent` |
| What's left to do | canopy-web `apps/agents/setup.py` (`GET /api/agents/<slug>/setup`) | Settings page, create wizard, `/canopy:create-agent` (MCP `agent_setup`) |
| Vault + its values | **canopy vault** (canopy-web; zero-knowledge: `canopy vault zk`, `vault_zk_cli.py`) | the agent's Settings → Security tab; the checklist's Vault step; `/canopy:create-agent` |
| A device's access to the vault | canopy-web's pending grants (`GET …/vault/zk`), granted with a holder's key | **Grant pending** on `/agents/<slug>`, then **Give them access** (Settings → Security); `canopy vault zk grant --agent X`; `canopy agent use <slug>` says when one is missing |
| The agent's canopy identity | canopy-web `POST /api/agents/<slug>/canopy-login` (sealed into canopy vault, never shown) | Settings → Security → **Give it its canopy login** |
| Use an agent yourself, no runner | `canopy agent use <slug>` (`src/orchestrator/agent_use.py`) | a person on their own computer ([running-an-agent.md](../running-an-agent.md)) |
| Make an agent runnable here | `canopy agent prepare` (`src/orchestrator/agent_prepare.py`; person mode is `canopy agent bootstrap`) | cloud box bootstrap and each claim; laptop runner at claim |
| Is it working | fleet facts: canopy-web `agent_setup` (+ `readiness`); this machine: `canopy agent doctor` | the doctor's `canopy-web setup` check (`src/orchestrator/agent_doctor.py::check_setup`) reads `agent_setup` |

`agent_prepare.py`'s module docstring lists its steps in order and the two credential modes (person vs
`--runner`). It is the per-agent, idempotent preparation: do not add a second one (a batch-at-boot pass
goes stale between claims). The doctor keeps only what is true of THIS machine (local auth, plugins, rails
firing, the materialized per-agent env file); every fleet fact comes from `agent_setup`, so the doctor and
the Settings page cannot disagree.

## What a person types vs what the system produces

Each credential in an agent's Canopy vault has a kind: **minted** (`canopy-pat`, by canopy-web),
**linked** (`gog-token`: Settings → External accounts → Connect Google), **shared** (`gog-oauth-client`:
the workspace's shared vault), or **asked**: only values a person alone has, like an outside service's key.
