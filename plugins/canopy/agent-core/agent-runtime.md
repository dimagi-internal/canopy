# Agent config + secrets — where every value lives

> **Fleet-canonical process (canopy agent-core).** Where an agent's configuration values live and
> how they reach a turn. Read this before adding ANY new id, key, folder, or account to an agent.

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

Each agent already ships an **env template** that `op` resolves into a **worktree-clean global env
home** (`~/.<agent>/.env`, mode 0600). emdash runs each turn in a fresh worktree, so a repo-local
`.env` would vanish; the global home is read by every worktree via `bin/_env.py`.

**Most agents — `config/secrets.yaml` + `canopy provision`:**

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

Run `canopy provision` (`--check` to dry-run) from the agent's repo.

**Echo — `.env.tpl` + `op inject`** (same idea, older format):

```bash
GDRIVE_ROOT_FOLDER=op://Agent-Echo/gdrive-root-folder/credential
# resolve:
op inject -i .env.tpl -o ~/.echo/.env --account dimagi.1password.com
```

Either way the rule is identical: **the template holds a `op://…` reference, never the value.**

## Adding a new value — the recipe

1. **Put it in the vault** (agent-specific → `Agent-<Slug>`; needed by all → `Canopy-Shared`):
   ```bash
   op item create --category "API Credential" --title "<name>" \
     --vault "Agent-<Slug>" "credential[password]=<value>"
   # exists already? edit instead:
   op item edit "<name>" --vault "Agent-<Slug>" "credential=<value>"
   ```
2. **Reference it** in the agent's `config/secrets.yaml` `env.vars` (or `.env.tpl`), never the value.
3. **Materialize** it: `canopy provision` (or `op inject`) → `~/.<agent>/.env`.
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

**Status (2026-07-23): only Echo has a `runtime.yaml`, and the reconciler is not what drives
laptop turns today.** Its own header says it supersedes `canopy provision` *for the runtime layer* —
that migration is real but unfinished. So:

- **Adding a value now → use `config/secrets.yaml` / `.env.tpl`** (above). It works on every box.
- **Don't declare the same value in both places** — you get two sources of truth that drift.
- When the reconciler does drive turns, the migration is mechanical: the vault items already exist
  and are correctly named; only the *declaration* moves.

## Rollout status (2026-07-23)

- **Vaults:** `Canopy-Shared` + `Agent-{Ace,Ada,Echo,Eva,Hal}` exist, each with `canopy-pat` /
  `claude-oauth-token` / `gog-token` + `gdrive-root-folder` (the agent's OWN root folder id — an
  agent never resolves anything about that root's parent).
- **Legacy:** the flat `AI-Agents` vault is still populated and still referenced by most agents'
  `secrets.yaml` / `.env.tpl`. Migrating those refs onto per-agent vaults is outstanding work —
  copy the value into `Agent-<Slug>` first, then repoint the ref.
- **`env:` blocks:** ada + hal have one; **eva does not** (needs `~/.eva/.env` added); echo uses
  `.env.tpl`.

## Related

- `deliverables.md` — the Drive filing standard; its `<Agent>` root is `$GDRIVE_ROOT_FOLDER`,
  a vault-resolved value under this standard.
- `turn.md` — the turn procedure these values make possible.
