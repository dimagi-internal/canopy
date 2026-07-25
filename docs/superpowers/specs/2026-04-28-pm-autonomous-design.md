# Autonomous mode for `canopy:product-management`

> **Status: shipped — historical record, not current-state. NOTE: the `.claude/pm/autonomous.yaml` location described here was superseded by the 2026-05-07 PM-state relocation to `<repo>/.canopy/pm/`.**

**Date:** 2026-04-28
**Status:** Approved design — ready for implementation plan
**Origin:** Brainstormed with jjackson during ace-web PM session 2026-04-28

## Summary

Add a fully-autonomous mode to the `canopy:product-management` skill so a single
project owner can let the loop scout, propose, implement, ship, and deploy
improvements without per-proposal approval — and stop only when it has
something genuinely worth showing the user.

The cycle is organized **working-backwards** (Amazon-style): each sprint
*starts* with drafting the customer-facing release-notes email and only
proceeds with engineering work that makes that email true. The sprint ends
when the email passes a strict critique (clear, testable, impressive) and
gets sent to the project owner — at which point the loop stops and waits
for a human to say "keep going."

This design:
- Does NOT remove or alter the existing human-gated `/canopy:pm-scout` flow.
- Adds two new commands: `/canopy:pm-autonomous` (one sprint) and
  `/canopy:pm-autonomous-loop` (sprint → wait for "keep going" → repeat).
- Is opt-in per-project via a new `.claude/pm/autonomous.yaml` config file.
- Generalizes across projects; ace-web is the first adopter.

## Design principles

1. **Working-backwards drives the cycle.** The press-release email is the
   planning artifact, not a post-hoc summary. The skill scouts harder until
   it can write an email worth sending — not the other way around.
2. **Convince yourself it's clean before shipping.** CI green is necessary but
   not sufficient. A multi-layer self-review gate forces the agent to name
   what it changed, what's risky, and whether it'd defend the diff in code
   review.
3. **No safe paths, no auto-revert.** The user explicitly chose to trust the
   agent with the whole codebase and let it fix forward when it breaks
   things. The only hard rule is no secret leaks.
4. **One sprint = one email.** The loop produces one customer-quality email
   per autonomous run, with screenshots/walkthroughs of new features as
   first-class proof. If a sprint can't converge on an email worth sending,
   it sends a brief "stuck" note and stops, doesn't pad with weak content.
5. **Continuous self-improvement of the skill itself.** Each sprint also
   writes back universal lessons as canopy PRs, no permission asked.

## Section 1 — Overall shape

The existing `canopy:product-management` skill grows a second mode:
**autonomous**. The existing `/canopy:pm-scout` entry path is unchanged:
Phase 3 still gates on `AskUserQuestion` and waits for human approval per
proposal. A new entry path runs the same scout/propose/implement/learn
cycle but:

- Auto-approves its own proposals (no `AskUserQuestion`)
- Runs a multi-layer **convince-self-it's-clean** gate before opening each PR
- Auto-merges on green CI and auto-deploys
- One PR in flight at a time (waits for deploy + health check before next)
- Rotates lenses across the sprint
- Detects "ready to send" via working-backwards email critique → drafts the
  customer-voice release notes + internal asterisk + canopy self-improvement
  asterisk → sends → exits

The skill stays project-agnostic. All project-specific bits (deploy command,
test commands, email recipient, health-check URLs) live in a per-project
`.claude/pm/autonomous.yaml` file. Other projects adopt by writing their own
`autonomous.yaml`.

The loop runs in the user's terminal via two new commands:

- `/canopy:pm-autonomous` — runs **one full sprint** (Phases A–E below) and
  exits. Standalone; invoke directly when you want a single sprint, no loop.
- `/canopy:pm-autonomous-loop` — wraps the above in `/loop` (self-pacing
  dynamic mode). Each sprint ends with email + stop, then the loop sleeps
  until the user sends "keep going" OR a 24h long-tail timeout fires. On
  timeout, sends a single non-nagging reminder ping.

## Section 2 — Per-project config: `.claude/pm/autonomous.yaml`

This file tells the skill how *this* project ships, tests, and stays safe.
Without it, `/canopy:pm-autonomous` refuses to run (validation in Phase 0).

