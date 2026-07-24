---
name: product-management
description: Use when acting as a product manager for autonomous development — exploring a codebase, proposing improvements, implementing, and learning from outcomes. Invoked by open claws or humans who want structured PM-style development cycles.
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])" 2>/dev/null)"
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
```

If output shows `UPGRADE_AVAILABLE <old> <new>`: tell the user "canopy **v{new}** is available (you're on v{old}). Run `/canopy:update` to upgrade." Then continue with the skill — do not block on the upgrade.

# Product Management — Autonomous Product Development

## Purpose

Act as PM + engineer for the current project: explore the codebase, propose improvements, implement approved changes, and learn from outcomes. The meta-goal is **getting better at getting better** — improving this process itself over time.

## Architecture

```
Supervisor (manager)                Claude Code (PM/engineer)
─────────────────                   ────────────────────────
Sets priorities & context     →     Explores codebase (read-only first)
Filters & ranks proposals     ←     Proposes improvements with rationale
Approves / redirects          →     Implements on branch + tests
Validates & reports           ←     Reports results
Logs outcomes & learns
```

### Why This Structure
- **Supervisor stays responsive** — never blocked by long Claude runs
- **Claude gets focused context** — one project, one task, clear acceptance criteria
- **Learning accumulates** in files that persist across sessions

## Two modes

This skill has two operating modes. The phases below describe the **human-gated** mode in detail — that is the original and default behavior.

- **Human-gated** (the Phase 0–6 procedure below). Entry point: `/canopy:pm-scout`. Phase 3 stops on `AskUserQuestion` for per-proposal disposition. Single sprint, exits when dispositions are recorded. **Unchanged.**
- **Autonomous.** Entry points: `/canopy:pm-autonomous` (one sprint) and `/canopy:pm-autonomous-loop` (sprint → wait → repeat). Auto-approves its own proposals. Runs a multi-layer convince-self-it's-clean gate, auto-merges on green CI, auto-deploys, and ends each sprint by sending a working-backwards release-notes email. Requires `$CANOPY_PM_DIR/autonomous.yaml`. See **Autonomous mode** below.

When in doubt, the human-gated mode is the right default. Autonomous mode is opt-in per project via the config file.

## Project State Convention

All project-level PM state lives at `<repo>/.canopy/pm/` — committed to the project's git repo so it's portable across machines and accounts. This shares the `.canopy/` namespace established by PR #37 (`<repo>/.canopy/lenses/`, `<repo>/.canopy/run-artifacts.yaml`, etc.).

```
<repo>/.canopy/pm/
├── context.md          ← what this project is, who uses it, what matters
├── learnings.md        ← project-specific learnings ("don't propose X again")
├── autonomous.yaml     ← autonomous-mode config (auto-bootstrapped on first run)
└── runs/               ← cycle logs (one per run)
    └── YYYY-MM-DD-<lens>.md
```

**Resolving the path** — call the resolver script once at the start of each run and capture its stdout:

```bash
PLUGIN_PATH=$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")
CANOPY_PM_DIR=$(bash "$PLUGIN_PATH/skills/product-management/scripts/resolve_pm_dir.sh")
```

Inside a git repo, this returns `<repo-root>/.canopy/pm` (created if missing). On the rare case of running outside a git repo, it falls back to `$HOME/.canopy/pm/<basename-of-cwd>/`.

**Auto-migration:** the resolver also performs a one-shot, idempotent migration from the legacy `~/.canopy/pm/<project>/` location. The first time PM runs in a project after this change, if `<repo>/.canopy/pm/` is empty AND a legacy directory exists, the resolver copies the files in, commits them on the current branch (`chore(canopy-pm): migrate state from ~/.canopy/pm/<project>/`), and writes a `.migrated` marker into the old location. Subsequent runs are no-ops. The user can delete `~/.canopy/pm/<project>/` whenever — nothing reads it after migration.

**Committing ongoing writes:** in autonomous mode, `.canopy/pm/` updates ride along with the cycle's PR commits, so they land on `main` when the PR merges. In interactive `/canopy:pm-scout` mode, treat `.canopy/pm/` updates like any other working-tree change — review with `git status` and commit alongside (or separately from) your feature work.

**Every run:** Read `context.md` and `learnings.md` before doing anything else. These are your memory.

## Bootstrapping: Building context.md

If `$CANOPY_PM_DIR/context.md` doesn't exist, build it interactively before doing anything else.

**Run all bootstrap steps sequentially.** Do not issue parallel tool calls during bootstrap — `$CANOPY_PM_DIR` may not exist yet, so any parallel call that touches it will fail and cancel its siblings.

### Step 1: Gather what you can automatically

Read these silently (don't dump them to the user). Issue them as **sequential** Read/Bash calls, not parallel:
- `CLAUDE.md` and `README.md` for project identity
- `package.json`, `pyproject.toml`, or equivalent for tech stack
- `git log --oneline -10` for recent activity
- Directory structure (top 2 levels) for shape of the codebase

### Step 2: Ask the user focused questions

Ask these **one at a time**, using what you learned in Step 1 to make them specific:

1. **"What does this project do in one sentence?"** — You'll have a guess from the README. Offer it and ask them to correct or confirm. Don't ask if the README already says it clearly.

2. **"Who uses this and how?"** — Ask about the actual users and their workflow. This is the most important question. Push for specifics: job role, how often, what they're trying to accomplish. Don't accept vague answers like "developers" — ask "what kind of developers, doing what?"

3. **"What matters most for this product right now?"** — Give 2-3 options based on what you've seen (e.g., "reliability for existing users" vs "new features to drive adoption" vs "reducing technical debt to move faster"). Let them pick or reframe.

4. **"Anything I should know about how this project works that isn't obvious from the code?"** — Open-ended. Captures tribal knowledge: deployment quirks, known gotchas, political constraints, integration dependencies.

Skip any question where the answer is already clear from the code. Don't ask 4 questions if 2 will do.

### Step 3: Write context.md

Write a short, dense file. Target: **under 40 lines**. Use this structure:

```markdown
# <Project Name> — Product Context

