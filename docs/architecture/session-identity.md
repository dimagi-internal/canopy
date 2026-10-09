# Session identity — who a session may act as (`canopy cred`)

> Contract for canopy#850 ("Design revision", Jonathan, 2026-10-09). Every implementing PR
> (canopy, canopy-web, the agent repos' MCP servers) follows this page. Part 2 of 4 of
> **shared agent plugins, separate identities**; setup is canopy#851.

## The problem

Identity used to live inside the plugin: `ace-gdrive` read `gws-sa-key.json` from its own plugin
data, so **any** session that loaded ACE's plugin acted as ACE. On 2026-10-09 an Ada session read a
doc through ace-gdrive and got "not found", because it was signed in as the wrong account. Identity
has to come from the **session**, not from whichever plugin happens to be loaded.

## 1. Pluggable backend: `1password` (default) or `canopy-web`

Per agent, a field on the canopy-web agent record: `credential_source: "1password" | "canopy-web"`.

- **`1password`** — the agent's values live in its vault (`Agent-<Slug>` by convention, or
  `config/agent.json` `op_vault`). `~/.<slug>/.env` is resolved with
  `op inject -f -i <agent repo>/.env.tpl -o ~/.<slug>/.env`.
- **`canopy-web`** — the values live in canopy-web's `AgentCredential` table and come back from
  `GET /api/agents/{slug}/credentials/resolve`.

canopy-web already holds each agent's 1Password service-account key, so compromising canopy-web
yields the secrets either way. Keeping values off canopy-web buys no real security; both backends
are first-class.

## 2. No repeated 1Password prompts

The broker resolves once and writes the existing per-agent env file `~/.<slug>/.env` (chmod 600) —
the same file `op inject` wrote before, so every `bin/` script and `_env.py` keeps working
unchanged. It re-resolves only when the file is missing, on `canopy cred refresh`, or when a caller
reports an auth failure (and then runs `canopy cred refresh`). Service-account reads never prompt.
`canopy cred check` caches canopy-web's `/access` answer and a successful 1Password probe for 10
minutes in `~/.canopy/cred-cache.json` (verdicts only, never values).

## 3. Who may resolve which agent

`canopy cred` decides; canopy-web enforces it for its own backend.

