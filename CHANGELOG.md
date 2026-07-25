# Changelog

All notable changes to canopy are documented here, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Versions track the `VERSION` file and `plugins/canopy/.claude-plugin/plugin.json`,
which are kept in lockstep (every change under `plugins/canopy/` requires a patch
bump — see `CLAUDE.md`). The project does not tag releases. Pre-history
prior to the entries below was not formally changelogged; this file starts from the
recent, verifiable themes in the git log.

## [0.2.338] - 2026-07-23

### Added

- `canopy agent doctor --fix` — applies the safe, non-interactive repairs (materialize
  secrets via `canopy provision`; register on canopy-web), then RE-RUNS the checks so the
  verdict reflects reality rather than intent. The fixer table is deliberately short: a
  repair earns its place only if it is non-interactive, idempotent, and cannot destroy
  work. gog consent, plugin install, and PAT minting stay printed instructions — a doctor
  that half-performs an interactive step leaves a worse mess than one that asks. Checkout
  drift is likewise reported, never auto-pulled: a stale checkout can carry unpushed
  commits, and blindly fast-forwarding is how that work gets stranded (this run found
  exactly one such commit, unpushed on a main 66 behind origin).
- `canopy agent doctor` — **Secrets materialized** check. `Secrets manifest` proves the repo
  *declares* what it needs; it says nothing about whether `canopy provision` was ever run
  HERE, which is the entire point of a per-machine doctor. A fresh macOS user has every
  repo, every manifest, and none of the resolved files. First run found two agents with no
  `.env` on a machine that had been running them all day.
- `canopy agent doctor` — **Rails enforced** check: an ACTIVE probe, not a file read. Every
  other rails check reads JSON, and none prove the guard blocks anything — a broken import,
  a bad interpreter, or a subtly wrong pattern all leave a perfectly valid config that stops
  nothing. The check predicts the guard's answer from its own effective rails, then executes
  `hooks/gating_guard.py` with a synthetic PreToolUse payload and requires exit 2. The probe
  command is passed as text and never run. Skips when no rail predicts a block, so it never
  asserts a block the agent's own config does not call for.

## [0.2.337] - 2026-07-23

### Added

- `canopy agent doctor` — **Plugin install** check. Every other check reads the agent's
  *repo*, so all of them pass on a machine where the repo is cloned but the plugin was
  never installed — a machine on which none of that agent's skills (`/ada:turn`,
  `/echo:turn`) can be invoked at all. This is the dominant real-world gap when moving to
  a new machine or macOS user; on one such account four of five agents had a full
  checkout, valid config, and no plugin. Missing registry is a skip, not a failure.
- `canopy agent doctor --all` — surfaces `fleet_align.checkout_warnings()` before the
  per-agent verdicts. An agent whose checkout is parked off its default branch is
  *invisible* to discovery and produces no row, so a fleet-wide "all ready" could be a
  confident green over a fleet quietly missing members. First live run flagged all five
  repos stale (7–67 commits behind) and one parked on a feature branch. The summary line
  now never reads as a bare "all ready" while drift warnings are present, and
  `--json-output` gains `discovered` and `checkout_warnings`.

## [0.2.336] - 2026-07-23

### Fixed

- `canopy agent doctor` — three false positives that made the fleet readiness gate
  untrustworthy. On a healthy machine it reported 3 of 5 agents FAIL; all three were
  the doctor's error, not the agents'.
  - **Gating rails** counted only `config/gating.json`'s local `deny` array, so agents
    that mount the fleet baseline via `"channels": ["email"]` (echo, hal) read as
    "0 deny rails but <slug>-email exists" while being fully railed at call time.
    The check now resolves the baseline the same way `hooks/gating_guard.py` does and
    reports *effective* rails (baseline + local). A mounted-but-unresolvable baseline —
    the state where the guard fails CLOSED and blocks every guarded call — is now its
    own explicit failure rather than a silent pass.
  - **Hook wiring** recognized only `.claude/settings.json`. Agents shipped as a Claude
    Code plugin (ace) register the guard in `hooks/hooks.json`; their live, firing rails
    were reported as decorative. Both registration paths are now accepted.
  - **Auth services** required the fleet-wide `LOGIN_SERVICES` of every agent — failing
    hal and ace over `appscript`, which neither uses, while never checking `slides`,
    which echo needs and the constant omits. Requirements are now per-agent via
    `gog_services` in `config/agent.json`, defaulting to `CORE_SERVICES`. The
    remediation command re-requests required *union* already-granted scopes, since
    `gog login --services` replaces the grant set and would otherwise revoke working
    scopes while fixing an unrelated gap.

## [0.2.309] - 2026-07-21

### Fixed