## What It Is
One sentence.

## Who Uses It
- **Primary users**: role, frequency, what they're trying to do
- **Usage pattern**: how they actually interact with it (ad-hoc, batch, continuous, etc.)

## What Matters Most
Numbered list, max 3 items. Each one sentence.

## Tech Stack
Bullet list of key technologies. Only what's relevant to making good proposals.

## Current State
2-3 sentences: what's working, what's active, what's rough.

## Known Considerations
Bullet list of non-obvious things: gotchas, constraints, political context, integration dependencies.
```

### Step 4: Confirm

Show the user the generated `context.md` and ask: "Does this capture your project accurately? Anything to add or fix?" Edit based on their feedback, then save.

Also create `learnings.md`. If `$CANOPY_PM_DIR/runs/` already exists with previous run logs, parse them for any closed/rejected items and pre-populate the "Closed Items" section. Otherwise start empty:

```markdown
# Product Management Learnings

Items closed or rejected during PM cycles. Read this before every scout run to avoid re-proposing.

## Closed Items
(none yet)

## Preferences
(none yet)
```

### Headless bootstrap (no user available)

When PM runs **without an interactive user** — an agent's email-driven turn, an autonomous sprint on
a repo with no PM state, any context where the Step 2 questions cannot be asked — do NOT skip state
creation, and do NOT block on the Q&A. Auto-derive instead:

1. Build `context.md` from what Step 1 gathers alone (CLAUDE.md, README, manifest, git log, tree).
   Fill every section with your best inference; where the interactive questions would have added
   signal you don't have (Who Uses It, What Matters Most), mark the line with `(inferred)`.
2. Stamp the file header: `<!-- auto-derived YYYY-MM-DD (headless) — unconfirmed by a human -->`.
3. Create `learnings.md` exactly as above (including the runs/ back-parse).
4. **First subsequent interactive run confirms it**: if `context.md` carries the unconfirmed stamp,
   open with the Step 4 confirmation ("here's what I inferred about this project — anything wrong?")
   before scouting, then drop the stamp.

An unconfirmed-but-present `context.md` beats no state: dispositions, learnings, and doc item-maps
(Lesson 21) need a home the first time PM touches a repo, which is often headless. Surfaced
2026-07-09: an email-driven re-evaluation on connect-labs had nowhere to record why items were NOT
proposed because the repo had no PM state and the interactive bootstrap couldn't run.

## The Loop

### Phase 0: Pre-flight (single sequential check, NEVER parallel)

**Run this ONE bash command synchronously before any other tool calls.** Do not parallelize anything until this completes — issuing parallel reads when the directory doesn't exist cancels every sibling call and forces sequential retries.

```bash
PLUGIN_PATH=$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")
CANOPY_PM_DIR=$(bash "$PLUGIN_PATH/skills/product-management/scripts/resolve_pm_dir.sh")
[ -f "$CANOPY_PM_DIR/context.md" ] && echo "PM_STATE: ready" || echo "PM_STATE: missing"
```

Branch on the output:

- **`PM_STATE: missing`** → Run the **Bootstrap** flow (see "Bootstrapping: Building context.md" above) **synchronously**. Do not issue parallel reads. After bootstrap completes, proceed to Phase 1.
- **`PM_STATE: ready`** → Proceed directly to Phase 1.

### Phase 1: Scout (explore, read-only)

**What to do:**
1. Read `$CANOPY_PM_DIR/context.md` for orientation
2. Read `$CANOPY_PM_DIR/learnings.md` for things to avoid
3. Check `git log --oneline -20` for recent momentum
4. Run the test suite — what passes, fails, is missing?
5. Look through open issues / TODO files
6. Explore through a specific lens (see below)

**Exploration Lenses** (use one per run, rotate):
- **User value**: what features would users love? What workflows are clunky?
- **Adoption blockers**: what makes someone stop using this? First-run friction, confusing UX, unreliable behavior
- **Integration depth**: how well does this connect to its ecosystem? Deeper = stickier
- **Trust & reliability**: bugs, wrong answers, silent failures, missing error handling
- **Tech debt**: dead code, flaky tests, missing types, outdated deps
- **UX / responsive / mobile**: does the product actually work at the sizes and on the devices people use it? Layout overflow, tap-target size, controls reachable while interacting, content hidden behind viewport edges. For this lens, **dogfood real device widths in a browser FIRST** (see Lesson 17) — don't theorize from CSS.

**Output format:**
For each finding, provide:
- **Title**: one line
- **What**: specific files/functions affected
- **Why it matters**: impact on users or developers
- **Effort**: S (< 1hr) / M (2-4hr) / L (day+)
- **How to validate**: concrete test or check that proves it's fixed
- **Risk**: what could go wrong

**Critical rules:**
- Check what already exists before proposing additions. Verify current state.
- Don't suggest vague refactors or "add more tests everywhere." Be specific.
- For bugs or broken behavior: try to write a failing test. If you can't, explain why.
- Check `$CANOPY_PM_DIR/learnings.md` — do NOT re-propose closed or rejected items.

### Phase 2: Propose (supervisor filters & ranks)

Supervisor reviews findings and:
1. Filters out low-value or risky items
2. Ranks by: user impact x feasibility x alignment with priorities
3. Picks top 3 to present

**Presentation format:**
> **Title** (Effort: S/M/L)
> What: one sentence
> Why: one sentence on impact
> Validate: how we'll know it works

### Phase 3: Approve (interactive menu)

Present proposals using `AskUserQuestion` so the user can give per-item dispositions through a structured menu rather than free-form chat. Each proposal gets its own question with four options:

- **Do it** → move to implement now
- **Backlog** → good idea, not now
- **Close** → don't want this, log the reason
- **Redirect** → re-scope it

Use `AskUserQuestion` with up to 4 questions (one per proposal). Each question should include the proposal title, effort, what/why/validate summary in the question text, and use `header` for a short label (e.g. "Skill Transfer"). The four disposition options above are the choices. The user can also select "Other" to provide custom feedback.

Example:
```
AskUserQuestion({
  questions: [
    {
      question: "Proposal 1: Add transfer-skill command (Effort: M)\n\nWhat: ...\nWhy: ...\nValidate: ...\n\nWhat's your disposition?",
      header: "Skill Transfer",
      options: [
        { label: "Do it", description: "Implement this now" },
        { label: "Backlog", description: "Good idea, not now" },
        { label: "Close", description: "Don't want this, won't revisit" },
        { label: "Redirect", description: "Re-scope — I have specific feedback" }
      ],
      multiSelect: false
    },
    // ... one question per proposal, up to 4
  ]
})
```

Track all dispositions in the run log. Closed items go into `learnings.md` so they're never proposed again.

**After dispositions**, ask the user what they'd like to do next:

```
AskUserQuestion({
  questions: [{
    question: "Anything else before we move on?",
    header: "Next step",
    options: [
      { label: "Move on", description: "Proceed to implementation / learning" },
      { label: "Add my own ideas", description: "I have ideas to add to this cycle" },
      { label: "Review backlog", description: "See backlogged items from previous runs — promote or close them" }
    ],
    multiSelect: false
  }]
})
```

- **Add my own ideas**: Capture the user's ideas and apply the same disposition flow (Do it / Backlog / Close). Log user-originated ideas in the run log with a `[user idea]` tag.
- **Review backlog**: Parse all previous run logs in `$CANOPY_PM_DIR/runs/` and collect items marked as "Backlog". Present each backlogged item via `AskUserQuestion` with options: **Promote** (move to "Do it" this cycle), **Keep** (leave in backlog), **Close** (won't do, log reason). This prevents the backlog from becoming a graveyard of forgotten ideas.

This keeps the scout session self-contained — the user doesn't need to break out of the cycle to contribute thoughts or revisit past decisions.

### Phase 4: Implement

**Key principles:**
- Give Claude a way to verify its work — always include test commands or expected output
- Always branch + PR: `<prefix>/<short-slug>`, never commit to main
- Claude must run full validation (lint + build + tests) and report output
- Commit with clear message, push and create PR

**Implementation prompt includes:**
- What to build (specific files, functions, behavior)
- Acceptance criteria (tests to pass, behavior to verify)
- Constraints (don't change X, follow pattern in Y)
- Verification command

**If validation fails:**
- Fix the issues and re-run validation
- If you can't fix after 2 attempts, report what's failing and stop — don't keep thrashing
- Run `/simplify` after implementation to catch code reuse, quality, and efficiency issues

### Phase 5: Validate & Ship

- Tests pass, build succeeds, no regressions
- Create branch, commit, push, create PR
- If code review bots are active: wait for review, address comments
- Verify CI passes
- Merge via squash

### Phase 6: Learn

After each cycle, do two things:

**1. Update project state:**

Write run log to `$CANOPY_PM_DIR/runs/YYYY-MM-DD-<lens>.md`:

```markdown
## YYYY-MM-DD — <lens>

