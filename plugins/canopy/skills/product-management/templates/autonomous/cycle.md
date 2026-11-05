# Autonomous PM cycle (Phases A–E)

This is the procedure executed by `/canopy:pm-autonomous` for one sprint. Read it top-to-bottom before starting; do NOT improvise the order.

The sprint succeeds when a customer-quality release-notes email is sent. It fails (gracefully) when convergence isn't possible — in which case a minimal "stuck" email is sent and the sprint stops.

This skill is project-agnostic. ALL project-specific knobs live in `<repo>/.canopy/pm/autonomous.yaml` — see `config-schema.md`.

## Phase 0 — Pre-flight

Run sequentially, NOT in parallel (`$CANOPY_PM_DIR` may not exist yet on a fresh project):

1. Resolve `$PLUGIN_PATH` and `$CANOPY_PM_DIR` once and reuse:

   ```bash
   PLUGIN_PATH=$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")
   CANOPY_PM_DIR=$(bash "$PLUGIN_PATH/skills/product-management/scripts/resolve_pm_dir.sh")
   ```

2. **If `$CANOPY_PM_DIR/autonomous.yaml` is MISSING, bootstrap it before validating — do NOT ask the user.** This skill defaults to autonomous; the user has already opted in by running this command. Derive defaults from project signals and write the file directly:

   ```bash
   # Derive a display name for subject prefix and branch namespace.
   # This is for cosmetic strings only — NOT used for path resolution.
   PROJECT_NAME=$(git config --get remote.origin.url 2>/dev/null \
     | sed 's|.*[/:]||;s|\.git$||' || true)
   if [ -z "$PROJECT_NAME" ]; then
     PROJECT_NAME=$(git rev-parse --show-toplevel 2>/dev/null || true)
     PROJECT_NAME=$(basename "${PROJECT_NAME:-$(pwd)}")
   fi
   ```

   - `email.to` = `git config user.email`
   - `email.from` = same as `email.to` for v1, EXCEPT: if the detected `email.sender_skill` is a known service-mailbox skill, use that mailbox. Known mapping today: `ace:email-communicator` → `ace@dimagi-ai.com`. When applying a service mapping, also print a single extra line: `email.from defaulted to <service-address> based on sender skill <skill>; edit autonomous.yaml if wrong.` This avoids the post-bootstrap manual fixup that was needed on ace-web.
   - `email.subject_prefix` = `[$PROJECT_NAME]` (origin URL → repo basename fallback; cosmetic only — path resolution is handled by `resolve_pm_dir.sh`)
   - `email.sender_skill` = if `~/.claude/plugins/installed_plugins.json` lists `ace@ace`, use `ace:email-communicator`; otherwise leave the literal string `ace:email-communicator` and proceed (the user can swap it in `$CANOPY_PM_DIR/autonomous.yaml` if a different sender ships first)
   - `shipping.branch_prefix` = `$PROJECT_NAME/auto/`
   - `shipping.pr_label` = `autonomous`
   - `shipping.merge` = `squash`
   - `shipping.deploy_command` and `shipping.deploy_workflow` — look in `.github/workflows/` for the most recently-modified `deploy*.yml` and use it. If none exists, write `echo "no deploy configured"` / `none.yml` and continue (the gate will catch deploy failures later if they matter)
   - `shipping.post_deploy_health` — look for any URL in README.md / CLAUDE.md matching `https?://[^ ]+/(health|api/health|healthz)`. If none, fall back to `["https://example.invalid/health"]` and continue (a 5xx is fine, the cycle will fix-forward)
   - `testing.unit` / `lint` / `types` — guess by detecting `pyproject.toml` (→ `uv run pytest -q` / `uv run ruff check .` / `uv run mypy .`), `package.json` (→ `npm test` / `npm run lint` / `npm run typecheck`), or fall back to `true` (no-op) for any not detectable
   - `testing.prepare` — OMIT by default (the field is optional). Only emit it for split-stack projects (BOTH `pyproject.toml` AND `frontend/package.json` or `web/package.json`) that regularly start sprints in fresh worktrees with empty deps. If emitted, default to `bash -c "uv sync --frozen && (cd frontend && npm ci)"` (adjust the frontend path to match what the repo uses). **Lockfile-pinned variants only** — `uv sync --frozen` not bare `uv sync`, `npm ci` not `npm install`, `pnpm install --frozen-lockfile` not bare `pnpm install`. Phase 0 enforces that `testing.prepare` does not mutate the worktree, so a mutating prepare command will fail the run before any work happens. The user can broaden the prepare command later by editing the file (e.g. add `uv pip install` for dev deps not in the lock).
   - `guardrails`: `one_pr_in_flight: true`, `diff_size_limit_lines: 1500`, `max_fix_forward_attempts: 3`
   - `theme_detection.lens_rotation`: `[user-value, adoption-blockers, integration-depth, trust-reliability, tech-debt]`

   Write the YAML to `$CANOPY_PM_DIR/autonomous.yaml`, print a single line: `Bootstrapped $CANOPY_PM_DIR/autonomous.yaml from project defaults (deploy=<workflow-or-none>, sender=<sender>, lens_rotation=5).` Then continue. Do NOT ask the user to confirm — they can edit the file later if anything's wrong.