- `agent-core/turn.md` — the turn now knows not to reply to its own message. A
  thread whose newest message is the agent's own outbound reply is already
  handled (the ball is in the counterpart's court); the first triage check on any
  unread thread is *who sent the last message*, and if it was the agent, mark it
  read and move on. A thread can re-enter `unread` for reasons unrelated to a new
  inbound (label churn, poller re-touch, a send that didn't clear the flag), so an
  unread badge is a hint to look, not proof someone replied. Prevents the "respond
  to yourself" failure hit on 2026-07-21 (Hal's Feature Requests thread).

## [0.2.308] - 2026-07-21

### Removed

- `patch-emdash` skill — retired. It flipped emdash's `type='automation-run'`
  tasks to `type='task'` so DB-injected automation runs appeared in the sidebar,
  but the canopy runner moved to the CDP executor (it drives emdash's real UI and
  creates real `type='task'` sessions directly — no automation runs, no injection).
  The asar patch buys nothing on that path. Retired alongside the runner's legacy
  `inject` executor + schema-vet in canopy-web; the surviving emdash read-surface
  is now checked by `canopy_runner verify-emdash`.

## [0.2.298] - 2026-07-17

### Changed

- Reverted the factory `stamp-commands` generator (0.2.293): a Claude Code skill is
  **already** a launchable slash command (`/<slug>:<skill>`) — custom commands were merged
  into skills upstream, so wrapping every entry-point skill in a `commands/*.md` file was
  redundant. Launchability is now controlled by native frontmatter instead of wrappers or a
  custom `commands/.exclude`: the scaffold ships `task-tracker` and `agent-turn-review` with
  `user-invocable: false` (Claude-invokes-only, kept out of the `/` menu), and `create-agent`
  documents the model — default = human + Claude; `user-invocable: false` for internal
  sub-steps/utilities; `disable-model-invocation: true` for human-only side-effecting actions.

## [0.2.286] - 2026-07-14

### Changed

- Inbox continuity is now DRY + fleet-canonical. Fleet **inbox filters** live in
  `src/orchestrator/inbox_filters.py` (single source of truth) and apply to one or
  all mailboxes via `canopy email apply-filters [--all] [--sweep]` (--sweep also
  retroactively clears the existing junk backlog). `agent-core/turn.md` Step 2 now
  honors an optional `--thread`/`--slack` scope (the harness runner hands the exact
  item so the agent doesn't re-resolve) and marks a handled thread read. Retired the
  `drain-turn` skill — the agent's own `/<slug>:turn` subsumes it; the runner calls
  that with the scope ref.

## [0.2.283] - 2026-07-14

### Changed

- Agents now request **Apps Script** at login: `LOGIN_SERVICES` adds `appscript`
  (some agents drive Google Drive via Apps Script; the scope must be granted at
  login). The per-agent doctor gains an **Auth services** check that reads
  `gog auth list --json` and fails when a required service (notably `appscript`)
  isn't granted, with the exact re-login command. Existing agents keep working
  for email; the doctor now flags any that need to re-login to pick up the scope.

## [0.2.282] - 2026-07-14

### Added

- `patch-emdash` skill — make emdash automation runs appear as live tasks in
  their project sidebar (canopy-runner turns show under the agent's project,
  not only under Automations). Safe same-length `app.asar` byte edit, no
  re-signing; idempotent + fail-closed; re-apply after emdash updates.

## [0.2.280] - 2026-07-13

### Added

- `drain-turn` skill — the body of an automated canopy-web harness turn:
  resolve the active turn, mark it running, drain the agent's pending board
  commands under normal guardrails, and finish the turn (never left open).
  Invoked by the emdash automation the canopy runner triggers
  (canopy-web#196).

## [0.2.276] - 2026-07-13

### Fixed
- **`walkthrough-share` + `ddd-upload` follow canopy-web's token-gated walkthrough model**
  (canopy-web PR #194): upload scripts now read the owner-only `share_url`
  (`…/walkthrough/<id>?t=<token>`) from the 201 response instead of the removed raw
  `share_token` field, print `/walkthrough/<id>` viewer URLs instead of the reclaimed
  pre-tenancy `/w/<id>` form, and the DDD docs-page hero embed rewrites both
  `/walkthrough/<id>` and legacy `/w/<id>` share URLs to their `/content` byte stream
  with the token preserved. Without this, published "Share:" links were login-gated
  rather than public.

## [0.2.264] - 2026-07-08

### Changed
- **Email engine `--reply-all` gains thread mode** (`--thread-id`, preferred): recipients
  AND the threading message-id derive from the thread's LATEST non-self message. Ports
  echo's live lesson — `gog gmail read` is a THREAD reader; a bare message id 404s on any
  multi-message thread, and replying to your own latest message is never right.
  `derive_reply_all` now returns `(to, cc, reply_to_message_id)` and takes exactly one of
  `thread_id`/`message_id` (message-id mode kept for single-message threads). Unblocks
  echo's convergence onto the engine with its documented `--reply-all --thread-id`
  invocation unchanged.

## [0.2.263] - 2026-07-08

### Added
- **CI runs the python suite** (`.github/workflows/python-tests.yml`) — closes the gap
  that let #274 land with an untiered module and silently break main's
  `test_plugin_boundary` (fixed in #287). Offline suite only (walkthrough/browser
  tests excluded — they need playwright + a display and run locally via
  `uv sync --extra browser`).

## [0.2.262] - 2026-07-05

### Added
- **canopy-gws MCP server** (`plugins/canopy/mcp/gws-server.ts` + `mcp/gws/`) — ACE's
  domain-neutral Google Workspace atoms extracted into canopy as a shared per-agent
  MCP (jjackson/canopy#262, implements the shared-gog-gdrive design's Drive half).
  32 atoms ported verbatim from ACE's production `ace-gdrive` server: all
  `drive_*` (17), `docs_*` (4), `sheets_*` (7), `slides_*` (3), plus
  `get_google_form_definition` and `read_personal_drive_doc`. The 10 ACE-aware atoms
  (resolve_opp_path, update_yaml_file, validate_run_state, render_run_readme, …)
  stay in ACE. Registered in the plugin's `mcpServers` as `canopy-gws` — every agent
  with canopy installed gets the Workspace atoms.
- **Per-agent identity contract, fail-loud** (`mcp/gws/lib/identity.ts`): the server
  resolves WHO it acts as from session env only — `GWS_IDENTITY_MODE=sa` +
  `GWS_SA_KEY_PATH` (sa mode implemented; `gog` mode fails loud as a tracked
  follow-up), `GWS_ROOT_FOLDER_ID` (surfaced via `drive_diagnose`), and
  `GWS_ALLOWED_DRIVE_IDS` (write allowlist enforced by the Shared-Drive write probe,
  generalizing ACE's `assertParentOnSharedDrive`). Missing identity env is a fatal
  startup error naming the vars; there is no default identity fallback.
- **Node toolchain for the plugin** (`plugins/canopy/package.json` + `tsconfig.json`
  + `vitest.config.ts`): per-API `@googleapis/*` subpackages via the google-shim
  (not the 194 MB `googleapis` meta-package), tsx, typescript, vitest.
  `google-auth-library` is pinned exactly to the version `googleapis-common` pins so
  npm dedupes to one copy (two copies break tsc with nominal `#private` mismatches).
  `/canopy:setup` gains step 7 (npm install in the installed plugin dir) and
  `/canopy:update` installs deps after the rsync.
- **Offline test suite + CI drift gates** (`plugins/canopy/test/gws/`, 12 files /
  ~115 tests): registration-coverage snapshot gate (32 atoms, prefix allowlist, no
  dupes, plugin.json wiring, fail-loud-identity source check, no-ACE-atom-leak),
  identity-contract tests, allowlist write-probe tests, and the portable
  handler/retry/classifier tests from ACE (mocked Google clients, no network).
  New `.github/workflows/gws-tests.yml` runs `npm install` + `tsc --noEmit` +
  `vitest run` on plugin-node paths.

## [0.2.260] - 2026-07-05

### Added
- **`canopy agent doctor`** (`src/orchestrator/agent_doctor.py`) — per-AGENT health
  check composing the existing point-checks into one command: identity
  (`config/agent.json`), gating rails, **hook wiring** (gating_guard.py actually
  registered as a PreToolUse hook — rails without it are decorative), secrets
  manifest (or a declared self-managed `provisioning` in agent.json — the ACE
  case), live gog email auth (full multi-line remediation preserved), and
  canopy-web registration + board reachability. Zero deny rails on an
  outbound-capable agent (email shim present) FAILS. `canopy doctor` covers the
  plugin install; this covers one agent repo on THIS machine. Born from hal
  (2026-07-02); review tweaks from the ACE flagship session (2026-07-03).
  Wired into `create-agent` § Channel + setup as the mandatory finish line.

## [0.2.256] - 2026-07-03

### Added
- **The generic verdict aggregator is now plugged into `ddd-run`** (canopy#273
  item 1). `scripts.ddd.verdicts.discover_extra_verdicts(run_dir)` sweeps the
  four out-of-chain verdict artifacts (`verdict-timing.json`,
  `verdict-video.json`, `verdict-why.yaml`, `verdict-actionability.yaml`),
  loads each through `load_verdict` (kind/gate/live_state_verified stamped,
  out-of-chain score cap enforced at the schema layer), and returns
  `(verdicts_by_kind, paths_by_kind)` shaped for
  `assemble_run_state(extra_verdict_paths=...)` +
  `compute_convergence(extra=...)`. `ddd-run` SKILL.md Step 4 and
  `agents/ddd.md` Step 7 now wire it, so the advisory verdicts flow through
  the unified schema in live runs instead of only in tests. Structure tests
  pin the wiring; the reference drift gate guards the imports.
- **Cap-visible verdict report lines** (canopy#273 item 3).
  `run_pipeline.format_verdict_line(verdict)` renders a capped verdict as
  `4.0/5 (pass — capped from 4.8, not live-state verified)` (from
  `uncapped_overall_score`), never a bare `4.0/5` indistinguishable from an
  honest score. `ddd-run` Step 5's summary renders every verdict line —
  gating pair + advisories — through it.
- **Structure test for the `verdict-user.yaml` metadata stamp** (canopy#273
  item 2): `kind: user_artifact` / `gate: gating` / `live_state_verified: true`
  can no longer silently drift out of the ddd-run SKILL.

### Changed
- **`Verdict.gate` now defaults to `advisory` (was `gating`)** (canopy#273
  item 4). A legacy verdict loaded via bare `model_validate` previously became
  a gating, unverified verdict that could hard-block convergence forever. The
  flip is the safe direction: `compute_convergence_all` returns False when no
  gating verdict is present, so convergence stays demonstrated, not defaulted.
  The two gating kinds (`concept`, `user_artifact`) are unaffected — both are
  stamped explicitly by their skills at write time and by
  `verdicts.KIND_DEFAULTS` at load time. `Verdict.json` regenerated. Audit
  found no other gating-by-default dependents (`.gate` is only read by
  `compute_convergence_all`; test stubs now stamp explicitly).

### Fixed
- **`SnapshotAction` missing from `ACTION_CLASSES`** — the `snapshot` action
  (0.2.253) was added to the `Action` union but not the class tuple, breaking
  the single-source guard tests on main
  (`tests/walkthrough/test_action_kinds_single_source.py`). One-line addition.
## [0.2.255] - 2026-07-03

### Added
- **Email engine: `--reply-all`** (`canopy email send --reply-all --reply-to-message-id <id>`)
  — derives To (original sender) + Cc (everyone else on the original To+Cc, de-duped,
  excluding the agent) from the message's JSON headers. Ported from echo's
  `derive_reply_all`; guards the silently-dropped-Cc bug (operating-model §1b rule 3).
  Explicit `--cc` merges in; dry-run output now carries the same `message_id`/`thread_id`
  keys as a real send so scripted callers never branch.
- **Identity-bleed guards**: `canopy email send --account <other>` warns loudly when it
  disagrees with the resolved repo identity, and the factory's templated gating.json
  gains a deny rail on `canopy email send … --account` (the shim pins repo identity).
- Factory: the email shim exits with an install hint when the `canopy` CLI is missing
  (was a bare FileNotFoundError traceback); `create_agent` warns on the
  `<slug>@example.com` mailbox placeholder instead of failing late at gog send.

### Changed
- **Email engine mark-read: no more Keychain** — now shells `gog gmail thread modify
  --remove UNREAD` per thread (gog's own token bucket). The previous Gmail-API path
  minted a token via macOS `security find-generic-password`, which blocks FOREVER on a
  GUI prompt in non-interactive agent shells (dimagi-internal/ace#827 — hit live in an
  ACE turn; echo's `echo_mark_read.py` carries the same class and should converge).
- `send()` now has a 120s subprocess timeout — a hung gog fails the send loudly instead
  of hanging the whole turn.

## [0.2.246] - 2026-07-01

### Added
- **Shared GOG email + Google Workspace capability design**
  (`docs/architecture/shared-gog-gdrive.md`). Decision-grade design for making the
  fleet's two universal needs — gog email and Drive/Docs/Sheets/Slides — canopy-provided
  engines with per-agent carve-outs (mailbox + gog client, allowlist tiers, SA-vs-gog
  identity mode, drive scoping). Names ACE's production `ace-gdrive` server as the
  extraction source (`gws-mcp`) and the echo/ACE HTML send wrapper as the `canopy email`
  adapter engine; generalizes ACE's derived correspond-tier allowlist as the fleet's
  internal+external counterpart model.

### Changed
- **Fleet gating default revised: rails, not approval gates** (Jon, 2026-07-01).
  `docs/agent-operating-model.md` §1a now records that approve/ask hook rules (the hal
  experiment) stall autonomous work; hooks carry deny rails only, and approval semantics
  live in the procedural layer (turn checklist approval step, ACE's pause-point state
  model). The create-agent factory's templated gating config should default `approve`
  to empty (tracked in issues).

## [0.2.245] - 2026-07-01

### Added
- **DDD runs can target a specific canopy-web workspace.** With canopy-web now
  multi-tenant, DDD artifacts (walkthroughs, reviews, run packages) previously
  always landed in the org default workspace (`dimagi`). A run can now be pinned
  to a workspace: the DDD write/read URLs become `/api/w/<ws>/…` and the human-
  facing package/landing links become `/w/<ws>/ddd/…`. Resolution precedence:
  explicit arg → env `CANOPY_WEB_WORKSPACE` → per-repo `.canopy/ddd/config.yaml`
  (`workspace:` key) → none (unchanged flat behavior → org default). So e.g. the
  Connect repo commits `.canopy/ddd/config.yaml` with `workspace: connect` and
  its DDD runs publish into the `connect` workspace. Implemented as a chokepoint
  in `orchestrator.canopy_web` (`resolve_workspace`, `scoped_api_path`,
  `scoped_app_path`) + `scripts/ddd/auth.resolve_ddd_workspace`, applied in
  `scripts/ddd/review.py` and `upload.py`.

## [0.2.240] - 2026-06-30

### Fixed
- **Packaging-version drift: `plugin.json`, `marketplace.json`, and `pyproject.toml` were stranded at 0.2.237 while `VERSION` reached 0.2.239.** The 0.2.238 and 0.2.239 bumps hand-edited `VERSION` only (not via `canopy version bump`), so the three derived manifests never advanced; `canopy version bump` then refused to run on the mismatched tree. This bump re-syncs all four files to a single version. The durable preventer is making CI's "Version sync check" a required status check on `main` (now possible since the repo is public).

## [0.2.239] - 2026-06-30

### Added
- **Plugin-side eval runner (Wave 2 / P3b): the `canopy eval` runner + the verdict write path.** `AgentClient.record_verdict` POSTs a judge/QA verdict to canopy-web's run-step verdict endpoint (`/api/agents/{slug}/runs/{run_id}/steps/{key}/verdict`); `orchestrator/eval_rubric.py` `score_rubric` weighted-aggregates per-dimension scores into an `overall_score` + tier (the ACE verdict-schema math, decoupled from ACE); `canopy eval score|record` ties them so any agent can self-grade against the unified run lifecycle. The LLM judge that produces the per-dimension scores stays a documented seam. Pairs with canopy-web's verdict endpoint + run-level eval aggregate (Wave 2 / P3a).

## [0.2.214] - 2026-06-18

### Added
- **`capture` action + late-binding `${var}`: mint an entity ON CAMERA and use its id in LATER scenes — a fresh end-to-end lifecycle each render, no fixed IDs, no per-render state resets.** The recorder's action kinds had no extract step, and `${var}` was resolved ONCE before the render from `setup.outputs` — so an id minted on camera (create a record → land on `/solicitations/207/`) couldn't flow forward, forcing fixed-ID + reset hacks. Now a scene can `capture` an id off the live page mid-render into a `${var}` that every LATER scene/action resolves. **The `capture` action** (`scripts/narrative/models.py`, dispatched in `scripts/walkthrough/_lib/recorder.py`): `source: url` reads `page.url` (a `pattern` regex's group 1 is the value; required); `source: element` reads a DOM node's `attr` (or its text when `attr` is omitted) with an OPTIONAL `pattern`. The value is trimmed; a no-group/non-matching pattern or empty result is a failure. `must_succeed` defaults **True** for capture (a later `${var}` that never bound would film a literal placeholder URL). A captured var beats a colliding setup output (captured wins; recorder warns). Each capture lands in the run report (`kind=capture, var, ok, value`) and the run summary. **Late binding** (`scripts/narrative/substitution.py` `resolve_string` + `scripts/walkthrough/_lib/orchestrator.py`): a live `vars` map is seeded from setup outputs and extended by each capture; a scene's `url` and each action's `target`/`value` resolve lazily, right before they execute — so a var captured in scene 1 flows into scene 5. The up-front pass still resolves every setup-known var (pre-warm + early scenes unaffected), leaving only genuinely-late vars for runtime; pre-warm skips any URL still holding a `${var}`. **Order-aware validation** (`scripts/ddd/spec_qa.py` + the shared validator): a `${var}` is valid iff a setup output OR a `capture` in an EARLIER scene provides it — a var used before anything binds it is rejected; a var bound only by `capture` needs no `setup:` block. JSON schema regenerated. Docs: `capture` + late-binding sections in the walkthrough and ddd-spec SKILLs, order-aware rule in ddd-spec-qa, late-binding note in ddd-run.

## [0.2.192] - 2026-06-12

### Added
- **Pre-warm pass: pay cold-cache waits OFF camera, not as frozen frames.** A 14-scene program-admin-report film ran 209s with ~45s of frozen-frame dead space — a 15s cold page load mid-scene and 7.5s of remote-image cold fetch among it. The legacy hand-built recorder this pipeline replaced had `defer_record=True` (visit everything once off camera, THEN film); canopy never got an equivalent. Now: `prewarm: true` on `UnifiedSpec` (default false; JSON schema regenerated) or CLI `--prewarm` / `--no-prewarm` (CLI wins; absent → spec value). Semantics: after `setup:` has run (so `${var}`-resolved URLs are final) and with the recording's auth (same cookies/storage_state; URL-auth replayed), but BEFORE the recorded context exists — Playwright's capture starts at page creation, so nothing here can film — a separate NON-recorded context visits each unique resolved scene URL once in spec order (continuation scenes skipped, duplicates visited once), `wait_until="domcontentloaded"` + a bounded network-idle settle (`prewarm_settle_ms`, default 4000ms; per-page cap `prewarm_page_timeout_ms`, default 15s). Best-effort throughout: a failing page is logged one-line and skipped; the render proceeds with whatever stayed cold. Provenance (`{pages, duration_seconds, failures}`) rides the RunReport as a `prewarm` key (omitted when off, mirroring `setup`). Docs: walkthrough SKILL spec example + Run section, ddd-run flag matrix, ddd-spec authoring pointer.

### Changed
- **One documented timing model: "Recording time & dead space" (walkthrough SKILL) is now the single authoritative map of what films vs what doesn't.** The mechanisms were real but scattered (pacing table here, leading-`wait_for` skip there, `--skip-empty-scenes` in a flag list) and partly fiction (see Fixed). The new section carries: the on-camera/off-camera table (setup + prewarm off camera; URL-auth nav, waits, holds, glides, settles on camera), every dead-space mechanism with when-to-use (prewarm, leading `wait_for` hold-skips, no-nav skip, `--skip-same-url`, `--skip-empty-scenes`, redundant-goto strip, crossfade), the honest pacing-preset table, and the explicit dwell hierarchy — `hold` actions (recommended, mid-scene) > `video_hold_seconds` (legacy per-scene end-of-scene override) > `final_hold_ms` (global floor) — plus the truth about `min_hold_ms` (accounting-only) and `scroll_speed_px_s` (dead). ddd-spec SKILL cross-references it (one paragraph, no copy); `_lib/config.py` opens with a code-side summary pointing back. The docs-sync gate learns the matching trigger paths: `scripts/narrative/models.py` (the canonical spec-author surface — the schemas/models.py entry was guarding a re-export shim) and `_lib/config.py` + `_lib/orchestrator.py` → walkthrough SKILL.
- **Authoring gotcha (walkthrough + ddd-spec SKILLs): never `wait_for` an element inside a collapsed/hidden container.** Selector waits wait for *visibility*; an `<option>` inside a closed `<select>` is never visible, so the wait burns its full timeout as frozen film and then "fails" even though the data loaded long ago (program-admin-report: a 20s frozen frame per take). Wait on the visible container via `:has()` — `select#x:has(option[value='…'])`, never the bare `option`.

### Fixed
- **`video_hold_seconds` was a silent no-op — now wired as the per-scene end-of-scene hold.** The SKILL advertised it ("dwell this long instead of scroll-paced timing") and `build_scenes_from_spec` threaded it through, but nothing consumed it after the orchestrator refactor removed the scroll-pan path. It now replaces `final_hold_ms` for its scene only — one defined slot in the dwell hierarchy, documented as legacy (prefer `hold` actions, which say *where* the dwell belongs). Same archaeology pass: `min_hold_ms` only floors the *reported* footage total (it never padded film) and `scroll_speed_px_s` is unconsumed — both docstrings and the pacing docs now say so instead of implying film-time behavior.

## [0.2.191] - 2026-06-12

### Fixed
- **Full-page scene snapshots no longer stamp sticky headers mid-image.** Chromium's beyond-viewport capture paints `position: sticky`/`fixed` elements at the LIVE scroll offset, so any scene that ended scrolled down (audit drills, long tables) produced a still with the navbar floating mid-page and a bar-less, clipped document top — judges read it as a broken render (program-admin-report iter1: 8 of 14 captures affected; reported by the user from the deck). `take_snapshot` now scrolls to top before a `full_page` capture and restores the scroll after; the capture moment moved from before to AFTER `final_hold_ms`, so the scroll bounce lands at the scene cut where the crossfade masks it on film. Viewport captures (`full_page: false`) are untouched — the live viewport IS the artifact. Best-effort: if the scroll evaluation fails, capture proceeds uncorrected.

## [0.2.190] - 2026-06-12

### Added
- **Product-findings review gate (`review_mode: human`): judge findings become ONE review link with per-finding evidence deep-links — deck scene anchors AND video timestamps — no manual searching.** Users who want to pick which PRODUCT findings get implemented (instead of the autonomous mechanical auto-apply) used to get a hand-written chat table, and verifying any finding meant scrubbing the deck and the clip by hand. Formalized end-to-end: (1) **per-scene video timestamps in the recorder** — `Recorder.run_scene` stamps `{scene_index, title, start_seconds, duration_seconds}` onto the RunReport (`scenes[]` in `run-report.json`); `record_video.main` anchors the epoch at page creation (Playwright's webm zero, so pre-scene auth nav counts toward scene 1's offset) and `--skip-empty-scenes` scenes get no entry; new stdlib-only `scene_timestamps()` reader in `_lib/results.py`. (2) **New `scripts/ddd/findings_review.py`** — clusters PRODUCT findings (concept `design_findings.json` deduped by scene+dimension or an explicit `cluster` key, worst-of severity/fix_kind merge; plus user-artifact verdict dimensions as `user-<dim>` clusters), attaches per-cluster evidence links (`<deck>#scene-<N>` + `<clip>#t=<seconds>`), and posts ONE `gate: product_findings` review with implement/skip/defer decisions per cluster + one overall proceed/discuss decision; CLI `post <run_id>` (resolves the run dir from the CWD git toplevel, reads `design_findings.json` + `verdict-user.yaml` + run_state's `iteration_decks`/`iteration_clips` + `run-report.json` timings, stamps new RunState fields `findings_review_id`/`findings_review_url`, prints `internal_url`/`share_url` JSON), `apply <response_json>` (emits the machine-readable implement/skip/defer selection), and `mode <spec_path>`. (3) **`review_mode: human | autonomous` on `UnifiedSpec`** (default `autonomous`, documented) — in human mode NOTHING auto-applies, not even mechanical PRODUCT findings; the gate posts to the review surface per standing policy (never AskUserQuestion). The review embeds the iteration clip (`/w/<id>/content`) so evidence plays inline, and `ReviewRequest.findings[]` carries the machine-readable clusters. Docs: new `skills/ddd-findings-review/SKILL.md` (post → present URL + inline summary table → await → apply → route) + matching command, `ddd-run` SKILL Step 5b, `agents/ddd.md` pause policy + route-findings mode check. JSON schemas regenerated. Companion canopy-web PR teaches the `/w/<id>` viewer to seek to `#t=<seconds>` on load.

## [0.2.189] - 2026-06-11

### Added
- **Data-setup contract: specs declare the synthetic generator that puts the world in a recordable state, and reference its minted IDs as `${var}`.** DDD specs used to assume the world was already recordable, which broke both ways on synthetic-data demos: verified-monitoring hardcoded `run_id=3720` while its regenerate script minted a fresh run on every reseed (the spec silently went stale), and PAR's manager-flow scenes MUTATE state during recording (they create a real audit + task), so the iterate loop's re-renders filmed the wrong UI ("View Audit" instead of "Create Audit"). New optional `UnifiedSpec.setup` block (`SetupBlock`): `command` (run with cwd = the git toplevel containing the SPEC file — written repo-root-relative, exactly as a human runs it), `outputs` (repo-root-relative flat JSON of string/number variables), `rerun: per_render | once` (per_render — the default — reruns the generator before EVERY render, which is what state-mutating demos require; once skips when the outputs file exists), and `timeout_seconds` (default 1200). The recorder (`record_video.py`) runs setup before any browser opens, streams its output to the log, aborts loudly on nonzero exit/timeout, then resolves `${var}` placeholders in `Scene.url` and every action's `target`/`value` from the outputs — at render time, never mutating the spec file. An unresolved `${...}` is a HARD error before recording starts (listing the missing var + the available keys), and `${...}` with no setup block at all is equally hard (catches misconfiguration). New `--skip-setup` escape hatch skips the command but still loads outputs, for fast re-renders on known-fresh data (documented as forbidden for mutating demos). Provenance — resolved vars + command + exit code + duration — rides on the RunReport (`report.setup`) and lands as `setup-vars.json` in the `--snapshots` dir: the data a film was made on is part of the run's evidence chain. spec-qa gains rule (i): `${...}` placeholders present ⇒ `setup.outputs` must be declared (declared-but-unused outputs are fine). Placeholder syntax lives in one place (`scripts/narrative/substitution.py`, stdlib-only) shared by the recorder and spec-qa; JSON schemas regenerated; walkthrough / ddd-spec / ddd-spec-qa SKILL.md authoring sections updated.

## [0.2.186] - 2026-06-08

### Changed
- **DDD loop is progress-aware, not iteration-capped — fix the obvious things and keep going.** The orchestrator used a raw `MAX_ITERATIONS = 3` count to decide when to stop auto-iterating, so it handed half-finished runs back to the human after three passes even when every finding was a trivial mechanical fix and each pass scored strictly better than the last — the opposite of "loop and fix the obvious things." A raw count was also blind to *regressions* (a fix that breaks another scene should stop the loop immediately; a count happily burns more iterations on it). Replaced with trajectory-gating: `run_pipeline.compute_auto_iterate` appends the gating score to `RunState.score_history` each iteration and returns `continue` while findings are mechanical AND the score is still improving, `stop_max_iter` only on a **stall/regression** (no new best across the last 2 iterations) or a `HARD_CAP` (10) runaway backstop, plus the existing `stop_done` / `stop_concept_change` / `stop_unclear`. `ddd-run` SKILL Step 5 and the `ddd` agent doc ("Converge or loop", stop_max_iter, rules) updated to match; `MAX_ITERATIONS` kept as a back-compat alias of `HARD_CAP`. New `score_history` field on RunState (JSON schema regenerated).

## [0.2.185] - 2026-06-08

### Changed
- **DDD: stop agents hand-driving runs; force render/judge/upload through the skills.** A recurring failure mode — especially when the human breaks in to build the feature directly, then a fresh agent sees a live product + an existing run and reaches for the low-level tools — is hand-driving: calling `record_video.py`, dispatching `visual-judge` Agents, or `walkthrough-share/upload.py` à la carte instead of going through `/canopy:ddd-run` and `/canopy:ddd-upload`. Hand-driving never assembles the dual-judge verdict into `run_state.yaml` (so the run looks stale/done and can't be resumed), and produces loose `/w/` clips instead of a `/ddd/<slug>/<run_id>` package. Three guardrails: (1) **the recorder now refuses** to write into a `.canopy/ddd/runs/` directory unless `/canopy:ddd-run` passes the new `--ddd-orchestrated` flag (deliberate one-off override: `--force-hand-render`) — the only *quiet* render path is the orchestrated one; (2) `ddd-run` SKILL passes `--ddd-orchestrated` and documents why; (3) the **`ddd` agent doc** leads with a "Never hand-drive a run" section + a **re-entry detection** recipe (if `.canopy/ddd/runs/*/run_state.yaml` exists, resume via `/canopy:ddd --resume <run_id>` rather than reconstructing state by hand or writing a bespoke continuation prompt).

## [0.2.183] - 2026-06-07

### Changed
- **concept-eval `visual_polish`: judge the polished PRODUCT, fix the product not the camera.** Two standing rules added to the `visual_polish` dimension (rubric + ddd-concept-eval SKILL), closing a gap that surfaced on a live product walkthrough. (1) **Host-product chrome is grounding, never a flaw.** The platform frame a product lives in — top nav, side rail, branding, account menu — is what makes a demo read as a *real shipping product* rather than a detached mockup; its presence is required and must never be deducted or cap a score ("looks like a tool", "too much app chrome" are not findings). Only genuinely broken chrome (occluding content, z-index collision, placeholder nav) counts. This reinforces `artifact_kind: product_walkthrough` so judges stop re-litigating whether the product frame belongs in the demo. (2) **Findings drive the product, never the camera.** Every `visual_polish` fix_recommendation must be a PRODUCT change (a bigger hero, a focused/expanded view, progressive disclosure, fixing an occluding layout) — never a capture workaround ("zoom in", "crop", "set full_page:false"). If the load-bearing number is too small to read, the *product* should present it legibly; a capture trick that makes an over-dense product look good hides a real product-readiness gap. The one capture-side exception (a full-PAGE strip of a long scrolling page is an inaccurate capture — judge the real viewport, flag once, don't deduct) is called out explicitly so it isn't abused as a license to crop.

## [0.2.182] - 2026-06-07

### Fixed
- **BUILD SEQUENCE now labels built vs to-build.** The narrative-agreement review's BUILD SEQUENCE listed every scene as if it were new work, with no signal for what already exists — so an already-shipped feature's narrative read as an all-new build plan. `build_narrative_review_request` now derives a per-beat `status` (`built` | `new`) from the why-brief, mirroring canopy-web's `sceneIsFrontier`: a beat is `new` when its `provenance` spine item is a gap (status != `grounded`) or a why-brief gap references it, otherwise `built`. The field rides on each `NarrationItem` (`narration[].status`) so the `ddd-narrative-review` inline table and the canopy-web panel agree. (canopy-web renders the badge on the BUILD SEQUENCE items in a paired change; it computes the same frontier client-side, so the badge also lights up on already-posted reviews once deployed.)

## [0.2.181] - 2026-06-07

### Changed
- **doc-regeneration: prune as well as add, and drop the 200-line target.** Two fixes aimed at high-velocity codebases where docs decay fast. (1) **No more line-count target.** The old Check 4 told the skill to "keep CLAUDE.md under ~200 lines" — an arbitrary number that penalizes large multi-plugin systems (ace, canopy) with legitimately long reference docs, and that the skill didn't even honor. Replaced with a *compactness principle*: there is no line target; cut waste (stale status, duplication, dead detail), never cut accurate load-bearing reference; judge every line by "would the next agent be worse off without this?" Justified growth (e.g. newly-shipped endpoint coverage) is correct and must be reported. (2) **New Check 6 — stale-content pruning (retirement).** Regeneration is now an explicit two-way edit: alongside adding missing facts, it classifies every standing learning/plan/spec as LIVING / HISTORICAL-ACCURATE / SUPERSEDED / CONTRADICTED, flags cross-doc contradictions, and recommends retirement. Apply mode adds `Status: shipped … historical record` banners to historical docs (low-risk, reversible) but gates archive/delete of superseded docs behind user confirmation in the PR body (or an explicit `--prune`) — deleting a design doc is high-regret. Phase 1 now discovers whatever design-doc dirs actually exist (`docs/specs/`, `docs/superpowers/{plans,specs}/`, …) instead of assuming `docs/plans/`.

## [0.2.172] - 2026-06-04

### Changed
- **walkthrough recorder: smooth scene transitions + filmable native `<select>`s.** Two fixes to the DDD/walkthrough video recorder, both general (not feature-specific). (1) **Crossfade** — the browser paints a white flash during `page.goto` between scenes, which the continuous recording captured as a jarring blink. The recorder now screenshots the outgoing scene and lays it over the incoming page at max z-index, fading it out once the new page is visually ready (its `load` event, or a safety cap) — so scenes dissolve into each other instead of flashing white. (2) **Filmable dropdowns** — native OS `<select>` popups can't be screen-recorded, so a `select` action used to silently flip the closed widget's value (the viewer never saw the options or that a choice was made). The recorder now renders a synthetic styled dropdown over the select showing every option with the chosen one highlighted, glides the cursor onto it, holds (`select_reveal_dwell_ms`, default 1000ms), then commits and closes. Both behaviours are on by default and gated by `RecorderConfig.crossfade` / `.select_reveal`. `.first` on the select commit keeps a multi-row selector from throwing strict-mode.

## [0.2.171] - 2026-06-04

### Added
- **ddd: lock the narrative on approve — an approved narrative is durable input, not regenerable text.** `UnifiedSpec` gains `narrative_locked` (+ `narrative_locked_at`), stored in the spec file so it travels with the narrative artifact (the whole spec: narrative paragraph + every scene's narrative/show/design_intent/features/actions). The narrative-agreement gate sets the lock on `approve` and clears it on `redraft`; `ddd-spec` and the orchestrator skip regeneration when locked. So you approve the story once, then re-iterate render→judge→converge→upload as many times as you like without ever rewriting it — `redraft` is the single explicit door back to authoring. New CLI: `python -m scripts.ddd.narrative locked|lock|unlock <spec>`. (#137)

## [0.2.169] - 2026-06-04

### Changed
- **visual-judge / ddd-concept-eval: real-but-ugly production data is grounding, not a flaw — only fixtures cap.** v0.2.167's `artifact_kind: product_walkthrough` correctly stopped penalizing real product chrome, but its DATA clause still listed "a raw program slug" as an always-flaw and hardcoded a real program's real name (`ACE-IT-1777407074899-renamed`) as the textbook bad example — so a live product got capped to 2/fail for showing the genuine (if ugly) name of a real record. That conflated two different things. The methodology now distinguishes **fixture/placeholder data that signals an unfinished build** (`test-user`, `Untitled`, lorem, duplicate titles — still a flaw in any mode) from **real-but-ugly production data** (a real entity's real system-assigned identifier, even an auto-generated slug — what the live product actually shows, so under `product_walkthrough` it is grounding like the chrome). When a judge can't tell fixture from real, it now defaults to real and does not deduct. Removes the hardcoded slug example from both skills. Matches the connect-labs precedent (PR #380 → #383): if a judge dings real-but-ugly env data, calibrate the rubric — don't hack a display name for the judge.

## [0.2.167] - 2026-06-04

### Changed
- **visual-judge / ddd-concept-eval: `artifact_kind` makes real product chrome grounding, not a deduction.** A new `context.artifact_kind` field distinguishes `product_walkthrough` (a frame of a real, shipping web app driven through a flow) from `standalone_deliverable` (a slide/figure, the default). In walkthrough mode the surrounding product chrome — nav, sidebar, breadcrumbs, account menu, the app's own buttons — is EXPECTED and grounds the demo as a real product, so it no longer fires the "internal app chrome → max 3" sanity floor and is not listed as a flaw. Test/placeholder DATA (raw primary-key slugs, `test-user`, `Untitled`, lorem) still caps in either mode. `ddd-concept-eval` now passes `artifact_kind: product_walkthrough` for every DDD scene. Motivated by walkthrough judges wrongly penalizing the real-website nav that is the whole point of a live-product demo.

## [Unreleased]

### Changed
- **DDD `ddd-promote` → `ddd-upload`; convergence returns the run PACKAGE URL,
  not a loose artifact link.** The terminal DDD step was named "promote" but it
  has no promotion semantics — it just uploads a converged run's artifacts to
  canopy-web. Renamed the skill/command/script (`scripts.ddd.promote` →
  `scripts.ddd.upload`, `promote()` → `upload_run()`) and the run phase
  (`promoted` → `uploaded`, with `promoted` kept as a read alias for existing
  run_state files). More importantly, `upload_run()` now returns the navigable
  **run package** URL `/ddd/<feature>/<run_id>` — the canopy-web view (PR #77)
  that groups the run's video + deck + narrative + links — instead of the loose
  `/w/<artifact-id>` docs-page link it returned before. Fixes the symptom where
  every converged run handed back a single isolated artifact rather than the
  navigable package.

### Added
- **`/canopy:ddd` infers the narrative when none is passed.** Saying "do DDD
  with the orchestrator" (no explicit `<feature>`) no longer errors or prompts
  for setup — a new `scripts.ddd.resolve_narrative` resolver ranks narratives by
  the newest `.canopy/ddd/runs/*` run, the newest `docs/walkthroughs/*.yaml`
  spec, and a match against the current git branch, then resumes the in-flight
  run (or starts a fresh one) and just proceeds. It only pauses to ask when
  several narratives were touched at once (`confidence: ambiguous`) or there's
  nothing to infer from. Wired into the DDD agent's Bootstrap as step 4.
- **CI gate: docs-sync between engine source paths and SKILL.md** (#117) —
  Structural enforcement of the rule the #115 audit surfaced: engine PRs that
  change user-facing authoring surface MUST update the teaching SKILL.md in
  the same PR, or carry a deliberate opt-out marker. Of the 14 canopy PRs
  shipped on 2026-06-01, only #108, #111, #113, and #115 explicitly touched
  SKILL.md when they should have — #100, #101, #102, #105, #112, and #114 all
  shipped new spec-author surface (`Scene.url`, `must_succeed`, prefix syntax,
  snapshot flags, `scroll_to` cursor glide, `Scene.viewport`) without updating
  the docs, costing a meta-audit PR (#115) and a follow-up (#116) to backfill.
  The new gate (`.github/workflows/docs-sync.yml` runs the testable Python
  script at `.github/scripts/docs_sync_check.py`) maps four trigger source
  paths to their required teaching docs:
  - `scripts/ddd/schemas/models.py` → `ddd-spec/SKILL.md` + `walkthrough/SKILL.md`
  - `scripts/walkthrough/_lib/recorder.py` → `ddd-spec/SKILL.md` + `walkthrough/SKILL.md`
  - `scripts/walkthrough/record_video.py` → `ddd-run/SKILL.md`
  - `plugins/canopy/skills/ddd-concept-eval/rubric.yaml` → `ddd-concept-eval/SKILL.md`

  When a PR touches a trigger key but skips any required value, the gate
  fails with a structured message that names both the trigger and the
  missing docs, cites the prior gap PRs as rationale, and teaches the
  opt-out marker syntax. Opt-out: a line `Docs-not-needed: <reason>` in the
  PR body — for genuine engine-internal changes (refactor, perf, bug fix
  with no authoring contract change), the gate passes with a notice logging
  the reason. 24 unit tests cover the pure-logic path plus a sanity check
  that every trigger/required-doc path actually exists in the repo (renames
  would otherwise silently disable the gate). No behavior change for PRs
  that don't touch trigger paths — fast no-op silent pass.
- **Pre-commit hook: auto-regen DDD JSON schemas from `models.py`** (#118) —
  The Pydantic models in `scripts/ddd/schemas/models.py` are the source of
  truth; the committed schemas at `scripts/ddd/schemas/json/*.json` are
  downstream artifacts consumed by `scripts/ddd/validate.py` and external
  tools. They're supposed to stay in lockstep, but the gap between "edit
  models.py and commit" and "CI catches the drift" is wide enough to bite
  real work: `DrawAction` (added in #104) was missing from `UnifiedSpec.json`
  for two days until someone re-ran the regen by hand during #108. The new
  hook (`.pre-commit-config.yaml`, repo-local) fires only when `models.py`
  is staged, runs `dump_json_schemas`, and uses pre-commit's standard
  modify-then-fail pattern: if the regenerated JSON differs from what's
  committed, pre-commit blocks the commit with "files were modified by this
  hook" and the developer runs `git add scripts/ddd/schemas/json/` to fold
  the regen into the same commit. The existing
  `tests/ddd/test_validate.py::test_committed_json_schemas_match_generated`
  test still catches anyone who skipped `pre-commit install` or pushed via
  the GitHub web UI — belt and braces. No new Python dependencies; no
  behavior change for developers who haven't run `pre-commit install` (the
  config is just dormant).

### Changed
- **DDD authoring docs — close 4 secondary gaps from #115's audit** (0.2.156) —
  Follow-up to #115. The PR #115 audit pass closed six high-leverage authoring
  gaps but explicitly scoped out four smaller ones for follow-up. They're all
  real and worth closing now while the audit context is fresh. Docs-only across
  two SKILL.md files; no engine behavior changed.
  - `plugins/canopy/skills/ddd-spec/SKILL.md` + `plugins/canopy/skills/walkthrough/SKILL.md`:
    **`Action.draw` generalized beyond Mapbox** — `tool`-field coordinate-click
    pattern documented as applying to ANY tiny canvas-control surface
    (Mapbox GL Draw, Leaflet.draw, MapLibre, custom React-Konva / SVG drawing
    surfaces), not just Mapbox. The existing Mapbox-only example was misleading:
    authors using Leaflet or custom canvas tools wouldn't discover from the
    docs that the same `kind: draw` recipe applies to their surface.
  - `plugins/canopy/skills/ddd-spec/SKILL.md` + `plugins/canopy/skills/walkthrough/SKILL.md`:
    **`click_menu` verb explained** — the verb appeared in the action-verb list
    with zero example. Now documents the "second click in an open-menu → pick-item
    sequence" intent, the shorter `menu_click_settle_ms`, and the explicit "don't
    use it for the click that OPENS the menu" anti-pattern. Authors were either
    falling back to verbose two-`click` sequences or stumbling onto it from
    `_lib/recorder.py` source.
  - `plugins/canopy/skills/ddd-spec/SKILL.md` + `plugins/canopy/skills/walkthrough/SKILL.md`:
    **`Action.note:` documented as a persistent artifact, not a comment** —
    `note:` ships into the run report + recorder per-action log + judge
    context, but the docs never said so. Authors were using it as throwaway
    YAML-comment-style annotations ("click submit") that add noise without
    signal. Now explicitly: notes ship; write them for disambiguation,
    ordering rationale, or downstream-trigger context.
  - `plugins/canopy/skills/walkthrough/SKILL.md`: **`scene_index` preservation
    across `--scene` filter for hand-edited sidecar JSON** — the existing
    `--scene` docs covered the rendered deck but didn't say that when authors
    hand-edit `/tmp/walkthrough-run-data.json` to add or correct scores, the
    `scene_index` field MUST stay as the original spec index, not flatten to
    1-of-N within the filtered set. Flattening breaks all cross-run analytics
    that key on the original index.

- **DDD authoring docs roundup — lock in PR #100–#114 best practices** (0.2.155) —
  Six concrete authoring patterns shipped between #100 and #114 but never made it
  into the skill docs, so the next agent doing DDD on a fresh feature would
  reinvent traps already solved. Docs-only; no engine behavior changed; existing
  specs continue to validate and record identically.
  - `plugins/canopy/skills/ddd-spec/SKILL.md`: new **Authoring checklist** near
    the top (scannable one-liners with pointers to the deeper sections); inside
    Step 4 (scene-start authoring), four new in-place sections — **Target
    resolution syntax** (prefix matrix: `css:`/`testid:`/`aria:`/`role:`/`text:`
    with the routing call each maps to and "when to use" guidance), **`must_succeed:
    true` for critical actions** (the form-submit / navigation that, if it silently
    misses, makes every later scene grade against the wrong page state),
    **Don't `wait_for` on a transient intermediate state** (the `Creating 10 plan`
    race; wait only on TERMINAL states + cite the `microplans-10-wards` example),
    and **Per-scene viewport override** (the `viewport: {width, height}` field
    from #114 — including the mp4-frame-size constraint authors need to know about
    so they don't expect higher-resolution video from it).
  - `plugins/canopy/skills/walkthrough/SKILL.md`: mirror of the same four sections
    in the interactive-recording area, so the walkthrough author hits the same
    guidance without needing to read the DDD spec skill.
  - `plugins/canopy/skills/ddd-run/SKILL.md`: new **Recording CLI flag matrix**
    in Step 2 — the canonical default flag set the orchestrator should pass to
    `record_video.py` (`--snapshots` always, `--report` always, `--skip-empty-scenes`
    + `--skip-same-url` for typical specs, `--cookies` for session auth), with
    per-flag "when to pass it" rationale. Also a "scan `run-report.json` for
    failed `must_succeed` actions before letting the judges run" note tying back
    to the spec-side `must_succeed: true` docs.

### Added
- **Per-scene viewport override (`Scene.viewport`)** (0.2.154) — `video_viewport_width` /
  `video_viewport_height` work at the spec top level but you couldn't bump one
  dense scene without inflating the whole recording. DDD agent on
  `microplans-10-wards-fullrun-2026-06-02-001` wanted scene 4 (Mapbox plan-review
  with a wide inspector panel) at 1440×900 without bumping the other five scenes.
  Now `Scene.viewport: { width: int, height: int }` (optional) does exactly
  that: `Recorder.run_scene` calls `page.set_viewport_size` BEFORE the goto so
  the freshly-loaded page lays out at the requested size from the first paint,
  then restores the spec-level default after `final_hold_ms`. The mp4 frame
  size stays fixed (Playwright's `record_video_size` is set at context creation
  and cannot change mid-stream) — the recording canvas re-fits / letterboxes
  the larger logical viewport into the spec-level frame. No-op fast-path when
  the requested viewport matches the current. Authored example:
  ```yaml
  scenes:
    - title: "Dana drills into the plan map"
      url: "/microplans/program/133/plan/3536/review/"
      viewport: { width: 1440, height: 900 }  # this scene needs more room
      actions: ...
  ```

### Fixed
- **Scene-transition gray-viewport window after WebGL/Mapbox scenes** (0.2.154) —
  frame-sampling `microplans-10-wards-fullrun-2026-06-02-001/iter1_clip.mp4` from
  t=102s to t=108s showed ~7s of solid gray while the recorder navigated from a
  Mapbox-heavy plan-review page to the glossary page. Root cause: leaving a
  WebGL-heavy page, residual GL-teardown telemetry + tile-fetch network activity
  from the torn-down page stalls Playwright's lifecycle tracking, so the `load`
  event signal can hang for the full `load_settle_timeout_ms` (8s) while
  Chromium hasn't painted the new page's first frame yet — pure gray for the
  viewer. The recorder's per-scene goto already knew (via `skip_settle=True`)
  that the next action was `wait_for` and would do its own polling, so it
  already skipped the trailing `goto_settle_ms` blind hold. Now `goto_and_settle`
  also uses `wait_until="commit"` (return as soon as the navigation request is
  committed) and skips the `wait_for_load_state("load")` block entirely on that
  same path — the wait_for action that's about to fire IS the settle, much more
  accurate than guessing at `load` event timing. Back-compat: `skip_settle=False`
  preserves the original `domcontentloaded` + `load` + `goto_settle_ms` flow for
  every non-wait_for first action.
- **WebGL/Mapbox screenshot timeouts now settle + retry once** (0.2.154) — the
  recurring SwiftShader-headless bug (`reference_browse_webgl_swiftshader`):
  `Page.captureScreenshot` hangs on canvas-heavy pages in headless Chromium.
  The DDD agent on `microplans-10-wards-fullrun-2026-06-02-001` worked around it
  manually by re-capturing the failing scene in a separate `playwright.sync_api`
  session with an explicit 8-10s sleep before `page.screenshot()`. That's now
  built into `Recorder.take_snapshot`: first attempt uses `timeout=10000`; on
  timeout, `wait_for_timeout(8000)` to give the GL context time to finish
  whatever frame it was mid-render on, then one retry at `timeout=20000`. If
  the retry also fails, the recorder writes the text dump regardless (so
  visual-judge has at least one input per scene) and logs a one-line warning —
  never raises, never aborts the multi-scene run. One settle + one retry is
  the contract — no infinite retry loop.

### Changed
- **DDD concept-eval rubric grows a `visual_polish` dimension** (0.2.153) — the
  prior 5-dim rubric (`concept_clarity`, `design_soundness`, `why_groundedness`,
  `claim_reality_coherence`, `motion_friction`) had no place to land pure-visual
  failures. `design_soundness`'s anchors and deduction rules were about
  INTERACTION coherence — dead ends, contradictory affordances, unlabeled
  actions — so demonstrably ugly UI (misaligned elements, garish color,
  inconsistent button styles, bad typography, crowded layouts) shipped through
  the judge with passing scores because the rubric never asked.
  - New `visual_polish` dim (weight 0.15): Tough Judge anchors from 5
    ("shippable as marketing material") → 1 ("unfinished or amateur"), with 7
    deduction rules covering alignment, contrast, hierarchy, density, control
    consistency, overflow, and semantic color usage. Counts toward the
    weakest-link `overall_score` like every other gating dim.
  - `design_soundness` weight trimmed 0.25 → 0.20; label clarified to "Design
    Soundness (Interaction Coherence)" to make the scope split explicit.
  - `claim_reality_coherence` weight trimmed 0.15 → 0.10 (still advisory).
  - `motion_friction` weight trimmed 0.20 → 0.15. Weights still sum to 1.0.
  - Routing: `visual_polish` findings → PRODUCT by default (visual fixes are
    template/CSS changes); rare CONCEPT route when the rendered chrome reveals
    an information-architecture problem.
  - `tests/skills/test_ddd_concept_eval_structure.py` updated to expect 6 dims
    including `visual_polish`. Full canopy suite green (1567 passed).
- **Scene-transition dead-air: cursor follows `scroll_to`, no-nav scenes skip
  `initial_hold_ms`** (0.2.152) — frame-sampling `microplans-10-wards` v0.2.151
  (https://canopy-web-ujpz2cuyxq-uc.a.run.app/w/0212e21b-238c-422d-8c32-62289f487f4c)
  after PR #111 landed: scene 1 dropped from ~12s to ~5s, but viewers still saw
  ~5s of "static workspace, nothing happening" between the page load (t=2s) and
  the first visible motion (t=7s). Two structural causes PR #111 didn't address.
  Both fixes are additive — existing specs record identically, just faster.
  - **`scroll_to` glides the cursor onto the target.** Mirrors the
    resolve → glide → act shape every other primitive (`click_text`,
    `fill_field`, `select_option`, `hover`) already uses. A common pattern is
    `scroll_to` defensively before a click; when the element is already in
    view the page may not move at all but the cursor still visibly arrives
    on what's about to be clicked, so the viewer never sees a frozen
    `scroll_settle_ms` of nothing. After the smooth-scroll, the recorder
    re-measures the locator (the smooth-scroll moved it) and short-glides
    the cursor to follow it to its new viewport position — same re-measure
    pattern `click_text` uses to land on a settled element.
  - **`initial_hold_ms` skipped on no-nav scenes.** PR #111 skipped the hold
    when the first action was `wait_for` (the wait_for IS the settle). This
    extends the skip: when `goto_for_scene` returns `None` (stay-on-page
    scene, either via `SkipSameUrlRecorder` or a scene authored without
    `url:`), there's no page-load transition to settle for. The previous
    scene's `final_hold_ms` already provided any transition pause. So the
    full skip condition is now `url is not None and first_action != wait_for`.
  - 11 new tests under `tests/walkthrough/` —
    `test_scrollto_glides_cursor.py` (five cases covering pre-scroll glide
    centre + steps, move-before-evaluate ordering, post-scroll re-measure
    & re-glide with `cursor_steps_short`, detached-locator no-crash, and
    unresolved-target back-compat) and `test_initial_hold_skip_no_nav.py`
    (six cases pinning the new no-nav skip vs PR #111's existing wait_for
    skip, including the stacked condition, empty-actions edge case, and a
    "no nav means no goto at all" sanity check).

### Fixed
- **Scene-start dead-air: redundant goto + blind holds** (0.2.151) — frame-sampling
  the latest `microplans-10-wards` recording found ~7s of preventable stillness at
  the top of every scene. Three additive recorder fixes (every existing spec keeps
  recording — they just record faster):
  - **Redundant leading `goto` stripped in `build_scenes_from_spec`.** PR #100
    introduced `Scene.url` as the declarative entry point — the orchestrator's
    `run_scene` now navigates to `scene.url` before running actions. But existing
    specs still lead with `{kind: goto, target: <same-url>}` (the pre-#100 entry
    pattern), so the recorder navigated twice — a ~2.5s reload right after the
    title card. When the first action is `goto` and its absolutized target matches
    the resolved `scene.url`, it's now stripped from the action list with a printed
    notice. Non-leading gotos and gotos to a different URL (intentional
    reload-then-elsewhere) are preserved.
  - **`initial_hold_ms` skipped when first action is `wait_for`.** `wait_for` IS
    the settle (polls until a known page state appears) — the default 800-2500ms
    blind hold on top of it is pure dead air. The orchestrator now inspects the
    first action and skips `initial_hold_ms` when it's `wait_for`. Every other
    first-action kind keeps the existing behavior (back-compat for static-scene
    paths and click-first scenes that need the page a moment to render before the
    cursor moves).
  - **`goto_settle_ms` skipped when first action is `wait_for`.** Same logic for
    `goto_and_settle`'s 600-2000ms blind tail — when a `wait_for` is about to run,
    the recorder skips the trailing pause and lets the `wait_for` be the settle.
    Threaded via a `skip_settle=` kwarg; default False keeps any external caller
    bit-identical.
  - Frame breakdown of scene 1 of the original clip (12s):
    `1.25s` real labs page load (TLS + TTFB + DOM parse) — unavoidable.
    `1.2s` `goto_settle_ms`, `1.5s` `initial_hold_ms`, `2.5s` redundant goto
    reload, `2.0s` spec's `hold seconds: 2.0` over-framing — 7.2s preventable.
    `1.5s` scroll glide, `1.0s` hold, `1.0s` `final_hold_ms` — legit. The
    framework changes recover the four blind-hold seconds at the top of every
    scene; the per-spec `hold seconds: 2.0` over-framing is now redundant with
    the leading `wait_for` already settling the page.
  - 13 new tests under `tests/walkthrough/` —
    `test_redundant_goto_dropped.py` (six cases covering match-drops,
    different-url-preserves, no-url-inferred-from-goto, non-leading goto,
    absolute-matches-relative, action-empty no-crash) and
    `test_initial_hold_skip_on_waitfor.py` (sentinel-config approach: scene-aware
    timeouts assert which blind hold fired, covering `scroll_to`/`press`/`wait_for`
    leads, empty-actions back-compat, `final_hold_ms` independence, no-url path).

### Changed
- **`ddd-spec` and `walkthrough` skill docs teach `url:`, not `goto`** (0.2.151) —
  the framework fix above is necessary because the skills themselves taught the
  bad pattern: every action-authoring example lead with `kind: scroll_to`, never
  with `url:` or `wait_for`, and never called out the duplicate-goto trap. So
  every spec authored from these skills shipped the bug. Now both skills lead the
  scene-authoring example with `url:` + `wait_for`, add an explicit anti-pattern
  callout for `url:` + leading-`goto` to the same path, recommend `wait_for
  seconds: N` over `hold seconds: N` for long bulk operations, and document the
  continue-scene pattern (omit `url:` to stay on the previous scene's ending
  page). The SCHEMA is unchanged — old specs still validate; new specs are taught
  the cleaner pattern.

### Added
- **Companion links on the walkthrough viewer** (0.2.150) — an uploaded
  walkthrough can carry typed companion links the canopy-web `/w/<id>` viewer
  renders: `narrative` (back to the story that generated it), `companion`
  (the sibling still-frame deck ↔ video), and `reference` (the app pages the
  demo visited, clickable + live). `walkthrough-share/upload.py` gains
  `--narrative-url`, `--companion-url`, `--link "Label::url"` (repeatable),
  and `--spec` (derives one reference link per scene `url`, deduped).
  `/canopy:ddd-run` attaches them automatically when it uploads each
  iteration's clip — pulling the narrative-review URL the
  `/canopy:ddd-narrative-review` gate now stamps on
  `RunState.narrative_review_url`. Requires the companion canopy-web change
  (the `links` field on the Walkthrough model + viewer panels).
- **`wait_for` per-action `seconds:` timeout + `--skip-empty-scenes`** (0.2.148) —
  two opt-in recorder knobs that together remove ~130s of dead-space from a
  238s microplans-10-wards hero clip. Both ADDITIVE — every existing spec
  records identically without changes.
  - ``WaitForAction.seconds`` is now a per-action timeout override. The
    recorder's default ``wait_for`` timeout is
    ``RecorderConfig.wait_for_timeout_ms`` (12s). When an author knows a
    particular condition might take longer (an SSE bulk-create stream that
    runs 30-90s), the spec can say
    ``{kind: wait_for, target: "Created 10 of 10 plans", seconds: 120}`` —
    the recorder exits the wait the instant the text appears (typically
    30-55s for the bulk-create case), instead of holding blindly. Before:
    authors padded with ``wait_for`` (12s default) → fixed ``hold seconds:
    90`` guarantee, paying the worst-case dead-air on every clip. Frame-
    sampling the latest microplans-10-wards recording every 5s found a
    fully-painted success card frozen from ~55s to ~155s — exactly the
    `wait_for` + blind-hold pattern. Now spec authors say what they
    actually mean: "wait up to N seconds, exit early on match." The schema
    is strict: non-numeric ``seconds`` (e.g. ``"fast"``) fails Pydantic
    validation; negative values floor at 0; ``None`` (default) preserves
    the existing 12s recorder-config default.
  - ``record_video.py --skip-empty-scenes`` — when set, scenes with
    ``len(actions) == 0`` are dropped from the recording loop entirely.
    The mp4 then skips them; the deck still shows them as title-card
    slides built from spec.scenes independently, so the narrative
    survives. Mirrors PR #105's ``--snapshot-empty-scenes`` (also default
    False — back-compat). Same frame-sampling found a 30s static window
    from 200s-240s where scenes 6-11 of microplans-10-wards (no actions)
    held ``min_hold_ms`` on the previous scene's glossary URL — zero
    informational content for 30 of the 238 seconds. The new flag drops
    those frames; the deck-as-title-cards path keeps the narrative.
    Filter runs AFTER ``build_scenes_from_spec`` so surviving scenes
    retain their 1-based ORIGINAL spec ``scene_index`` (matches snapshot
    + ActionResult tagging). Extracted as a tiny pure helper
    (``filter_empty_scenes``) so the test suite pins the contract without
    spinning a browser.
  - 24 new tests under ``tests/walkthrough/`` —
    ``test_waitfor_seconds_override.py`` (Pydantic accepts/rejects, the
    primitive forwards to ``wait_for_target`` as int ms, the dispatcher
    routes ``action["seconds"]`` through, custom ``RecorderConfig`` default
    respected, fractional seconds coerce to int ms, negative floors to 0)
    and ``test_skip_empty_scenes.py`` (``_is_empty_scene`` for empty /
    missing / None ``actions``, ``filter_empty_scenes`` preserves order +
    ``scene_index``, microplans-10-wards back-half shape, composition with
    PR #105's per-scene snapshot gate, back-compat when flag is omitted).
    JSON schemas regenerated via ``dump_json_schemas`` —
    ``UnifiedSpec.json`` now includes ``WaitForAction.seconds`` (also
    backfills the previously-missing ``DrawAction`` defs).

- **Per-scene snapshots + `scene_index`-tagged action results** (0.2.146) —
  three small recorder-framework gaps the DDD orchestrator hit running
  ``microplans-10-wards`` are closed so the full DDD loop runs without
  side-script workarounds.
  - ``record_video.py --snapshots <dir>`` — captures one ``scene_<N>.png``
    (full-page) + one ``scene_<N>_page_text.json`` (``document.body.innerText``
    + url + title) per scene at the **steady-state moment** — between the
    action loop and ``final_hold_ms``, after every post-action settle has
    fired. That's the same surface ``canopy:walkthrough`` eval + ``ddd-
    concept-eval`` need to dual-judge a recording; the DDD agent used to
    write a side script (``/tmp/capture_scenes.py``) to fill the gap. New
    ``Recorder.take_snapshot`` is an overridable hook so subclasses can
    switch to viewport-only, write to S3, or grab extra artifacts (HAR,
    ARIA tree) without re-implementing the steady-state gating.
    ``--snapshot-empty-scenes`` toggles in narrative-only scenes (default:
    skip — there's nothing the cursor could change between init and final).
    Filenames use the **1-based ORIGINAL spec index** so a ``--scene 3``
    partial run produces ``scene_3.png``, not ``scene_1.png`` — matches the
    deck's scene numbering + the actionability-eval scene_index field.
  - ``ActionResult.scene_index`` is now populated (was always ``None``).
    The orchestrator's ``run_scene`` stamps the 1-based original spec index
    onto each result via ``dataclasses.replace`` (the dataclass is frozen).
    ``execute_action`` stays scene-agnostic — the dispatcher doesn't need
    to know which scene it's serving — while every result in ``RunReport``
    carries its source scene for downstream grouping. ``RunReport.to_json``
    serialises the field so external tools see it.
  - ``_render_stars`` in ``generate_presentation.py`` accepts float scores
    (the judge schema's ``ai_evaluation.score`` is a float — 4.5, 5.0). The
    star count rounds to the nearest int while the numeric tail
    (``4.5/5``) keeps the underlying float so half-star precision isn't
    lost. Defensive: ``max_score`` is coerced too in case a future
    contributor emits ``5.0``. The previous ``"★" * score`` threw
    ``TypeError`` whenever the judge produced a half-step score, blocking
    deck generation on real DDD runs.
  - 23 new tests under ``tests/walkthrough/`` —
    ``test_record_video_snapshots.py`` (PNG + JSON contract, original-spec-
    index naming, action-empty gating, lazy dir creation, fallback chain),
    ``test_action_result_scene_index.py`` (multi-scene tagging, partial-
    run index preservation, kwarg-over-dict override, JSON round-trip,
    direct-dispatcher default), ``test_render_stars_accepts_float.py``
    (4.5 + 5.0 don't raise, integer scores unchanged, float ``max_score``
    tolerated). Existing ``Recorder`` / ``RunReport`` tests unchanged.

### Changed
- **Recorder delegates target resolution + clicks to Playwright locators** (0.2.143) —
  the previous resolver shipped a hand-rolled ``_box_center`` JS that scanned the
  DOM ranking actionable-vs-text pools, a polling ``_glide_to`` deadline that
  re-implemented ``Locator.wait_for(state="visible")``, and a
  ``page.mouse.click(x, y)`` coordinate click that bypassed Playwright's
  actionability checks (visible, stable, receives events, enabled, not
  detached). Net effect: a less-reliable clone of a thing Playwright already
  provides correctly. Now the prefix syntax maps directly to ``get_by_*``:
  - ``css:#x`` → ``page.locator("#x")``
  - ``testid:foo`` → ``page.get_by_test_id("foo")``
  - ``aria:Foo`` → ``page.get_by_label("Foo", exact=False)``  (was a CSS
    ``[aria-label*=...]`` substring match that missed ``aria-labelledby`` and
    ``<label for>`` associations; now uses Playwright's accessible-name
    semantics)
  - ``role:button`` / ``role:button:Sign in`` → ``page.get_by_role(...)``
  - ``text:Foo`` → ``page.get_by_text("Foo")``
  Bare targets use a tightened heuristic — ``+ Bulk paste list`` used to read
  as CSS (leading ``+``) and threw on the invalid selector, silently breaking
  the whole microplans-10-wards scene-2 cascade. Now a leading combinator is
  text unless followed by an identifier-shaped char. The auto path also falls
  through from CSS-attempt → text-engine on miss, so a heuristic
  mis-classification is recoverable. ``click_text`` / ``fill_field`` /
  ``select_option`` / ``hover`` / ``scroll_to`` all call ``Locator.click``,
  ``Locator.fill`` + ``Locator.type``, ``Locator.select_option``,
  ``Locator.hover``, ``Locator.scroll_into_view_if_needed`` — full
  actionability checks intact, cursor still glides visually via ``slow_move``.
  E2E vs origin/main on microplans-10-wards: 245 s footage (was 240 s, +2 %
  for the genuine actionability waits — silent failures no longer hide the
  real time cost), 61/62 actions ok, same transient ``Creating 10 plan``
  timeout that's a UI race not a recorder bug. ``tests/walkthrough/
  test_targets.py`` rewrites the dispatch tests against a ``FakePage`` that
  records which Playwright API each prefix routes to.

### Added
- **Discriminated `Action` union — strict per-kind field validation** (0.2.142) —
  the flat `Action` Pydantic model accepted any of `target / value / seconds /
  note / must_succeed` for every verb, so a spec that wrote `{kind: type,
  target: "Buy"}` (the spec author meant `click`, not `type`) used to validate
  clean and silently no-op at recording time. Now `Action` is a Pydantic
  discriminated union with one strict subclass per verb — `GotoAction(target)`,
  `ClickAction(target)`, `FillAction(target, value)`, `SelectAction(target,
  value)`, `TypeAction(value)`, `PressAction(value="Enter")`, `HoverAction(
  target, seconds?)`, `ScrollToAction(target)`, `ScrollAction(value="bottom")`,
  `WaitForAction(target)`, `HoldAction(seconds)`, `ClickMenuAction(target)` —
  each with `extra="forbid"`. Wrong field on a verb is a loud `ValidationError`
  naming the field and the action subclass. `note` and `must_succeed` are
  shared on the base class so every kind keeps both. Surveyed 38 real specs
  across canopy / connect-labs / ace-web / canopy-web — every action shape
  already matches; zero migration required. Adds 20 new `tests/walkthrough/
  test_action_discriminated_union.py` tests pinning per-kind field accept/
  reject rules; existing `test_action_kinds_single_source.py` updated to
  guard the new `ACTION_CLASSES` tuple. JSON Schema regenerated.
- **Per-scene `url` field on the spec schema** (0.2.142) — `Scene.url:
  str | None = None` promotes a scene's starting URL from "inferred from the
  first `goto` action" to an explicit, declarative authoring affordance.
  `record_video.py` already reads this field (added in 0.2.141 ahead of the
  schema). Resolution order in the recorder is now (1) explicit `Scene.url`,
  (2) first `goto` action, (3) captured slide URL from `--input`, (4) `None`
  (stay on previous URL). Backward-compat: existing specs that don't set
  `url` keep validating and recording identically.
- **Recorder primitives refactor — extensible by hook, not by fork** (0.2.141) —
  the per-scene loop, target resolution, timing, and action results are now four
  small modules under `scripts/walkthrough/_lib/` instead of one 405-line file
  plus a monolithic `record_video.main()`. The motivating bug: the
  microplans-10-wards recording needed "skip nav when URL hasn't changed
  between scenes" (so scene 2's resolved table survives into scene 3) and the
  only way to get it was to hand-write a 121-line replacement script that
  re-implemented browser bootstrap, cookie loading, scene iteration, and ffmpeg
  conversion to add a single 4-line behaviour change. Now:
  - `_lib/targets.py` — one `resolve_target(page, target)` and one
    `looks_like_selector()` heuristic, used by every primitive. New explicit
    prefix syntax: `text:`, `css:`, `testid:`, `aria:`, `role:`. Bare targets
    keep working via heuristic (CSS-shaped → selector engine; English → text
    ranking). The previous "wait_for runs page.wait_for_selector on a
    plain-text target and stacks a 12 s hang before the text fallback" bug
    is gone for good (and is covered by a unit test).
  - `_lib/config.py` — every timing constant is a field on a frozen
    `RecorderConfig` dataclass with `fast`/`medium`/`slow` presets. Spec
    authors override any subset via `video_recorder_config: { typing_delay_ms: 20 }`.
    Unknown keys are ignored (forward-compat for newer recorders).
  - `_lib/results.py` — `execute_action` now returns an `ActionResult` with a
    tagged `error_kind` (`target_not_found` / `timeout` / `unknown_kind` /
    `playwright`). `RunReport` accumulates them and the orchestrator prints
    `"62 actions: 61 ok, 1 failed"` plus the failure list at the end of every
    run, so silent fails stop hiding (an action like
    `scroll_to("Plan config")` against a page that no longer has that text
    used to look identical to success in the video; now it shows up in
    `--report report.json` as `error_kind: target_not_found`). Per-action
    `must_succeed: true` raises `ActionAssertError` instead of swallowing.
  - `_lib/orchestrator.py` — `Recorder` class with five overridable hooks
    (`goto_for_scene`, `before_scene`, `after_scene`, `before_action`,
    `after_action`). The skip-nav case is now a 7-line subclass
    `SkipSameUrlRecorder` you opt into with `--skip-same-url`. Custom
    instrumentation (screenshot-on-fail, headed-debug, side-by-side) ships
    as another small subclass — no more orchestrator forks.
  - `record_video.py` — slim CLI over the Recorder. `--input` (the
    walkthrough-run-data.json from `canopy:walkthrough`) is now optional;
    when absent, the YAML spec is the only source of truth for scenes. New
    `--skip-same-url` and `--report <path>` flags.
  - Single `ACTION_KINDS` source in `scripts/ddd/schemas/models.py`. The
    recorder imports it; a guard test fails if the Pydantic `Literal` and the
    tuple ever drift.
  - 46 new unit tests under `tests/walkthrough/` covering target prefixes,
    the heuristic, RecorderConfig presets/overrides, RunReport summaries,
    and the dispatcher's verb table + `must_succeed` behaviour.
  - Backward-compatible: existing specs (no prefixes, no
    `video_recorder_config`, no `must_succeed`) record the same as before
    (verified end-to-end against `microplans-10-wards`: 10 plans created in
    Madobi LGA, 240 s of footage, identical visual pacing).
- **Recorder gains `select` action verb** (0.2.138) — for native `<select>` controls (which `click` cannot reliably open across platforms). `value` is the option's `value` attribute / 0-based-index-as-digit-string / visible label — recorder tries each in order. Adds `select_option()` helper in `scripts/walkthrough/_lib/recorder.py` that uses Playwright's `locator.select_option`. Action enum in `scripts/ddd/schemas/models.py:Action` extended; `scripts/ddd/schemas/json/UnifiedSpec.json` regenerated. SKILL docs for `ddd-spec` and `walkthrough` updated.
- **DDD auto-uploads per-iteration deck/clip to canopy-web** (0.2.135) — `/canopy:ddd-run` Step 2b (new, between render and judge dispatch) generates the iteration's HTML deck, uploads it via `/canopy:walkthrough-share`, and stamps the hosted URL onto `state.iteration_decks[<iteration>]`. Same for the optional MP4 clip → `state.iteration_clips[<iteration>]`. Deck generator now emits `id="scene-<N>"` anchors on every scene slide (original spec index, stable across partial/full runs), so consumers deep-link with `<DECK_URL>#scene-<N>`. Step 5 report banner prints the hosted URLs inline and decorates each finding with its scene deep-link. The `agents/ddd.md` artifact-link contract now reads URLs from run_state directly — surfaced findings never do a one-off manual upload. On upload failure: log to `<run_dir>/upload-errors.md`, fall through to verbal description, never `file://`. Closes the gap where every surfaced "do you want me to apply this fix?" required a manual upload to be answerable from anywhere.
- **DDD surfaced decisions must include ace-web hosted artifact links** (0.2.134) — `agents/ddd.md` pause-policy section + every stop_* branch (`stop_concept_change`, `stop_unclear`, `stop_max_iter`) now require that surfaced `ReviewRequest` items include hosted ace-web screenshot URLs (NOT local `file://` paths), with an `element_locator` naming what the finding is about. Local paths fail the moment the user reads the message on another device; hosted URLs work everywhere. If upload fails, the agent says so explicitly and falls back to a verbal description, never a local path.
- **Auto-iterate gate based on per-finding `fix_kind`** (0.2.132) — judges
  now emit `fix_kind ∈ {mechanical, options, redesign}` per finding so the
  orchestrator can decide auto-apply vs ask WITHOUT re-parsing prose. The
  `/canopy:ddd-run` Step 5 report computes `auto_iterate_next_action ∈
  {continue, stop_done, stop_partial, stop_concept_change, stop_unclear,
  stop_max_iter}` and stamps it onto `run_state.yaml`. The `/canopy:ddd`
  agent's Converge-or-loop branches on it: mechanical findings auto-apply
  per route (PRODUCT → labs PR+merge+deploy, CONCEPT → spec edit,
  RESEARCH → why_brief edit, DEFER → log only), then re-fire the same
  scope. The loop stops only when `options`/`redesign` findings appear or
  max-iter is reached. Removes the previous behavior where ddd-run's
  "warn but no PRODUCT/CONCEPT-routed concrete fix" still stopped the
  agent and asked the user. Per the labs autonomy mandate, mechanical
  PRODUCT fixes auto-deploy from main without prompting.
- **Deck generator renders scene_total + partial-scope banner** (0.2.129) — `generate_presentation.py` now reads `scene_total` from the run-data sidecar and labels each scene slide "Scene N of M" (the original spec index/total), not "Slide 1/1". When the run-data carries `scenes_run` + `scene_filter` (from a `--scene` render), the title slide gets a "Partial run" banner naming the selector, the rendered scene indices, and that promotion requires a full-spec run. Backward-compatible: legacy run-data without those fields renders unchanged.
- **RunState gains `scenes_run` + `scene_filter` fields** (0.2.128) —
  makes the 0.2.127 SKILL.md contract real on the underlying pydantic
  model. Without it, `runstate.save()` rejected partial-run metadata.
- **`--scene <selector>` filter for walkthrough + ddd-run** (0.2.127) —
  run the canonical rubric on a subset of scenes when iteration only
  touched one scene's feature. Selector forms: `2` (index), `2,4,5`
  (list), `2-4` (range), `name-match` (title/spine substring).
  Preserves original scene indices in the output JSON and deck so a
  scene-2 score from a partial run is directly comparable to a scene-2
  score from a full run. Sidecar gains `scenes_run` + `scene_filter`
  fields. `run_state.yaml` carries the same. `/canopy:ddd-promote`
  hard-refuses partial runs at Step 0 — promotion requires full-spec
  coverage. Lets users stay on the DDD path instead of falling back to
  ad-hoc screenshots when iterating on a single scene's feature.
- **`patch-gstack-browse` skill** (0.2.124) — re-applies the SwiftShader WebGL
  patch to gstack `browse` so headless Chromium can render WebGL pages (Mapbox
  GL, three.js, deck.gl) for screenshot QA. Idempotent: patches
  `browser-manager.ts` with `--enable-unsafe-swiftshader`, rebuilds + re-signs
  the binary, restarts the daemon, and verifies a WebGL2 context. Re-run after a
  gstack update overwrites the source. Ships a `/canopy:patch-gstack-browse`
  command wrapper.
- **PAT-mint test harness** (0.2.121) — ported ace's PAT-mint test to canopy and
  added minimal vitest infrastructure to cover the canopy-web Personal Access
  Token loopback flow.
- **`canopy doctor` CLI** (0.2.120) — read-only plugin-health diagnostic (hook
  registration, session log, repo map, workbench token, plugin version) that exits
  non-zero so it can gate CI.
- **`canopy structure-drift` CLI** — one-pass self-audit of canopy's documented
  structural invariants: command/skill collisions following Pattern B,
  reserved-built-in-name collisions, version agreement across
  VERSION/plugin.json/marketplace.json, and the per-skill description budget.
  `--strict` makes it a CI gate.
- **`canopy:alignment` skill** (0.2.108) — a read-only two-project drift sweep that
  compares sibling systems and posts ranked, reasoned `[alignment]` findings to the
  canopy-web `/insights` feed. Added a matching canopy-web `[alignment]` category
  and divergence path.
- **`canopy:find-session` skill** — targeted lookup of your other active Claude Code
  session on a repo, for picking up context from a parallel worktree.
- **DDD (demo-driven-development) pipeline** — a large multi-PR arc building the
  demo-authoring loop: narrative-first spec authoring with first-class personas;
  a cohesive review surface; verifiable feature chunks with an actionability eval;
  build-order sequencing independent of video order; review edits (add/delete
  scenes, edit features, collect feedback); a narrative-coherence check that catches
  outcome leakage; and `ddd-promote`, which finishes the user-facing docs layer
  (capabilities, why-summary, poster, getting-started, play affordance).

### Changed
- DDD gates always use the canopy-web review surface; the narrative review now
  shows literal narrative sentences 1:1 with scenes.
- Default workflow is documented as always-PR with auto-merge (no maintainer
  review), and the plugin-update policy is documented as never hand-patching the
  installed plugin cache.

### Fixed
- `alignment` / `portfolio-review`: the canopy-web clear endpoint is `POST`, not
  `DELETE`.
- `ddd-promote`: fixed a urllib `UnboundLocalError` and tightened the `ddd-spec`
  persona rule (personas are people only).
- `ddd-actionability-eval`: sandbox the cold-derivation agents from the source repo
  so derivations stay independent.