### Do it
1. **Title** — Effort: S — Status: pending/done/merged
   - Branch: prefix/slug
   - Outcome: ...

### Backlog
1. **Title** — Effort: M — Why not now: "..."

### Closed
1. **Title** — Why: "user said..."
   - Learning: don't propose X-type things

### Meta-observations
- What worked well
- What was wasteful
- Prompt adjustments for next time
```

Update `$CANOPY_PM_DIR/learnings.md` with any new closed items or preferences.

**2. Evaluate for universal improvements** (see Self-Improvement Protocol below).

## Autonomous mode

The procedure for autonomous sprints lives in template files. Read them in order at the start of every autonomous run:

1. `templates/autonomous/config-schema.md` — `$CANOPY_PM_DIR/autonomous.yaml` schema and example
2. `templates/autonomous/cycle.md` — Phases A–E (the working-backwards sprint)
3. `templates/autonomous/convince-self-gate.md` — the multi-layer gate that runs before every PR
4. `templates/autonomous/email-format.md` — body template for the working-backwards release-notes email

These templates are read using the Read tool from the cached plugin path:

```bash
PLUGIN_PATH=$(python3 -c "import json; d=json.load(open('$HOME/.claude/plugins/installed_plugins.json')); print(d['plugins']['canopy@canopy'][0]['installPath'])")
ls "$PLUGIN_PATH/skills/product-management/templates/autonomous/"
```

The autonomous mode does NOT modify the human-gated Phase 0–6 procedure above. `/canopy:pm-scout` still runs the human-gated path verbatim.

### Hard rules for autonomous mode

1. **No proposal advances without passing the convince-self gate.** Mechanical checks, five self-review questions, dogfood (when applicable), post-deploy health.
2. **No weak emails.** Phase A loops until the email draft passes Clear/Testable/Impressive. Phase D refuses to send if reality diverged into something not worth sending — it sends a stuck-state note instead.
3. **Emails ship as HTML with prod screenshots, clickable feature elements, and a render-and-look pass before AND after send.** The release-notes email is the only customer-facing output of the cycle — visual quality matters and is part of user delight. The hard contract: body must be HTML (sender skill's `--body-html` or equivalent), screenshots must come from prod (not localhost), inline images must use persistent https URLs (e.g. `raw.githubusercontent.com` against a `pm-assets/<sprint-slug>` branch on the project's repo — `cid:` and data URIs don't render reliably in Gmail), every highlight's title AND hero image must wrap in `<a href="<TRY-IT-URL>">` so recipients can click anywhere intuitive, AND the cycle MUST render the final `email.html` in a real browser at desktop + mobile widths before sending (E.4 gate) and again after sending to write a self-critique with concrete improvement ideas (E.5). See `templates/autonomous/email-format.md` "Hard rules" + reference layout + "Self-review" section.
4. **One autonomous PR in flight at a time.** Resume an open one before opening a new one.
5. **No auto-revert on broken prod.** Fix forward, up to `guardrails.max_fix_forward_attempts` cycles, then stop with a stuck-state email.
6. **The skill stays project-agnostic.** Every project-specific value (deploy command, health URLs, sender skill, branch prefix, test commands) lives in `<repo>/.canopy/pm/autonomous.yaml`. Never hardcode them in SKILL.md or templates.

## Self-Improvement Protocol

After completing a cycle, evaluate your meta-observations:

### Is this learning project-specific?
Examples: "don't propose keyboard shortcuts for this project", "this repo uses pytest not jest"

→ Write to `$CANOPY_PM_DIR/learnings.md`. Done.

### Is this learning universal?
Examples: "Claude over-engineers when not told to check existing functionality", "always verify current state before proposing additions"

→ Propose a PR to `jjackson/canopy` (NOT the current project repo):

1. Clone `jjackson/canopy` to a temp directory (or use an existing clone)
2. Create branch: `learn/<short-description>`
3. Edit: `plugins/canopy/skills/product-management/SKILL.md`
3. Make the specific improvement (new lesson, tightened instruction, revised template)
4. Never delete existing lessons — refine or append
5. Open PR with:
   - **What was learned**: the universal insight
   - **Evidence**: which project/run surfaced it
   - **Change**: before/after of the affected section

The PR will be reviewed before merging. This is intentional — unchecked self-modification can degrade the skill.

## Lessons Learned

1. **Don't assume usage patterns** — ask about how people actually use the product before theorizing. Verify with the user.
2. **Claude over-engineers solutions** — check what already exists before proposing new infrastructure. The field/API/feature may already be there.
3. **Speculation without evidence has low hit rate** — when proposing from code structure alone without testing, expect most proposals to miss. Write failing tests as proof.
4. **Architectural issues: flag, don't fix** — things that need design direction should be logged, not autonomously implemented.
5. **Product value > engineering elegance** — proposals should lead with user impact, not code cleanliness.
6. **Always git pull before exploring** — stale code = stale proposals.
7. **Feed closed items into future runs** — without this, Claude re-proposes rejected ideas. Always read `learnings.md` first.
8. **Use structured menus for dispositions** — presenting proposals via `AskUserQuestion` with per-item options gives the user precise control and avoids ambiguous bulk chat responses. Each proposal should be independently dispositioned.
9. **For adoption-blockers scouts, diff the template against BOTH the installed copy AND the code tree.** When a project ships a template file (`.env.tpl`, `config.example.yml`, `settings.template.json`, etc.) that the user instantiates into a live copy, adoption-blockers come in two subclasses and you need two diffs to surface both:
   - **Forward diff** — `diff <(keys from installed copy) <(keys from template)`. Catches keys added to the template after the user last instantiated. Classic drift: new release adds a var, existing installs don't auto-pick it up, feature silently breaks.
   - **Inverse sweep** — for each key in the template, `grep -rE "\b$KEY\b"` across the code-bearing dirs (source/, lib/, scripts/, skills/, bin/, hooks/, etc.). Catches keys declared but never read — dead ceremony that the user pastes / injects / configures for no runtime benefit.
   Forward-only scouts miss the second class entirely; most first-run friction lives there (paste-this lines in READMEs for values nothing consumes). Both diffs are one-line commands; run them together in Phase 1 Step 1 of every adoption-blockers scout and carry any hits from either into proposals. Bonus: either class closes nicely with a class-level doctor/health-check (same pattern as any bottleneck pre-flight).
10. **Before shipping a diagnostic check, exercise it against both PASS and FAIL inputs.** When adding a doctor/health-check/CI gate that emits PASS/WARN/FAIL, run it against a known-good state AND a synthetic bad state at authoring time — confirm it passes when it should and fires when it should. Two failure modes to catch:
    - **Check itself is wrong (false positives).** A grep filter that over-excludes, a regex that's too narrow, a comparison against the wrong field. Ships a green check that misses the real problem, or (worse) a noisy check that users learn to ignore.
    - **Remediation hint is unreachable (false fixes).** The `fix:` command the check tells the user to run might not actually work — a command-line flag that doesn't parse, a reference that errors out. Test the hint end-to-end at least once before landing.
    Common pitfall: the check author has a mental model that's out of sync with what the check is actually doing (regex, filter, or path). The mental model says "this will catch X"; the code says "this will catch X minus some edge case Y." Running against a known-good input with a known-good result surfaces the gap before users do. Applies to any diagnostic — doctor scripts, lint rules, CI assertions, test preconditions.
11. **The release-notes email is the only customer-facing output of the autonomous cycle — its visual quality matters.** Five rules surfaced in real use, captured in detail in `templates/autonomous/email-format.md`:
    - **HTML body, never raw markdown.** Mail clients render HTML; markdown handed in renders as literal `##`/`**`/`-` characters and looks amateur. The sender skill must be invoked with `--body-html` (or equivalent) and a brief plain-text fallback for `--body`.
    - **Screenshots from prod, never localhost.** Drive the deployed app via the configured `headless_browser_skill` and authenticate via the project's automation login (e.g. `/auth/e2e-login/`). Localhost shots show port numbers and seeded fake data — recipients spot it and lose trust.
    - **Inline images via persistent https URLs, never `cid:` and never data URIs.** Most CLI mailers (e.g. `gog gmail send`) emit `multipart/mixed` when given attachments, so `cid:` refs in the HTML resolve to broken-image icons. Data URIs are stripped by many clients. Reliable pattern: commit the screenshots to a `pm-assets/<sprint-slug>` branch on the **project's** origin (not canopy's) and reference via `https://raw.githubusercontent.com/<owner>/<repo>/<branch>/<path>` in the `<img src>`.
    - **Every feature highlight is clickable in three places — title, hero image, AND the explicit "Try it" CTA.** A small CTA buried at the bottom of each card is not enough; recipients scan and shouldn't have to hunt for the click target. Wrap each highlight's `<h2>` and `<img>` in `<a href="<TRY-IT-URL>">` (with `text-decoration:none`).
    - **Render-and-look passes BEFORE and AFTER sending.** Phase E.4 (pre-send gate) renders `email.html` via the configured `headless_browser_skill` at desktop + mobile widths and refuses to send if anything looks off. Phase E.5 (post-send critique) writes a self-review into the run log with 2-4 concrete improvement ideas surfaced as the cycle's closing message to the user. Without these passes, visual-quality drift goes unnoticed across cycles.

    Visual design: typographic hierarchy over bordered boxes, restrained palette, hero image per highlight, footer for internal notes — see the canonical layout in `email-format.md`. Reference standard is "Linear / Stripe / Vercel changelog", not "GitHub issue body". Surfaced 2026-04-29 from real PM feedback after the first autonomous run on ace-web.
