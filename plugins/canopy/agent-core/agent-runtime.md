# Agent config + secrets — where every value lives

> **Fleet-canonical process (canopy agent-core).** Where an agent's configuration values live and
> how they reach a turn. Read this before adding ANY new id, key, folder, or account to an agent.

## Sibling rule: CODE lives in canopy, CONFIG lives in the agent

The rule above governs *values*. This one governs *files*, and it is the same idea one level up:

| Belongs to the agent repo | Belongs to canopy |
|---|---|
| `config/agent.json`, `config/gating.json` (its own rails + `channels`) | the gating **engine** (`agent-core/gating_guard.py`) |
| persona, domain skills, its own `bin/` capabilities | the publishing engines (`canopy gdoc` / `canopy gsheet`) |
| anything genuinely specific to what this agent *is* | anything every agent would want identically |

**If a file would be byte-identical in every agent repo, it does not belong in an agent repo.**
Ship it with the plugin and have the agent load it, so a fix reaches the fleet via
`/canopy:update` instead of N pull requests.

Why this is written down (2026-08-13): the deny **rails** were centralized here from the start,
but `hooks/gating_guard.py` — the engine that reads them — was *copied* into each agent at scaffold
time and never updated. Config was shared; code was forked. Measured across four agents, every
predicted symptom had arrived: three copies were behind and silently missing rail features, so
rails that existed did nothing for them; a one-line fix needed one PR per agent; and worst,
**improvements flowed the wrong way** — ada had built `per_statement` against a real false positive
and it stayed in ada for three weeks, unusable by anyone else, while another agent was bitten by a
sibling of the same bug. The engine now ships with the plugin and each `hooks/gating_guard.py` is a
loader with no agent-specific text in it at all.

