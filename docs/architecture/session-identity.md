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

- **`1password`** — the agent's values live in its vault: the instance this machine is bound to
  (`~/.<slug>/canopy-instance.json`, written by `canopy agent env`), else the `op_vault`
  canopy-web records for the agent — never a name derived from the slug, and a missing one is
  an error. `~/.<slug>/.env` is resolved with
  `op inject -f -i <agent repo>/.env.tpl -o ~/.<slug>/.env`, `AGENT_VAULT` set to that vault.
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
A value changed in the vault reaches the file without anyone re-running anything: the canopy
SessionStart hook (`agent_op_env.py`) runs `canopy cred sync` with the agent's own service-account
key, which re-injects `~/.<slug>/.env` only when the vault's `content_version` or `.env.tpl`
changed. It checks at most every 5 minutes per agent (`~/.<slug>/.env.stamp`), with one
`op vault get` per vault. The cloud runner applies the same rule when it claims a turn
(canopy-web `cloud_runner._refresh_agent_env`), so laptops and cloud agree on what "changed" means.
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

**Shared integration keys are for people, not agents.** A tool that signs in to Google with a shared
service account (chrome-sales' Drive tools) acts as the session's agent instead when there is one:
`canopy cred google-token` hands it a short-lived token for that agent's own Google login (the
mailbox login the runner already keeps), and the service-account key stays a human-session path.
An agent then reads and writes exactly what was shared with it, under its own name.

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
canopy cred sync    [--agent X] [--ttl S]
canopy cred exec    [--agent X] [--ref NAME …] -- <cmd> [args…]
canopy cred google-token [--agent X]
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
- **`exec`** runs `<cmd>` with X's variables in **the child's environment only** — no
  `~/.X/.env`, nothing exported into the session. Same decision as `check`, except that an
  already-resolved `~/.X/.env` is not itself a reason to allow. 1Password: `op inject` to a pipe;
  canopy-web: `/resolve` (with `?refs=` when `--ref` narrows it). Exits with `<cmd>`'s status.
- **`sync`** without a 1Password key in the env refreshes a **canopy-web-backed** agent's
  `~/.X/.env` from canopy-web (throttled, rewritten only when the values' fingerprint moved);
  for a 1Password agent it is a no-op, as before.
- **`google-token`** writes `~/.canopy/google-token/X.json` (0600: `access_token`, `expires_at`
  epoch seconds, `account`) for X's own Google login and prints **only its path**. It exchanges the
  refresh token gog holds for X's mailbox (client per `canopy email client`) and reuses the file
  while 5+ minutes remain. A runner turn may ask only for its own agent (exit 3); a missing login
  exits 1 naming the `gog login` to run.
- **`whoami`** prints `identity: agent 'X'` (runner turn) or `identity: human (you)`, plus `why:`.

No command prints a secret value; `env` and `google-token` print a path.

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
  runner pairs and agent owners/admins. Every canopy caller goes through one client,
  `orchestrator/cred_resolve.py`, which adds `?refs=` when it wants a subset (re-asking once
  without it if the server dropped a ref, so an old server and an undeclared ref both behave as
  before), `X-Canopy-Turn-Id` when a turn is known, and a `DPoP` device proof when this machine
  holds a registered device key (below).

## Canopy Vault — the canopy-web backend, opt in per agent (canopy#946)

1Password stays the default and fully supported. An owner/admin can move ONE agent's values to
canopy-web (`credential_source = "canopy-web"`); the server half is canopy-web#1538.

```
canopy vault import --agent X [--switch]       # 1Password -> canopy-web, as YOU (op, Touch ID ok)
canopy vault status --agent X [--no-drift]     # per ref: present, last read, drift (hash only)
canopy vault switch --agent X --to canopy-web|1password
canopy vault export --agent X [--check]        # canopy-web -> 1Password (the rollback)
canopy vault device [--enroll|--reenroll|--refresh] [--kind K]
canopy vault binding --agent X on|off          # PATCH /device-binding {require_device_binding}
```

- **Refs** = every `NAME=` line of the agent's `.env.tpl` (an `op://` ref is read; a literal is
  carried as-is so the canopy-web env is complete) plus canopy-web's `runtime_sources`. Items
  another flow owns (`gog-token`, the shared OAuth client) are left alone. `import` never edits
  the vault; `--switch` / `switch --to canopy-web` refuse until canopy-web reports every ref set.
- **Runners** for a canopy-web agent write `~/.X/.env` from `/resolve` values (`canopy agent
  prepare --runner`), and the SessionStart hook's `cred sync` keeps it current — canopy-web
  withholds the vault key from such an agent, so `op inject` cannot run.
- **Device key.** `require_device_binding` on an agent makes `/resolve` answer 401
  `device_proof_required` unless the request carries a DPoP proof (RFC 9449: `typ dpop+jwt`,
  ES256, `jwk` header; `htm`, `htu` without query, `iat`, `jti`) from an **approved** key. On a
  Mac the key is a Secure Enclave key made by `canopy-device-key` (canopy-web
  `runner/device-key/`, built into `~/.canopy/bin/` by the runner installer) with no
  user-presence requirement, so unattended turns never prompt; elsewhere it is a software
  P-256 key in `~/.canopy/device-key/sw.pem` (0600). The laptop runner enrolls it at startup
  (`POST /api/runner-device-keys/`); a person approves the `approve_url` in canopy-web with a
  passkey. `canopy vault device` / `status` show it.