12. **When a foundational assumption shifts (auth model, tenancy model, identity model, allowed-user filter), sweep test docstrings for admissions of the OLD assumption.** Tests are sticky: once a behavior was deliberately allowed under the old assumption, that behavior often gets pinned into the test suite — sometimes as a positive assertion, sometimes as a comment that openly admits "any authed user can do X". When the assumption changes, those tests become **bug-as-feature pins** that quietly defend the now-incorrect behavior. Concrete trigger: any cycle that lands a multi-tenancy layer, drops an allowed-domain filter, introduces RBAC, or otherwise changes who-can-do-what. Action: `grep -nE "any authed|any auth|all users|all authed|every (user|auth)|cross-tenant|cross-workspace"` across `**/test_*.py` (and equivalents in `*.test.ts(x)`), then re-read each docstring against the current security contract — if the docstring describes a behavior that would now be objectionable, the test is pinning a stale assumption and the *behavior under test*, not just the assertion, needs revisiting. Surfaced across three confirming cases on ace-web 2026-04-28: pin-the-literal default test (adoption-blockers cycle), `test_connect_auto_joins_non_participant`, and `test_messages_list_allows_any_authed_user` (trust-reliability cycle, where this lesson directly led to discovering a cross-workspace session-read leak).
13. **After dropping a foundational invariant, grep the codebase for `for now / deferred / temporary / will reintroduce / future sharing / Dimagi user`-style docstrings — each is a candidate stale-guard bug.** When the surrounding architecture no longer enforces the invariant the docstring was acknowledging, the helper or callsite the docstring decorates becomes a security/correctness hazard. The docstring tells you exactly what's wrong: it's a TODO disguised as a comment, written when the invariant *was* active, never updated when the invariant fell. Concrete trigger: any cycle that drops `@dimagi.com` filter, lifts a single-tenant assumption, generalizes a single-user-only path, or opens a previously-internal surface to third parties. Action: `grep -rnEi "for now|deferred|temporary|will reintroduce|future (sharing|work)|dimagi user|internal (only|use)"` across the source tree (skip `tests/`, `node_modules/`, `vendor/`, `migrations/`). Read each hit against the current invariants. Surfaced 2026-04-28-trust-reliability — the cross-workspace session-read leak was right there in `apps/sessions/consumers.py::_participant_role`'s docstring, untouched since the multi-tenancy layer dropped the assumption it was guarding.
14. **For split-stack autonomous projects (Python + frontend), define `testing.prepare` to bootstrap deps before the gate.** Mechanical-checks (3a) assume `.venv` and `node_modules` are populated, but in worktree-first workflows (emdash, conductor) fresh worktrees start empty and `uv sync --frozen` doesn't install dev deps that aren't in the project's resolved lock. Two confirming cases on ace-web (2026-04-28-adoption-blockers, 2026-04-28-first-chat-path) lost ~10 min each to this. The optional `testing.prepare` config key in `autonomous.yaml`, run once at the top of Phase 0 with a 5-min timeout, closes the gap cleanly without baking project-specific assumptions into the skill. See `templates/autonomous/config-schema.md` for the schema and `templates/autonomous/cycle.md` Phase 0 step 7 for the run.
15. **Phase 0 must guarantee a clean worktree before mechanical checks; `testing.prepare` must be lockfile-non-mutating.** The convince-self gate's mechanical checks (3a) run against the *working tree*, but `git commit` ships whatever is in the *index*. When those diverge — e.g. the user staged file A, then `Edit`'d it, then ran the gate (which sees the working-tree edit) and committed (which ships the older staged version) — the gate certifies one snapshot and the PR ships another. ace-web cycle 1 (2026-05-01) hit this exact bug with a `<Button asChild>` slip: gate green on PR CI, fix-forward needed after deploy caught it.
    The cleanest fix is **upstream**, not a stash dance around the checks: enforce a clean worktree at Phase 0 entry (refuse to start with `git diff --quiet HEAD` non-zero) AND require `testing.prepare` to be lockfile-pinned (`npm ci` not `npm install`, `uv sync --frozen` not `uv sync`, `pnpm install --frozen-lockfile`, etc.) so prepare itself doesn't dirty the worktree. With both preconditions, the staged-vs-working divergence stops being possible — `git commit -a` and `git commit <staged-only>` produce identical content. Surfaced 2026-05-01 across two consecutive ace-web sprints (cycle 1 hit the bug, cycle 2 confirmed the clean-worktree precondition prevents recurrence). Implemented in `templates/autonomous/cycle.md` Phase 0 steps 5 and 7; canonical example in `config-schema.md` updated to use `uv sync --frozen` + `npm ci`.