Example for ace-web:

```yaml
# .claude/pm/autonomous.yaml — drives canopy:product-management autonomous mode.

email:
  to: jjackson@dimagi.com
  from: ace@dimagi-ai.com
  subject_prefix: "[ace-web]"
  # Email is sent via the ace plugin's email-communicator skill.
  # Any project can specify a different sender mechanism in this block;
  # the skill validates the named sender skill exists.
  sender_skill: ace:email-communicator

shipping:
  branch_prefix: ace-web/auto/
  pr_label: autonomous          # PRs get this label so they're visibly tagged
  merge: squash
  deploy_command: gh workflow run deploy-labs.yml --ref main -f run_migrations=false
  deploy_workflow: deploy-labs.yml   # so the loop can poll status
  post_deploy_health:
    - https://labs.connect.dimagi.com/ace/api/health

testing:
  unit:    .venv/bin/python -m pytest -q
  lint:    .venv/bin/python -m ruff check .
  types:   bash -c "cd frontend && node_modules/.bin/tsc -b"
  # Dogfood (visual proof for the email) is required for any change named
  # in a 'Try it' line of the target email — see §3c and §4.
  dogfood:
    base_url: http://localhost:8000/ace
    start_command: docker compose up -d
    wait_for: http://localhost:8000/ace/api/health
    headless_browser_skill: gstack    # or 'browse'

guardrails:
  one_pr_in_flight: true
  diff_size_limit_lines: 1500       # autonomous won't ship a diff over this
  max_fix_forward_attempts: 3       # before "stuck" → minimal email + stop
  # No off-limits paths. The user trusts the agent with the whole codebase.
  # Secret-leak scan is hardcoded into the convince-self gate — not config.

theme_detection:
  # Coherent theme is now driven by email-quality critique (§4+5), not
  # mechanical lens-exhaustion. These are still useful as starting points
  # for the working-backwards email draft.
  lens_rotation:
    - user-value
    - adoption-blockers
    - integration-depth
    - trust-reliability
    - tech-debt
```

The skill loads this on Phase 0, validates required keys, refuses to run if
malformed. The schema is documented in the skill itself; this file is the
canonical example.

## Section 3 — The convince-self-it's-clean gate

Runs before each PR opens. Goal: not just "tests pass" but "I'd defend this
in a code review." Layered, in order. Any layer failing means the proposal
is dropped (logged in the cycle log) and the loop moves on.

### 3a. Mechanical checks

All from `autonomous.yaml`:
- `unit`, `lint`, `types` — must exit 0
- **Secret-leak scan (hardcoded, not configurable):** `git diff` of the
  staged change checked against:
  - `AKIA[0-9A-Z]{16}` (AWS access key)
  - `sk-ant-[A-Za-z0-9_-]+` (Anthropic API key)
  - `gh[ps]_[A-Za-z0-9]{36}` (GitHub token)
  - Any value present in `.env` (if it exists) — string-match each non-empty
    value as a substring search
  - Any AWS Secrets Manager key name pattern referenced in
    `deploy/aws/task-definition.json` or equivalent — string-match values
- **No restricted files in diff:** `.env`, `*.key`, `*.pem`,
  `credentials.json`, `gws-sa-key.json`, anything matching `*-secret.*`
- **No leftover debug:** `print(`, `console.log(`, `breakpoint()`,
  `debugger;` in non-test files
- **Diff-size sanity:** `git diff --stat` total line count must be <
  `diff_size_limit_lines`. Anything bigger gets logged + dropped (the
  autonomous loop is for incremental improvement, not refactors).

### 3b. Self-review pass — five questions, written to the cycle log

The agent re-reads its own diff and answers, in writing, before proceeding:

1. **What invariant did I just change?** If "none" or "I don't know" → fail.
   Forces explicit framing of the change's contract.
2. **What's the riskiest line in this diff?** Force naming a specific line.
   "Nothing is risky" is a fail for any non-trivial change.
3. **What would a senior eng object to in code review?** If can't think of
   anything for a non-trivial change → fail (probably blind to a real
   issue).
