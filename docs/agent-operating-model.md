# The Agent Operating Model — building and self-improving a fleet of Claude Code agents

*Status: decision-grade draft. Author: Claude (Opus 4.8) for Jonathan, 2026-06-19.*
*Scope: how canopy should turn the one working agent (echo) into a repeatable way to build agent #2, #3, … and make each one get measurably better over time.*

> Companion to echo's own `docs/research/agentic-frameworks.md` (the "which runtime"
> decision). This doc is the layer above: the *operating model* — the reusable primitives,
> the factory that stamps them out, the loop that improves them, and the stance on memory.
> A best-practices synthesis from OpenClaw and other harnesses is folded into §6 (cited).

---

## TL;DR

You already built the hard part without naming it. **Echo is a working prototype of an
operating model for controllable autonomous agents**, and almost none of its load-bearing
parts are marketing-specific. The work now is not "build more agents like echo by hand" —
it's three moves:

1. **Extract echo's operating model into a factory** (`canopy:create-agent`) so agent #2 is
   a thin *persona + domain skills* on top of a shared substrate, not a fork-and-find-replace.
2. **Make canopy the fleet's self-improvement engine.** Reef proved that *observing* skill
   gaps without a loop to *act* on them is a dead end. Canopy already has the closed loop
   (capture → observe → propose → implement → track) for Claude Code sessions; point it at
   the agents' turns.
3. **Deprioritize memory as infrastructure.** Evidence (echo's unused gbrain, reef's own
   "skills are the unit of collective learning, not memories" lesson) says: skills and hooks
   are the durable, improvable, enforceable unit; memory is for per-person/episodic facts only.

