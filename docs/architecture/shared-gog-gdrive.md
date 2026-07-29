# Shared GOG email + Google Workspace capability for the agent fleet

**Date:** 2026-07-01 · **Status:** decision-grade design (Jon's directive: "nearly everything needs
to use gog and gdrive — figure out the idealized version of what we've built and make it available
to all agents via canopy, with the appropriate settings carved out for each agent")
**Companion:** `docs/agent-operating-model.md` (§4a common-vs-agent boundary; §4c capability
borrowing) · ACE's adoption spec (`ace: docs/superpowers/specs/2026-07-01-agent-operating-model-adoption.md`)

## 1. What exists today (the parts of the ideal, scattered)

Every proven piece already exists — in three different repos:

| Piece | Where it lives | State |
|---|---|---|
| Google Workspace MCP (~50 domain-neutral atoms: `drive_*`, `docs_*`, `sheets_*`, `slides_*`, forms read, markdown→doc, template-copy) | ACE `mcp/google-drive-server.ts` | Production-hard: shared-drive write probe, transient-retry envelope, large-payload resolver, `google-shim` (194 MB `googleapis` → per-API subpackages) |
| HTML email send wrapper (multipart, reflowing; Gmail display-wraps plain text at ~72 cols) | echo `bin/echo_email.py` → adapted as ACE `bin/ace-email` | Proven in echo turns; ACE copy adds plugin-data `.env` resolution |
| Mark-read (gog has no mark-read; API reads don't clear UNREAD) | echo `bin/echo_mark_read.py` → ACE `bin/ace-mark-read` | Works; gog-credential reuse via keychain |
| Raw-send deny rail | echo `block_raw_gog_send.py` → hal's generalized `hooks/gating_guard.py` → ACE's copy (+ `tool_pattern` for MCP atom names) | Three near-identical copies |
| Inbound routing contract (send records `thread_id` → inbound triage routes the reply to the right state scope) | ACE (comms-log per run) · echo (contact-memory per sender) | Same shape, agent-specific routing map |
| Counterpart tiers (act / correspond / none) | ACE `config/allowlist.txt` + derived correspond tier | ACE innovation — see §3 |
| Per-agent gog MAILBOX (`<slug>@dimagi-ai.com`, never shared) over a SHARED fleet OAuth client (`canopy`) | Mailbox enforced by convention in echo AND ace; client shared fleet-wide | The mailbox is the identity that must not bleed — the client is just the app |

The problems with the status quo: echo consumes ACE's Drive MCP **under ACE's service-account
identity** (it cannot see the Connect Marketing drive — wrong identity, no per-agent scoping); hal
has email documented but unwired (nothing to build on); every new agent re-copies the wrapper +
mark-read + hook trio; and fixes don't propagate.

## 2. Decision: what goes common vs what stays per-agent

Per the operating model's §4a boundary (*"logic, adapters, and cross-agent skills are common;
identity, rules, secrets, and domain skills are the agent's"*):

| Canopy provides (engine/adapter, fix-once-propagate) | Each agent supplies (mounts/carve-outs) |
|---|---|
| `canopy email` adapter: guarded HTML send, reply-threading, mark-read, login preflight | Mailbox + gog client name (`config/agent.json`: `email`, `gog_client`), the allowlist, the routing map (thread → agent-state scope) |
| The gating **engine** (`gating_guard.py`, already factory-templated) | The gating **rules** (`config/gating.json`) — deny rails only, see §4 |
| Google Workspace MCP (extracted from ACE, atoms unchanged) | Identity mode + scope: SA key path OR gog-OAuth client; root folder / allowed shared drives |
| Factory templates that wire all of the above | Persona, domain skills, turn checklist text |

## 3. Email: the idealized adapter

**Engine** (canopy Python package, `canopy email <send|mark-read|archive|preflight>`):
- `send` — the echo/ACE HTML wrapper, parameterized by agent config: `--agent <slug>` resolves
  mailbox + client from the agent repo's `config/agent.json`; body-file contract (single-line
  paragraphs, bullets); `--reply-to-message-id`; `--dry-run`; JSON result with `message_id` +
  `thread_id`. The wrapper is the ONLY send path (each agent keeps a thin `bin/<slug>-email` shim
  or calls the CLI; the deny rail points at it).
- `mark-read` — Gmail API UNREAD-label removal via the agent's own gog credentials.
- `archive` — the other half of turn housekeeping: removes INBOX + UNREAD so a handled thread
  leaves the agent's own inbox. Own mailbox only and reversible, so it rides no approval gate.
- `preflight` — gog auth liveness for the agent's client, with the exact `gog login …` remediation.

**Per-agent carve-outs:**
- **Identity:** one mailbox per agent (`<slug>@dimagi-ai.com`), minted at agent-creation time and
  never shared, over ONE shared fleet OAuth client (`canopy`). A gog "client" is the app identity
  (client_id + client_secret) — reusing it across agents is fine and reduces setup; the failure the
  fleet was built to avoid is acting as another agent's MAILBOX, governed by `--account`, not the
  client.
- **The shared OAuth app is intentionally "External" + "In Production" — do NOT switch it to
  Internal.** The `canopy` OAuth client (Google Cloud project `canopy-494811`) stays **External**
  because canopy-web also uses it to let **`dimagi-associate.com`** accounts — which are *outside*
  the Dimagi Workspace — OAuth in; "Internal" user type would lock those logins out. Its publishing
  status is **In Production**, so refresh tokens are long-lived. (The 7-day refresh-token expiry that
  bites automated OAuth apps only affects the **"Testing"** publishing status — NOT this app, which
  is in Production. Don't "fix" a non-problem by flipping user type.) So when fleet email breaks, it's
  a **provisioning** issue — a missing/misnamed 1Password item so `credentials-canopy.json` never
  lands — which `canopy email preflight` now diagnoses precisely; it is not the OAuth app config.
- **Migration status (2026-07-08, superseded 2026-07-25):** `hal` and every agent minted by the
  factory use the shared `canopy` client (item `Canopy - gog OAuth client` in 1Password
  AI-Agents). `echo` was **grandfathered** on its own hand-placed `credentials-echo.json`, not
  declared for provisioning — a known latent gap. The fleet has since moved to `.env.tpl` + `op
  inject`/`op read` as the standard (`config/secrets.yaml` deleted from ada/eva/hal/echo — see
  `agent-core/agent-runtime.md`); a client credentials file like this one now resolves directly
  via `op read "op://<vault>/<item>/<field>" > credentials-<slug>.json` rather than through a
  declarative manifest.
- **Tiers (generalizing ACE's model):** `act` = static allowlist (`config/allowlist.txt`) — senders
  who may steer the agent's work; `correspond` = **derived from the agent's own state** (ACE: LLO
  contacts in the routed run's `run_state.yaml`; echo: contacts with an existing contact-memory
  page) — approval-gated replies only; unknown = read-only triage. The derived tier is the important
  invention: the allowlist stays in sync with the source of truth instead of rotting in a file. This
  is also the **internal + external** answer — agents can face external counterparts safely because
  the external tier is scoped to state the agent itself created.
- **Routing map:** the send-side contract is universal (every send records `thread_id` into the
  agent's state layer); *where* it lands is the agent's choice.

## 4. Gating: rails, not approval gates (fleet-wide revision, Jon 2026-07-01)

Operational experience: hal's approve/ask hook rules **worked poorly** — a PreToolUse "ask" is a
blocking modal that stalls autonomous work and nags interactive sessions. ACE's model is better:
**hooks carry deny rails only** (make the wrong path impossible; the agent hits the rail, reads the
message naming the right path, self-corrects, keeps going — zero autonomy cost), and **approval
lives in the procedural layer** (ACE: pause-point mode matrix persisted to `run_state.yaml`; echo:
the turn's explicit approval step; both: review posture). Procedural gates are *state* — resumable,
auditable, non-blocking — not modals.

Consequences:
- `create-agent` factory: the templated `config/gating.json` should default to deny rails +
  an empty `approve` list, and the generated `turn` skill should carry the approval step
  procedurally (it already does — Step 2's "present for approval").
- `docs/agent-operating-model.md` §1a's enforcement ladder stands, with this revision: "hooks make
  the wrong path impossible" ≠ "hooks ask permission." Asking is the turn checklist's job.
- hal: convert its three approve rules (`git push`, `gh pr create|merge`, `gh repo create|delete`)
  to procedural turn steps + (where a genuinely wrong path exists) deny rails.

## 5. Google Workspace: extraction path

**Endgame:** the ~50 domain-neutral Workspace atoms leave ACE and become a standalone shared MCP
(working name `gws-mcp`; TS + `npx tsx`, no build step, exactly as they run today), consumed by any
agent via a `.mcp.json` entry (operating model §4c — the echo↔chrome-sales borrowing pattern,
formalized). ACE's ACE-aware atoms (`resolve_opp_path`, `validate_run_state`,
`classify_phase_writeback`, `update_yaml_file`, …) stay in ACE, layered on the shared server.

**Per-agent settings contract** (env, provisioned via `canopy provision` / 1Password):

```
GWS_IDENTITY_MODE = sa | gog          # service-account (headless) or agent-OAuth via gog creds
GWS_SA_KEY_PATH   = <path>            # sa mode: per-agent SA key (or shared SA + per-drive grants)
GWS_GOG_CLIENT    = <slug>            # gog mode: acts AS the agent (Docs authored by the persona)
GWS_ROOT_FOLDER_ID / GWS_ALLOWED_DRIVE_IDS   # scope: writes probe-checked against this allowlist
```

Two identity modes are both real needs today: ACE writes headlessly as a service account; echo
authors Docs *as echo* on the Connect Marketing drive. The scoping env is what fixes "echo runs
ACE's server under ACE's SA": each agent mounts the same server with its own identity + scope.

**Interim (until extraction lands):** borrowing ACE's installed `ace-gdrive` stays acceptable for
SA-mode reads/writes on drives the ACE SA can reach — but no NEW agent should take a dependency on
`mcp__plugin_ace_…` tool names; wire a `.mcp.json` entry pointing at the server file instead, so the
rename to `gws-mcp` is a config change.

**Interim doc adapter — `canopy gdoc` (shipped 2026-07-21, `orchestrator/agent_gdoc.py`).** The one
Workspace operation the fleet does constantly — publish a markdown deliverable as a *rendered* Google
Doc authored **as the agent** — did not need to wait for the full extraction, and its absence had the
fleet re-copying the wrapper anyway (echo `bin/echo_gdoc.py`, eva's `gog drive upload --convert` prose,
hal's `--convert-to doc` stub — three implementations of one procedure, drifting on format + share
posture). So it gets the same treatment email did: a thin `canopy gdoc publish` adapter, sibling of
`canopy email`, that resolves identity from `config/agent.json` and does convert→share→verify in one
place. It authenticates through **gog's token bucket** (`--account`/`--client`), *not* the macOS
Keychain echo's REST route used (which blocks in non-interactive turn shells) and *not* ace-gdrive's
service account (which can't author as the agent — the whole point). Per-agent carve-outs are two
optional agent.json keys: `gdrive_root_folder` (Projects-root, so `--parent` defaults) and
`gdrive_share_default`. This does **not** preempt §5: the full `gws-mcp` extraction still owns the rich
`docs_*`/`sheets_*`/`slides_*` batch surface and can absorb this verb later; `gdoc` covers only the
single high-frequency op. Each agent's `gdoc-writer` skill collapses to identity + a call to it.

**Travels with the extraction:** `google-shim.ts`, `transient-retry.ts`, `atom-payload-resolver.ts`,
the shared-drive write probe, and ACE's drift gates (registration-coverage, schema-dump staleness)
as the new repo's CI.

## 6. Sequencing

1. **Now (this PR):** this design doc + tracking issues; ACE ships its side (turn framework, rails,
   guarded email — ace#816).
2. **Adapter first** (small, high leverage): `canopy email` engine + factory template updates +
   rails-only gating default. hal gets a working email channel as the validation case (its README
   already promises one).
3. **Extraction second** (bigger): `gws-mcp` repo, per-agent env contract, echo migrates off
   `mcp__plugin_ace_…` names, ACE consumes the shared server + keeps its domain atoms local.
4. **Not decided here:** ace-web vs canopy-web consolidation (explicitly deferred by Jon); Slack/
   Telegram adapters (the email adapter's interface should not preclude them — same
   send/receive/route shape).