4. **Did I touch any test that codifies a behavior I'm changing?** If yes,
   did I update the test's *intent*, or just patch around it? Catches the
   "test as bug-feature" anti-pattern (recurring in the ace-web 2026-04-28
   sessions).
5. **Would I be comfortable if this shipped while I was on vacation?** If
   hesitating → fail.

Answers go into the cycle log. If any question fails, the proposal is logged
as "self-review blocked" with the failing question, and the loop moves on
(possibly re-deriving the email highlight that proposal was meant to
satisfy — see §4+5 Phase C).

### 3c. Dogfood pass — required for any "Try it" feature

For any change that's named in a "Try it" line of the target email, the
agent MUST:

1. Start the local stack (`testing.dogfood.start_command`)
2. Wait for `wait_for` URL to return 200
3. Drive the change in `headless_browser_skill` — actually click through the
   feature, verify the expected behavior visibly happens
4. Capture a sequence of screenshots showing the "before" (revert briefly,
   screenshot, restore — OR use a feature flag, OR just describe before
   from memory if a clean before-state isn't available) and the "after"
5. Save screenshots to `.claude/pm/sent-emails/<sprint-slug>/screenshots/`
6. Reference them in the email body (see §4)

A purely backend change (no user-visible surface) can skip dogfood, but
then it cannot appear as a "Try it" highlight in the email — only in the
internal asterisk section.

### 3d. Post-deploy health check

After deploy:
1. Poll each `post_deploy_health` URL for 200 OK with backoff (5s, 10s,
   20s, 40s, 80s — total ~5 min)
2. If any URL fails or stays 5xx, the loop **does NOT auto-revert**
3. Instead, it immediately starts a fresh investigation cycle to fix
   forward — the broken-prod state becomes its own scout finding
4. If still red after `max_fix_forward_attempts` cycles, log "stuck",
   send a minimal "no email this sprint, here's why" note, stop

## Sections 4 + 5 — Working-backwards cycle

The cycle is organized by working backwards from the customer email. Phases
A–E execute in order; Phase A may loop on itself before progressing.

### Phase A — Working-backwards draft (~5–10 min)

1. Quick scout pass across all 5 lenses, just enough to see what's ripe.
2. Draft the **target email** in the customer-facing format (§4 below). As
   if it were already true. Specific feature names, specific value
   statements, no placeholders.
3. **Self-critique against three tests:**
   - **Clear:** Could a non-technical user read this and know what's better
     today than yesterday? Does each highlight name a concrete thing they
     can click on?
   - **Testable:** Can I write a one-line "Try it" instruction for each
     highlight that proves it works? If a highlight doesn't survive a "go
     click this URL and see X happen" test, it's vapor.
   - **Impressive:** Does this email move the product forward in a way the
     user would *care about* — not "code is cleaner," but "you can now do
     thing-Y you couldn't before, or thing-Z is meaningfully nicer"? If
     the email's most exciting line is "we polished some copy," answer is
     no.
4. If any test fails → **loop on Phase A.** Scout deeper, swap a weak
   highlight for stronger, change the theme, expand scope. Don't ship
   until the draft passes all three.

### Phase B — Derive the work

Each highlight in the approved draft becomes one or more concrete
proposals. Estimate effort per proposal. If total estimated effort exceeds
~6 hours of cycles, trim — pick the most "Try it"-able subset and save
the rest as a future sprint.

### Phase C — Ship

For each proposal, in order (one PR in flight at a time):
- Self-review gate (3b)
- Mechanical checks (3a)
- Dogfood (3c) — required for any "Try it" change, with screenshots
- Open PR → wait CI → merge → deploy → poll health (3d)
- Update cycle log

If self-review blocks a proposal, the corresponding email highlight gets
re-derived — try a different angle on the same value, or drop the
highlight and scout for a replacement. The email must remain impressive
or it's not worth sending.

### Phase D — Reality reconciliation

Reality always diverges from plan. Before sending:
1. Rewrite the email based on what actually shipped (which highlights
   survived, what new value emerged, what got cut).
2. Re-run the three critiques on the rewritten version.
3. If it still passes → send.
4. If it doesn't pass → don't send a weak email. Instead log "sprint
   failed to converge on a great email" in the cycle log, send a much
   shorter "no email this sprint, here's why" note, stop.

### Phase E — Send + stop

- Send the email (customer voice + internal asterisk + canopy
  self-improvement asterisk)
- Stop the loop
- `/canopy:pm-autonomous-loop` sleeps until "keep going" or 24h timeout

### The email format

**Recipient:** from `email.to` and `email.from` in `autonomous.yaml`.

**Subject:** `<subject_prefix> Release notes — <theme summary> — <YYYY-MM-DD>`
e.g. `[ace-web] Release notes — onboarding polish & integration depth — 2026-04-29`

**Body:**

```markdown
# What's new in <product>

> 2-3 sentence customer pitch — what's better today than yesterday, framed
> as the value to the user / LLO / contributor. No PR numbers, no jargon.

## Highlights

- **<Feature 1>** — One sentence. Why it matters to you.

  ![<feature-1 screenshot>](screenshots/feature-1-after.png)

  *Try it:* one-line instruction with a clickable URL.

- **<Feature 2>** — …

  ![<feature-2 walkthrough>](screenshots/feature-2-walkthrough.png)

  *Try it:* …

- **<Feature 3>** — …
  (3-6 highlights; one per shipped item or grouped if they tell one story.
  Every highlight has at least one screenshot from the dogfood pass.)

## Walkthrough

> Optional: if multiple features compose into a single user journey, embed
> a 2-4 panel sequence showing the journey end-to-end.

---

## * Internal notes

**Sprint summary:** <theme>, <N PRs>, <X cycles>, <Y minutes wall-clock>.

**What shipped (engineering view):**

| PR  | Lens                | Title                       | Self-review verdict   |
|-----|---------------------|-----------------------------|-----------------------|
| #143 | user-value         | …                           | "would defend in CR"  |
| #144 | adoption-blockers  | …                           | "would defend in CR"  |

**Self-review blocks (proposals dropped before PR):**
- `<title>` — blocked on Q3 ("can't name what a senior would object to" →
  blind spot suspected in <area>)
- `<title>` — blocked on Q5 ("hesitated on vacation-test")

Each block is a real signal. If a pattern emerges across sprints (e.g.
always blocking on Q3 in the integration-depth lens), the next sprint's
first action should be a deeper read of that area before scouting.

**Deploy / health:**
- N deploys, all green
- (or: "deploy-X failed at <step>, fixed forward in PR#Y, see cycle log")

**What I'd do next** (suggestion, not commitment):
- One or two specific lenses or surfaces with the most untapped value.

---

## ** Canopy self-improvement notes

Process improvements I made to the `canopy:product-management` skill this
sprint (separately committed PRs to `jjackson/canopy`):

- **<insight>** — link to canopy PR#Z. What changed and why.
- **<insight>** — …

If no canopy improvements this sprint, this section says "No new universal
lessons this sprint."
```

The body is generated programmatically from cycle logs in `.claude/pm/runs/`,
the dogfood screenshots, and the canopy PR list. Not freehand-written.
This keeps the email honest — no inventing wins, no hand-waving past
failures.

## Section 6 — Loop wiring + commands

### Two new commands

`/canopy:pm-autonomous` — runs one full sprint (Phases A–E) and exits.
Idempotent and standalone. Use directly when you want a single sprint
without the loop wrapper.

`/canopy:pm-autonomous-loop` — wraps the above in `/loop` (self-pacing
dynamic mode). Each sprint ends with the email + stop, then the loop
sleeps until the user sends "keep going" OR a 24h long-tail timeout. On
timeout, sends one (non-nagging) reminder ping.

### Inside one sprint

```
/canopy:pm-autonomous fires
  ├─ Phase 0: load .claude/pm/autonomous.yaml + context.md + learnings.md
  │     refresh context.md if learnings.md flagged it stale last sprint
  ├─ Phase A: scout + draft email + critique loop
  │     until draft passes (clear, testable, impressive)
  ├─ Phase B: derive proposals from highlights
  ├─ Phase C: ship loop
  │     for each proposal:
  │       self-review gate (3b)
  │       mechanical checks (3a)
  │       dogfood (3c) if proposal corresponds to a "Try it" highlight
  │       open PR → wait CI → merge → deploy → poll health (3d)
  │       update cycle log
  │     on self-review block: re-derive corresponding email highlight
  │     on hard fail (CI red after 2 fix attempts, deploy red after 2):
  │       switch to fix-forward investigation cycle (still autonomous);
  │       if still red after max_fix_forward_attempts cycles, log
  │       "stuck", send minimal email noting stuck state, stop
  ├─ Phase D: rewrite email from reality, re-critique
  ├─ Phase E: send email + stop
  └─ exit (returns control to /loop wrapper if invoked via -loop)
```

### State that persists across sprints

```
.claude/pm/
├── context.md
├── learnings.md
├── autonomous.yaml                           ← per-project config (§2)
├── runs/
│   └── YYYY-MM-DD-<theme-slug>.md            ← cycle log + self-review verdicts
└── sent-emails/
    └── YYYY-MM-DD-<theme-slug>/
        ├── email.md                          ← actual email body sent
        └── screenshots/
            ├── feature-1-after.png
            ├── feature-2-walkthrough.png
            └── …
```

`sent-emails/` exists so future sprints can avoid repeating "we shipped X"
claims and so the user has a browseable archive of what's been said in
their voice.

### Canopy self-improvement integrated into Phase D

Phase D also asks: *"What did I learn about the PM process itself this
sprint?"* Universal lessons → autonomous PR to `jjackson/canopy` → linked
in the email's `**` section. No human approval; the canopy PR review
happens out-of-band when the user has time. The autonomous mode is
deliberately self-improving.

## Out of scope

- **Cron-scheduled remote agents.** The user chose local /loop. A future
  variant could wrap `/canopy:pm-autonomous` in `canopy:schedule` for
  remote execution; the skill has no opinion either way.
- **Tiered risk classification.** The user explicitly rejected the
  off-limits-paths and tiered-risk approaches in favor of "anything is
  fair game." If experience shows a class of mistake worth gating, this
  is a future addition.
- **Auto-revert.** The user explicitly chose fix-forward over revert.
- **Daily PR/deploy budget.** Out by user request; the email-and-stop
  pattern naturally bounds blast radius.
- **Multi-tenant adoption.** The skill stays general but ace-web is the
  first / only adopter. Generalization comes from real second-project
  use, not speculation.

## Open questions for implementation

(None blocking. Reasonable defaults exist for everything below; flagging
in case the implementation plan wants to confirm.)

1. **`/loop` long-tail timeout default.** Spec says 24h; could be 12h or
  configurable per `autonomous.yaml`. Pick a default; expose if needed.
2. **"Keep going" message detection.** The wrapper should treat any user
  message after a stop as resume. Is "stop" or "pause" the inverse? Spec
  doesn't define; default to: any user message resumes UNLESS it
  literally says "stop" / "pause" / "halt".
3. **Canopy PR opening mechanics.** The existing skill's
  Self-Improvement Protocol describes cloning `jjackson/canopy` to a
  temp dir. Reuse that verbatim or move to a stored worktree path?
  Detail for the implementation plan.
4. **Email rendering of screenshots.** Markdown email or HTML? The ace
  email-communicator skill should be checked for image-attachment
  support; if it only sends plain markdown, screenshots may need to be
  uploaded somewhere (gist? S3?) and linked. Implementation detail.

## Acceptance criteria

The skill is shipped when:

1. `/canopy:pm-autonomous` runs against ace-web's `.claude/pm/autonomous.yaml`
   and produces:
   - At least one shipped PR (merged + deployed + health-checked)
   - A customer-quality email sent to `jjackson@dimagi.com`, containing
     screenshots from the dogfood pass
   - An updated cycle log in `.claude/pm/runs/`
2. The existing `/canopy:pm-scout` flow continues to work unchanged
   (regression test: the human-gated path still produces an
   AskUserQuestion menu).
3. Secret-leak scan blocks a synthetic test commit containing a fake
   `AKIA...` value.
4. Diff-size limit blocks a synthetic test commit > 1500 lines.
5. Self-review gate fails when given a no-op diff with question 1
   answered "none."
6. Documentation: `canopy:product-management` SKILL.md describes both
   modes, the new commands, and the `autonomous.yaml` schema.