- **Phase 1b kinds (canopy-web#1549).** `--kind` defaults to the strongest the machine has.
  - **`tpm`** (Windows): a non-exportable P-256 key named `canopy-device-key` in the CNG
    "Microsoft Platform Crypto Provider", ctypes over `ncrypt.dll`, no UI policy (never
    prompts). Same DPoP proof. No TPM → `software`. Only `tpm.json` (a marker) is on disk.
  - **`aws-iam`** (an EC2 box with an instance role): no key — the role IS the identity
    (HashiCorp Vault AWS-auth). Each resolve carries `X-Canopy-Aws-Proof` = base64 JSON
    `{method, url, headers, body}` of a SigV4-signed, **unsent** `sts:GetCallerIdentity`
    (`POST https://sts.amazonaws.com/`, `Action=GetCallerIdentity&Version=2011-06-15`) whose
    SIGNED headers include `X-Canopy-Proof-Htu` (the URL being called, no query) and
    `X-Canopy-Proof-Iat` (unix seconds). canopy-web replays it to its own pinned STS endpoint.
    Enrollment posts `{runner, kind: "aws-iam", role_arn, proof}` (the role ARN learned by
    actually sending GetCallerIdentity once). Credentials come from IMDSv2 and stay in memory.
    canopy-web's `runner/ec2/cloud_runner.py` carries the same stdlib signer for its own
    resolves; both are pinned to a botocore `SigV4Auth` vector.

### Zero-knowledge (canopy-web#1550)

For an agent with zero-knowledge on, canopy-web holds only `zk1:` ciphertext and wrapped keys.
The formats are canopy-web's `docs/vault/zk-v1/zk_v1_reference.py`; `orchestrator/vault_zk.py`
reproduces its vectors byte for byte (`tests/fixtures/vault_zk/zk_v1_vectors.json`, verbatim).

- **ECDH device key.** Beside the signing key every machine holds a P-256 key-agreement key the
  agent's vault key (VK) is wrapped to: `se-ecdh.key` via the helper's `ecdh-create` /
  `ecdh-public-jwk` / `ecdh-derive <epk_b64u>` (Secure Enclave, no presence), the TPM key
  `canopy-device-ecdh` (`NCryptSecretAgreement` + `NCryptDeriveKey(BCRYPT_KDF_RAW_SECRET)`, whose
  little-endian output is reversed), or a 0600 `ecdh.pem` for `software` and `aws-iam`. It only
  ever answers Z. Enrollment sends `ecdh_public_jwk`; an older helper or server is not an error.
- **Resolve.** A ZK answer is `{zk: true, kv, values: {name: "zk1:…"}, wrap}`; `cred_resolve`
  unwraps with `device_info(agent_id, kv, ecdh_jkt)` and decrypts, so `cred`, `agent prepare
  --runner`, `cred exec` and `cred sync` see plaintext exactly as for a custodial agent.
  `agent_id` = the answer's `agent_id` if sent, else `GET /api/agents/<slug>/`'s `id`. 409
  `zk_wrap_missing` → "ask an admin to grant this device".
- **Sealed values (`zks1:`).** canopy-web mints some secrets itself (Connect Google mailbox's
  `gog-token`, a rotated `canopy-pat`) and holds no VK, so it seals each to the recipients'
  PUBLIC keys: `"zks1:" + b64u(JSON {kv, r: [{kind: device|member, target: ecdh jkt | user id,
  epk, ct}]})`, key = HKDF(ECDH, salt="", info=`canopy-vault/v1/seal-value|{agent_id}|{name}|
  {kv}|{kind}:{target}`), AAD = the value AAD. `cred_resolve.open_zk` opens this device's own
  entry, in a ZK or a custodial answer. A VK holder **settles** them — re-encrypts as `zk1:`
  under VK, so every member reads them: `canopy vault zk settle`, which `import --zk` and
  `switch` also run. The runner's Gmail token therefore works on a ZK agent: the bootstrap's
  `fetch_web_token` goes through `cred_resolve`, which hands it plaintext.
- **CLI, as this device** (a member through its device wrap):
  `canopy vault zk status|enable|set|grant|rotate|settle --agent X` and `canopy vault import
  --zk` (which now carries the Gmail token too, encrypted here).
  `enable` wraps for this device, your keyset (`GET /api/vault/keyset`) and every pending target,
  plus recovery, and prints the recovery code once; `grant`/`rotate` list every public key they
  will wrap to and ask first (`--yes` to skip).

**Pilot (Eva, Jonathan's Mac):** `canopy vault import --agent eva` → approve the runner's
device key in canopy-web → `canopy vault switch --agent eva --to canopy-web`, then set
`require_device_binding` on Eva → run Eva as usual. Rollback: `canopy vault export --agent eva
&& canopy vault switch --agent eva --to 1password`.
