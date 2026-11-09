# Session identity — who a session may act as (`canopy cred`)

> Contract: every implementation (canopy, canopy-web, the agent repos' MCP servers) follows this page. Identity comes from the **session**, not from whichever plugin is loaded: a plugin that reads an identity from its own data makes every session that loads it act as that agent.

Flags, subcommands and output of `canopy cred` are in `canopy cred --help` and the module docstring of `src/orchestrator/cred_cli.py`; this page holds the rules around them.

## Where secrets live and how they reach a machine

- Every agent's secrets live in its **Canopy vault** on canopy-web, read from `GET /api/agents/{slug}/credentials/resolve`. A zero-knowledge agent's values are ciphertext there, opened only on a device that holds a grant (§ Zero-knowledge). `canopy agent env`, `canopy cred` and the SessionStart hook all take this path; the Canopy vault is the only credential source.
- The broker writes the per-agent env file `~/.<slug>/.env` (0600) once, so every `bin/` script keeps working. It re-resolves only when the file is missing, on `canopy cred refresh`, or when `canopy cred sync` sees the vault's fingerprint change: the SessionStart hook (`plugins/canopy/hooks/agent_session_start.py`) runs `sync`, throttled by `~/.<slug>/.env.stamp` (`cred_cli.py::SYNC_TTL`). `canopy cred check` caches canopy-web's `/access` verdict (never values) in `~/.canopy/cred-cache.json` (`cred_cli.py::CACHE_TTL`).
- Every canopy caller resolves through one client, `src/orchestrator/cred_resolve.py` (`?refs=` for a subset, `X-Canopy-Turn-Id` when a turn is known, a device proof when the machine holds a device key; module docstring).

## Who may resolve which agent (`cred_cli.py::decide`)

`canopy cred` decides; canopy-web enforces the same rule for its backend.

- **A runner turn** (a turn id is known: `$CANOPY_TURN_ID`, the caller envelope, or the laptop's by-task record) may act only as its own agent (`$CANOPY_AGENT_SLUG`, else `$CANOPY_AGENT`, else the envelope's `agent`). Any other `--agent` is refused; runner turns never take the human path below.
- **A human session** may act as X when `~/.X/.env` is already resolved on this machine (non-empty, owned by the user, no group/other bits; checked without asking canopy-web; `--refresh` skips it), or when `GET /api/agents/X/credentials/access` says `may_resolve` (the user pairs a runner X routes to, `via: "runner"`, or is X's owner/admin, `via: "admin"`).
- **Otherwise** the session acts as the human, and the refusal paragraph names the access to get.
- An unreachable `/access` is treated as `may_resolve = false`: an unreachable canopy-web never grants anything. `/access` is asked with the current turn, because a licence holder resolves only during their own local turn.

Exit codes callers may rely on (`cred_cli.py::EXIT_*`): 0 allowed, 1 error, 2 usage (no agent named or detectable), 3 refused (stderr carries one actionable paragraph), 4 undetermined (treat as refused). No command prints a secret value; `env` and `google-token` print a path. The one exception is `env --runner --print-json`, for a runner process's in-memory cache; a baseline rail denies it in an agent's turn.

## Agent-identity MCP servers

An MCP server that acts as an agent (canopy-gws, the agent repos' Drive servers) calls `canopy cred check --agent <its agent>` before acting. Where the session may not be that agent it returns the refusal text and **never falls back to a bundled key**; a missing `canopy` CLI is a refusal too. Minimal contract (Node): `spawnSync("canopy", ["cred","check","--agent",agent])`; non-zero exit means refuse with stderr as the error text. A reference implementation: `plugins/canopy/mcp/gws/lib/session-identity.ts`.

**Shared integration keys are for people, not agents.** A tool that signs in to Google with a shared service account acts as the session's agent when there is one: `canopy cred google-token` hands it a short-lived token for that agent's own Google login, so the agent reads and writes exactly what was shared with it. `google-token --delegated ACCOUNT` does the same for a Google account a person delegated to the agent (the vault entry `google-delegated-<account>`, sealed only to the machines that run the agent); the runner stages it before each turn (`cred_cli.py`, `$CANOPY_DELEGATED_GOOGLE`).

## What keeps one agent's secrets from another

What each layer actually enforces, and what it does not:

- **The decision (§ above).** Governs *resolving* only; it does nothing about a file another agent's resolve already left on disk.
- **Unix users per isolation unit.** The operating system is the boundary only on a runner box that gives each unit its own Unix user; on a box that runs every agent as one user, and on a laptop (one desktop user), only the decision and the rails apply. Root on a box is trusted.
- **Secrets off disk** (opt-in per runner, `$CANOPY_TURN_ENV_ROOT`): `canopy agent prepare --runner` resolves and checks in memory and writes no `~/.<slug>/.env`, `<clone>/.env` or `.gws-sa-key.json`; each turn's env is written by `canopy cred env --runner --out PATH` (0600; refused outside the root), handed over as `$CANOPY_AGENT_ENV_FILE` and removed when the turn ends. Every reader looks there first (`src/orchestrator/agent_env_file.py`, the MCP headersHelper). The file is skipped for a slug other than the turn's agent. `agent_session_start.py` leaves it alone.
- **Per-agent mail keyrings** (opt-in per runner, `$CANOPY_GOG_SECRET_FILE`): each agent gets its own gog home `~/.gog/<slug>/` and a derived keyring password, so a confined turn's mail access carries only its own agent's pair and never the secret's path (`src/orchestrator/gog_home.py::confined_mail_env`). Without the variable gog keeps one home and one keyring.
- **Read rails (defence in depth inside one Unix user).** `plugins/canopy/agent-core/gating-baseline.json` (`always`, `agent_only`) denies an agent's turn reading another agent's `~/.<other>/.env`, another turn's per-turn env directory, the box's `~/.canopy/device-key/`, another agent's `~/.gog/<other>/`, the shared gog credential files, and `/etc/canopy-runner/`, through the shell and the file tools, plus `canopy cred env --print-json`. A person's own session is not railed, and `~/.claude/projects/` is not railed (transcript-review skills read other sessions by design). Rails match spelled-out paths; a turn that builds a path another way is not stopped by them. They are not the boundary.
- **Residual risk:** one agent's FULL turn reading another's secrets in the SAME Unix user is stopped only by the rails.

## Canopy Vault

- `/resolve` answers 401 `device_proof_required` (for every agent) unless the request carries a device proof from an **approved** device key (a DPoP proof, RFC 9449), or, on an EC2 box with an instance role, `X-Canopy-Aws-Proof`. A person approves a new key in canopy-web with a passkey. `canopy vault device` and `canopy vault status` show it.
- Kinds, from the strongest the machine has (`src/orchestrator/device_key.py::KINDS`, module docstring): `secure-enclave` (macOS; no user-presence requirement, so unattended turns never prompt), `tpm` (Windows), `aws-iam` (the role IS the identity), `software` (`~/.canopy/device-key/sw.pem`, 0600). **A runner isolation unit on an EC2 box must use `software`** (`--kind software` or `$CANOPY_DEVICE_KEY_KIND=software`), because `ensure()` otherwise picks `aws-iam` wherever the instance role answers; the box's pairing step registers each unit's `--public-jwk` and records it with `--registered KEY_ID --runner ID`.
- `$CANOPY_ECDH_BROKER=<socket>` puts the unit's ECDH key behind a root-owned KMS broker (kind `kms-broker`; it only ever answers the ECDH secret); nothing is created on disk.

### Zero-knowledge

For a zero-knowledge agent canopy-web holds only `zk1:` ciphertext and wrapped keys. Formats: canopy-web's `docs/vault/zk-v1/zk_v1_reference.py`; `src/orchestrator/vault_zk.py` reproduces its vectors byte for byte (`tests/fixtures/vault_zk/`). CLI: `canopy vault zk status|enable|set|grant|rotate|settle` (`src/orchestrator/vault_zk_cli.py`).

- Each machine holds an ECDH key the agent's vault key is wrapped to. A ZK answer is unwrapped and decrypted by `cred_resolve`, so `cred`, `agent prepare --runner`, `cred exec` and `cred sync` see plaintext exactly as for a custodial agent. A 409 `zk_wrap_missing` means an admin must grant this device.
- **Sealed values (`zks1:`).** Secrets canopy-web mints itself (a Connect Google mailbox's `gog-token`, a rotated `canopy-pat`) are sealed to recipients' PUBLIC keys because canopy-web holds no vault key. `cred_resolve.open_zk` opens this device's entry; a vault-key holder **settles** them into `zk1:` with `canopy vault zk settle` so every member reads them.
- **Giving an agent a secret without its vault key** (`vault zk set --sealed`, or `--request ID`): the CLI seals `zks1` **only** to recipients carrying a valid zk-v1 §7 endorsement from a person pinned in `~/.canopy/vault-pins.json` (keyed by person, like `known_hosts`). It recomputes every thumbprint locally; a first-seen endorser is shown and confirmed before pinning (a value piped on stdin never answers the question); a pinned person whose key changed stops the command and is never re-pinned silently; no endorsed recipient is an error, never a fallback to the server's list. Skipped recipients get the value at the next settle.

## Local use: a private runner, a licence, a local turn

A person who is not an agent's admin can run the agent in their **own** Claude Code, and canopy keeps the record. `canopy agent use <slug>` (`src/orchestrator/agent_use.py`) sets it all up idempotently and reports ok / done / NEEDS YOU per step.

1. **This computer is registered for local use** (`src/orchestrator/local_cli.py::register_this_computer`; canopy-web calls it a *private runner*), recorded in `~/.canopy/private-runner.json`. A runner set private takes no fleet work, only its owner's pins and bound sessions, and signs in with its device key instead of a PAT. Acting at a lower role is per turn or per client (`CANOPY_ACTS_AS`), never a machine setting.
2. **`$CANOPY_AGENT`** is set for the project (`.claude/settings.local.json`).
3. Each session is a **local turn**. `agent_session_start.py` (SessionStart) runs `canopy local open` FIRST: canopy-web issues the **licence** when the agent's caller list gives the person the whole agent (else 403, recorded for its admins), opens a RUNNING `origin=local` turn, and the hook exports `CANOPY_TURN_ID`, so the credential reads that follow name the turn and go out under the licence (`via=license` in the read log). `caller_context.py` shows the envelope on the first prompt and renews the lease; `local_turn_sync.py` streams the transcript while the session runs (`CANOPY_LOCAL_LIVE_TRANSCRIPT=0` turns that off; the close still uploads everything); `local_turn_close.py` (SessionEnd) uploads the rest and finishes the turn.

A licence read never carries the agent's own canopy login, its owner's GitHub delegation, a lent credential or the shared vault key. A runner's own sessions are not eligible (`$CANOPY_TURN_ID` already set, a confined `cx-` session, a headless-engine session `CANOPY_HEADLESS` / `CANOPY_ONE_SHOT_TURN`, a desktop-engine worktree `<cwd>/.canopy-desktop/`, an emdash session on a machine that runs the runner app): `agent_session_start.py::RUNNER_ENV_MARKERS` and neighbours decide.

A session idle past the 8-hour lease (`local_cli.py::STALE_AFTER`) finds its turn swept `lost`; the next prompt's `canopy local renew` opens a new local turn for the same session. A session that died without SessionEnd is finished `failed` by the next session's `canopy local open`, not `done`.
