# Canopy

Autonomous self-improving system and Claude Code plugin that watches Claude
Code sessions across all projects, identifies friction and gaps, and builds
improvements — to MCP servers, skills, hooks, workflows, CLAUDE.md docs, or
the orchestrator itself.

Converges with gstack and superpowers: invokes their skills headlessly via the
skill runner for review, QA, and implementation quality.

## Git Worktree Rules
This repo uses emdash which manages git worktrees. If you are in a worktree
(check: `git rev-parse --git-dir` contains `/worktrees/`), then `main` is
checked out in the main repo at `~/emdash-projects/canopy/`.
You CANNOT `git checkout main` from a worktree — it will fail.

To merge to main:
```bash
cd ~/emdash-projects/canopy && git merge <branch-name> && git push
```

If that fails with local changes, stash first:
```bash
cd ~/emdash-projects/canopy && git stash && git merge <branch-name> && git push
```

If remote is ahead, pull first:
```bash
cd ~/emdash-projects/canopy && git pull --rebase && git push
```

## Shipping Changes — always PR, then auto-merge (the maintainer does NOT review)

**Default workflow for every change: open a PR and merge it yourself.** The
maintainer does not review canopy PRs — opening one and waiting for review just
strands the work. So the PR is a record-keeping + CI step, not a review gate.

```bash
# from the worktree branch, once work is committed:
git push -u origin <branch>
gh pr create --title "..." --body "...\n\nCloses #<issue>"   # link the issue if any
gh pr checks <n>                    # wait for check-version to pass (~10s)
gh pr merge <n> --merge             # then merge; a red check-version rejects the merge
```

Then follow the plugin-update steps below (`/canopy:update` etc.) if
`plugins/canopy/` changed.

- **Always PR** (don't `git merge` straight to main) so CI runs and there's a
  durable record. The direct-merge commands above are the fallback for when
  `gh` is unavailable or a merge conflict needs hand-resolving in the main
  checkout — not the default.
- **`main` is branch-protected — the merge IS gated on CI.** canopy is a
  **public** repo with branch protection: `check-version` is a **required status
  check** and `enforce_admins` is on. `gh pr merge --merge` on a PR whose
  `check-version` hasn't gone green is rejected ("base branch policy prohibits
  the merge"). Repo-level auto-merge is **disabled** (`gh pr merge --auto` errors
  with "Auto merge is not allowed for this repository"), so the flow is: wait for
  `check-version` (it runs in ~10s — `gh pr checks <n>`), then `gh pr merge
  --merge`. Don't reach for `--admin` to force past a red check — a red
  `check-version` means the version isn't bumped right; fix that instead (see the
  STOP block below).
- **No human reviews, but CI is a real gate now.** Required reviewers are NOT
  configured, so you still merge your own PR — but `check-version` will block a
  bad bump, unlike the old private-repo era where it was advisory. Before
  merging: the suite passes (or only known-unrelated failures remain) and — for
  `plugins/canopy/` changes — `canopy version verify` is green.
- Keep PRs scoped and the body honest about what was and wasn't verified.

## Tech Stack
- Python 3.11+, PyYAML, Click
- Claude Code hooks and skills
- Subprocess invocation of `claude -p` for analysis and proposals; proposal
  *implementation* is dispatched to Claude Code agents via `/canopy:improve` (the
  Python pipeline stops after proposals)