The differentiator vs. OpenClaw is exactly the thing you said you wanted: **the ability to
*force* behavior** (hooks make the wrong path impossible) and **to improve the agent from the
outside** (canopy's loop), instead of an opaque always-on process you can only watch.

---

## 1. What echo actually is (the reusable primitives)

Echo is shipped as a Claude Code plugin: a persona + a skills library + `bin/` CLIs + hooks +
a canopy-web workspace. Stripping out "marketing," the generic primitives are:

| # | Primitive | Where it lives in echo | Why it generalizes |
|---|-----------|------------------------|--------------------|
| 1 | **Persona + routing key** | `echo@dimagi-ai.com` mailbox; `From:`/`threadId` as identity | Every agent needs an identity and a way to key *who/what* a turn is about. Mailbox gives thread+sender for free. |
| 2 | **The `turn` orchestrator** | `skills/turn/SKILL.md`, re-read every turn | The single entry point that sequences preflight → drain board → do work → **skill self-check** → close. The file *is* the checklist. |
| 3 | **Reads-free / writes-gated guardrail** | CLAUDE.md "Hard guardrail"; enforced per-action | Safest autonomy floor: search/read run freely, every outbound action waits for human approval. |
| 4 | **Invariants-as-hooks, not memory** | `scripts/hooks/block_raw_gog_send.py` + `.claude/settings.json` | The best idea in the repo. Make the *wrong* thing impossible instead of trusting the model to remember a rule under load. |
| 5 | **Capability in CLI/MCP, skills orchestrate** | `bin/*.py` hold logic; SKILL.md is declarative | MCP/CLI is the portability boundary to the Agent SDK; skills stay thin and portable. |
| 6 | **Self-improvement baked into the turn** | `turn` Step 4 + `manager-sync` | "Did I create/improve a skill? Did I hand-repeat something that should be a skill?" — compounding instead of re-deriving. |
| 7 | **canopy-web agent workspace** | `skills/canopy-publish`, `task-tracker`; `/agents/echo` | First-class agent home: kanban board, syncs with self-grades, mirrored skill catalog. The board is a **second trigger surface** — others can queue work without a keyboard. |

**The gap:** all seven live *inside the echo repo*. There is no factory, so agent #2 means
forking echo and find/replacing the marketing parts. That is the first thing to fix.

### 1a. "Force it to use skills" — how echo actually does it

You said the reason you're building this rather than using OpenClaw is the ability to *force*
skill use and otherwise control behavior. Echo's three mechanisms, weakest to strongest:

- **Prose in CLAUDE.md** (weakest — relies on the model re-reading and choosing to comply;
  drops under load).
- **The `turn` file as a re-read-every-time checklist** (medium — works because the turn is
  the unskippable entry point, but echo notes its plugin *isn't registered* for Skill-tool
  dispatch, so it leans on "re-read the file," which is fragile exactly when load is high).
- **Hooks that make the wrong path impossible** (strongest — `block_raw_gog_send.py` denies a
  raw send; the agent *cannot* bypass the HTML wrapper). CLAUDE.md states the rule directly:
  *"Hard behavioral rules do NOT belong in memory… encode each as enforcement."*

**Design implication:** the strongest form of "force it to use skills" is **hooks for hard
invariants + a thin always-loaded turn checklist + registering the agent plugins so Skill
dispatch fires.** The factory should template all three.

> **Revision (Jon, 2026-07-01) — rails, not approval gates.** Operational experience with hal
> showed the two hook verbs are not equals: **deny rails** (block the wrong path, name the right
> one; the agent self-corrects and keeps going) work well and cost autonomy nothing, while
> **approve/ask rules** (blocking permission modals) stall autonomous work and nag interactive
> sessions. The fleet default is now: hooks carry deny rails only; approval semantics live in the
> procedural layer (the turn checklist's explicit approval step; ACE's pause-point state model).
> The factory's templated `config/gating.json` defaults its `approve` list to empty. Full
> rationale: `docs/architecture/shared-gog-gdrive.md` §4 and ACE's
> `docs/superpowers/specs/2026-07-01-agent-operating-model-adoption.md § Gating`.

### 1b. Counterpart-communication primitives (the reply-quality checklist)

The turn loop decides *what* an agent does; these decide whether the *message* back to a human
lands. Each rule below is here because a fleet agent got it wrong in a live turn — they are the
communication counterpart to the artifact `-eval` regime, and they belong in canopy (not any one
agent) because a second agent wants every one of them verbatim. The factory templates them into
every new agent's `turn` + `self-review` skills; existing agents adopt them by reference.

1. **Self-review against the original words.** Before any outbound reply, extract EACH discrete
   ask as a numbered checklist, confirm the draft does exactly that (read cited sources; don't
   reconstruct from memory), and reflect the checklist back one line per ask.
2. **Lead with what you DID** + a recommendation + options — not junior "which should I do?"
   questions when the brief already gave you authority to decide.
3. **Deliverables and attachments are Google Docs; the draft is shown inline.** A substantial
   artifact goes in a shared gdoc the reply links — never a wall of pasted text, never a local
   `.txt` you point at. When presenting a draft for approval, show the body itself in the
   conversation.
4. **Decide-then-show, in one coherent order.** Either you decided and you show the result, or you
   ask a clean question — never a jumble of "asking about (1) while showing (2)." Keep ask-order
   and show-order consistent, and don't manufacture a decision out of a thread already classified
   as not actionable.
5. **Verify recipients from the structured reader** (or `--reply-all`), never a raw text mail view
   — raw reads hide `Cc:` and silently drop cc'd people. Choose reply-all vs. direct on purpose.

Rules 1–2 already shipped in the factory templates; rules 3–5 were proven in echo and promoted
here after ACE repeated all three by hand in a single turn (draft-in-a-txt-file, a jumbled
non-actionable "decision," and a hand-parsed `Cc` list).

---

## 2. Why reef died — and why that's the good news

Reef was a Next.js console to manage a fleet of OpenClaw instances on Digital Ocean (SSH +
DO API). It could **see** that instance A had a skill instance B lacked — the HTML report even
had a "Skills to Spread" section — but it had:

- **no transfer operator** (couldn't copy a skill A→B; `transfer-skill` stayed backlogged),
- **no execution trigger** (even copied, B wouldn't know to run it),
- **no feedback loop** (no measure of whether a skill changed behavior; the hygiene check was
  a `TODO` placeholder).

Reef's own PM notes name the lesson precisely: ***"skills are the unit of collective learning,
NOT memories — memories are usage artifacts."*** It was a **passive observatory** — it could
watch but not act. Combined with OpenClaw's opacity (you couldn't reach in and fix instances),
momentum died.

**The good news:** the active learning system reef lacked is *canopy*. Self-improvement needs
three things — (1) spread, (2) execute, (3) feedback-measure — and canopy already has all three
for Claude Code sessions:

| Self-improvement need | Reef | Canopy (already exists) |
|---|---|---|
| Capture what happened | SSH-scrape agent dirs | `hooks/post_tool_use.py` logs every session |
| Observe friction/gaps | "Skills to Spread" (passive) | `analyzer.py` → `observations.py` |
| Propose a change | — | `proposer.py` → `proposals.py` |
| Execute the change | — | agent dispatch via `/canopy:improve` → PR |
| Measure / track outcome | — | `tracker.py` |
| Make it visible | HTML report | canopy-web feeds (`/insights`, `/agents/<name>`) |

Echo's hand-rolled `manager-sync` + Step-4 self-check is a *per-agent miniature* of canopy's
loop. **Don't rebuild reef. Point canopy's loop at the agents' turns.**

---

## 3. The memory question (answered with evidence)

You flagged you haven't really used memories in gbrain and aren't sure they matter. The
evidence says **deprioritize memory as fleet infrastructure**:

- gbrain is **not installed or populated** on this machine. Echo's most recent turn made
  **zero** gbrain calls (the inbox was empty, so triage never ran). Memory integration is
  backlog item T8 — aspirational, never exercised.
- Reef's hard-won lesson: collective learning rides on **skills**, not memories. Memories were
  "usage artifacts," not a vector for getting better.

**Stance:**
- **Skills and hooks** are the durable, enforceable, improvable unit of capability and learning.
  Anything you're tempted to "remember" as a behavior should become a skill or a hook.
- **Memory matters in exactly two narrow places:** (a) **per-contact CRM isolation** when an
  agent talks to many people — echo's real need *once its inbox is non-empty* — and (b)
  **cross-turn episodic continuity** ("what did I commit to last week"). Both are facts about
  the world, not behaviors.
- Keep memory **behind the MCP boundary** (gbrain or a successor) so it's swappable, and wire
  it **narrowly** (echo's per-contact partition) to validate the pattern on one agent before
  treating it as a fleet service.

Memory would have made reef *feel* smarter without making it *get* better. Skills are how it
gets better.

---

## 4. Recommendation — three builds, in order

### Build 1 — The agent factory (`canopy:create-agent`)
Extract the §1 primitives into a generator. Output for a new agent `<name>`:

- plugin manifest + `persona.md` (identity, mandate, voice, stakeholders)
- `turn/` orchestrator templated to the agent's trigger surfaces
- reads-free / writes-gated guardrail wired to the agent's outbound actions
- an **invariants-as-hooks** starter (`.claude/settings.json` + a `hooks/` example)
- canopy-web workspace registration (board + syncs + skill catalog)
- the Step-4 skill self-check baked into the turn
- a `setup`/`preflight` bootstrap pattern (1Password `.env.tpl`, per-machine provisioning)

Agent #2 = run the factory, fill in persona + domain skills. *This is the literal ask:
"so I can create more of these agents."*

### Build 2 — Canopy as the self-improvement engine (the reef that works)
Add an **agent-turn lens** to canopy's existing loop: review each agent's recent turns, detect
friction, propose skill/hook improvements, ship them via the same PR flow the agents use, and
track outcomes. Echo's `manager-sync` becomes a capability every agent inherits. First test
cases are echo's *real* friction from its last session (see Build 3).

### Build 3 — Harden echo (the first iterations of Build 2)
Not a separate workstream — these are the first improvements the loop should produce, and the
best test of what the factory must template:
- **OAuth bootstrap can't self-heal** (new-machine "People API not enabled" needed manual
  console work) → detect + guide/auto-enable.
- **1Password half-wired** (items created, never round-trip validated; special-char titles
  break `op read`) → validate readability after create.
- **Reply checklist gets dropped under load** → move the always-do checklist from prose to a
  PreToolUse hook (the §1a "strongest form" upgrade).

---

## 4a. Repo & framework topology (decided)

**Each agent lives in its own git repo and consumes a common canopy-provided framework via a
hybrid split.** This is the shape echo already has (`dimagi-internal/echo`), generalized.

Two new first-class requirements drive this:
- **Team-runnable.** A teammate clones *one* agent repo, runs its `setup` (1Password provision
  + canopy-web registration), and drives it. Multi-operator = "more people run the same repo,"
  **not** one shared always-on instance (that's the OpenClaw topology that bled contexts).
- **Channels over time.** Each agent mounts the comms channels it needs (email today; Slack,
  Telegram, etc. later) from a shared adapter interface.

**Canopy IS the kit.** The "common framework" is not a new package to invent — **canopy itself
is installed in every agent's environment, as both a Claude Code plugin and a Python package.**
That single decision answers "where does the shared substrate live" *and* "what stays common vs.
what goes in the agent's repo":

- **As a plugin**, canopy gives every agent its cross-agent *skills/commands* — `create-agent`,
  `improve`, `session-review`, the agent-web publisher, future fleet tooling — available from
  inside any agent's Claude Code session.
- **As a package** (`orchestrator.*`, on PATH as the `canopy` CLI), canopy gives every agent's
  hooks/CLIs the shared *logic* — the gating engine, channel adapters, the canopy-web client —
  callable via the stable `canopy <subcommand>` interface. Fix it once in canopy, bump, every
  agent inherits it. *(This is the "spread" verb of §6.5, for infrastructure.)*

**The common-vs-agent boundary** (the line you draw when deciding what to extract):

| Stays **common** (canopy plugin + package) | Lives **in the agent's repo** |
|---|---|
| Gating *engine* (the hook logic) | Gating *rules* (`config/gating.json`) + the thin hook shim + `.claude/settings.json` wiring |
| Channel *adapters* (email→slack→telegram) | Channel *mounts* (which channels this agent uses) |
| canopy-web client; self-improvement loop; `create-agent`; cross-agent skills | Persona, domain skills, allowlist, `.env`, the `turn` checklist text |
| The operating-model invariants (as enforced defaults) | This agent's identity, mandate, secrets |

Rule of thumb: **logic, adapters, and cross-agent skills are common; identity, rules, secrets,
and domain skills are the agent's.** Anything an operator must read or edit to understand/steer
*this* agent stays in the repo (so it's "forced" per §1a and improvable by canopy's loop);
anything that's pure infra you'd want to fix fleet-wide goes in canopy.

*v1 vs. target:* the factory today **copies** a self-contained gating hook into the agent
(robust even before canopy is installed as a dependency). The target is a **thin shim** that
calls canopy's installed engine, leaving only `gating.json` in the repo — so engine fixes
propagate by bumping canopy. Same pattern for the canopy-web client and channel adapters: copied
now, thinned to canopy-backed as the package boundary firms up.

**Channels as a shared adapter interface.** Canopy defines an inbound (`reads-free`) / outbound
(`writes-gated`, per the §6.6 gating table) adapter contract. Adding Telegram later = write one
adapter in canopy + mount it in the agents that want it — not a per-agent rebuild. Echo's
`email-communicator` is the first adapter; it gets generalized into canopy.

## 4b. canopy-web — the agent web interface & multi-player (decided direction)

**canopy-web is the web surface for the whole fleet**, not just a per-project dashboard. Echo
already proved the shape: a first-class `/agents/<slug>` workspace with a kanban **board**,
**syncs** (with self-grades), **work products**, and a mirrored **skill catalog**, fed by a
canopy-web client (echo's `bin/echo_canopy.py` against `/api/agents/*`). Generalize that client
into canopy so **every** agent gets a workspace for free, and canopy-web becomes:

- **The fleet console** — a roster of agents, each with its workspace, turn history, and grades.
- **The trigger + approval surface** — the board is where humans queue work ("Accept", "do this
  now", "decline with reason") and, crucially, where **outbound actions get approved** (the
  run-time HITL gate of §6.6, surfaced to a browser instead of a terminal).
- **The visibility layer for the self-improvement loop** (Build 2) — observations, proposals, and
  the PRs canopy ships into an agent's repo show up here as the agent's "getting better" feed.

### Multi-player — why an *agent* is the right place to crack it

Multi-player has been the persistent pain point (and unsolved in ace-web — in practice everyone
works solo). The structural insight: **collaborating *through an agent* is fundamentally easier
than collaborating *on a document*, because the agent is a serialization point.** The hardest
part of multi-player — concurrent conflicting edits to shared state — largely disappears when:

- **The agent is the single-threaded actor.** It runs **one turn at a time**; the "one
  counterpart / one memory scope per turn" cardinal rule (§1) already forbids the cross-context
  bleed that sank the shared-OpenClaw topology. Humans don't edit shared state concurrently —
  they **queue intents** (board tasks) that the agent serializes.
- **The board is a CRDT-friendly work queue, not a shared canvas.** Append-only task cards +
  per-card state transitions + per-action approvals compose cleanly across many humans without
  merge conflicts. Two people can queue two tasks; the agent drains them in order.
- **Attribution is per-intent.** Who queued a task, who approved a send — recorded on the card.
  This is the one piece worth building beyond echo's current board (see §5 open Q4).

So the multi-player bet is narrow and tractable: **make canopy-web's agent board the shared
queue+approval surface, with per-operator attribution, and let the agent's serialized turn be the
conflict-resolution mechanism.** That sidesteps the part of multi-player that's genuinely hard,
and it's a different (easier) problem than multi-player editing in ace-web — worth validating
here precisely because the agent removes the concurrency.

*v1:* generalize the canopy-web client into canopy + have `create-agent` register a workspace and
add a board-drain step to the generated `turn`. Per-operator attribution and browser-side
approval come next.

## 4c. Cross-project capability awareness (decided)

Agents must **use the capabilities of Jonathan's *other* projects in their environment — not
recreate them.** This generalizes Hal's "use the stack, don't recreate it" to every agent, and it's
the same boundary as §4a (shared capability, per-agent binding) applied across repos.

- **Mechanism:** an agent's repo declares the other projects' MCP servers it borrows in its
  `.mcp.json`, and its `CLAUDE.md` names those capabilities and says to prefer them. Tools then
  appear as `mcp__<server>__<tool>` in the agent's sessions.
- **Portability rule:** reference borrowed servers by a **PATH-resolved command**, never a
  per-machine absolute path. The provider repo exposes a launcher (npm `bin` + `npm link`) that
  resolves its own root from the script location, so its headless, script-relative creds and deps
  are found no matter who launches it. (First instance: chrome-sales ships `chrome-sales-mcp`;
  Eva borrows its `salesforce` + `google-drive` MCPs — `sheets_*` is how Eva safely touches Beth's
  GSP tracker.)
- **Private + per-operator:** provider code stays private (grant GitHub access per-person); creds
  are per-operator secrets, never committed. For service-account auth, the target resource (a sheet,
  a Drive folder) is shared with the SA email.
- **Future:** a `canopy capabilities` discovery so agents enumerate available MCPs/plugins instead
  of hardcoding — keeps this current as the stack changes (echoes Hal's "weekly tool review").

## 4d. Runs in emdash worktrees — keep the loop worktree-clean (decided)

Jonathan runs everything through **emdash**, so an agent turn usually executes from a **git
worktree**, not the main checkout. The loop must work identically there. Two consequences for every
agent the factory produces:

- **Everything the loop needs at runtime is either tracked or globally resolved.** Tracked files
  (skills, `config/`, `hooks/`, `.mcp.json`, `.claude/settings.json`) are present in any worktree.
  Global/PATH-resolved deps (the canopy CLI + plugin; borrowed MCP launchers like `chrome-sales-mcp`;
  the canopy-web PAT at `~/.claude/canopy/`) resolve regardless of cwd. The gating hook already uses
  `$CLAUDE_PROJECT_DIR`, which is the worktree root — so it just works.
- **Avoid depending on gitignored repo-local files (a `.env`) for the core loop** — they don't
  exist in a fresh worktree. Prefer global/PATH-resolved deps and tracked, non-secret config (e.g.
  `config/trackers.json` for a sheet id). This is why the §4c portability rule (PATH-resolved
  command, not abs path) and global PAT placement matter: they're what make worktree runs seamless.
- *Known residual friction:* a worktree may re-prompt for MCP-server approval on first use, and any
  genuinely secret per-operator `.env` must be provisioned per-worktree (or read from a global
  location). Keep secrets out of the hot path where possible.

## 4e. Portable secret provisioning (decided; superseded for agents — see below)

> **Update (2026-07-25): for AGENT repos, `.env.tpl` + `op inject`/`op read` is now the fleet
> standard** — see `agent-core/agent-runtime.md`. The fleet has migrated (ada/eva/hal/echo ship
> `.env.tpl`; `config/secrets.yaml` deleted from those repos). `canopy create-agent` now scaffolds
> `.env.tpl`. The recipe below (`config/secrets.yaml` + `canopy provision`) still works and is
> kept functional — it remains the path for non-agent PROVIDER repos (e.g. chrome-sales) and any
> agent that hasn't migrated yet — but it is no longer what new agents should be built on.

Agents need credentials (service-account keys, OAuth creds, PATs) that must never be in git yet
must be available wherever the agent runs — main checkout, any emdash worktree, every operator's
machine. Hand-shuffling keys is the lazy, non-portable status quo. The (legacy) standard:

- **1Password is the source of truth** (the house pattern; `op` is already the auth path for
  echo/ace/chrome-sales).
- **Each agent/provider repo declares a tracked `config/secrets.yaml`** — *no secret values*, only
  1Password references + local targets (`{repo}/…`, `~/…`, or repo-relative; default mode `0600`;
  `optional: true` to skip-not-fail).
- **`canopy provision`** materializes them via `op` — idempotent, validated, one command on any
  machine (`--check` to dry-run). Anyone with vault access + the right grants is set up instantly.
- **Worktree-clean (§4d):** targets that an agent reads globally point at `~/.canopy`/`~/.claude/canopy`;
  provider-local creds (e.g. chrome-sales's MCP keys) point at `{repo}/…` in the provider's main
  checkout, where its launcher resolves them. Never a gitignored repo `.env` a worktree lacks.

Backed by `src/orchestrator/provision.py` + `canopy provision`. First adopter: chrome-sales
(`config/secrets.yaml` → `.gws-sa-key.json` + `.sf-creds.json`).

### Service-account lifecycle (Phase 2 — design)
Provisioning (above) is the *distribution* layer; it assumes the secret already exists in 1Password.
The *creation + sharing* of service accounts is the remaining manual bit, automatable as:
1. **Create** — `gcloud iam service-accounts create <name>` + `keys create` → a JSON key (needs a
   GCP project + gcloud authed). One SA per capability domain (e.g. an agents-gdrive SA), not per agent.
2. **Store** — `op item create` the key into the AI-Agents vault under the name the manifest expects,
   so `canopy provision` distributes it everywhere.
3. **Share** — grant the SA's `client_email` access to each target resource (a sheet, a Drive
   folder). Scriptable via the Drive API / the gdrive MCP's permission tools; the SA then acts as
   itself, visible to humans (e.g. Beth) as a named collaborator.

A `canopy sa-setup` could wrap steps 1–2 (and emit the email + a share command for step 3). Until
then: create/own the SA once, `op item create` it, and every machine gets it via `canopy provision`.

## 5. Sequencing & open questions

**Sequence:** doc (this) → Build 1 factory → use the factory to refactor echo onto the shared
substrate (proving the extraction is real) → Build 2 loop → Build 3 falls out of Build 2.

**Open questions to resolve before/while building:**
1. **Register agent plugins for Skill-tool dispatch?** (Stronger "force skills," but adds a
   registration step per agent. Recommendation: yes — pair it with hooks.)
2. ~~**Where does the shared substrate live?**~~ **Resolved (§4a):** versioned kit for logic +
   copied/sync-refreshed templates for the editable agent surface.
3. **How much of `manager-sync`/grading moves into canopy** vs. stays per-agent.
4. **Multi-human triggering** — the canopy-web board already lets others queue turns; that is
   the sanctioned path for "others could trigger turns too" (multi-operator = more people run
   the same repo, per §4a). Open sub-question: do we want **per-operator scoping/attribution**
   on the board (who queued / who approved a send), or is one shared agent identity enough?

---

## 6. Best practices from the field (OpenClaw + other harnesses)

> **Provenance caveat (the same one echo's doc carries).** This synthesizes a deep-research
> run over 9 sources (8 primary: Anthropic hooks/SDK docs, OpenAI Agents SDK, LangGraph,
> the Zep arXiv paper, an agent-memory survey, Neo4j/Graphiti). The run's automated
> verification stage was **rate-limited mid-flight**, so every claim's three verifiers
> *abstained* (`0-0`) and the harness auto-killed all 25 — exactly the tooling failure echo's
> `agentic-frameworks.md` hit. The killed list is therefore "unverified," **not** "refuted."
> I've kept claims I can corroborate against the primary docs directly, flagged vendor-reported
> numbers ⚠, and marked framing-only sources. The web search did **not** surface verified
> OpenClaw-specific detail, so §6.1 leans on the firsthand reef evidence instead.
>
> *Redo status (2026-06-20):* re-run. Attempts 1-2 were throttled at the fetch layer (0 sources);
> attempt 3 **fetched cleanly** — a richer, 11-source all-primary harvest (25 claims) — but the
> *verification* stage throttled again (same 0-0 abstain). So we now have better-cited material
> (folded into §6.8), still machine-unverified. The verifier keeps tripping the limit because it
> fires ~75 calls at once; a future re-run should throttle the verify fan-out.

### 6.1 OpenClaw — what it is, and why it's opaque (firsthand, via reef)

From reef's integration code, an OpenClaw instance is a per-droplet agent farm with a
file-on-disk operating model strikingly close to echo's:

```
~/.openclaw/
  agents/<id>/agent/{auth-profiles.json, workspace/}
  workspace/
    SOUL.md, IDENTITY.md         # persona (cf. echo's persona)
    TOOLS.md, HEARTBEAT.md       # config / cadence
    BOOTSTRAP.md, MEMORY.md      # setup + memory index
    skills/<name>/SKILL.md       # capability unit (same primitive as us)
    memory/*.md                  # episodic memory as markdown
  settings.json, channels.json   # bindings + Telegram/Discord tokens
```

A "turn" is driven by `openclaw agent --agent <id> -m "<msg>" --json` (gateway HTTP API, with
an embedded-runtime fallback); channel messages route via a static `bindings` array
(`{match:{channel,accountId}, agentId}`). **Takeaways:**

- **OpenClaw already converged on the same two primitives we did** — a persona file + a
  `skills/<name>/SKILL.md` library. This validates the model. The difference is *control*:
  OpenClaw runs always-on on a remote droplet behind an HTTP gateway, so you observe it through
  a console (reef) rather than *drive and shape* it the way Claude Code lets you.
- **Why it's opaque (your complaint, now concrete):** the turn loop, the gating, and the
  self-improvement step are *inside* a running process you reach only over SSH/HTTP. There's no
  re-readable checklist you control, no hook you can drop in to make a wrong action impossible,
  and (per reef) no working hygiene/self-repair path. You can watch but not reach in. **Our
  model inverts this:** the harness (Claude Code) is the thing *you* hold, every invariant is a
  hook you author, and improvement happens from the outside via canopy.
- **The self-improvement gap is structural, not a missing feature.** OpenClaw stored memory as
  markdown and skills as files but had no mechanism to *spread* a skill across instances,
  *trigger* its use, or *measure* its effect — the same three holes reef inherited (§2).

### 6.2 Skills/tools as the unit of capability — and of collective learning

- **Skills are portable across runtimes; MCP is the integration boundary.** A `SKILL.md` +
  bundled scripts runs unchanged across Claude.ai, Claude Code, and the Agent SDK; the Agent
  SDK uses MCP as its standard tool layer. *Design implication:* keep capability in MCP/CLI and
  skills declarative (echo already does this) so the fleet ports to the Agent SDK later without
  a rewrite. [Anthropic Agent Skills / Agent SDK docs — primary]
- **The Voyager lesson: a skill library is how an agent compounds.** Voyager's central result
  is that an agent that *writes successful behaviors into a retrievable skill library and reuses
  them* outperforms one that re-derives each time — skills are the substrate of lifelong
  learning, and they transfer to fresh agents. *This is the academic backing for echo's Step-4
  ("did I repeat work that should be a skill?") and for canopy treating skills as the unit it
  spreads.* [Voyager, arXiv 2305.16291 — from training knowledge; not in this run's verified set]
- **Reef's own conclusion independently matches Voyager:** "skills are the unit of collective
  learning, NOT memories." Two independent paths to the same rule strengthens §3's stance.

### 6.3 Self-improvement loops — reflection, and the evaluator–optimizer pattern

- **Reflexion: agents that turn failure into a written lesson improve on retry.** The pattern —
  *act → evaluate → write a natural-language reflection on what went wrong → retry with that
  reflection in context* — is the canonical self-improvement loop. Canopy's analyze→propose
  loop is the *fleet-scale, persisted* version of this: the "reflection" becomes a durable skill
  or hook edit shipped via PR, not just an in-context note. [Reflexion, arXiv 2303.11366 —
  training knowledge]
- **Evaluator–optimizer is a first-class agent workflow.** Anthropic's "Building Effective
  Agents" names *evaluator–optimizer* (one model proposes, another critiques against a rubric,
  loop) and *orchestrator–workers* as core patterns. Canopy's proposer + the verify/track stages
  are an evaluator–optimizer; echo's `self-review`/`manager-sync` are a per-agent one. *Design
  implication:* make the agent-turn lens (Build 2) an explicit evaluator–optimizer — propose a
  fix, score it against the friction it targets, only then ship. [Anthropic, Building Effective
  Agents — primary]
- **Keep the loop's output durable and enforceable.** The difference between reef (failed) and
  canopy (works) is that canopy's improvements land as committed skills/hooks, not as ephemeral
  agent state. A reflection that isn't written to a file the next turn re-reads is lost.

### 6.4 Memory — when it actually matters

- **"Agent memory" is not RAG, and treating it as static document retrieval is the trap.** A
  2026 agent-memory survey (arXiv 2512.13564) argues agent memory must be delineated from
  LLM/parametric memory, RAG, and context-engineering — and analyzed by *Forms / Functions
  (factual·experiential·working) / Dynamics (formation·evolution·retrieval)* rather than just
  long-vs-short-term. *Useful framing: the only memory echo actually needs is **factual**
  (per-contact facts) + **experiential** (what happened in past threads). Both are narrow.*
  [arXiv 2512.13564 — primary, unverified-this-run]
- **Temporal knowledge graphs are the right shape *if* you do invest in memory.** Graphiti is a
  real-time, incrementally-updated KG with **bi-temporal** edges (each fact carries valid/invalid
  intervals), so stale facts get invalidated instead of accumulating contradictions — unlike
  batch-recompute GraphRAG. gbrain already gives you this shape. [Neo4j/Graphiti blog —
  secondary; bi-temporal modeling corroborated]
- **Zep's benchmark numbers are vendor-reported ⚠.** DMR 94.8% vs MemGPT 93.4%; LongMemEval
  +18.5% accuracy / −90% latency vs full-context. Directionally credible, treat as vendor
  marketing, not independent. [Zep, arXiv 2501.13956 — vendor-reported]
- **Net for us (reinforces §3):** memory pays off specifically for *cross-session synthesis*
  and *per-entity isolation* — echo's per-contact partition. It does **not** substitute for
  skills as the learning mechanism. Don't build it as fleet infra until a non-empty inbox makes
  the per-contact case real.

### 6.5 Fleet orchestration — and the "passive observatory" trap reef fell into

- **Orchestrator–workers is the documented multi-agent shape** (a lead agent decomposes and
  dispatches to workers), and it's what canopy's agent-dispatch already does for implementation.
  *Don't* reach for an always-on multi-instance farm (OpenClaw/reef) — that's the topology that
  burned you. [Anthropic, Building Effective Agents / multi-agent research — primary]
- **The failure mode has a name now: observability without actuation.** Reef could *see* "skills
  to spread" but had no transfer/execute/measure operators. The field framing: a console that
  surfaces signals but can't act on them is a dashboard, not a control system. Canopy must own
  all three verbs (spread, execute, measure) or it becomes reef. This is the single most
  important lesson to carry forward.

### 6.6 Human-in-the-loop & invariants-as-enforcement (the strongest convergence)

Every harness studied implements the *same* split echo arrived at — reads-free, writes-gated,
with an out-of-band approval — but with progressively stronger enforcement primitives:

- **Claude Code / Agent SDK — the model we're already on.** Permissions resolve in a fixed
  pipeline — *hooks → deny → ask → permission-mode → allow*, falling through to a `canUseTool`
  callback. Crucially, **hooks and deny rules override even `bypassPermissions`** — i.e.
  *invariants-as-hooks supersede invariants-as-prompt/mode*, in the vendor's own words. A
  `PreToolUse` hook fires before any tool call, can **block by exiting code 2** (stderr is
  returned to Claude as feedback), or emit a structured `permissionDecision: deny|allow|ask`
  to escalate to a human. Plan mode is literally "reads run, writes route through `canUseTool`."
  *This is exactly echo's `block_raw_gog_send.py` pattern, and it is the platform-blessed way to
  "force" behavior.* [code.claude.com hooks-guide + agent-sdk/permissions, claude.com steering
  blog — primary]
- **OpenAI Agents SDK** — `needs_approval` (bool or per-call callable) pauses the run;
  `RunResult.interruptions` carries `ToolApprovalItem{agent, tool, args}`; resume via
  `state.approve()/reject()` + `Runner.run(agent, state)`. [openai.github.io — primary]
- **LangGraph** — `interrupt()` pauses a node (modeled on `input()`), persisted by a
  first-class checkpointer; four canonical HITL patterns (approve/reject, review-and-edit
  state, review/edit tool calls, multi-turn). The durable-resume story is why echo's doc tagged
  LangGraph as the likely *future* runtime. [langchain.com — primary]
- **Progressive autonomy is a ladder, and risk-tiering is how you climb it.** A clean staging
  model (framing-only source ⚠): Level 0 = approve every action → Level 3 = independent within
  scope, anomaly-alert only; with actions risk-tiered into *auto (reads/internal drafts/logs)* /
  *log-and-alert (external comms, CRM/API writes)* / *approval-required (bulk comms, deletes,
  financial)*. This operationalizes echo's autonomy ladder (§4 of its own doc) and tells the
  factory **what to template per action class**. [mindstudio blog — secondary/framing]

**The convergence, stated plainly:** independent ecosystems (Anthropic, OpenAI, LangChain) all
landed on *read-free / write-gated + an approval interrupt + deterministic enforcement that
outranks the prompt*. Echo got there first by instinct. The factory should bake this in as the
default, with hooks (not prose) as the enforcement layer — because that is the one thing the
prompt can't be trusted to do under load, and the one thing OpenClaw couldn't give you at all.

### 6.7 What this changes in the recommendation

Nothing reverses; three things sharpen:
1. **Build 1 (factory)** should template a **per-action-class gating table** (auto / alert /
   approve) and ship each agent with a `PreToolUse` hook skeleton — not just a guardrail in
   prose. (§6.6)
2. **Build 2 (canopy loop)** should be framed explicitly as an **evaluator–optimizer**: propose
   → score against the targeted friction → ship. And it must own all three fleet verbs —
   spread, execute, measure — or it regresses to reef. (§6.3, §6.5)
3. **Build 3 / memory** is unchanged and now better-grounded: skills are the learning unit
   (Voyager + reef agree); memory is narrow (per-contact factual + experiential), temporal-graph
   shaped if/when built. (§6.2, §6.4)

### 6.8 Re-run corroboration (11 primary sources, cited)

A second research pass (2026-06-20) pulled a richer, fully-primary harvest — 11 sources, 25
claims. Verification was throttled *again* (same 0-0 abstain failure), so these are
unverified-not-refuted, but every one is corroborable against the primary doc named. Three of
them are sharp enough to change how we talk about the system:

- **The unit of self-improvement is the *scaffolding*, not the model.** STOP (Zelikman et al.,
  arXiv `2310.02304`) shows a *fixed* LLM writing code that recursively improves its own
  *scaffolding program* — and is explicit that this is **not** model-weight self-improvement.
  This is the academic spine of our whole thesis: canopy improves skills/hooks/CLAUDE (the
  scaffold), never the model. Build 2 is a scaffolding-improvement loop, which is exactly the
  tractable, proven layer to operate at.
- **Today's best-practice self-improvement is *human-prompted capture*, with agent self-authoring
  as the stated *future*.** Anthropic's Agent Skills engineering post frames agents
  authoring/editing/evaluating their own skills as a future direction; *today* it's
  human-prompted capture of successes and mistakes into a skill. So canopy's loop (surface
  friction → human-gated authoring of a skill/hook fix → ship) isn't a stopgap — it's the
  current frontier done systematically. Keeping a human/gate in the loop is correct, not a
  limitation. [anthropic.com/engineering — primary]
- **A textual-gradient optimization loop measurably works.** APO/ProTeGi (arXiv `2305.03495`)
  improves a prompt by up to **+31%** using natural-language "gradients" (critiques) + beam
  search — no weight access. It's the prompt-layer analog of what Build 2 does to skills/hooks,
  and it quantifies that an automated critique→edit loop yields real gains. [arXiv — primary]

The rest reinforce existing subsections, now with citations:

- **Skills as the capability unit (§6.2)** — a skill is a `SKILL.md` folder (name+description
  frontmatter + bundled scripts); **three-level progressive disclosure** (metadata always
  loaded → SKILL.md on demand → bundled files by name); **model-discovered/auto-invoked**; and
  **portable across surfaces** (one format → Claude apps, Claude Code, API/Agent SDK) — which is
  literally the "spread a capability across the fleet" mechanism. Voyager (arXiv `2305.16291`)
  adds: skills are executable code in an ever-growing library, **compositional and compounding**.
  [anthropic.com/engineering, claude.com/blog/skills, arXiv — primary]
- **MCP is the integration/portability boundary (§5, §6.2)** — open standard; three capability
  units (data / tools / workflows); transports (stdio, streamable HTTP, SSE, hosted). Confirms
  "capability in MCP/CLI, skills orchestrate." [modelcontextprotocol.io — primary]
- **Self-improvement via verbal feedback (§6.3)** — Reflexion (arXiv `2303.11366`): convert task
  feedback into self-reflective text in an **episodic memory buffer** that guides the next
  attempt; no fine-tuning. The in-loop analog of canopy persisting a fix as a durable skill/hook.
- **Memory architecture (§6.4)** — Generative Agents (arXiv `2304.03442`): memory stream (full
  NL record) + **reflection** (synthesize memories into higher-level inferences) + retrieval.
  Note the *reflection* step — even memory-heavy designs convert raw episodes into higher-level
  learnings, which in our model become skills, not more memories.
- **Fleet orchestration + gating (§6.5, §6.6)** — Anthropic's multi-agent research system
  (orchestrator-workers at scale); OpenAI Agents SDK: handoffs *vs.* manager/supervisor
  orchestration, guardrails that run **in parallel and fail fast**, and `require_approval`
  (`always`/`never`/bool/per-tool) — invariants-as-config, the same shape as our gating.json.
  [anthropic.com/engineering, openai.github.io — primary]

**Net:** the re-run didn't change a single recommendation — it *cited* them. The strongest
addition is STOP + Anthropic Skills jointly: **improve the scaffolding (skills/hooks), human-gate
the authoring today, and that is the actual frontier — not a compromise.**

---

## Appendix — evidence base

- **echo**: `CLAUDE.md`, `docs/research/agentic-frameworks.md`, `skills/{turn,inbox-triage,
  contact-memory,canopy-publish,task-tracker,self-review,manager-sync}/SKILL.md`,
  `scripts/hooks/block_raw_gog_send.py`, `bin/*.py`; analysis of the most recent turn
  transcript (setup-heavy turn: 13 PRs, OAuth/1Password friction, zero gbrain calls).
- **reef**: `lib/openclaw.ts` (hygiene-check `TODO`), `lib/insights.ts`, `lib/report-html.ts`
  (renderLearningOpportunities — observe-only), `.claude/pm/{context,learnings}.md` and
  `runs/*` (transfer-skill backlogged; "skills are the unit of collective learning").
- **gbrain**: gstack `setup-gbrain`/`sync-gbrain` skills, `USING_GBRAIN_WITH_GSTACK.md`
  (per-remote trust policy), echo `contact-memory/SKILL.md`; confirmed not installed/populated
  on this machine.
- **canopy**: `src/orchestrator/{pipeline,analyzer,proposer,tracker,observations,proposals}.py`,
  `hooks/post_tool_use.py`, canopy-web `/agents` + `/insights` surfaces.
- **deep-research run** (`wf_1c88e6e6-2ab`, 9 sources, 43 claims; verification rate-limited —
  claims unverified-not-refuted, see §6 caveat):
  - Anthropic — Claude Code *hooks-guide* & Agent SDK *permissions* (`code.claude.com`) — primary
  - Anthropic — *Steering Claude Code: skills, hooks, rules, subagents* (`claude.com/blog`) — primary
  - OpenAI — *Agents SDK: human-in-the-loop* (`openai.github.io`) — primary
  - LangChain — *HITL agents with interrupt()* (`langchain.com/blog`) — primary
  - Zep — *temporal KG agent memory* (arXiv `2501.13956`) — primary, benchmarks vendor-reported ⚠
  - *Agent memory survey* (arXiv `2512.13564`) — primary
  - Neo4j — *Graphiti knowledge-graph memory* — secondary
  - MindStudio — *progressive autonomy* — blog/framing ⚠
  - *From training knowledge (not in run):* Anthropic *Building Effective Agents*.
- **deep-research re-run** (`wf_096855c9-527`, 2026-06-20, 11 primary sources, 25 claims;
  verification throttled — see §6.8 caveat): Anthropic *Agent Skills* (engineering) + *Skills*
  (blog); *Model Context Protocol* intro; OpenAI *Agents SDK* + its *MCP* guide; Reflexion
  (arXiv `2303.11366`); Voyager (arXiv `2305.16291`); APO/ProTeGi (arXiv `2305.03495`); STOP
  (arXiv `2310.02304`); Generative Agents (arXiv `2304.03442`); Anthropic *multi-agent research
  system* — all primary.