- **A runner turn** — a turn id is known (`$CANOPY_TURN_ID`, the caller envelope, or the laptop's
  by-task record) — may act only as its own agent (`$CANOPY_AGENT_SLUG`, else `$CANOPY_AGENT`, else
  the envelope's `agent`). Any other `--agent` is refused. It resolves through the runner-provided
  `OP_SERVICE_ACCOUNT_TOKEN` (scoped to its own vault), never through a desktop-app session.
- **A human session** may act as agent X when:
  - **X's env is already resolved on this machine**: `~/.X/.env` exists, is non-empty, is owned
    by this user and is private (no group/other bits). This is checked first and asks neither
    1Password nor canopy-web, so the one-time resolve really is one-time: a locked 1Password
    app does not refuse every new session (Jonathan, 2026-10-09). `--refresh` skips it and
    re-proves access. Runner turns never take this path. Otherwise:
  - backend `1password`: the user's **own** `op` can read X's vault (`op vault get <vault>`; a
    service-account key a hook staged into the session is tried only after the user's own `op`,
    and only reads that agent's own vault), or
  - backend `canopy-web`: `GET /api/agents/X/credentials/access` says `may_resolve` — the user
    pairs a runner X routes to (`via: "runner"`) or is X's owner/admin (`via: "admin"`). The
    `/resolve` route gains the admin-bearer path, audited like the runner path.
- **Otherwise** the session acts as the human, and the refusal says exactly what access to get.

If `/access` can't be reached, `canopy cred` assumes the default backend (`1password`): the
1Password probe still decides, so an unreachable canopy-web never grants anything by itself.

## 4. Agent-identity MCP servers

ace-gdrive, chrome-sales gdrive and canopy-gws (unregistered since 2026-10-09) call `canopy cred check --agent <their agent>`
before acting. In a session that isn't allowed to be that agent they return a clear refusal that
names the right path, and **never fall back to their bundled key**. A missing `canopy` CLI is a
refusal too.

## 5. Honest limit

On a single-user laptop every agent's `~/.<slug>/.env` sits under one Unix user, so isolation there
comes from rails (a turn reading another agent's env path is denied by the gating hook), not from
the operating system. Real isolation is on runner boxes, which stage only the turn's agent.

---

## The CLI contract

```
canopy cred check   [--agent X] [--refresh] [--json]
canopy cred env     [--agent X] [--refresh]
canopy cred refresh [--agent X]
canopy cred whoami  [--json]
```

`--agent` defaults to the session's agent: `$CANOPY_AGENT_SLUG` / `$CANOPY_AGENT`, the dispatched
turn's envelope, else the cwd's agent repo (`.claude-plugin/plugin.json` name). With none of those,
the command exits 2: `this session is not an agent … Pass --agent <slug>.`

| exit | meaning |
|---|---|
| 0 | allowed (`check`); env present/resolved (`env`, `refresh`) |
| 1 | error — resolve failed (no repo / no `.env.tpl`, `op inject` failed, canopy-web `/resolve` failed) |
| 2 | usage — no agent named and none detectable, or a malformed slug |
| 3 | **refused** — this session may not act as X; stderr carries one actionable paragraph |
| 4 | undetermined — `check` hit an unexpected error; treat as refused |

- **`check`** prints `ok: this session may act as 'X' — <reason>` on success. With `--json`, stdout
  is one JSON object: `{agent, allowed, identity: "agent-turn"|"human", source, via, reason,
  message, op_mode, vault, exit_code, notes}`. Refusal paragraphs:
  - turn for another agent: *This is agent 'ada's runner turn (…), so it may act only as 'ada' —
    not as 'ace'. To get work done as 'ace', dispatch it or ask it …*
  - `op` missing: *Acting as agent 'ace' needs its credentials from 1Password (vault 'Agent-Ace'),
    and the 1Password CLI is not installed. Install the 1Password CLI (…) and sign in
    (`op signin`), or ask the vault owner to share 'Agent-Ace' with you. Until then this session
    acts as you, not as 'ace'.*
  - not signed in / no vault access / canopy-web says no: same shape, naming `op signin`, the vault
    owner, or the agent admin on canopy-web (`/agents/X → Settings`).
- **`env`** ensures `~/.X/.env` (0600) — resolving only when missing or with `--refresh` — and
  prints **only its path** on stdout (a status line goes to stderr). Refused → exit 3, nothing
  written. The `canopy-web` backend writes each `values` entry whose name is an env identifier;
  others (e.g. `gog-token`) are staged by their own paths and listed, by name, on stderr.
- **`refresh`** drops the cached verdicts and re-resolves `~/.X/.env`.
- **`whoami`** prints `identity: agent 'X'` (runner turn) or `identity: human (you)`, plus `why:`.

No command prints a secret value.

### Calling it from an MCP server (Node/TS)

```ts
import { spawnSync } from "node:child_process";

/** Refuse unless this session may act as `agent`. Never fall back to a bundled key. */
export function assertActingAs(agent: string): void {
  const r = spawnSync("canopy", ["cred", "check", "--agent", agent], { encoding: "utf8" });
  if (r.error) throw new Error(`canopy CLI not available — refusing to act as ${agent}: ${r.error.message}`);
  if (r.status !== 0) throw new Error((r.stderr || "").trim() || `not allowed to act as ${agent}`);
}
// Then read credentials from ~/.<agent>/.env — `canopy cred env --agent <agent>` prints its path.
```

Python: `subprocess.run(["canopy", "cred", "check", "--agent", agent], capture_output=True,
text=True)`; non-zero → return the stderr as the tool's error text.

### canopy-web routes this depends on

- `GET /api/agents/{slug}/credentials/access` → `{agent, credential_source, may_resolve,
  via: "runner"|"admin"|null, reason}` — no values.
- `GET /api/agents/{slug}/credentials/resolve` (bearer) → `{values, op_vault, op_sa_token,
  shared_op_vault, shared_op_sa_token, github_token, salesforce_creds, mailbox}` — allowed for
  runner pairs and agent owners/admins.