3. Validate `$CANOPY_PM_DIR/autonomous.yaml`. The validator declares its YAML dep via PEP 723 inline metadata, so invoke it with `uv run --script` (NOT plain `python3`) — that way uv resolves PyYAML on the fly without requiring it on the user's system python:

   ```bash
   uv run --script "$PLUGIN_PATH/skills/product-management/scripts/validate_autonomous_config.py" "$CANOPY_PM_DIR/autonomous.yaml"
   ```

   Refuse to run on non-zero exit. Print the validator stderr and stop. If `uv` is missing on the user's system, ask them to install it (`brew install uv` or `pip install uv`) and stop. Note: if the file existed already (i.e. the user supplied it), still validate — never silently overwrite a user-supplied config.

4. Read `$CANOPY_PM_DIR/context.md` and `$CANOPY_PM_DIR/learnings.md`. If `context.md` is missing, run the existing skill's bootstrap flow first (see SKILL.md "Bootstrapping: Building context.md"), THEN re-enter Phase 0.

   **Also pre-summarize the last 3 run logs into your context** so Phase A
   step 1's "avoid re-claiming shipped work" check becomes a scan-what-you-
   already-have instead of N file reads. For each of the 3 most recent
   `$CANOPY_PM_DIR/runs/*.md` files, extract the sprint slug, the **lens** that
   drove it (look in the file for `Lens:` or `theme:` near the top), and the
   **highlights that shipped** (look for the Phase D "Reality reconciliation"
   section's bullet list, or fall back to the first H2 heading). Surface a
   compact block like:

   ```
   Prior runs (last 3):
     2026-05-01-first-chat-path  lens=adoption-blockers  shipped: chat link from /opp page; preserved-chat on refresh
     2026-05-01-opp-chat-link    lens=integration-depth  shipped: opp↔chat round-trip; back-button parity
     2026-04-28-adoption         lens=adoption-blockers  shipped: anonymous chat onboarding banner
   ```

   This costs one bash + one short awk/python pass at Phase 0 entry; without
   it the agent re-reads each run log individually during Phase A scouting
   (~10 tool calls per sprint) to perform the same overlap check. The
   summary lives in agent context only — do not write a derived cache file.

5. Capture the **starting branch** (so Phase E can return to it), confirm `origin/main` is reachable, and **enforce a clean worktree precondition**:

   ```bash
   STARTING_BRANCH=$(git rev-parse --abbrev-ref HEAD)
   git fetch origin main

   # Refuse to start if the worktree has staged or unstaged modifications.
   # Untracked files are fine. Reason: the convince-self gate's mechanical
   # checks (3a) run against the working tree, but `git commit` ships
   # whatever's in the index. Pre-existing uncommitted modifications
   # create a divergence where the gate certifies one snapshot and the
   # PR ships another. Two real fix-forwards on ace-web traced to this
   # exact class of bug (2026-05-01 cycles 1 + 2). Cleanest fix: refuse
   # up front and let the user decide.
   if ! git diff --quiet HEAD; then
     echo "autonomous: refuse to start — worktree has uncommitted modifications:"
     git status --short | grep -vE '^\?\?'
     echo
     echo "stash with:   git stash --include-untracked"
     echo "or commit:    git add -A && git commit -m '...'"
     echo "then re-run /canopy:pm-autonomous"
     exit 1
   fi
   ```

   The skill is still **worktree-friendly** in the sense that it does NOT require you to be on `main` — Phase C branches from `origin/main` directly, so the *branch* you're sitting on is irrelevant. But it does require a clean *worktree* at start: in-progress edits get caught by mechanical checks but not by the commit, leading to gate-green-but-deploy-red surprises. Stash or commit before kicking off. If `git fetch` fails, stop and report — without `origin/main` we can't branch safely.

6. Confirm only ONE autonomous PR is in flight (per `guardrails.one_pr_in_flight`). Query:

   ```bash
   gh pr list --label "$PR_LABEL" --state open --json number,headRefName
   ```

   If a previous autonomous PR is still open, RESUME that PR instead of starting fresh — pick up at Phase C and drive it to merge before opening anything new.

7. **Run `testing.prepare` if configured.** Optional bootstrap for split-stack projects (`pyproject.toml` + `frontend/package.json`) where fresh worktrees start with empty `.venv` / `node_modules`. Two confirming cycles on ace-web (2026-04-28 adoption-blockers + first-chat-path) had mechanical-checks fail until deps were built — `testing.prepare` closes that gap.

   **`testing.prepare` MUST be lockfile-non-mutating.** Use `npm ci` (not `npm install`), `uv sync --frozen` (not `uv sync` alone), `bundle install --frozen`, `pnpm install --frozen-lockfile`, etc. Reason: a mutating prepare leaves `package-lock.json` / `uv.lock` / equivalent in a "modified" state, which fails the clean-worktree precondition in step 5 on every re-run AND drags lockfile churn into the autonomous PR's diff. Lockfile-pinned variants are also faster (no resolution pass) and reproducible. If your lockfile *should* update, do that as a deliberate human-driven commit before running the autonomous cycle.

   ```bash
   PREPARE_CMD=$(yq '.testing.prepare // ""' "$CANOPY_PM_DIR/autonomous.yaml")
   if [ -n "$PREPARE_CMD" ]; then
     timeout 300 bash -lc "$PREPARE_CMD" || { echo "testing.prepare failed; abort sprint"; exit 1; }
     # Sanity-check: confirm prepare didn't violate its non-mutating contract.
     # Catches the easy mistake of using `npm install` instead of `npm ci`.
     if ! git diff --quiet HEAD; then
       echo "autonomous: testing.prepare mutated the worktree:"
       git status --short | grep -vE '^\?\?'
       echo
       echo "Switch the prepare command to a lockfile-pinned variant"
       echo "(npm ci / uv sync --frozen / pnpm install --frozen-lockfile)"
       echo "and commit any legitimate lockfile updates before re-running."
       exit 1
     fi
   fi
   ```

   If the prepare command exits non-zero, hits the 5-minute timeout, or violates the non-mutation contract, log the failure in the run log and stop the sprint — do NOT continue into Phase A with a half-built or dirty environment.

8. **Legacy `sent-emails/` cleanup hint.** Earlier versions of this skill stored email artifacts under `$CANOPY_PM_DIR/sent-emails/`; that storage was removed in v0.2.62 (the asset branch + run log carry everything now). If the directory still exists, print one line and continue — do NOT auto-delete:

   ```bash
   if [ -d "$CANOPY_PM_DIR/sent-emails" ]; then
     echo "Legacy $CANOPY_PM_DIR/sent-emails/ found — safe to remove with: rm -rf $CANOPY_PM_DIR/sent-emails"
   fi
   ```

9. **Create `$EMAIL_WORKDIR`** — the temp working dir that the post-deploy 3c dogfood step will write screenshots into and that Phase E will read from. Created here (not at the start of Phase E) because 3c needs it earlier in the cycle:

   ```bash
   EMAIL_WORKDIR=$(mktemp -d -t canopy-email-XXXXXX)
   mkdir -p "$EMAIL_WORKDIR/screenshots"
   trap 'rm -rf "$EMAIL_WORKDIR"' EXIT
   ```

   Treat the EXIT trap as the cleanup contract — the dir lives for the rest of the sprint and is discarded automatically when the cycle exits. Do not redefine `$EMAIL_WORKDIR` later.

## Phase A — Working-backwards draft (5–10 min)

Goal: produce a target email DRAFT that can pass three critiques before any engineering happens.

1. **Avoid re-claiming shipped work — use the Phase 0 step 4 prior-runs summary.** You already have the last 3 run logs' lens + shipped-highlights summarized in context from Phase 0 step 4. Scan that block; if a candidate highlight overlaps something already announced, drop it or reframe. Only `Read` the full run-log file when you need a detail the summary omits. The run logs ARE the memory of what's been claimed; do not introduce a separate "shipped emails" archive to scan.
2. Quick scout pass across the lenses in `theme_detection.lens_rotation`. Just enough breadth to see what's ripe — no deep dives.
3. Draft the target email using `email-format.md`'s template, AS IF IT WERE ALREADY TRUE. Specific feature names. Specific value statements. No placeholders.
4. Self-critique against three tests; write the verdict for each into the cycle log:
   - **Clear:** Could a non-technical user read this and know what's better today than yesterday? Does each highlight name a concrete thing they can click on?
   - **Testable:** For each highlight, can I write a one-line "Try it" instruction that proves it works? If a highlight doesn't survive a "go click this URL and see X happen" test, it's vapor — drop it.
   - **Impressive:** Does this move the product forward in a way the user would *care about*? Not "code is cleaner" — "you can now do thing-Y you couldn't before, or thing-Z is meaningfully nicer." If the most exciting highlight is "polished some copy," the answer is no.
5. If any critique fails, LOOP on Phase A. Scout deeper. Swap a weak highlight. Change the theme. Expand scope. Do NOT proceed to Phase B with a draft that is not yet impressive — that is the single most important rule of this skill.

The approved draft, with every critique annotated PASS, is the input to Phase B.

## Phase B — Derive the work

For each highlight in the approved draft, write one or more concrete proposals — title, files, expected diff shape, validation. Estimate effort per proposal. If the total estimated effort exceeds ~6 hours of cycle time, TRIM: keep the most "Try it"-able subset and save the rest as a future-sprint note in the run log.

## Phase C — Ship

For each proposal, in order, ONE PR IN FLIGHT AT A TIME:

1. Create branch FROM `origin/main` (not from current HEAD — the user is often in a worktree on an unrelated feature branch). `git fetch` already ran in Phase 0; assume `origin/main` is fresh:

   ```bash
   git checkout -b "$BRANCH_PREFIX$(slug-of title)" origin/main
   ```

   This guarantees the autonomous PR's diff contains ONLY the work this proposal does, regardless of the worktree's pre-existing state.
2. Implement the change. Use TDD where it fits (`superpowers:test-driven-development`); skip TDD only when the change is purely a behavior-of-no-test-yet thing and a test would be theatre.
3. Stage the change: `git add -A`
4. Run the **pre-merge** layers of the gate per `convince-self-gate.md` — sections 3a (mechanical checks) and 3b (five-question self-review). Section 3c is **not** run here; it has moved to step 11 below because it now exercises deployed prod, not localhost.
5. If 3a or 3b drops the proposal, log it under `self-review-blocked` in the run log AND re-derive the corresponding email highlight (try a different angle, or drop it and scout for a replacement). The email must remain impressive or it's not worth sending.
6. Open PR: `gh pr create --label "$PR_LABEL" --base main --title "<title>" --body "<body>"`. Body cites the email highlight this proposal makes true.
7. Wait for CI. On red:
   - Up to 2 fix-forward attempts on the same PR (re-run the gate each time)
   - Beyond that, switch this PR to a fix-forward investigation cycle. Track attempts against `guardrails.max_fix_forward_attempts`.
   - If exhausted, log "stuck", call Phase E with a stuck-state email, exit.
8. On CI green: merge per `shipping.merge`.
9. Run `shipping.deploy_command`.
10. Poll deploy status (use `gh run list --workflow="$DEPLOY_WORKFLOW"` until completion).
11. Run section 3d post-deploy health check.
12. Run section 3c **prod dogfood + screenshot capture** (now post-deploy — see `convince-self-gate.md` §3c). Drive each `Try it:` line against the deployed app via the configured `headless_browser_skill`; this both proves the user-visible behavior and produces the hero screenshots for the email. Save shots into `$EMAIL_WORKDIR/screenshots/` so Phase E.2 can push them to the asset branch unchanged. If the click-through fails, treat it as a fix-forward (counts against `guardrails.max_fix_forward_attempts`).
13. Update the cycle log with: branch, PR number, gate verdicts (each Q answered), deploy status, health-check status, dogfood verdict.
14. Move to the next proposal.

## Phase D — Reality reconciliation

Reality always diverges from plan. Before sending the email:

1. Rewrite the email body based on what ACTUALLY shipped — which highlights survived, what new value emerged, what got cut.
2. Re-run the three critiques (Clear, Testable, Impressive) on the rewritten version.
3. **Also ask:** "What did I learn about the PM process itself this sprint?" Universal lessons (NOT project-specific) become a separate canopy PR per `SKILL.md`'s Self-Improvement Protocol. Link them in the email's `**` section.
4. If the rewritten email still passes → proceed to Phase E.
5. If it doesn't → do NOT send a weak email. Log "sprint failed to converge on a great email" in the run log, build the stuck-state email body per `email-format.md`, proceed to Phase E.

## Phase E — Send + stop

The email MUST be HTML, with hero screenshots captured from prod, hosted via persistent https URLs, and laid out per `email-format.md`'s reference template. Read that file first — its **Hard rules** section is non-negotiable. Phase E has 8 substeps; do not skip E.4 or E.5.

**Working directory note.** `$EMAIL_WORKDIR` was created in Phase 0 step 9 with an EXIT trap that discards it when the cycle ends. The post-deploy 3c dogfood step (Phase C step 12) already populated `$EMAIL_WORKDIR/screenshots/` with the prod hero shots. Phase E reads from the same dir; nothing email-specific persists in `$CANOPY_PM_DIR`. The persistent homes are: (a) `$CANOPY_PM_DIR/runs/<sprint-slug>.md` for the cycle log (already established), and (b) the `pm-assets/<sprint-slug>` branch on the **project's** repo for the rendered HTML and screenshots that the email's `<img src>` URLs resolve to.

1. **Verify the prod screenshots from 3c.** Confirm `$EMAIL_WORKDIR/screenshots/` contains one PNG per "Try it" line in the target email — these are the same shots that 3c (Phase C step 12) captured against deployed prod. If a surface didn't get captured (because 3c noted it as unreachable, e.g. a "disconnected" branch masked by a global fallback), describe it textually in the body — never substitute a localhost shot, and never re-capture from a localhost stack. If a re-capture is needed at all (3c crashed mid-run, etc.), drive prod again — same `headless_browser_skill`, same automation auth — localhost is not an option.

2. **Publish screenshots to a persistent branch on the PROJECT'S repo** (the project being PM'd, not canopy). Create `pm-assets/<sprint-slug>` from `origin/main`, copy `$EMAIL_WORKDIR/screenshots/` into the branch checkout, commit, and push to origin (no PR — this branch is asset hosting, not code). Verify each `https://raw.githubusercontent.com/<owner>/<repo>/pm-assets/<sprint-slug>/.../<file>.png` URL returns HTTP 200. The branch lives on the project's origin forever; the email's `<img>` URLs resolve forever — that's the only persistent home for the rendered email and its screenshots.