16. **Dogfood pass runs against deployed prod, never localhost.** The 3c gate previously required spinning up the local stack and clicking through there. Three confirming sprints on ace-web (2026-05-01 cycles 1–3) all hit the same wall — local stack unreachable on the dev's machine for ordinary reasons (docker socket permissions, port 8000 owned by another tenant, no local Postgres binary) — and every cycle defaulted to "skip dogfood, hope the post-deploy screenshot capture works." That worked, but only because the *real* dogfood was the post-deploy capture all along; the localhost step was ceremony in front of the actual proof.
    The fix is structural: the dogfood pass moves from "before opening the PR" to "after deploy" (Phase C step 12 — between 3d health check and Phase E.1), and it drives the `headless_browser_skill` against the deployed app, not localhost. The same click-through that proves the user-visible behavior also captures the email's hero screenshots — `Try it:` dogfood and `email-format.md` Hard-rule-3 prod screenshot are now the **same artifact**. Localhost is rejected with no escape hatch: prod must be reachable for 3d to have just succeeded, so driving it as the automation user is no harder than the health check. If prod is genuinely unreachable, that's a stuck-state, not a "fall back to localhost" decision. The localhost-only fields in `autonomous.yaml` (`testing.dogfood.base_url` / `start_command` / `wait_for`) are deprecated for new projects; `headless_browser_skill` stays. Implemented in `convince-self-gate.md` §3c rewrite + `cycle.md` Phase C step 12 + `cycle.md` Phase 0 step 9 (creates `$EMAIL_WORKDIR` early so 3c can write into it).