**And the hook registration moved too (canopy#849, 2026-10-09).** Agent plugins are installed at
user scope, so a hook an agent plugin registers fires in EVERY session on the machine: ACE's
body-file rail blocked a `gh pr create` in an Ada session. canopy's own plugin now registers the
one gating hook (`agent-core/gating_guard.py --session`). It works out whose session it is —
`$CANOPY_AGENT_SLUG`, then `$CANOPY_AGENT`, then the agent repo (`config/gating.json` or
`config/agent.json`) at or above the project dir, else nobody — and applies the fleet baseline plus
that agent's `deny` list. Nobody's session gets only the channel-independent `always` rails. An
agent repo keeps `config/gating.json` and sets `CANOPY_AGENT` in `.claude/settings.json` `env`; it
registers **no** PreToolUse gating hook and ships no `hooks/gating_guard.py`. A leftover loader is
harmless (the engine makes it stand down outside its own agent's session) and `canopy agent
doctor` / `canopy fleet-align` flag it as a warning to remove.

## The one rule

**No environment-varying value is committed to git.** Not a password or API key — and *also not* a
Drive folder id, doc id, sheet id, or script id. The fleet runs in **multiple environments** (your
laptop, the cloud runner, a fresh box) and those values differ per environment/Workspace. A literal
in a repo can't vary; a **credential in the agent's Canopy vault** can.

> **The test is NOT "is it sensitive?" — it is "does this differ between environments?"**
> If yes → it goes in the vault. This is the rule that was being missed: ids kept getting written as
> literals because "an id isn't a secret", which is true and irrelevant.

## One declaration — the agent's Canopy vault

**An agent's secrets are named in exactly one place: its Canopy vault on canopy-web** (the
agent's Settings → Credentials). Every agent is on it (2026-10-22). Each stored name is one env
var: `canopy cred env --agent <slug>` (laptop, `/<agent>:setup`) and a runner's prepare write
`~/.<slug>/.env` straight from it — no template is read. The vault's names are also what
canopy-web hands a runner and what its undeclared-read alert checks.

A value **every agent in a workspace** uses — the fleet's gog OAuth clients `gog-oauth-client`
and `gog-oauth-client-web` — lives ONCE in that workspace's vault (dimagi: `dimagi/Shared`),
layered under each agent's own; no agent repeats it (Jonathan, 2026-10-22: "the oauth client
ids should go into the workspace vaults for dimagi since they are the same across all agents").

So an agent repo names **no secret and no vault**:
- **No secrets list in the repo.** A template or manifest naming the agent's secrets is a second
  list that drifts (ACE's went 40 names out of step). The vault is the list.
- **No `runtime.yaml`.** Nothing reads one; capabilities publish from their `capability.yaml`
  (see § No agent ships a `runtime.yaml` below).
- **`config/agent.json`** keeps identity (mailbox, `gog_client`) — facts, not secrets.

## The gog client — never type one

Every raw `gog` call names the mailbox (`-a <mailbox>`) and takes its client from
**`canopy email client`**, the one rule every gog caller follows — the canopy engine, the
runners, MCP servers, agent scripts and the lines in these procedures:

```bash
gog gmail search "is:unread" -a <your-mailbox> --client "$(canopy email client --repo .)"
```

It prints the client this machine should use for the agent's mailbox: the declared
`gog_client` when this machine holds a token under it; else the fleet client it does hold —
`canopy`, then `canopy-web`; else the single other client holding the mailbox. `canopy` and
`canopy-web` are ONE Google app with two ways to sign in (a laptop's `gog login`, and
canopy-web's "Connect Google mailbox" button, which can only mint under the Web client), so
a mailbox signed in from either door works. Hardcoding `--client canopy` breaks the second
door; omitting `--client` leans on gog's `account_clients` map, which nothing keeps in step.
Code calls the same rule directly: `agent_email.reconcile_client` (Python) or
`canopy email client --json` (anything else).

## How a value reaches a turn

Every env var an agent needs is resolved from its Canopy vault into a **worktree-clean global env
home** (`~/.<agent>/.env`, mode 0600). emdash runs each turn in a fresh worktree, so a repo-local
`.env` would vanish; the global home is read by every worktree via `bin/_env.py`.

### Setup: `canopy agent env` — bind ONE instance, fill the env

Every agent's `/<agent>:setup` calls the one shared step:

```bash
canopy agent env --plugin <plugin> [--out ~/.<plugin>/.env] [--agent <instance>] [--force] [--print]
```

A plugin is code; an *instance* is canopy-web's Agent record — identity, workspace and credentials.

1. **Instance.** The instances of `<plugin>` you can see on canopy-web (the catalog entry's
   `metadata.instances`, else `GET /api/agents/` filtered by `plugin.name`). `--agent` picks one;
   the instance this install is already bound to is kept; exactly one is used and named; several
   stop with the exact re-run line for each (it never prompts — scripts and slash commands call
   it); none stops.
2. **Binding.** Recorded beside the output: `~/.<plugin>/canopy-instance.json`
   (`{plugin, agent, workspace, bound_at}`). Re-running for a DIFFERENT instance refuses
   unless `--force` — an install never switches instance by accident; using another instance
   means running setup again.
3. **Fill.** The instance's Canopy vault values, resolved on this device and written 0600
   (`canopy cred`'s resolve). A refusal names its own fix: `canopy agent use <slug>` says what this
   computer still needs (sign-in, device key, a grant, your licence).

`--print` emits the resolved instance as JSON and changes nothing.

**Non-env credential FILES** (something an SDK/CLI wants as a JSON file on disk, e.g. a gog
OAuth-client json, a service-account key) are credentials in the vault like any other; code that
needs the file writes it from the value (`canopy cred exec --agent <slug> --ref NAME -- <cmd>`
puts one value in a child's environment without writing a file).

## Session identity — resolve through `canopy cred` (fleet baseline, canopy#850)

**Recommendation for every agent and every agent-identity MCP server:** don't resolve
secrets by hand and don't read a key bundled in plugin data. Ask the broker:

```bash
canopy cred check --agent <slug>   # exit 0 = this session may act as <slug>; 3 = refused (stderr says why)
canopy cred env   --agent <slug>   # ensures ~/.<slug>/.env (0600), resolving only when missing; prints the path
canopy cred refresh --agent <slug> # after a rotation or an auth failure
canopy cred sync  --agent <slug>   # re-write only if the vault's values changed (the SessionStart hook runs this)
canopy cred exec  --agent <slug> -- <cmd>   # the agent's env in <cmd>'s process ONLY — no file
```

- A **runner turn** may act only as its own agent; a **human** session may act as an agent when
  canopy-web says they are its runner operator / owner / admin (or hold a licence and are in their
  own local turn). Details: `docs/architecture/session-identity.md` § Canopy Vault.
- `~/.<slug>/.env` is the file `canopy cred env` writes, so `bin/_env.py` and every `bin/` script
  keep working unchanged.
- An MCP server that acts as an agent (ace-gdrive, chrome-sales gdrive; canopy-gws when registered) calls
  `canopy cred check --agent <its agent>` before acting and, on non-zero, returns the stderr as
  its refusal — **never** falls back to a bundled key.

Contract, exit codes and a Node/TS snippet: `docs/architecture/session-identity.md` in the canopy repo.

## A credential you leaked — rotate it yourself, now

**The moment you learn a secret of yours went somewhere it does not belong, rotate it in the same
turn — before you finish what you were doing, and without asking first.** "Somewhere it does not
belong" means: sent to a host that is not the service it is for (a third-party CDN, a tracker, an
analytics endpoint), pasted into a chat, an email, an issue, a PR or a log, committed, or written to
a file another agent or person can read. Do not weigh the odds that anyone saw it — the CDN that
"surely doesn't log auth headers" is the reasoning a leak hides behind. Rotating your own credential
can only shorten the old one's life, so it is never a decision that needs a human; waiting for one
is the failure. (Jonathan, 2026-10-24, after Hal found its link checker had been sending its canopy
token to Google Fonts and jsDelivr and asked him to rotate it: "you should always know to trigger a
request to rotate your key if you leak it, do it now.")

- **Your canopy token (`CANOPY_WEB_PAT`)** — rotate it yourself: the `rotate_own_pat` tool
  (`POST /api/agents/<slug>/pat-rotation/own {"reason": "<where it went>"}`, as your own login).
  The new token goes into your stored `CANOPY_WEB_PAT`; the old one keeps working 24 h so your other
  boxes don't break, then is revoked. Then `canopy cred sync` (or `canopy agent env`) on this box,
  and confirm a call with the new token works.
  **A rotation is not done until the LEAKED token is dead or dying — check, don't assume.** Read
  the response: `old_token_revoked_after` null means nothing was retired. Then list your tokens
  (`GET /api/tokens/`), find the leaked one (the long-lived one, not the new `<slug> agent
  (rotated …)`), revoke it (`DELETE /api/tokens/<id>/`), and confirm the leaked value now gets
  401. (2026-10-24: Hal's first self-rotation printed "nothing revoked" and the leaked token stayed
  live; canopy-web#2069 now retires the token you called with, but verify either way.)
- **Any other secret** (a gog token, an API key, a vault credential) — you cannot mint a replacement,
  so the request IS the action: ask your owner right away with `request_agent_secret` (it puts a
  card in front of them) naming the secret and where it went, and record it as a board task. Never
  leave it in a closing summary as something for them to notice.
- **Close the hole, then say so.** Fix what leaked it in the same turn, and tell the people you are
  reporting to that it leaked, where, and that it has been rotated — in the summary and in any email
  about the work. A leak you fixed silently is a leak nobody can audit.

## Adding a new value — the recipe

Store it in the agent's Settings → Credentials on
canopy-web (or `canopy vault zk set --agent <slug> <NAME>`, value on stdin), named as the env var
it becomes; a value every agent in the workspace needs goes in the workspace vault instead
(`canopy vault zk set --vault <id> <NAME>`; ids from `canopy vault ws list`). Then
`/<agent>:setup` or the next runner prepare materializes it, and code reads the env var. Nothing
in the agent repo changes.

## No agent ships a `runtime.yaml` — capabilities publish from `capability.yaml`

There is no repo-root `runtime.yaml` any more (retired fleet-wide 2026-10-26). It was canopy-web's
Agent Runtime Registry file, and by the end it did one thing: point at an agent's capability
manifests. Its `plugins:`, `tools:`, `env:`, `preflight:` and `engine:` were read by a reconciler
that was designed but never wired into any runner (retired 2026-10-15), and its secret names were
a second copy of what the vault holds. An agent is prepared by `canopy agent prepare` and its env
filled by `canopy cred sync` from the vault (docs/architecture/agent-setup-lifecycle.md).

- **Adding a value → store it in the agent's Canopy vault** (§ One declaration). Nothing in the
  repo reaches an agent's env.
- **Publishing a capability** (a machine or pool the agent leases from canopy-web) → ship a
  `capability.yaml` per capability in the agent's repo (ACE: `infra/capabilities/*/capability.yaml`)
  and run `canopy-runtime publish-capabilities --dir infra/capabilities --agent <slug>`.
  The list is the agent's complete set: canopy-web retires any capability it leaves out and
  releases its live leases, so use `--dir`, which refuses an empty directory.
- **Do not add one back.** A file that nothing reads drifts, and an agent that adds it has
  declared something no one will ever act on.

## Related

- `deliverables.md` — the Drive filing standard; its `<Agent>` root is `$GDRIVE_ROOT_FOLDER`,
  a vault-resolved value under this standard.
- `turn.md` — the turn procedure these values make possible.
