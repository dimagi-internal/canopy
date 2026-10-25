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
in a repo can't vary; a **1Password reference** can.

> **The test is NOT "is it sensitive?" — it is "does this differ between environments?"**
> If yes → it's a reference. This is the rule that was being missed: ids kept getting written as
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
- **No `.env.tpl`.** It was the 1Password path's template. A canopy-vault agent never reads it;
  keeping it is a second list that drifts (ACE's went 40 names out of step). `canopy agent
  doctor` passes a repo without one and flags one that remains. The factory stopped stamping it.
- **No `secrets:` list in `runtime.yaml`.** runtime.yaml keeps what is not a secret — plugins,
  tools, capabilities, engine, true literals (see § `runtime.yaml` below).
- **`config/agent.json`** keeps identity (mailbox, `gog_client`) — facts, not secrets.

The sections below on `.env.tpl`, `op inject`, `canopy agent vault` and `Canopy-Shared` describe
the **1Password path**, kept only until the last 1Password code is removed.

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

## Two-tier vault topology

| Vault | Holds |
|---|---|
| `Canopy-Shared` | values **every** canopy agent resolves — `gog-oauth-client`, `github-token` |
| the instance's vault (`Agent.op_vault`) | one per agent instance — its identity + integration values: `canopy-pat`, `claude-oauth-token`, `gog-token`, `gdrive-root-folder`, … |

**The vault belongs to the instance, never to the plugin** (Jonathan, 2026-10-14). A plugin is
code; an *instance* is canopy-web's Agent record — identity, workspace and 1Password vault
(`Agent.op_vault`, set by the instance's owner on `/agents/<slug>`). Nothing in canopy or an
agent repo derives a vault name from the slug (today's fleet vaults happen to be called
`Agent-Ace`, `Agent-Hal`, … — a name the owner recorded, not a rule), and nothing falls back to
one: an instance with no recorded vault is an error that says so.

One vault per agent (**not** the legacy flat `AI-Agents` dumping ground) so each agent is
least-privilege and independently grantable. Every item is an **API Credential** whose value sits
in a single `credential` field, so a reference resolves as `op://<vault>/<item>/credential`.

New agent? `canopy agent vault --slug <slug>` (the canopy-web setup checklist's "Give it a
vault" step) creates the vault, makes the owner its admin, creates and FILLS every item the
agent's `.env.tpl` references — minting what the system can, asking only for what a person
alone knows — and stores the read-only key on canopy-web. Safe to re-run; `--check` reports.
`canopy-web/deploy/secrets/migrate_echo.sh` is the worked example of copying values out of the
legacy `AI-Agents` vault.

## How a value reaches a turn (use this today)

**`.env.tpl` + `op inject` is THE standard** — for every env var an agent needs, committed and
resolved into a **worktree-clean global env home** (`~/.<agent>/.env`, mode 0600). emdash runs
each turn in a fresh worktree, so a repo-local `.env` would vanish; the global home is read by
every worktree via `bin/_env.py`. The fleet has already migrated onto this: ada, eva, hal, and
echo all ship `.env.tpl`, with `config/secrets.yaml` deleted.

Templates never name the vault — they reference **`${AGENT_VAULT}`**, which `op inject`
substitutes from its environment (`op inject --help`: "Use variables in secret references"):

```bash
GDRIVE_ROOT_FOLDER=op://${AGENT_VAULT}/gdrive-root-folder/credential
```

### Setup: `canopy agent env` — bind ONE instance, fill the env

Every agent's `/<agent>:setup` calls the one shared step:

```bash
canopy agent env --plugin <plugin> [--template .env.tpl] [--out ~/.<plugin>/.env] \
                 [--agent <instance>] [--vault <name>] [--force] [--print]
```

1. **Instance.** The instances of `<plugin>` you can see on canopy-web (the catalog entry's
   `metadata.instances`, else `GET /api/agents/` filtered by `plugin.name`). `--agent` picks one;
   the instance this install is already bound to is kept; exactly one is used and named; several
   stop with the exact re-run line for each (it never prompts — scripts and slash commands call
   it); none stops.
2. **Vault.** `--vault` wins, else the instance's `op_vault`. None recorded → error: *"this agent
   instance has no 1Password vault recorded on canopy-web; its owner sets it"*.
3. **Binding.** Recorded beside the output: `~/.<plugin>/canopy-instance.json`
   (`{plugin, agent, workspace, vault, bound_at}`). Re-running for a DIFFERENT instance refuses
   unless `--force` — an install never switches instance by accident; using another instance
   means running setup again. The `agent_op_env` SessionStart hook exports the binding's
   `AGENT_VAULT`/`AGENT_SLUG` into agent sessions, finding it in `~/.<slug>/` or, for an agent
   whose setup writes to its plugin data dir (ACE), `~/.claude/plugins/data/<slug>-<marketplace>/`
   (several there → the installed one; still ambiguous → nothing exported, one line).
4. **Fill.** `op inject -i <template> -o <out>` with `AGENT_VAULT=<vault>` and
   `AGENT_SLUG=<instance>` in its environment. `op` missing / not signed in / vault unreadable
   → one message naming the vault, `dimagi.1password.com`, and "ask the agent's owner for access".

`--print` emits the resolved instance and vault as JSON and changes nothing. The rest of canopy
asks the same question the same way (`agent_instance.instance_vault`: this machine's binding,
else canopy-web's `op_vault`, else a loud error) — `canopy agent bootstrap`, `canopy cred
env|sync|check` all set `AGENT_VAULT` before injecting, and the SessionStart hook exports the
bound `AGENT_VAULT` / `AGENT_SLUG` into agent sessions so a hand-run `op inject` works too.
Runners must set `AGENT_VAULT` (= `Agent.op_vault`) for every `op inject` of an agent template.

**Non-env credential FILES** (something an SDK/CLI wants as a JSON file on disk, e.g. a gog
OAuth-client json, a service-account key) are NOT env vars — resolve them directly with native
`op read`, never through `.env.tpl`:

```bash
op read "op://$AGENT_VAULT/gog-oauth-client/credential" > ~/Library/Application\ Support/gogcli/credentials-<slug>.json
chmod 0600 ~/Library/Application\ Support/gogcli/credentials-<slug>.json
```

Either way the rule is identical: **the template/command holds an `op://…` reference, never the
value.**

> ## ⚠️ CRITICAL SECURITY RULE — never write a literal `op://…` in a comment
>
> `op inject` resolves **every** `op://` reference it finds in the input file — **including
> inside `#` comments and example lines.** It does not know the difference between "the config
> line I should resolve" and "the example I put in a comment for a human to read." So:
>
> - **`.env.tpl` must NEVER contain a real, resolvable `op://<vault>/<item>/<field>` string in a
>   comment or example** — only in an actual `KEY=op://…` assignment line you intend to resolve.
>   A "just for illustration" `op://` in a comment resolves exactly like a real line does; the
>   resolved secret then prints to stdout the next time someone does `op inject -i .env.tpl | cat`
>   (or anything else that reads the output), leaking it.
> - **In `.env.tpl` itself, no `op://` in a comment AT ALL — not even a placeholder.** A
>   placeholder does not leak, but it BREAKS the inject: `op://${AGENT_VAULT}/<item>/<field>` in
>   a comment expands to `op://Agent-X/<item>/…`, which op rejects ("invalid secret reference …
>   too few '/'") and fails the whole file. Every factory-made agent shipped such a comment
>   until 2026-10-15 (lex's env never materialized). Point at a live line instead;
>   `canopy agent doctor` fails a template that breaks this.
> - **In any other doc, use an angle-bracket placeholder**: `op://<vault>/<item>/<field>` —
>   never a real vault/item name, even a plausible-looking one.
> - This applies to **any file `op inject` might ever be pointed at**, not just `.env.tpl` — treat
>   it as a rule about writing `op://` strings in text at all, not a quirk of one filename.

## Session identity — resolve through `canopy cred` (fleet baseline, canopy#850)

**Recommendation for every agent and every agent-identity MCP server:** don't run `op inject`
by hand and don't read a key bundled in plugin data. Ask the broker:

```bash
canopy cred check --agent <slug>   # exit 0 = this session may act as <slug>; 3 = refused (stderr says why)
canopy cred env   --agent <slug>   # ensures ~/.<slug>/.env (0600), resolving only when missing; prints the path
canopy cred refresh --agent <slug> # after a rotation or an auth failure
canopy cred sync  --agent <slug>   # re-inject only if the vault/.env.tpl changed (the SessionStart hook runs this)
canopy cred exec  --agent <slug> -- <cmd>   # the agent's env in <cmd>'s process ONLY — no file
```

- **Canopy Vault** (canopy#946) is an **opt-in** second backend: an owner moves one agent's
  values to canopy-web with `canopy vault import --agent <slug> --switch` (and back with
  `canopy vault export` + `switch --to 1password`). Nothing above changes for the agent — `cred
  env`/`sync` and the runners read the backend the agent record names. 1Password stays the
  default. Details: `docs/architecture/session-identity.md` § Canopy Vault.

- A **runner turn** may act only as its own agent; a **human** session may act as an agent when
  their own `op` can read its vault (`1password` backend) or canopy-web says they are its runner
  operator / owner / admin (`canopy-web` backend, the agent record's `credential_source`).
- `~/.<slug>/.env` is the same file `op inject` writes, so `bin/_env.py` and every `bin/` script
  keep working unchanged.
- An MCP server that acts as an agent (ace-gdrive, chrome-sales gdrive; canopy-gws when registered) calls
  `canopy cred check --agent <its agent>` before acting and, on non-zero, returns the stderr as
  its refusal — **never** falls back to a bundled key.

Contract, exit codes and a Node/TS snippet: `docs/architecture/session-identity.md` in the canopy repo.

## The legacy path — `config/secrets.yaml` + `canopy provision`

Still supported (`canopy provision`, `--check` to dry-run, is fully functional), and some
repos/agents that haven't migrated yet still use it — but **do not scaffold new agents onto it.**
It is a declarative YAML manifest `canopy provision` materializes:

```yaml
env:
  target: "~/.<agent>/.env"
  mode: "0600"
  vars:
    - key: SOME_LITERAL
      value: "not-environment-varying"                      # rare — see the rule above
    - key: GDRIVE_ROOT_FOLDER
      op: "op://<vault>/gdrive-root-folder/credential"  # the instance's vault, by name
```

Migrating an agent off it: write the equivalent `.env.tpl` (one `KEY=value` / `KEY=op://…` line
per `env.vars` entry — same placeholder-in-comments rule applies), point any file-type `secrets:`
entries at a direct `op read ... > <file>` step instead, verify with a real `op inject` run, then
delete `config/secrets.yaml`.

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
- **Any other secret** (a gog token, an API key, a 1Password item) — you cannot mint a replacement,
  so the request IS the action: ask your owner right away with `request_agent_secret` (it puts a
  card in front of them) naming the secret and where it went, and record it as a board task. Never
  leave it in a closing summary as something for them to notice.
- **Close the hole, then say so.** Fix what leaked it in the same turn, and tell the people you are
  reporting to that it leaked, where, and that it has been rotated — in the summary and in any email
  about the work. A leak you fixed silently is a leak nobody can audit.

## Adding a new value — the recipe

**On the Canopy vault (every agent today):** store it in the agent's Settings → Credentials on
canopy-web (or `canopy vault zk set --agent <slug> <NAME>`, value on stdin), named as the env var
it becomes; a value every agent in the workspace needs goes in the workspace vault instead
(`canopy vault zk set --vault <id> <NAME>`; ids from `canopy vault ws list`). Then
`/<agent>:setup` or the next runner prepare materializes it, and code reads the env var. Nothing
in the agent repo changes.

**1Password path (legacy):**

1. **Reference it** in the agent's `.env.tpl` as `KEY=op://${AGENT_VAULT}/<name>/credential`
   (legacy agents: `config/secrets.yaml`'s `env.vars`) — never the value, never a vault name, and
   never a real `op://…` in a comment (see the security rule above).
2. **Put it in the vault**: `canopy agent vault --slug <slug>` creates the item and asks for the
   value with hidden input — never `op item create … "credential[password]=<value>"`, which puts
   the value in argv, visible to every process on the machine. A value the fleet can PRODUCE
   (minted, or made by another control) belongs in `KINDS` in `src/orchestrator/agent_vault.py`,
   so no person is ever asked for it. Needed by every agent → `Canopy-Shared` instead.
3. **Materialize** it: `/<agent>:setup` (`canopy agent env --plugin <plugin>`; legacy: `canopy provision`).
4. **Read it** as the env var in code/skills — never re-hardcode the value.
5. **Verify**: `canopy agent vault --slug <slug> --check` (every reference filled, key stored)
   and confirm the key landed in `~/.<agent>/.env`.

## `runtime.yaml` — not a secrets path

Some agents (Ace, Echo) ship a repo-root `runtime.yaml` (canopy-web's Agent Runtime Registry
schema, `canopy-web/packages/canopy_runtime/`). Today it does two things only: it declares the
agent's **capabilities** (`canopy-runtime publish-capabilities`), and its declared secret names
feed canopy-web's credentials screen. It never fills an env. A *reconciler* that would have
resolved those names and provisioned a box was designed but never wired into any runner, and
was retired on 2026-10-15 — an agent is prepared by `canopy agent prepare` and its env filled
from `.env.tpl` (docs/architecture/agent-setup-lifecycle.md).

- **Adding a value → store it in the agent's Canopy vault** (§ One declaration). Nothing in
  runtime.yaml reaches an agent's env.
- **runtime.yaml carries no `secrets:` list.** A secret named there is a second declaration of
  what the vault already holds, and it drifts.

## Rollout status (2026-07-25)

- **`.env.tpl` + `op inject` is standard fleet-wide:** ada, eva, hal, and echo all ship `.env.tpl`;
  `config/secrets.yaml` has been deleted from those repos. `canopy create-agent` scaffolds
  `.env.tpl` for new agents. Any agent still on `config/secrets.yaml` is on the legacy path and
  should migrate when convenient (see "Migrating an agent off it" above).
- **Vaults:** `Canopy-Shared` + one vault per instance (today named `Agent-Ace`, `Agent-Hal`, …
  — recorded on each Agent as `op_vault`) exist, each with `canopy-pat` /
  `claude-oauth-token` / `gog-token` + `gdrive-root-folder` (the agent's OWN root folder id — an
  agent never resolves anything about that root's parent).
- **Legacy:** the flat `AI-Agents` vault is still populated and still referenced by some agents'
  `.env.tpl` (or a not-yet-migrated `secrets.yaml`). Migrating those refs onto per-agent vaults is
  outstanding work — copy the value into the instance's vault first, then repoint the ref.
- **`${AGENT_VAULT}` migration (2026-10-14):** agent templates still naming their vault
  (`op://Agent-Ace/…`) keep working — `canopy agent env` warns about each — and move to
  `op://${AGENT_VAULT}/…` once every runner that injects them sets `AGENT_VAULT`.

## Related

- `deliverables.md` — the Drive filing standard; its `<Agent>` root is `$GDRIVE_ROOT_FOLDER`,
  a vault-resolved value under this standard.
- `turn.md` — the turn procedure these values make possible.