3. **Render the email body to HTML in the temp working dir.** Use the canonical layout in `email-format.md` (brand bar, hero, per-highlight blocks with `<img>` referencing the raw.githubusercontent.com URLs from step 2, footer with internal notes). Save to `$EMAIL_WORKDIR/email.html`. Also commit a copy to the `pm-assets/<sprint-slug>` branch alongside the screenshots so the rendered email itself is archived (`screenshots/email.html` or `email.html` at branch root — your call). Per Hard rule #5: every highlight's `<h2>` AND `<img>` must wrap in `<a href="<TRY-IT-URL>">`.

4. **E.4 pre-send rendering check (gate).** Before invoking the sender skill, render `$EMAIL_WORKDIR/email.html` via `headless_browser_skill` at 1280×800 (desktop) and 375×812 (mobile). Save shots into `$EMAIL_WORKDIR/screenshots/email-rendered-{desktop,mobile}.png`. Look at them — do all hero images load? Do titles look like links? Is the headline sharp? Is mobile not wrapped/broken? If anything is off, fix the HTML and re-render. See `email-format.md` "Self-review" section for the full checklist. Optionally push the rendered shots to the asset branch too.

5. **Invoke `email.sender_skill`** with `subject`, `body_html` (read from `$EMAIL_WORKDIR/email.html`), and `body_text` (a generic "please view as HTML" fallback). Generally do NOT pass `attachments` — `cid:` refs do not resolve when sender skills produce `multipart/mixed`, and Gmail-rendering of attached images is unreliable. Hosted https URLs are the contract. Capture the returned `messageId` and `threadId` for the run log.