## Commands
- `canopy sessions status` — show session log entry count and classification summary
- `canopy sessions list [--hours N] [--json-output]` — list recent sessions
- `canopy create-agent <slug> --mandate "..." [--name --mailbox --stakeholders --into --force]` — scaffold a new Claude Code agent (its own git repo) from the operating model: persona, `turn` orchestrator, reads-free/writes-gated gating hook, canopy-web-ready layout. See `docs/agent-operating-model.md`.
- `canopy agent-publish {register|skills|sync|work} [--repo DIR ...]` — publish an agent repo to its canopy-web workspace (`/agents/<slug>`): register, mirror the skill catalog, post a sync, or push work products. Run from an agent repo root; identity resolved from its `.claude-plugin/plugin.json` + `config/agent.json`. The shared generalization of echo's `bin/echo_canopy.py`.
- `canopy agent-review <slug-or-path> [--hours N --no-llm --model --json-output]` — Build 2 of the operating model: review an agent's recent TURNS for friction (tool failures, retries, gating blocks, auth friction, checklist gaps) and synthesize ranked findings + fixes scoped to the agent repo. Deterministic signals always; `claude -p` synthesis unless `--no-llm`.
- `canopy issue {create|context|delete} ...` — architect-routed GitHub issues with `canopy.origin/v1` provenance. **`create`** (reads a JSON record on stdin) files a CLEAN, portable GitHub issue (mandate + done-criteria + a pointer — NO local paths/yaml) and stores the rich *understanding* (provenance, intent, evidence, POINTERS to the drilled sessions) as a record locally + synced to canopy-web (`/api/issues`, django-ninja, Bearer-auth). **`context <owner/repo#n>`** hydrates that understanding (local, else canopy-web) + emits the `canopy harvest strip` commands to recover the local transcripts (flagging which exist on this machine — sessions stay local, never in the web record). **`delete <owner/repo#n> [--close-gh]`** removes the canopy-web + local record for cleanup. Backed by `src/orchestrator/issue_origin.py` + canopy-web `apps/issues/`.
- `canopy harvest {map|strip|intent-audit|corpus} ...` — cross-user session reading for Hal-as-architect (reads every readable `/Users/*/.claude/projects`; flags `confidence: half-blind`). **`map <initiative> --match terms`** = a tiny per-session digest of EVERY matched session (your inputs + final output, tool noise stripped) — the whole arc in one cheap read (~54K tokens for DDD's 236 sessions); then **`strip <session-path> [--mode final|full]`** drills into an interesting session (full stripped conversation). **`intent-audit <session-path>`** = the one judgment lens harvest otherwise omits: reconstruct what the human asked/decided from their OWN words and flag intent-misses (approved-X/shipped-Y, question-read-as-approval, unapproved-judgment) as evidence-backed findings (PR #374). `corpus` is the older ends-only sampler (superseded by map). Deterministic material otherwise; intent/drift judgment is the agent's job. See memory `harvester-architect`.
- `canopy email {send|read|fetch-attachment|mark-read|apply-filters|review-receipt|preflight} [--repo DIR | --agent SLUG | --account/--client]` — the fleet's shared guarded email engine (docs/architecture/shared-gog-gdrive.md §3): **`send`** = the echo/ACE HTML multipart wrapper (Gmail display-wraps plain text at ~72 cols; HTML reflows) with body-file contract, `--reply-to-message-id`, `--dry-run`, reply-all-by-default on threads, and a JSON result whose `thread_id` every caller must record into the agent's state layer; **`read`** = inbound thread read as normalized JSON (decoded `body_text` + `reply_all` recipients, PRs #391/#394) and **`fetch-attachment`** = download ONE attachment; **`mark-read`** = Gmail API UNREAD removal via the agent's own gog OAuth (gog has no mark-read; API reads don't clear the flag); **`apply-filters`** = push the fleet-wide inbox junk filters (`inbox_filters.py`) to an agent mailbox; **`review-receipt`** = record that agent-turn-review ran against THIS body — send refuses without a matching receipt (the rail is keyed to the body, not the agent's memory); **`preflight`** = gog auth liveness with the exact `gog login` remediation. Identity (mailbox + gog client) resolves from the agent repo's `config/agent.json` (`email`, `gog_client`); agents keep only a thin `bin/<slug>-email` shim (factory-templated) plus a deny rail blocking raw `gog gmail send/reply`. Backed by `src/orchestrator/agent_email.py`.
- `canopy gdoc publish [--repo DIR | --agent SLUG] ...` — shared Google-Doc authoring engine for the fleet (shared-gog-gdrive.md §5 interim): publish markdown as a rendered Google Doc authored as the agent; `--replace` edits native Docs **in place** via the Docs API (stable URL — no trash+recreate churn, #353). Backed by `src/orchestrator/agent_gdoc.py`.
- `canopy provision [--repo DIR --check --json-output]` — materialize an agent/provider repo's secrets from 1Password per its `config/secrets.yaml` (declarative refs + targets, no values) — two block types: `secrets:` (one file per secret) and `env:` (many `KEY=value`, op refs + non-secret literals, into ONE `.env` at a **worktree-clean global home** like `~/.<slug>/.env`, NOT a per-worktree repo `.env`). Portable across machines/operators/worktrees; idempotent; `--check` dry-runs. The agent factory stamps this by default (+ a `bin/_env.py` that reads the global home), so new agents never recreate `.env` per worktree. Backed by `src/orchestrator/provision.py`. See `docs/agent-operating-model.md` §4e.
- `canopy openclaw-harvest {snapshot|inventory|compare|bootstrap|reconcile}` — bridge a live OpenClaw into the fleet: snapshot its readable workspace (persona/skills/memory, NEVER creds), compare to the agent's GitHub repo, then bootstrap a new agent repo from it or reconcile its novel skills into the existing one. Engine is offline/testable; only `snapshot` needs ssh.
- `canopy improve` — run a full improvement cycle (analyze → propose → implement)
- `canopy improve --observe-only` — analyze transcripts without proposing
- `canopy improve --dry-run` — analyze and propose without implementing
- `canopy serve` — start transcript browser web UI on localhost:8484
- `canopy analyze <transcript.jsonl> [--propose]` — analyze a specific transcript
- `canopy brief [--model MODEL]` — generate strategic brief
- `canopy patterns [--json-output]` — show cross-session friction patterns
- `canopy observations list [--type T --status S --severity X --limit N --json-output]` — list observations
- `canopy observations show <id>` — show full YAML for one observation (id prefix accepted)
- `canopy proposals list [--status S --complexity X --limit N --json-output]` — list proposals
- `canopy proposals show <id>` — show full YAML for one proposal (id prefix accepted)
- `canopy skills list [--scope all|plugin|user --source PLUGIN --search TERM --json-output]` — list installed skills (JSON output includes `installed_version` + `cache_path` per entry)
- `canopy skills find <query> [--limit N --json-output]` — fuzzy-match installed skills by name and description; prints top matches with SKILL.md path. Use this before brainstorming a new skill ("do we already have one for X?").
- `canopy skills overlap <action text>` — check whether a proposed skill action duplicates an existing skill (exit 1 on overlap)
- `canopy skills budget [--scope ... --source ... --per-skill-limit N --aggregate-limit N --top N --json-output]` — show description-size budget (per-skill ranked table + aggregate gauge). Use when Claude Code prints "N skills dropped" and you need to know which skills are pushing the system prompt over the cap.
- `canopy skills dropped [--scope ... --source ... --per-skill-limit N --aggregate-limit N --json-output]` — simulate Claude Code's drop logic and print which skills get dropped under the aggregate cap.
- `canopy version verify` — confirm VERSION and plugin.json agree (CI-safe)
- `canopy version verify-bump` — fail if `plugins/canopy/` changed without a VERSION bump past origin/main (the check CI and the push guard run)
- `canopy version bump` — bump VERSION + plugin.json by `max(local, origin/main) + patch+1`. Fetches origin first so a parallel worktree's bump is visible before deciding the next number. Use this instead of editing the two files by hand.
- `canopy doctor` — diagnose canopy plugin health (workbench token, repo-map, session log, hook registration)
- `canopy agent doctor [--repo DIR | --slug SLUG]` — diagnose ONE agent's readiness on THIS machine (identity, gating rails, secrets manifest, live gog email auth, canopy-web registration + board); exits non-zero on failure. `--all` sweeps the fleet; `--fix` remediates what it safely can.
- `canopy agent health [--repo DIR | --slug SLUG]` — work-state readiness for the agent's NEXT turn (open board items + unread inbox), distinct from `doctor` (machine/install readiness). Backed by `src/orchestrator/agent_health.py`.
- `canopy agent turn [--title ... --summary ... --task-ext-id ID --work-product-url URL --upload-transcript]` — package one turn of work as a unit (optionally reducing + uploading its transcript to a `/share/<token>` link hung off the turn). Part of the `canopy agent …` operator plane. Backed by `src/orchestrator/session_upload.py`.
- `canopy fleet-align` — CLI entry for the cross-agent improvement spread (the `fleet-align` skill's analysis half). Backed by `src/orchestrator/fleet_align.py`.
- `canopy eval {score|record} ...` — the eval runner: `score` weighted-grades a rubric, `record` writes the score → verdict path so any agent self-grades against the run lifecycle. Backed by `src/orchestrator/eval_cli.py` + `eval_rubric.py`.
- `canopy shareout [...]` — gather a date range of sessions + PRs into a teammate-facing work briefing for the canopy-web /shareouts feed
- `canopy portfolio-discover` — list local emdash repos with recent activity that canopy can act on
- `canopy structure-drift` — self-audit canopy's documented structure against the actual tree (agent frontmatter, modules, docs)
- `canopy test-audit [...]` — build a test corpus for the agent to judge and prune dumb tests
- `canopy verify-findings` — re-verify session-review proposals against the current state of their target repos

## Framework/Product Boundary (the one invariant)

The `orchestrator` package splits into **framework** (generic, agent-agnostic
agent-runtime substrate — `canopy_web`, `agent_client`, `agent_factory`, `capture`,
`transcripts`, `scanner`, `scheduler`, `provision`, `agent_email`, `fleet_align`, …) and
**product** (canopy's own self-improvement / DDD / portfolio features — `analyzer`,
`proposer`, `observations`, `harvest`, `shareout`, `test_audit`, …). **Framework
code must never import product code; product imports framework freely.** The only
product→framework coupling lives in 3 orchestration **hubs** (`cli`, `pipeline`,
`server`), by design. This keeps the framework substrate cuttable (liftable onto a
standalone host without dragging canopy's product). It's a *direction, not a wall* —
do NOT split `orchestrator/` into `framework/`/`product/` subpackages. Enforced by
`tests/test_plugin_boundary.py` (fails on a framework→product import, or a new
untiered module). Full tier list + rationale: **`src/orchestrator/TIERS.md`**.

## Session-analysis surfaces — which one for which grain

`analyze` / `improve` / `agent-review` / `session-review` / `select-session` are
overlapping peers. There is no single "front door" command (a unified entry with
sub-modes is a deferred design call — see #352); until then, this table is the
decision guide. Reach for the row that matches your **grain**:

| Grain | Use | What it is |
|-------|-----|------------|
| One specific transcript | `canopy analyze <transcript.jsonl> [--propose]` | Analyze a single session file. Runs from any cwd — no registry/checkout required (the capability-registry dependency was removed). |
| Broad batch across sessions | `canopy improve` (or `--dry-run` / `--observe-only`) | The full analyze → propose → implement cycle over discovered transcripts. |
| Menu-driven pick + act | `/canopy:select-session` / `/canopy:session-review` | Interactive: choose a project/session, analyze, propose, implement behind gates. |
| One AGENT's own turns | `canopy agent-review <slug>` / `/canopy:agent-review` | Reviews an agent's recent TURN transcripts for operating-model friction. Assembles its prompt INLINE (see note below). |

**Prompt-loading convention (post-#352) — one rule per tier:**
- **PRODUCT, user-editable templates** (`analyze` / `propose` / `review` /
  `briefing` / `verify-findings`) → external `prompts/*.md`, loaded install-safely
  via `importlib.resources` (`orchestrator/prompts/__init__.py::load_prompt`) —
  robust even when the wheel is relocated/zipped, belt-and-suspenders with #351's
  `package-data`.
- **FRAMEWORK logic-prompts** (`agent_review.build_review_prompt`,
  `fleet_align.build_judgment_prompt`) → **inline** — static, co-located with the
  logic they serve, and immune to the #351 packaging class (a Python string literal
  always ships). Loading the PRODUCT `prompts/` package from a FRAMEWORK module
  would also break the framework→product boundary (`tests/test_plugin_boundary.py`).

#352 read the inline/external split as an accidental inconsistency to collapse. A
canopy + fleet scan showed otherwise: the framework-inline set is a real class
(`agent_review` **and** `fleet_align`; the 5 agents have no python-level prompts at
all — theirs live in markdown skills), and a framework template store would serve
only those two packaging-immune prompts. So the split is kept as a deliberate
two-tier rule — the loader is hardened (`importlib.resources`), not the conventions
merged. A future framework judge-prompt follows the framework rule: inline it.

## Key Modules

### Core pipeline
- `src/orchestrator/pipeline.py` — full improvement cycle (scanner discovery, circuit breaker, rate limiter)
- `src/orchestrator/analyzer.py` — transcript analysis via claude -p
- `src/orchestrator/proposer.py` — proposal generation via claude -p
- `src/orchestrator/skill_runner.py` — headless invocation of any Claude Code skill
- `src/orchestrator/paths.py` — shared CANOPY_DIR constant and legacy migration
- _No implementer module._ Proposal implementation is dispatched to Claude Code
  agents via the `/canopy:improve` skill; `pipeline.py` stops after saving
  proposals. (The old `claude -p` implementer was removed in `a6991a6` —
  "replace claude -p implementation pipeline with agent-based dispatch".)

### Data models
- `src/orchestrator/observations.py` — friction, gaps, patterns extracted from sessions
- `src/orchestrator/proposals.py` — improvement proposals with verification plans
- `src/orchestrator/campaigns.py` — multi-day improvement arcs
- `src/orchestrator/tracker.py` — proposal outcome tracking for self-improvement

### Intelligence
- `src/orchestrator/patterns.py` — cross-session pattern detection
- `src/orchestrator/briefing.py` — strategic brief with gstack cognitive patterns
- `src/orchestrator/router.py` — tiered routing (inline/single/team)
- `src/orchestrator/skill_catalog.py` — enumerates installed skills (plugin + user) and detects when a `new_skill` proposal duplicates one that already exists. Wired into the proposer prompt and `_validate_proposals` to silence the "we just proposed building something that already ships" pattern.
- `src/orchestrator/skill_budget.py` — computes per-skill + aggregate description-size budget (backs `canopy skills budget` / `dropped`)
- `src/orchestrator/shareout.py` — gathers sessions + PRs over a date range into teammate-facing work briefings (backs `canopy shareout`)

### Discovery
- `src/orchestrator/scanner.py` — transcript discovery and metadata extraction
- `src/orchestrator/transcripts.py` — Claude Code transcript parsing
- `src/orchestrator/repo_map.py` — project-to-GitHub-repo mapping (JSON, stdlib only)
- `src/orchestrator/repo_paths.py` — resolves local repo checkout paths for a project
- `src/orchestrator/portfolio_discover.py` — discovers local emdash repos with recent activity (backs `canopy portfolio-discover`)

### Capture & hooks
- `hooks/post_tool_use.py` — captures repo mapping on every tool call, logs MCP calls
- `src/orchestrator/capture.py` — session log writer

### Browser UI (visibility tool, not primary interface)
- `src/orchestrator/server.py` — HTTP server with JSON API
- `src/orchestrator/static/index.html` — SPA frontend
- `src/orchestrator/labels.py` — transcript label storage
- `src/orchestrator/reviewer.py` — AI strategic review via claude -p

### Scheduling
- `src/orchestrator/scheduler.py` — launchd plist generation
- `src/orchestrator/circuit_breaker.py` — stops pipeline after consecutive failures
- `src/orchestrator/rate_limiter.py` — caps API calls per hour

### CLI & utilities
- `src/orchestrator/cli.py` — Click CLI entry point
- `src/orchestrator/corpus.py` — activity corpus builder for analysis
- `src/orchestrator/digest.py` — daily digest generation from improvement runs
- `src/orchestrator/run_log.py` — improvement cycle run log tracking
- `src/orchestrator/doctor.py` — canopy plugin health diagnostics (backs `canopy doctor`)
- `src/orchestrator/structure_drift.py` — self-audit of documented structure vs the actual tree (backs `canopy structure-drift`)
- `src/orchestrator/verify_findings.py` — re-verifies session-review proposals against current repo state (backs `canopy verify-findings`)
- `src/orchestrator/version_bump.py` — VERSION coordination across worktrees (backs `canopy version bump`)
- `src/orchestrator/agent_factory.py` — agent factory: stamps out a new agent repo from the operating-model templates (persona, turn, the config-driven gating hook). Backs `canopy create-agent`. Templates are embedded as the editable starting point every agent inherits. See `docs/agent-operating-model.md` (§4 Build 1, §4a topology).
- `src/orchestrator/canopy_web.py` — the single source of canopy-web transport + auth: `resolve_base_url`/`resolve_token` (PAT precedence: arg → `CANOPY_WEB_PAT` → `~/.claude/canopy/workbench-token`), `CanopyError`, an injectable `Transport`, and the low-level `call()`. stdlib `urllib` only (no `requests`). Everything that talks to canopy-web resolves auth through here (agent_client, agent_web, scripts/ddd/auth, shareout, the share-session/walkthrough-share uploaders).
- `src/orchestrator/agent_client.py` — typed operator-plane client for `/api/agents` over `canopy_web`: `AgentIdentity`, `BoardCommand`, `AgentClient` (register/syncs/work-products/skills/tasks-sync/command-drain/patch-task), and the shared `catalog_from_repo` + `_frontmatter` skill-catalog helper. Explicit identity + injectable transport (unit-testable, no network). Backs the `canopy agent` CLI. REST contract: `docs/architecture/agent-client-rest-contract.md`.
- `src/orchestrator/agent_cli.py` — `canopy agent …` CLI (`register`/`sync`/`syncs`/`sync-delete`/`work`/`turn`/`skills`/`tasks-sync`/`tasks`/`add`/`commands`/`apply`/`set`/`doctor`/`health`/`coverage`), a thin shell over `AgentClient` for shell-driven agents. Registered on `main` in `cli.py`.
- `src/orchestrator/session_upload.py` — reduces + uploads a Claude session transcript to canopy-web's `/api/sessions/upload` and returns a `/share/<token>` link; the transcript-packaging half behind `canopy agent turn`.
- `src/orchestrator/fleet_align.py` — cross-agent improvement spread (the *spread* verb of the operating model): compares the factory-stamped fleet against the current template and each other, weighs recent-session evidence, and dispatches PRs. Backs the `fleet-align` skill. FRAMEWORK tier.
- `src/orchestrator/eval_cli.py` + `eval_rubric.py` — the eval runner behind `canopy eval {score|record}`: `eval_rubric` is the generic weighted-rubric scorer (ACE's verdict-schema math, decoupled), `eval_cli` records the score → verdict path so any agent self-grades against the run lifecycle.
- `src/orchestrator/turn_synthesis.py` — generic, stdlib-only transcript reducer: prompt + final-reply per turn, with `active_seconds` + session timespan. The agent-agnostic substrate shared by share-session, harvest, and `session_upload`. See memory `turn-synthesis`.
- `src/orchestrator/issue_origin.py` — `canopy.origin/v1` provenance for architect-routed GitHub issues (backs `canopy issue create/context/delete`). Splits the CLEAN portable GitHub issue from the rich local+canopy-web understanding record.
- `src/orchestrator/test_audit.py` — builds a test corpus for the agent to judge and prune dumb tests (backs `canopy test-audit`). PRODUCT tier.
- `src/orchestrator/agent_doctor.py` — per-agent health checks (backs `canopy agent doctor`): identity, gating rails, secrets manifest, live gog email auth (via `agent_email.preflight`), canopy-web registration + board. The agent-level counterpart of `doctor.py`. Supports `--all` (fleet sweep) and `--fix`.
- `src/orchestrator/agent_health.py` — per-agent WORK-state readiness (backs `canopy agent health`): open board items + unread inbox for the agent's next turn. Complements `agent_doctor.py` (machine/install readiness).
- `src/orchestrator/agent_coverage.py` — per-agent bring-up coverage probe (backs `canopy agent coverage`): how much of an agent's promised surface is actually wired.
- `src/orchestrator/agent_gdoc.py` — shared Google-Doc authoring engine (backs `canopy gdoc`): markdown → rendered Doc as the agent; `--replace` edits native Docs in place via the Docs API (#353).
- `src/orchestrator/inbox_filters.py` — fleet inbox junk filters, defined ONCE and applied to any agent mailbox via gog Gmail filters (backs `canopy email apply-filters`). Conservative: only obviously automated/marketing mail is skipped-inbox, never a real person.
- `src/orchestrator/review_receipt.py` — the pre-send review rail: a receipt keyed to the BODY (not the agent's memory) recording that agent-turn-review ran; `canopy email send` refuses without it. Backs `canopy email review-receipt`.
- `src/orchestrator/repo_evidence.py` — generic git-repo evidence helpers (FRAMEWORK tier, stdlib + git only), shared by the evidence-weighing passes in agent-review/fleet-align.
- `src/orchestrator/session_sources.py` — injectable session-corpus sources: the seam that lets harvest/review read cross-user session roots without hard-coding discovery.
- `src/orchestrator/agent_email.py` — shared guarded email engine for the agent fleet (backs `canopy email {send|mark-read|preflight}`): the generalization of echo's `bin/echo_email.py` + `bin/echo_mark_read.py` / ACE's `bin/ace-email` + `bin/ace-mark-read`. HTML multipart send via gog, Gmail-API mark-read, gog preflight with exact remediation (incl. the API-not-enabled self-heal). Per-agent identity from `config/agent.json` (`email` + `gog_client`, client defaults to slug — never share another agent's client). Injectable runner/opener, fully unit-testable offline. Implements shared-gog-gdrive.md §3.
- `src/orchestrator/agent_web.py` — repo-identity convenience layer over the shared core: resolves an agent's identity from its `.claude-plugin/plugin.json` + `config/agent.json` (and a git-origin skill-URL template), then publishes via `AgentClient`. Backs `canopy agent-publish`. Transport/auth/frontmatter are NOT duplicated here — they delegate to `canopy_web`/`agent_client` (keeps `base_url`/`token`/`_call` shims for `issue_origin` + back-compat). The "common" half of the §4a boundary — canopy owns the client, the agent repo owns only its identity (`config/agent.json`).
- `src/orchestrator/harvest.py` — harvest corpus engine (the deterministic half of Hal-as-architect): cross-user session discovery (`user_session_roots` walks `/Users/*`), origin-anchored initiative filtering (`find_initiative_sessions`, oldest-first), `assemble_corpus` (origin + recent slices, human-message extraction, `confidence` half-blind flag). NO judgment — intent/drift is the agent's native job. Backs `canopy harvest corpus`. See memory `harvester-architect`.
- `src/orchestrator/provision.py` — portable secret provisioning: reads a repo's `config/secrets.yaml` (1Password refs + targets, no values) and materializes each via `op` into its target (idempotent, 0600, `{repo}`/`~`/relative targets, `--check` dry-run). Backs `canopy provision`. 1Password = source of truth; worktree-clean (targets global or provider-repo, never a gitignored repo `.env`). See `docs/agent-operating-model.md` §4e.
- `src/orchestrator/openclaw_harvest.py` — OpenClaw → canopy-agent bridge: snapshot (rsync, excludes creds) + inventory + compare-to-repo + bootstrap-new / reconcile-novel-skills. Engine is pure/offline (testable); backs `canopy openclaw-harvest` + the `openclaw-harvest` skill. Salvages ideas off the dead OpenClaw droplets into git.
- `src/orchestrator/agent_review.py` — agent self-improvement lens (Build 2): finds an agent's recent turn transcripts (by cwd, across repo + worktrees), extracts deterministic friction signals (failures/retries/gating-blocks/auth/checklist-gaps), and runs an optional `claude -p` synthesis into ranked findings + fixes. Backs `canopy agent-review` + the `agent-review` skill. Reuses transcripts.py / repo_paths.py / the analyzer pattern — a lens on the existing loop, not a fork. See `docs/agent-operating-model.md` §4 Build 2.

### Plugin (Claude Code skills, commands, agents)
- `plugins/canopy/skills/` — skill definitions (agent-review, agent-turn-review, alignment, auth-preflight, brief, canopy-doctor, context-ingestion, create-agent, ddd-ace-render, ddd-concept-eval, ddd-evidence-audit, ddd-findings-review, ddd-narrative-actionability-eval, ddd-narrative-coherence, ddd-narrative-review, ddd-run, ddd-spec, ddd-spec-qa, ddd-timing-eval, ddd-upload, ddd-video-improve, ddd-video-judge, ddd-why-brief, ddd-why-eval, ddd-why-qa, doc-regeneration, find-session, fleet-align, improve, improve-lens, information-architecture, issue-triage, openclaw-harvest, patch-gstack-browse, patterns, portfolio-guide, portfolio-review, product-management, project-status, select-session, share-session, shareout, test-audit, update, verify-findings, visual-judge, walkthrough, walkthrough-defect-creator, walkthrough-eval, walkthrough-share, website-builder)
- `plugins/canopy/commands/` — slash commands (alignment, auth-preflight, brief, canopy-web-pat-mint, ddd, ddd-ace-render, ddd-concept-eval, ddd-evidence-audit, ddd-findings-review, ddd-narrative-actionability-eval, ddd-narrative-review, ddd-run, ddd-spec, ddd-spec-qa, ddd-upload, ddd-why-brief, ddd-why-eval, ddd-why-qa, doc-regen, find-session, improve, issue-triage, patch-gstack-browse, patterns, pm-autonomous, pm-autonomous-loop, pm-scout, pm-status, portfolio-guide, portfolio-review, project-status, select-session, session-review, setup, test-audit, update, verify-findings, walkthrough, walkthrough-defect-creator, walkthrough-eval, website-builder)
- `plugins/canopy/agents/` — autonomous agents (ddd, pm-supervisor, session-review, walkthrough, website-builder)
- `plugins/canopy/agent-core/` — canonical fleet process docs, read at RUNTIME by the ~20-line stubs stamped into every agent repo (NOT under `skills/`, so they cost zero skill-description budget): `turn.md`, `task-tracker.md`, `manager-sync.md`, `deliverables.md`, `agent-runtime.md`, plus `gating-baseline.json` (fleet-baseline deny rails keyed by channel mount — the factory-stamped `gating_guard.py` merges baseline + local rails at call time, fail-closed if the baseline is unresolvable). A rail or process fix here propagates by `/canopy:update`, no per-agent PRs. Shipped as chunks A+B (PRs #312, #315); design: `docs/superpowers/specs/2026-07-13-agent-core-shared-skills-design.md`.
- `plugins/canopy/mcp/` — TypeScript MCP servers shipped with the plugin (ESM, run via `npx tsx`, no build step). `gws-server.ts` = **canopy-gws**, the domain-neutral Google Workspace server ported from ACE's `ace-gdrive` (32 atoms: `drive_*` / `docs_*` / `sheets_*` / `slides_*` + `get_google_form_definition` + `read_personal_drive_doc`; the ACE-aware atoms stayed in ACE). Per-agent identity resolves from SESSION env — `GWS_IDENTITY_MODE=sa` + `GWS_SA_KEY_PATH` (required; `gog` mode is a tracked follow-up), optional `GWS_ROOT_FOLDER_ID` default root + `GWS_ALLOWED_DRIVE_IDS` write allowlist enforced by the Shared-Drive write probe. **Fails loud at startup when identity env is absent — no default identity fallback.** Node toolchain lives in `plugins/canopy/package.json` (installed by `/canopy:setup` step 7 and the `/canopy:update` rsync step; `node_modules` is uncommitted). Offline vitest suites in `plugins/canopy/test/gws/` (registration-coverage drift gate, identity contract, mocked-client handler tests) run in CI via `.github/workflows/gws-tests.yml`.
- `.claude-plugin/marketplace.json` — plugin marketplace manifest

## Important: Hook Must Use Stdlib Only
`hooks/post_tool_use.py` runs with system python3 which may not have PyYAML.
The repo map uses JSON (not YAML). Any hook code must use only stdlib modules.

## Skill Authoring: Bash Positional Parameters
Bash code blocks in skill/command markdown files must NOT use `$1`, `$2`, etc.
for function arguments. Claude Code's slash-command argument expansion strips
these before the shell sees them, causing silent failures (empty strings).

**Workaround:** Move complex bash logic to standalone scripts in `bin/` or
`scripts/` and invoke them from the skill. Simple inline commands (no functions)
are fine.

## Skill Authoring: Command/Skill Name Collisions

If `commands/<name>.md` AND `skills/<name>/SKILL.md` both exist, the Skill tool
silently resolves `canopy:<name>` to the slash-command file — the actual
SKILL.md never lands in context, and the agent improvises from memory.

**Rule:** colliding commands MUST follow Pattern B — read SKILL.md from disk
explicitly before following it:

```bash
python3 -c "import json; d=json.load(open('$HOME/.claude/plugins/installed_plugins.json')); print(d['plugins']['canopy@canopy'][0]['installPath'] + '/skills/<name>/SKILL.md')"
```

Then `Read` that path and follow the SKILL.md exactly.

`tests/test_command_skill_collisions.py` enforces this — every colliding
command must reference `skills/<name>/SKILL.md` in its body. Adding a new
colliding command without Pattern B will fail CI.

## Skill Authoring: Built-in Command Namespace

Claude Code reserves a set of built-in slash command names (`/help`, `/clear`,
`/doctor`, `/config`, `/compact`, `/model`, `/fast`, `/login`, `/logout`,
`/agents`, `/mcp`, `/permissions`, etc.). Naming a plugin skill / command /
agent the same as a built-in causes a silent collision:

- `canopy:<name>` invocations route to the built-in handler, NOT your skill
- Worse, the entire skill description block can be truncated from the system
  prompt at session start — historically 142 skill descriptions vanished when
  `canopy:doctor` collided with native `/doctor`

**Rule:** before naming a new skill/command/agent, check whether the bare name
already works as a slash command in a stock Claude Code session. If yes, pick a
different name. The canonical fix when a native built-in lands on top of an
existing plugin entry is to namespace-prefix it (e.g. `doctor` →
`canopy-doctor`, shipped in v0.2.79).

`tests/test_builtin_command_collisions.py` enforces this — adding a new
plugin entry with a reserved name will fail CI. The reserved-set list lives
in that test; update it when Claude Code introduces new built-ins.

## Skill Authoring: Description Limits

Claude Code surfaces every installed skill's frontmatter `description` in the
system prompt at session start. Two budgets apply:

- **Per-skill cap**: Anthropic's authoring guidance recommends ≤1024 chars
  per description. Over the cap, Claude Code truncates.
- **Aggregate cap**: across all installed skills, the system prompt has a
  hard ceiling. Once it's hit, subsequent skills are dropped wholesale and
  Claude Code prints something like "N skills dropped" with no detail on
  which ones.

When a session reports dropped skills, run:

```bash
canopy skills budget               # ranked size table + aggregate gauge
canopy skills dropped              # simulated drop list under default caps
canopy skills dropped --aggregate-limit 30000   # try a different cap
```

The defaults (`--per-skill-limit 1024 --aggregate-limit 1500`) are
conservative — `--aggregate-limit` is the one to tune since the real
Claude Code ceiling is not publicly documented and shifts with releases.

## Cache Path Resolution

Plugin caches live at `~/.claude/plugins/cache/<plugin>/<plugin>/<version>/`
(double-nested by design — the inner dir name matches the outer plugin name).
The version segment changes every time the plugin is upgraded, so any path
constructed by string-concatenating a known version is fragile.

**Always derive the install path from `installed_plugins.json` instead of
hardcoding a version.** Canonical one-liner:

```bash
python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])"
```

For the same shape per skill, use `canopy skills list --json-output` —
each entry now carries `installed_version` and `cache_path` fields. No more
string-concatenated versions.

## Plugin Runtime Bundle — how skills reach the Python runtime

The installed plugin cache is **self-contained**: the updaters (`/canopy:update`
Step 2 and the fleet session-start hook) sync the repo-root runtime — `src/`,
`scripts/`, `evals/`, `pyproject.toml`, declared in
`plugins/canopy/.claude-plugin/runtime.json` — into `<installPath>/runtime/`
alongside the plugin. The runtime a skill executes is therefore version-locked
to the installed plugin **by construction** (same rsync, same SHA), not by a
process step that can be forgotten (the global CLI once drifted 67 versions
stale exactly that way).

**The ONE idiom.** Every bash block in plugin markdown that touches the runtime
starts with the canonical two-liner and goes through the shared resolver:

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
```

then either `uv run --project "$CANOPY_ROOT" canopy <subcommand> …` (CLI; cwd
is preserved, so cwd-sensitive commands like `test-audit collect .` still work)
or `(cd "$CANOPY_ROOT" && uv run python -m scripts.ddd.<module> …)` (module
runs). `uv` materializes `.venv` inside the runtime dir on first use.

Resolver ladder (`plugins/canopy/scripts/canopy-runtime.sh`): `$CANOPY_RUNTIME_ROOT`
env override → `<plugin>/runtime` (production) → the script's containing repo
(when run from a checkout's copy) → marketplace clone (bootstrap/fresh-install
gap, self-heals next session start) → dev checkouts **last**, deliberately — an
edited checkout must never silently shadow the installed version.

**Rules (CI-enforced by `tests/test_plugin_runtime_resolution.py`):**
- Plugin markdown must NOT reference `~/emdash-projects/canopy`,
  `~/emdash/repositories/canopy`, or `~/.claude/plugins/marketplaces/canopy`
  directly, and must not use bare `uv run canopy`. Allowlisted exceptions
  (update/setup/doctor/video-engine) are enumerated in the test with reasons.
- The global `canopy` CLI on PATH is a convenience (humans at a shell, fleet
  shims like ACE's `bin/ace-email`) — **never a skill dependency**.
- `video-engine/` is the one dev-checkout exception: Remotion's `node_modules`
  is too heavy to bundle per plugin version, so local render resolves a dev
  checkout where `/canopy:setup` ran `npm ci`, with a loud error otherwise.
- The PostToolUse capture hook is **plugin-managed**
  (`plugins/canopy/hooks/hooks.json` → `${CLAUDE_PLUGIN_ROOT}/hooks/post_tool_use.py`).
  Repo-root `hooks/post_tool_use.py` is a legacy-compat shim; the plugin copy
  defers while a working legacy settings.json registration exists, and
  `/canopy:setup` removes that legacy entry.

## Tool Hygiene (Read, Bash, parallel calls)

A few repeat foot-guns that have shown up in session reviews:

- **Read before Edit, with absolute paths.** The Edit tool errors with "File
  has not been read yet" if you try to edit a file you haven't first read in
  the current session. Worktree-relative paths (`./src/foo.py`) sometimes
  resolve to a different file than the absolute path that Edit later asks
  for, so always Read with the absolute path you'll Edit with.
- **Bash parallel-call sibling cancellation.** When you fire several Bash
  tool calls in one turn, a sibling failure can silently cancel still-running
  calls and discard their output. If you need every result, run them
  sequentially or split across turns. Reserve parallel Bash for genuinely
  independent reads where partial loss is OK.
- **Don't extract YAML with awk/sed/grep.** Folded scalars (`>`, `|`),
  multi-line strings, and quoted block keys break naive line-based parsing
  and produce silent truncation. Use Python's `yaml.safe_load` instead:
  `python3 -c "import sys,yaml; d=yaml.safe_load(open(sys.argv[1])); print(d['description'])" path.md` (after stripping the frontmatter delimiters), or invoke a CLI that already does this (e.g. `canopy skills budget` for SKILL.md descriptions).
- **Don't dump binaries with `strings` / `cat`.** A multi-MB Mach-O dump in
  Bash output blows up your context. Pipe through `head -c 4000` or
  `xxd | head` if you really need a peek; otherwise use a real parser.

## Plugin Updates — NEVER locally patch

**Golden rule: never patch the installed plugin directly — always go through the
release flow.** There are three copies of canopy on a machine; only the first is editable:

| Location | What it is | May I edit it? |
|----------|------------|----------------|
| `plugins/canopy/` in **this repo** | Source of truth | ✅ **Yes — all changes start here** |
| `~/.claude/plugins/cache/canopy/canopy/<version>/` | The **installed** plugin Claude Code actually runs | ❌ **Never hand-edit** |
| `~/.claude/plugins/marketplaces/canopy/` | The update **channel** `/canopy:update` pulls from | ❌ Never edit/develop here |

**CRITICAL: Never directly copy, rsync, or write files into `~/.claude/plugins/cache/`
or edit `~/.claude/plugins/installed_plugins.json` by hand.** This is "local patching"
and it bypasses the plugin system, creates version mismatches, and makes bugs hard to
diagnose. If you feel the urge to locally patch, STOP — use `/canopy:update` instead.

### ⛔ STOP — the #1 mistake: forgetting to bump VERSION

**If you change ANYTHING under `plugins/canopy/` (skills, commands, agents, the
`.claude-plugin/plugin.json` `description`, anything) you MUST bump the version.**

The version bump is the ONLY signal that tells installed sessions "there is new work to
pick up." Without it:

- `/canopy:update` reports `UP_TO_DATE` and refuses to sync the cache
- Every existing Claude session keeps running the OLD cached copy of your skill
- Your PR effectively didn't ship — you changed `main` but nobody will ever see it

**This is a historically ridiculous, silent failure mode.** PR #49 (silent video
recording) merged exactly this way: plugin files changed, VERSION was not bumped, the
CI `check-version` job failed — but back then canopy was **private with no GitHub
Pro**, so the merge button wasn't gated on the check and the PR went in anyway,
leaving `/canopy:update` reporting `UP_TO_DATE` forever after. **That hole is now
closed:** canopy is public and `check-version` is a **required status check** on
`main`, so a missing/bad bump now blocks the merge rather than merely reddening the
PR UI. The Claude Code push guard (below) still catches it even earlier.

**The fix is layered prevention:**

1. **`canopy version bump`** — the only correct way to advance the version. Fetches
   origin/main, picks `max(local, origin/main) + patch+1`, writes both files atomically.
2. **Local pre-push hook** — refuses to push a branch where `plugins/canopy/` changed
   but VERSION didn't advance beyond origin/main. Catches the mistake before the PR
   is even opened. See § Git Hooks below — you must opt in with `git config core.hooksPath`.
3. **CI version-check workflow** — runs `canopy version verify-bump` on every PR, and
   is now a **required status check** on `main`: a red `check-version` blocks the merge
   (no longer advisory-only, since the repo went public).

**Mental checklist before EVERY canopy commit touching `plugins/canopy/`:**

1. Did I run `uv run canopy version bump`? (It updates all THREE version files
   together — `VERSION`, `plugins/canopy/.claude-plugin/plugin.json`, and the two
   fields in `.claude-plugin/marketplace.json`. Editing them by hand is how
   marketplace.json drifted in the 0.2.157 bump — #120 changed only two files.)
2. Do all three agree? `VERSION` == `plugin.json` version == every
   `marketplace.json` version field. CI's version-check now fails on any mismatch.
3. Did the pre-push hook pass?

## Git Hooks

### Claude Code PreToolUse guard (auto-active, no opt-in) — the primary defense

Because **100% of canopy commits are AI-generated through Claude Code**, the
most reliable guard is a Claude Code `PreToolUse` hook, not a git hook. It is
checked into `.claude/settings.json` and loads automatically — no
`git config` step, works regardless of what emdash sets `core.hooksPath` to.

- `hooks/pre_tool_use_version_bump_guard.py` — intercepts any `git push` Bash
  call and runs the same `verify_bump_when_plugin_changed` check CI runs. If the
  branch touched `plugins/canopy/` without advancing VERSION past `origin/main`,
  it **denies the push** and hands the agent the exact fix (`canopy version
  bump`). This catches both failure modes that historically reddened CI: (A)
  forgot to bump, and (B) a parallel worktree already claimed your patch number.
  Override with `CANOPY_ALLOW_PUSH_NO_BUMP=1`.
- `hooks/pre_tool_use_plugin_cache_guard.py` — blocks local-patching of the
  plugin cache (`CANOPY_ALLOW_CACHE_PATCH=1` to override).
- `hooks/pre_tool_use_main_branch_guard.py` — keeps the **primary** canopy checkout
  (`~/emdash-projects/canopy`, a real `.git` dir) on `main`: blocks `git
  checkout/switch` to a non-main branch and branch creation there. Feature work goes
  in an emdash **worktree**; the global `canopy` CLI is deployed from main, so
  branching in the primary checkout silently changes what `canopy` runs (this is how
  a fresh session got `No such command 'harvest'`). Worktrees branch freely.
  Override `CANOPY_ALLOW_PRIMARY_BRANCH=1`.

This is the only guard that fires in practice for AI-driven worktree flow — see
the git hooks below for why.

### Git hooks (opt-in, belt-and-suspenders) — dormant unless installed

Two hooks ship in `scripts/hooks/`. They are **inert until you opt in** with:

```bash
git config core.hooksPath scripts/hooks
```

In emdash worktrees `core.hooksPath` typically points at the main checkout's
default `.git/hooks` (which has no canopy hooks), and no human runs the opt-in —
so these never fired, which is exactly why CI kept catching missing bumps. The
Claude Code guard above supersedes them for the AI flow; keep these for humans.

- `pre-commit` — when `VERSION` or `plugins/canopy/.claude-plugin/plugin.json` is staged,
  runs `canopy version verify` to refuse a half-bump (one file edited, the other not).
- `pre-push` — refuses direct pushes to `main` AND runs `canopy version verify-bump`
  on the branch. Refuses the push if `plugins/canopy/` changed without VERSION
  advancing past origin/main.

Bypass either hook with `git push --no-verify` / `git commit --no-verify` — almost
always the wrong call. These local hooks are still advisory (they only fire if a human
opts in), but they're no longer the *only* backstop: since the repo went public,
`check-version` is a **required status check** on `main` that enforces the bump
server-side. The hooks now just catch the mistake earlier, before the push.

### Update workflow (the ONLY way to update)
1. Make changes to skills/commands/agents in `plugins/canopy/` **OR** to the CLI/package in
   `src/orchestrator/`. **Either kind you want deployed bumps VERSION** — canopy is one versioned
   artifact (plugin + CLI ship together). `/canopy:update` gates on VERSION, so a CLI-only change
   with NO bump will report `UP_TO_DATE` and never deploy. When in doubt, bump.
2. Bump the **patch version** with `uv run canopy version bump` — do NOT hand-edit. It advances `VERSION`, `plugins/canopy/.claude-plugin/plugin.json`, AND both `.claude-plugin/marketplace.json` fields together (e.g. `0.2.6` → `0.2.7`). Hand-editing only two of the three is the drift that #120 introduced. See the STOP block above — a missing/partial bump is the #1 mistake. CI's version-check fails if the three disagree, but will NOT catch a missing bump on `main`.
3. Commit, push, PR, and auto-merge (see § Shipping Changes — the maintainer
   does NOT review; merge it yourself):
   ```bash
   # From a worktree:
   git add -A && git commit -m "feat/fix: description"
   git push -u origin <branch>
   gh pr create --title "..." --body "..."
   gh pr merge <n> --merge
   ```
4. **IMMEDIATELY after pushing**, run `/canopy:update` in the current session.
   This is mandatory — it pulls from GitHub, creates a new cache dir, updates
   `installed_plugins.json` (the **plugin**), AND (Step 3 of the update skill)
   **redeploys the `canopy` CLI** — `uv tool install --force` from the marketplace
   clone. **canopy is a plugin AND a CLI; both ship from the same `main`.** The CLI
   is NON-editable — never an editable install of `~/emdash-projects/canopy` (that
   couples `canopy` to your dev branch and strands new commands; for CLI dev use
   `uv run` from a worktree). Without `/canopy:update`, the current session runs
   stale plugin AND stale CLI. Do NOT skip.
5. Run `/reload-plugins` to activate the new plugin version in the current session

New sessions auto-detect the version bump on startup — no manual steps needed.

### How it works
- `~/.claude/plugins/known_marketplaces.json` — marketplace entry pointing at this git repo
- `~/.claude/plugins/installed_plugins.json` — installed plugin entry with version + commit SHA
- Cache dir is keyed by version: `~/.claude/plugins/cache/canopy/canopy/<version>/`
- On session start, Claude Code pulls the marketplace repo and compares `plugin.json` version
  against the installed version — if different, it re-installs
- `/reload-plugins` only reloads skills from the existing cache — it does NOT detect
  version changes or re-install. That's why `/canopy:update` must run first.
- Releases are **version-on-`main`** — there are no git tags. "Released" means merged to
  `main` with the version bumped; `/canopy:update` is what distributes it.

## Fleet Auto-Update (session-start self-heal) — issue #357

Fleet agents (eva, hal, ada, echo, ace) run from a **versioned plugin cache**, not
their repo, so a merge to an agent's `main` was invisible to the running harness
(interactive sessions AND cron turns) until someone hand-ran an update skill —
installs froze for days. Two pieces, both shipped by canopy, close this:

**A. Session-start auto-updater (canopy plugin hook — the whole fleet).** canopy is
installed user-scope alongside every agent, so its `plugins/canopy/hooks/hooks.json`
registers an **`async` SessionStart** hook →
`plugins/canopy/hooks/fleet_session_start_update.py`. On every session it discovers the
fleet (canopy + any plugin shipping `config/agent.json`), and for each one **git-pulls
the marketplace clone, rsyncs the plugin source into a version-keyed cache dir, `npm
install`s if needed, and patches `installed_plugins.json`** — the same thing
`/canopy:update` does by hand, automated. It is **SHA-driven** (compares installed
`gitCommitSha` vs `origin/main`), so it self-heals even when a version bump was
forgotten — the failure mode that makes a version-keyed `claude plugin update` a silent
no-op. Because it does the sync + registry-patch itself, the version bump is NOT
load-bearing; it's for label honesty only.
  - High blast radius → built to be inert: `async` (never blocks startup), a
    non-blocking lock (concurrent sessions don't stack), timeout-bounded subprocesses,
    all errors swallowed, one line per run to `~/.claude/canopy/fleet-update.log`,
    emits nothing on stdout.
  - **Opt out per machine:** `CANOPY_FLEET_AUTOUPDATE=0` or `touch
    ~/.claude/canopy/fleet-autoupdate-disabled`. Tuning: `CANOPY_FLEET_UPDATE_PLUGINS`
    (explicit allowlist), `CANOPY_FLEET_UPDATE_EXCLUDE`, `CANOPY_FLEET_UPDATE_MIN_INTERVAL`.

**B. Bump-on-every-merge (per-agent repo; canopy owns the template).** A
`.github/workflows/auto-version-bump.yml` bumps the PATCH version in `plugin.json` +
`marketplace.json` (kept in lock-step by `scripts/bump-plugin-version.py`) on **every**
merge to `main` — bumping often is intentional. This keeps the version label +
`claude plugin list` honest and closes the "skill changed, version forgotten" gap. The
agent factory (`canopy create-agent`) stamps both files into new agents; existing agents
get them distributed as PRs. GITHUB_TOKEN pushes don't retrigger workflows, and the
bump commit carries `[skip bump]` — so no loop.

## Per-Project Canopy State

Per-project canopy state lives at `<repo>/.canopy/`, committed to the project's git repo:

- `<repo>/.canopy/pm/` — `canopy:product-management` state (autonomous.yaml, context.md, learnings.md, runs/). Resolved via `plugins/canopy/skills/product-management/scripts/resolve_pm_dir.sh` from any PM markdown file or agent.
- `<repo>/.canopy/lenses/` — per-project lens descriptors (PR #37).
- `<repo>/.canopy/run-artifacts.yaml`, `<repo>/.canopy/README.md` — project run-artifact map and onboarding.

Outside a git repo, the PM resolver falls back to `$HOME/.canopy/pm/<basename-of-cwd>/`.

The global "self-improvement brain" (`~/.claude/canopy/observations/`, `proposals/`, `session-log.jsonl`, etc.) stays under `$HOME/.claude/canopy/` — that data is intentionally cross-project on a single machine.

## Repo Notes (DDD & rendering)

These ship in **this repo**, not the plugin cache:

- `scripts/ddd/` (DDD loop helpers). Since the runtime bundle (PR #395),
  `scripts/ddd/` ships INSIDE the plugin runtime: skills resolve
  `DDD_REPO="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")"` (the canonical
  two-liner in § Plugin Runtime Bundle) and run
  `(cd "$DDD_REPO" && uv run python -m scripts.ddd.<mod> …)`. The old
  `DDD_REPO=$HOME/emdash-projects/canopy` hardcode is gone.
- `scripts/narrative/` — the neutral narrative substrate (schemas, models,
  `${var}` substitution) extracted out of `scripts.ddd` so non-DDD callers can
  reuse it (PR #160, repointed in #162). DDD builds on top of it.
- `video-engine/` — the general Remotion video renderer, relocated into canopy
  (PR #191). Both the walkthrough and DDD render paths produce video through it;
  `render_locally.py` is the local entry point.
- DDD pause gates (`concept_change`, `external_release`, the narrative-agreement gate)
  post to the **canopy-web review surface**, never the built-in `AskUserQuestion` tool.
  See `plugins/canopy/agents/ddd.md` § Pause policy.

## Testing
- `uv run pytest` from project root (~2,260 tests across 175 test files). A handful of browser-dep tests error on collection unless the optional extras are installed: `pip install -e '.[browser]'`.
