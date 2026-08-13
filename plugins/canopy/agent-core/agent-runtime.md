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

## The one rule

**No environment-varying value is committed to git.** Not a password or API key — and *also not* a
Drive folder id, doc id, sheet id, or script id. The fleet runs in **multiple environments** (your
laptop, the cloud runner, a fresh box) and those values differ per environment/Workspace. A literal
in a repo can't vary; a **1Password reference** can.

> **The test is NOT "is it sensitive?" — it is "does this differ between environments?"**
> If yes → it's a reference. This is the rule that was being missed: ids kept getting written as
> literals because "an id isn't a secret", which is true and irrelevant.

## Two-tier vault topology

| Vault | Holds |
|---|---|
| `Canopy-Shared` | values **every** canopy agent resolves — `gog-oauth-client`, `github-token` |
| `Agent-<Slug>` | one per agent — its identity + integration values: `canopy-pat`, `claude-oauth-token`, `gog-token`, `gdrive-root-folder`, … |

One vault per agent (**not** the legacy flat `AI-Agents` dumping ground) so each agent is
least-privilege and independently grantable. Every item is an **API Credential** whose value sits
in a single `credential` field, so a reference resolves as `op://<vault>/<item>/credential`.

New agent? `canopy-web/deploy/secrets/bootstrap_1password.sh <slug…>` idempotently creates the
vaults + placeholder items. `migrate_echo.sh` is the worked example of copying values out of the
legacy `AI-Agents` vault.

## How a value reaches a turn (use this today)

**`.env.tpl` + `op inject` is THE standard** — for every env var an agent needs, committed and
resolved into a **worktree-clean global env home** (`~/.<agent>/.env`, mode 0600). emdash runs
each turn in a fresh worktree, so a repo-local `.env` would vanish; the global home is read by
every worktree via `bin/_env.py`. The fleet has already migrated onto this: ada, eva, hal, and
echo all ship `.env.tpl`, with `config/secrets.yaml` deleted.

```bash
GDRIVE_ROOT_FOLDER=op://Agent-<Slug>/gdrive-root-folder/credential
# resolve:
op inject -i .env.tpl -o ~/.<agent>/.env --account dimagi.1password.com
```

**Non-env credential FILES** (something an SDK/CLI wants as a JSON file on disk, e.g. a gog
OAuth-client json, a service-account key) are NOT env vars — resolve them directly with native
`op read`, never through `.env.tpl`:

```bash
op read "op://Agent-<Slug>/gog-oauth-client/credential" > ~/Library/Application\ Support/gogcli/credentials-<slug>.json
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
> - **Use an angle-bracket placeholder in every example, in `.env.tpl` and in any doc**:
>   `op://<vault>/<item>/<field>` — never a real vault/item name, even a plausible-looking one,
>   even one that doesn't currently resolve to anything live.
> - This applies to **any file `op inject` might ever be pointed at**, not just `.env.tpl` — treat
>   it as a rule about writing `op://` strings in text at all, not a quirk of one filename.

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
      op: "op://Agent-<Slug>/gdrive-root-folder/credential"  # resolved from the agent's vault
```

Migrating an agent off it: write the equivalent `.env.tpl` (one `KEY=value` / `KEY=op://…` line
per `env.vars` entry — same placeholder-in-comments rule applies), point any file-type `secrets:`
entries at a direct `op read ... > <file>` step instead, verify with a real `op inject` run, then
delete `config/secrets.yaml`.

## Adding a new value — the recipe

1. **Put it in the vault** (agent-specific → `Agent-<Slug>`; needed by all → `Canopy-Shared`):
   ```bash
   op item create --category "API Credential" --title "<name>" \
     --vault "Agent-<Slug>" "credential[password]=<value>"
   # exists already? edit instead:
   op item edit "<name>" --vault "Agent-<Slug>" "credential=<value>"
   ```
2. **Reference it** in the agent's `.env.tpl` as `KEY=op://Agent-<Slug>/<name>/credential` (legacy
   agents: `config/secrets.yaml`'s `env.vars`) — never the value, and never a real `op://…` in a
   comment (see the security rule above).
3. **Materialize** it: `op inject -i .env.tpl -o ~/.<agent>/.env` (legacy: `canopy provision`).
4. **Read it** as the env var in code/skills — never re-hardcode the value.
5. **Verify**: `op read "op://Agent-<Slug>/<name>/credential"` and confirm the key landed in
   `~/.<agent>/.env`.

## The other mechanism — `runtime.yaml` (know the difference)

There is a **second, newer** path: the **Agent Runtime Registry** — an agent ships a repo-root
`runtime.yaml` declaring plugins, tools, engine, preflight, and values **by reference name only**
(no vault named); a *reconciler* resolves each name against `[Agent-<Slug>, Canopy-Shared]`, scans
the box, applies gaps, and injects env before the turn. Design + code:
`canopy-web/docs/superpowers/specs/2026-07-20-agent-runtime-registry-design.md` and
`canopy-web/packages/canopy_runtime/` (`python -m canopy_runtime.cli --agent <slug> --print-env`).

**Status (2026-07-25): only Echo has a `runtime.yaml`, and the reconciler is not what drives
laptop turns today.** Its own header says it supersedes `canopy provision` *for the runtime layer* —
that migration is real but unfinished. So:

- **Adding a value now → use `.env.tpl`** (above; legacy agents still on `config/secrets.yaml` can
  keep declaring there until they migrate). It works on every box.
- **Don't declare the same value in both places** — you get two sources of truth that drift.
- When the reconciler does drive turns, the migration is mechanical: the vault items already exist
  and are correctly named; only the *declaration* moves.

## Rollout status (2026-07-25)

- **`.env.tpl` + `op inject` is standard fleet-wide:** ada, eva, hal, and echo all ship `.env.tpl`;
  `config/secrets.yaml` has been deleted from those repos. `canopy create-agent` scaffolds
  `.env.tpl` for new agents. Any agent still on `config/secrets.yaml` is on the legacy path and
  should migrate when convenient (see "Migrating an agent off it" above).
- **Vaults:** `Canopy-Shared` + `Agent-{Ace,Ada,Echo,Eva,Hal}` exist, each with `canopy-pat` /
  `claude-oauth-token` / `gog-token` + `gdrive-root-folder` (the agent's OWN root folder id — an
  agent never resolves anything about that root's parent).
- **Legacy:** the flat `AI-Agents` vault is still populated and still referenced by some agents'
  `.env.tpl` (or a not-yet-migrated `secrets.yaml`). Migrating those refs onto per-agent vaults is
  outstanding work — copy the value into `Agent-<Slug>` first, then repoint the ref.

## Related

- `deliverables.md` — the Drive filing standard; its `<Agent>` root is `$GDRIVE_ROOT_FOLDER`,
  a vault-resolved value under this standard.
- `turn.md` — the turn procedure these values make possible.