6. **E.5 post-send self-critique.** Use the rendered shots from step 4 (already captured). Write a short critique into `$CANOPY_PM_DIR/runs/<sprint-slug>.md` under a `### Phase E.5 — post-send self-review` heading. List 2-4 concrete improvement ideas ranked by impact, plus: sender skill ID, message ID, link to the asset branch (`https://github.com/<owner>/<repo>/tree/pm-assets/<sprint-slug>`). The email is already sent — these feed the *next* cycle and, if structural, become a follow-up canopy PR. Surface the critique to the user as the cycle's closing message.

7. **Return the worktree to its starting branch.** The temp working dir is discarded automatically by the `trap` set at the top of Phase E.

   ```bash
   git fetch origin main:main 2>/dev/null || true   # update local main if not currently checked out elsewhere
   git checkout "$STARTING_BRANCH"
   ```

   If `git fetch origin main:main` fails (because local main is checked out in another worktree, common in emdash setups), that's fine — origin/main is the source of truth and the next sprint will pick it up via `git fetch origin main`. Don't error on it.

8. Stop the loop. Exit cleanly. The `/canopy:pm-autonomous-loop` wrapper, if it invoked us, will sleep until "keep going" or 24h timeout — see `pm-autonomous-loop.md`.

## State that persists across sprints