17. **When an AI-generation feature "isn't good enough to use," first check whether it hardcodes an assumption about the output's *purpose* — not just whether it lacks context or grounding.** The instinct on a weak generator is to feed it more (more retrieval, more fields, more examples). But the higher-leverage failure is often upstream: the prompt bakes in a single intent the user never gets to set. Concrete trigger: the generator only ever produces one *flavor* of output regardless of the situation (every email reads like a re-engagement note; every summary is an exec brief; every reply is an apology). Tell: the system/template prompt opens with a fixed role+purpose ("You are drafting re-engagement emails…") and the user-facing flow exposes no "what is this *for*?" input. Action — read the actual prompt template before proposing context/grounding work, and look for (a) a hardcoded purpose/intent clause, and (b) the absence of a user-supplied goal field. If both are present, propose a goal/intent input + a purpose-driven prompt rewrite *first*; grounding improvements compound on top of it but rarely fix "not usable" on their own. Surfaced 2026-06-17 on the chrome-sales email generator (user-value lens): the `outreach-draft` prompt was locked to "re-engagement emails," so intros, post-meeting follow-ups, and info-sends all came out as awkward "it's been a while" notes. Letting the user state the goal — and generalizing the prompt — was the change that moved it from "not good enough to send" to usable; voice and deal-context grounding were valuable but secondary. The pattern is provider- and domain-agnostic (any LLM feature that generates NL output for varied situations).
18. **For any UX / responsive / mobile lens, dogfood real device widths in a browser FIRST — then read code only for the confirmed offenders.** This is the visual corollary to Lesson 3 ("speculation without evidence has low hit rate"): for a layout/UX lens, the headless-browser screenshot at a real device width *is* the failing test, and reading CSS is the speculative path. Scouting a kids' game site through an "ease of use on mobile" lens (game-generator, 2026-06-08), code-reading alone surfaced ~4 plausible mobile problems (fixed-px grid cells, number-pad placement, a tall grid pushing the pad below the fold, controls above the puzzle). A 390px dogfood of all 7 screens with `getBoundingClientRect` evidence collapsed that to **one** real failure — a logic-grid matrix whose overflow lived in an inner `overflow:auto` box, so the page didn't scroll and an entire answer column sat hidden with no affordance — and cleared the other six as measurably fine. Acting on the code-read list would have shipped 2–3 fixes to things that already worked. Two durable habits fall out: (a) **a clean negative result is a deliverable** — "6 of 7 screens are mobile-solid, here are the measured tap sizes" is worth recording in `learnings.md` so later scouts don't re-litigate them; and (b) **verify the fix the same way you found the problem** — re-screenshot at the same device width (and a desktop width, to catch responsive regressions) before merging, not just "tests pass." Mechanism: drive the project's `headless_browser_skill` / a `browse`-type tool at explicit widths (e.g. 390×844 phone, 1280×800 desktop); for a static build that can't use `astro preview`/a dev server, serve the build output dir over `python3 -m http.server` and point the browser at it.
19. **When a stakeholder hands you a feature-request doc, code-dive at current `main` and judge each item on strategic ALIGNMENT, not just per-item viability — and read the principal's inline comments as a direction-thesis.** The shallow failure is to treat the doc as the spec: restate its items, filter by which ones have an open comment, and call that a proposal. That skips the two things that actually decide what to build. (a) **Real code dive at current `main` first (Lesson 6), and find the ACTUAL code** — a stale or missing local checkout is not a license to fall back to the doc; the feature may live in a different repo than the obvious one (surfaced 2026-07-08: a "Labs Bulk Image Audit" doc pointed at `commcare-connect/audit`, which is *audit reports*; the real image-review feature was in `connect-labs`, and half the doc's asks were already shipped or stubbed — verifiable only in code). (b) **Alignment is a first-class axis alongside viability.** A requested feature can be perfectly buildable and still be the wrong thing because it fights the architecture's direction. In the same case the principal's inline comments ("audit creation should be workflow-driven", "program-owned workflows already exist", "workflows make session-reuse a non-issue") weren't per-item vetoes — they were a *thesis*, and the code confirmed it: a program-owned, multi-opp workflow creator already existed, the generation layer was deliberately single-fire "no reuse", and runs already auto-saved/resumed. So several "clear wins" were things the architecture already solved or deliberately omitted; building them on the standalone UI would have pushed against the direction. The move: map each ask to EXISTS / PARTIAL / DOES-NOT-EXIST in code (Lesson 2), then to ALIGNED / FIGHTS-DIRECTION against the stated thesis, and only the intersection (outstanding ∧ aligned ∧ unambiguous) is "just tackle" — the rest is a grounded design note or a "this is already handled, here's where" reply. A code-grounded "most of this is already done or handled by X, here are the two real gaps" is a far stronger PM output than a faithfully-restated list.
20. **A shared feature-request doc is a LIVE work queue with humans on it — dedup each candidate against in-flight human work before proposing it.** Lesson 19's map (EXISTS / PARTIAL / MISSING) answers "is it built?"; it does not answer "is someone building it right now?" — and on an actively-worked doc those diverge within days. Concrete trigger: the doc carries strike-throughs, an "(X: I am implementing these and striking off as I deploy)" note, or items resolved by very recent merges. Action, per candidate item before it goes in a proposal: (a) `gh pr list --state all --search "<feature keywords>"` on the target repo and read the last few days of merged titles + authors — a teammate who shipped adjacent items from the same doc TODAY is probably continuing down the list; (b) `git ls-remote --heads origin | grep -iE "<slug words>"` for unmerged branch names; (c) recent commits touching the exact files the item would change. State the dedup result in the proposal ("no open PRs or branches touch X/Y/Z") — it is part of the evidence, and running the check corrects false premises, not just honesty. Surfaced 2026-07-09 on the same Bulk Image Audit doc as Lesson 19: two other developers merged three doc items (delete-UX, pass-threshold, program fan-out) in the 24h before the re-read; proposing without the check would have double-claimed a teammate's active lane.
21. **Re-evaluating a previously-triaged stakeholder doc is a DELTA pass — persist the item map so the re-read starts from the last verdict, not from scratch.** A second pass over the same doc ("re-read it and see what's now obvious") re-derives everything if nothing was persisted: every item re-mapped to code, every discussion re-read, every prior disposition reconstructed from memory or email archaeology. Instead, on the FIRST Lesson-19 pass, write the item map to `$CANOPY_PM_DIR/docs/<doc-slug>.md` in the TARGET repo's PM state: one row per doc item — verbatim ask, EXISTS/PARTIAL/MISSING verdict with file:line evidence, ALIGNED/FIGHTS call, disposition (shipped-in-PR-N / proposed / under-discussion / not-obvious-because), and date. A re-read then diffs three things against that map: new strike-throughs and comments in the doc, commits/PRs merged since the recorded date, and dispositions whose "under discussion" status may have resolved — and only touches code for rows that changed. Bonus: the map is the natural place to record why an item was NOT proposed (content-dependent, recompute semantics, principal's open design question) — exactly what gets re-litigated on every pass when unrecorded. Surfaced 2026-07-09: the second pass over the Bulk Image Audit doc re-derived the full 15-item map from scratch because the first pass (2026-07-08) left no artifact; the actual delta was small (3 items shipped by others, 2 new candidates unlocked).
22. **Write dispositions BACK to the shared stakeholder doc — as `[<Agent>]`-prefixed comments, never body edits — and on an actively-developed surface, propose but don't auto-start.** The doc is the stakeholders' single source of truth (they strike items off in it); status that lives only in an email thread or a repo file is invisible there, so shipped-but-unstruck items get re-litigated. Procedure (principal-approved for the fleet, Jonathan 2026-07-09): (a) post via `gog drive comments create <fileId> "<text>"` — the CLI posts unanchored comments into the doc's comment stream (it does not expose text-anchoring), so ONE consolidated status comment beats N floating ones; (b) prefix the content with the agent's tag, e.g. `[Hal] `, so humans instantly see it's agent-authored; (c) content is factual dispositions only — "shipped in #N", "exists at file:line", "proposed on <thread>, awaiting owner" — never opinions or requests (those belong on the email thread with its approval gate); (d) never edit the doc body — strike-throughs are the stakeholders' gesture, not yours; (e) link the repo item-map (Lesson 21) from the comment so the deep evidence is one hop away. And the companion rule that governs what the comment may SAY: when the dedup check (Lesson 20) shows teammates actively shipping from the same doc, the agent does NOT auto-start even approved-looking items — it proposes on the thread and asks who takes what ("should I take any of these on, or are they in someone's lane?"). Auto-starting into an active human lane converts a helpful agent into a merge-conflict generator; the ask costs one email round-trip and usually gets a cleaner division of labor. Both surfaced 2026-07-09 on the Bulk Image Audit doc (first fleet write-back: comment AAAB_l87s2o).

## Token Efficiency

- **Scout phase**: one call, focused by lens
- **Implement phase**: one call per task, scoped tightly with acceptance criteria
- **Don't explore everything every run**: use one lens, rotate across runs
- **Read context.md and learnings.md upfront** — don't re-discover what's already known