The PM cycle has two memory channels:

```
<repo>/.canopy/pm/                  ← committed to the project repo, portable across machines
├── context.md                           ← project identity, who uses it, what matters
├── learnings.md                         ← accumulated learnings across cycles
├── autonomous.yaml                      ← autonomous-mode config
└── runs/
    └── YYYY-MM-DD-<theme-slug>.md       ← cycle log + Phase D shipped highlights
                                            + Phase E.5 post-send self-review
                                            (THIS is what Phase A reads to
                                            avoid re-claiming shipped work)
```

```
pm-assets/<sprint-slug>                  ← branch on the PROJECT'S repo origin
├── screenshots/
│   ├── *.png                            ← prod feature shots referenced by
│   │                                      the sent email's <img src>
│   └── email-rendered-{desktop,mobile}.png
└── email.html                           ← the rendered HTML body that was sent
```

**No sent-emails archive in `$CANOPY_PM_DIR`.** Earlier versions of this skill stored `email.md` + `email.html` + `screenshots/` under `$CANOPY_PM_DIR/sent-emails/<slug>/`, but no instruction ever read that storage and the rendered HTML was already canonical on the asset branch. The run log captures shipped highlights (Phase D), the asset branch hosts the polished email and screenshots, and `learnings.md` accumulates universal lessons. Anything else is dead storage.

If a project has a legacy `~/.canopy/pm/<project>/sent-emails/` directory from before this redesign, the user can delete it whenever — `rm -rf ~/.canopy/pm/<project>/sent-emails/`. The skill does not auto-prune.
