# Agent-Core Shared Skills Implementation Plan (chunk A)

> **Status: shipped (PR #312, chunk A) — `plugins/canopy/agent-core/` live. Historical record, not current-state.**

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the fleet's shared process-skill text (`turn`, `task-tracker`) to one canonical home in canopy (`plugins/canopy/agent-core/`), shrink the factory templates to runtime-read stubs, and migrate echo, eva, and hal onto the stubs.

**Architecture:** Canonical docs live in the versioned canopy plugin (NOT under `skills/`, so they cost zero global skill budget). Agent repos keep ~20-line stubs that resolve the installed canopy path from `installed_plugins.json`, staleness-check it, read the core doc, and bind it to the agent's identity + local notes. Distribution becomes `/canopy:update`; `self-review` is retired into a supersession stub pointing at the already-central `canopy:agent-turn-review`.

**Tech Stack:** Python 3.11+ (`agent_factory.py`), pytest, markdown skills, `gh` CLI.

**Spec:** `docs/superpowers/specs/2026-07-13-agent-core-shared-skills-design.md`

## Global Constraints

- **Never hand-edit `VERSION` / `plugin.json` / `marketplace.json`** — only `uv run canopy version bump` (all three files together).
- **Never write into `~/.claude/plugins/cache/` or `installed_plugins.json` by hand** — distribution is `/canopy:update` only.
- Canopy PRs: push branch → `gh pr create` → wait `gh pr checks <n>` green (~10s) → `gh pr merge <n> --merge`. Never `--admin` past a red check.
- Agent repos (`~/emdash/repositories/{echo,eva,hal}`, remotes `dimagi-internal/<slug>`) are main checkouts: do feature work in a `git worktree`, merge with `gh pr merge <n> --squash` (NEVER `--delete-branch`), then `git worktree remove`.
- Canopy tests: `uv run pytest` from the canopy repo root. Factory/fleet suites: `uv run pytest tests/test_agent_factory.py tests/test_fleet_align.py -q`.
- Core docs are **agent-agnostic**: second person ("you"), `<slug>` placeholders explained by the stub's Identity block. No `{{TOKEN}}`s may appear in `agent-core/*.md` (tokens are a stamp-time mechanism; core docs are read at runtime).
- Work in the existing canopy worktree `/Users/jjackson/emdash/worktrees/canopy/emdash/ace-agent-gevcy` (branch `emdash/ace-agent-gevcy`) for Tasks 1–3.

---

### Task 1: Canonical core docs `plugins/canopy/agent-core/{turn,task-tracker}.md`

**Files:**
- Create: `plugins/canopy/agent-core/turn.md`
- Create: `plugins/canopy/agent-core/task-tracker.md`
- Test: `tests/test_agent_factory.py` (append)

**Interfaces:**
- Produces: `plugins/canopy/agent-core/<name>.md` for `name in {turn, task-tracker}` — the docs Task 2's stubs reference by exactly that relpath.

- [ ] **Step 1: Write the failing test** — append to `tests/test_agent_factory.py`:

```python
def test_agent_core_docs_exist_and_are_agent_agnostic():
    """The stubs stamped by the factory point at agent-core docs shipped in the plugin;
    those docs must exist, be substantial, and carry no stamp-time {{TOKEN}}s
    (they are read at RUNTIME by any agent — identity lives in the stub)."""
    root = Path(__file__).resolve().parents[1] / "plugins" / "canopy" / "agent-core"
    for name in ("turn", "task-tracker"):
        doc = root / f"{name}.md"
        assert doc.is_file(), f"missing agent-core doc: {doc}"
        text = doc.read_text()
        assert len(text) > 1000, f"{doc} suspiciously small — did the template body move here?"
        assert "{{" not in text, f"stamp-time token leaked into runtime doc {doc}"
```

(`Path` is already imported in this test file; if not, add `from pathlib import Path`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_factory.py::test_agent_core_docs_exist_and_are_agent_agnostic -q`
Expected: FAIL with `missing agent-core doc`.

- [ ] **Step 3: Create `plugins/canopy/agent-core/turn.md`** — the body of the current `_TURN_SKILL` (agent_factory.py, v0.2.271), converted agent-agnostic. Exact content:

````markdown
# Turn — the fleet-canonical full turn of work

> **Fleet-canonical process (canopy agent-core).** Your `skills/turn/SKILL.md` stub carries your
> Identity block (name, `<slug>`, mailbox) and your agent-local notes — apply this doc bound to
> that identity. To change THIS process for the whole fleet, PR canopy
> (`plugins/canopy/agent-core/turn.md` + `canopy version bump`); agent-specific quirks go in your
> stub's local-notes section instead.

**Re-read this doc at the start of every turn and follow it in order.** Running a turn from
memory is how steps get dropped under load. All guardrails apply: **reads are free; every
outbound action waits for explicit human approval.** Approval is PROCEDURAL — the gating hook
carries deny rails only (it blocks wrong paths, it does not ask for you), so drafting-then-asking
in Step 2 is the gate. There is no modal to catch you if you skip it.

**Narrate as you go.** Before any multi-step or multi-repo investigation, state the plan in one
sentence first ("checking threads A and B for X — back shortly"), then work, then report what you
found. Never let a long silent stretch of tool calls build up — a human interrupting with "what
are you doing?" means the turn's communication already failed.

## Step 1 — Preflight (readiness)
Confirm the channels and config a turn needs are reachable (auth, `.env`, any board PAT). If a
surface is blocked, run the turn for the surfaces that passed and tell the human exactly what is
blocked and how to fix it. Do not abort the whole turn for one blocker.

## Step 2 — Process inbound, one counterpart at a time
For EACH inbound item in order: read it, check the sender against `config/allowlist.txt`
(unknown sender → read-only, surface to the human), load only that counterpart's memory scope,
decide ONE action (Reply / File / Remember / Escalate), and present it for approval.
**Never reason about two counterparts in one step** — the cardinal rule.

Before every outbound reply, run the `agent-turn-review` skill (it invokes the fleet-wide
`canopy:agent-turn-review`): re-read the original request, extract EACH discrete ask, confirm the
draft does exactly that (read any source they cited; don't reconstruct from memory), confirm every
"I'll do X" is something you can actually execute (no vague "sync with <person>"), then lead with
what you DID + a recommendation + options.

**Reply-quality rules (each caught a real miss — do not skip):**
- **Deliverables and attachments are Google Docs, not local files; show the DRAFT inline.** A
  substantial artifact (a script, a report, a plan) goes in a shared gdoc and the reply links it;
  it does NOT get pasted as a wall of text into the email body, and it is NOT stashed in a local
  `.txt` you point the human at. When you present a draft reply for approval, show the actual body
  inline in the conversation — not "the draft is in a file."
- **Decide-then-show, in one coherent order.** Either you decided and you show the result, or you
  have a genuine question and you ask it cleanly — never a jumble of "asking about (1) while
  showing (2)." Number your asks/items and keep the order consistent between what you ask and what
  you present. Don't manufacture a decision out of a thread you've already classified as not
  actionable.
- **Verify recipients before sending.** Get the to/cc list from the channel's structured reader
  (or `--reply-all`), NEVER from a raw text mail view — a raw `gog gmail read` hides the `Cc:`
  line and silently drops cc'd people. Confirm reply-all vs. direct deliberately.

**Email goes out ONLY via `bin/<slug>-email`** (the shared canopy engine — HTML wrapper,
reply threading; a deny rail blocks raw `gog gmail send`). Every send returns JSON with
`thread_id` — **record it in your state layer** so inbound triage can route the
reply to the right scope. Auth flaky? `canopy email preflight --repo .` prints the exact fix.

## Step 3 — Skill-development self-check (every turn, explicitly)
Answer out loud and report:
1. **Did I create or improve a skill this turn?** Name it.
2. **Did I hand-repeat a multi-step pattern that SHOULD be a skill?** If so, build it now (or say
   why it is genuinely one-off). Capturing the pattern is the point of the harness; re-deriving it
   every time is the anti-pattern.
3. **Did friction this turn suggest a fix to my own skills?** A stale checklist step, a wrong
   command in a SKILL.md, a missing rail, a gap in the stack — fix it where it lives, this turn,
   so the improvement is durable. **Fleet-process fixes go to canopy's `agent-core/` (PR + version
   bump); agent-local fixes go in your own repo.** Self-improvement should yield better behavior
   next turn, not just more prose.

## Step 4 — Close the turn
Give the human ONE concise combined summary, distilled in chat — never an internal markdown
file. **Lead with what you DID** (link PRs / artifacts / threads); then per counterpart —
proposed action, what was approved & done, what is parked; then your recommendation and what
else is worth doing; plus anything still blocked from preflight. Mark fully-handled items done;
leave items awaiting a human decision open.

Then refresh your canopy-web workspace so `/agents/<slug>` reflects this turn
(the installed canopy plugin provides the shared client — no per-agent client to maintain):
```
canopy agent skills        # mirror the skill catalog (registers the agent if new)
```
If this turn produced a shareable deliverable, also `canopy agent work <items.json>`. The board at
`/agents/<slug>` is the shared trigger + approval surface — where teammates queue work and
approve outbound actions.

Then **package this turn** as a unit of work so `/agents/<slug>` records what you did and
ties it to the request(s) you advanced:
```
canopy agent turn --slug <slug> --title "<what this turn did>" \
  --task <ext_id> [--task <ext_id> …]      # the board task(s) this turn advanced
  # --work-product-url <url> per deliverable produced this turn
```
**Optional transcript link (ASK FIRST):** uploading the transcript publishes to canopy-web — an
outbound action — so it rides the same approval gate as a send. Only if the human says yes, append
`--upload` (reduces THIS session to conversation-only, then hangs a `/share/<token>` link off the
turn). Put that link in your close-out summary. Without `--upload`, the turn is still packaged
(request → what you did → deliverables), just with no transcript.

**CLOSE CHECKLIST — confirm each in the summary (these get silently skipped under load):**
1. `agent-turn-review` ran on every outbound reply (Step 2).
2. Skill-development self-check answered (Step 3).
3. Workspace refreshed (`canopy agent skills` above).
4. Turn packaged (`canopy agent turn …`); transcript uploaded ONLY if the human approved.

**Shipping a skill change from a worktree** — emdash runs each turn in a worktree while `main` is
checked out elsewhere, so `git checkout main` and `gh pr merge --delete-branch` FAIL ("main already
checked out"). Instead: `gh pr merge <n> --squash`, then verify with `gh pr view <n> --json state`.

## Related skills
- `agent-turn-review` — gate every outbound reply against the original request AND against what you
  can actually execute (invokes the fleet-wide `canopy:agent-turn-review`) before sending.
- `task-tracker` — durable multi-turn state (`agent-core/task-tracker.md` via your stub); drain
  board commands at turn start, package advanced tasks at close.
- canopy plugin (installed alongside every agent) — `create-agent`, `agent-publish`, `improve`, and
  the fleet self-improvement loop. Use them.
````

- [ ] **Step 4: Create `plugins/canopy/agent-core/task-tracker.md`** — the body of the current `_TASK_TRACKER_SKILL`, converted. Exact content:

````markdown
# Task Tracker — fleet-canonical iterative-work state

> **Fleet-canonical process (canopy agent-core).** Your `skills/task-tracker/SKILL.md` stub binds
> this to your identity (`<slug>`, mailbox) and carries your local notes. Fleet-process changes →
> PR canopy; agent quirks → your stub.

**One board task per iterative thread/project** — an email thread you'll act on across turns, a
feature-request doc you're working through, a multi-PR initiative. Single-turn one-offs don't
need a task; the close-out summary covers them. Backed by canopy-web's
`/api/agents/<slug>/tasks/` (kanban at `/agents/<slug>`); all verbs come from the installed
canopy CLI.

## The vocabulary (echo conventions, fleet-wide)
- **Title** — the outcome. **Next action** — the single concrete next step, *verb-first*.
- **Status** — `suggested` (you proposed it; a human validates) → `in_progress` →
  `done` / `declined`. There is no "blocked": *waiting on a person* is expressed by **Assigned**.
- **Owner** — the human stakeholder who owns the outcome — **never the agent**.
- **Assigned** — who the next action waits on: you, or the person it's on (renders as
  an amber "Waiting on X" on the board).
- **Confidence** — `high` / `low`, for suggested items (how sure you are).
- **Due** — `YYYY-MM-DD`; past-due un-done tasks are flagged on the board.
- **Links** — every stable artifact: the thread, the doc, PRs, the project folder. Working state
  (item maps, dossiers, notes) hangs off the task via links — NOT committed into target repos.

The board groups by **who has the ball**: Suggested · Waiting on a human · agent
working · Done.

## Verbs (installed canopy CLI — no bespoke script)
```
canopy agent add  --slug <slug> --title "…" --next-action "…" \
    --status in_progress --owner <human> --assigned <agent-name> \
    --links "Thread|https://…, Doc|https://…"          # create (auto T<N>)
canopy agent set  --slug <slug> --task-id <id> \
    --rationale "why" --plan "first steps" --source-url <url>   # store context — never re-derive
canopy agent tasks --slug <slug>                # read the board (JSON)
canopy agent commands --slug <slug>             # drain queued human actions each turn
canopy agent apply --slug <slug> --id <N> --note "what I did"
```

## Acting on board commands (the canopy-web DB is the source of truth)
The board at `/agents/<slug>` is a **control surface**: a human can Accept a suggested
task, Decline it (with a reason), or Dispatch ("do this now") — each queues a command
you drain. **At the start of every turn, check the queue:**
```
canopy agent commands --slug <slug>      # list actions queued for you
# ... do the work (under the normal guardrails — outbound actions still need approval) ...
canopy agent apply --slug <slug> --id <N> --note "what I did"   # mark it handled
```
- **Accept** already flipped the task to in_progress / assigned to you; the queued
  command means "go do it."
- **Dispatch** ("do this now") is the same — just act and apply.
When you *suggest* a task, store the context immediately (`set` — rationale, plan,
source url) so it is never re-derived later.

## Project folder per work item (so links are clean)
When taking on a work item that produces deliverables, give it a **Drive project folder** and
keep its deliverables there, so the tracker links to one stable place instead of a loose doc:
```
gog drive mkdir "<Work item>" --parent "$PARENT_FOLDER_ID" --account <mailbox> --client canopy
gog drive move <docId> --parent <projectFolderId> --account <mailbox> --client canopy
```
Put the **folder** link in the task's Links (gdoc deliverables get created in / moved into it).
Keep your Drive parent-folder id in the worktree-clean global `.env`
(`~/.<slug>/.env`, read via `bin/_env.py`) — e.g. `<slug>_DRIVE_FOLDER_ID`.

## When to use (turn-loop wiring)
- **Start of every turn:** drain `commands` → act → `apply`. The board is a trigger surface
  alongside the inbox.
- **Taking on multi-turn work:** create the task (status `in_progress` if a human asked for it,
  `suggested` if you are proposing it), immediately `set` rationale + plan + links,
  and give it a **project folder** whose link goes in Links.
- **During work:** keep **Next action** current — it is the card headline a human scans.
- **Close of turn:** package every turn that advanced a task —
  `canopy agent turn --slug <slug> --title "…" --task <ext_id> --work-product-url <url>`.
  This builds the per-task history spine: which turn did what, with which deliverables.
````

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_agent_factory.py::test_agent_core_docs_exist_and_are_agent_agnostic -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add plugins/canopy/agent-core/ tests/test_agent_factory.py
git commit -m "feat(agent-core): canonical fleet turn + task-tracker docs in the plugin"
```

---

### Task 2: Shrink factory templates to runtime-read stubs

**Files:**
- Modify: `src/orchestrator/agent_factory.py` (`_TURN_SKILL` ~line 384, `_TASK_TRACKER_SKILL` ~line 690 — replace both template strings entirely; `_TEMPLATES` keys unchanged)
- Test: `tests/test_agent_factory.py` (append)

**Interfaces:**
- Consumes: `plugins/canopy/agent-core/{turn,task-tracker}.md` from Task 1.
- Produces: stamped stubs at `skills/turn/SKILL.md` + `skills/task-tracker/SKILL.md` containing the strings `installed_plugins.json` and `agent-core/<name>.md` — Task 4–6 migrations render the same stub shape; fleet-align's taxonomy still derives from `_TEMPLATES` (keys unchanged).

- [ ] **Step 1: Write the failing test** — append to `tests/test_agent_factory.py`:

```python
def test_stub_skills_reference_agent_core(tmp_path):
    """turn + task-tracker are stamped as thin stubs that resolve the installed canopy
    plugin and read the canonical agent-core doc — never a full process copy."""
    create_agent(_spec(), tmp_path / "echo")
    for name in ("turn", "task-tracker"):
        text = (tmp_path / "echo" / "skills" / name / "SKILL.md").read_text()
        assert "installed_plugins.json" in text, f"{name} stub must resolve the installed canopy path"
        assert f"agent-core/{name}.md" in text, f"{name} stub must point at its core doc"
        assert "canopy-update-check.sh" in text, f"{name} stub must staleness-check the core"
        assert "{{" not in text
        assert len(text) < 3000, f"{name} looks like a full copy, not a stub"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_factory.py::test_stub_skills_reference_agent_core -q`
Expected: FAIL (current templates are full copies — `installed_plugins.json` absent / length > 3000).

- [ ] **Step 3: Replace `_TURN_SKILL` in `src/orchestrator/agent_factory.py`** with exactly:

````python
_TURN_SKILL = '''---
name: turn
description: >
  {{AGENT_NAME}}'s full turn-of-work orchestrator. Use when a human says "do a turn", "check your
  inbox", or otherwise triggers {{AGENT_NAME}} to work. The canonical procedure is fleet-wide and
  lives in the installed canopy plugin (agent-core/turn.md); this stub binds it to {{AGENT_NAME}}'s
  identity. THE entry point for a turn.
---

# Turn — {{AGENT_NAME}} (stub over the fleet-canonical core)

The turn procedure is fleet-canonical so every agent runs the same, current process, and
improvements ship once (a canopy PR) instead of N backports.

1. **Resolve the installed canopy plugin and check freshness:**
   ```bash
   CANOPY=$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")
   bash "$CANOPY/scripts/canopy-update-check.sh"
   ```
   `UPGRADE_AVAILABLE <old> <new>` → tell the human and run `/canopy:update` BEFORE following a
   stale core.
2. **Read `$CANOPY/agent-core/turn.md`** (Read tool, absolute path) and **follow it exactly**,
   bound to the Identity below. Where it says `<slug>`, use this Identity.

## Identity
- Name: **{{AGENT_NAME}}** · slug: `{{AGENT_SLUG}}` · mailbox: `{{MAILBOX}}`
- Email shim: `bin/{{AGENT_SLUG}}-email` · board: `/agents/{{AGENT_SLUG}}`

## {{AGENT_NAME}}-local notes (the ONLY hand-edited section — fleet-process changes go to canopy)
- (none yet)
'''
````

- [ ] **Step 4: Replace `_TASK_TRACKER_SKILL`** with exactly:

````python
_TASK_TRACKER_SKILL = '''---
name: task-tracker
description: >
  {{AGENT_NAME}}'s project/task state — one board task per iterative thread/project, backed by
  canopy-web (kanban at /agents/{{AGENT_SLUG}}). The canonical procedure is fleet-wide and lives
  in the installed canopy plugin (agent-core/task-tracker.md); this stub binds it to
  {{AGENT_NAME}}. Use when taking on multi-turn work, when a new request arrives, and at every
  turn's board-drain and close.
---

# Task Tracker — {{AGENT_NAME}} (stub over the fleet-canonical core)

1. **Resolve the installed canopy plugin and check freshness:**
   ```bash
   CANOPY=$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")
   bash "$CANOPY/scripts/canopy-update-check.sh"
   ```
   `UPGRADE_AVAILABLE` → tell the human and run `/canopy:update` BEFORE following a stale core.
2. **Read `$CANOPY/agent-core/task-tracker.md`** and **follow it exactly**, bound to the
   Identity below. Where it says `<slug>`/`<mailbox>`, use this Identity.

## Identity
- Name: **{{AGENT_NAME}}** · slug: `{{AGENT_SLUG}}` · mailbox: `{{MAILBOX}}`
- Board: `/agents/{{AGENT_SLUG}}` · Drive folder id env: `{{AGENT_SLUG}}_DRIVE_FOLDER_ID`

## {{AGENT_NAME}}-local notes (the ONLY hand-edited section — fleet-process changes go to canopy)
- (none yet)
'''
````

- [ ] **Step 5: Run the factory + fleet suites**

Run: `uv run pytest tests/test_agent_factory.py tests/test_fleet_align.py -q`
Expected: ALL PASS. (Fleet-align fixtures are synthetic — independent of live template text; `test_artifact_taxonomy_is_derived_from_factory_stamp_table` passes because `_TEMPLATES` keys are unchanged.) If a factory test asserts full-copy content of the old templates, update that assertion to the stub shape — but do NOT weaken `test_create_agent_writes_full_layout`.

- [ ] **Step 6: Run the whole suite**

Run: `uv run pytest -q`
Expected: pass counts consistent with main (~2,235 tests; a handful of browser-dep collection errors are pre-existing and OK).

- [ ] **Step 7: Commit**

```bash
git add src/orchestrator/agent_factory.py tests/test_agent_factory.py
git commit -m "feat(factory): stamp turn + task-tracker as runtime-read stubs over agent-core"
```

---

### Task 3: Ship the canopy PR and deploy

**Files:**
- Modify (via CLI only): `VERSION`, `plugins/canopy/.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`

- [ ] **Step 1: Version bump**

Run: `uv run canopy version bump`
Expected: prints the new version (0.2.272 or higher — it takes `max(local, origin/main)+1`).

- [ ] **Step 2: Commit, push, PR**

```bash
git add -A && git commit -m "chore: version bump for agent-core"
git push -u origin emdash/ace-agent-gevcy
gh pr create --title "feat(agent-core): canonical fleet process docs + factory stubs (chunk A)" \
  --body "Implements docs/superpowers/specs/2026-07-13-agent-core-shared-skills-design.md chunk A (canopy side): plugins/canopy/agent-core/{turn,task-tracker}.md as the fleet-canonical process docs; factory stamps runtime-read stubs. Verified: full pytest suite green. Per-agent migrations follow in each agent repo."
```

- [ ] **Step 3: Wait for checks, merge**

Run: `gh pr checks <n>` until `check-version` passes, then `gh pr merge <n> --merge`
Expected: merged. If `check-version` is red: re-run `uv run canopy version bump` (a parallel worktree may have claimed the number), amend, re-push.

- [ ] **Step 4: Deploy — the update skill's three steps (plugin cache + CLI)**

```bash
bash ~/.claude/plugins/marketplaces/canopy/plugins/canopy/scripts/canopy-update-check.sh
# then follow skills/update/SKILL.md steps 2+3 with the reported new version
```
Expected: `VERIFIED: v<new> installed and matches GitHub` and `CLI DEPLOYED`.
Verify the core docs actually shipped:
```bash
CANOPY=$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")
ls "$CANOPY/agent-core/"   # expect turn.md task-tracker.md
```

---

### Task 4: Migrate echo (`~/emdash/repositories/echo`, remote `dimagi-internal/echo`)

**Files (in echo's repo, via a fresh worktree):**
- Replace: `skills/turn/SKILL.md` (full copy → stub)
- Replace: `skills/task-tracker/SKILL.md` (evolved copy → stub; unique leftovers → local notes)
- Create: `skills/agent-turn-review/SKILL.md` (echo is missing it)
- Replace: `skills/self-review/SKILL.md` (→ supersession stub)

**Interfaces:**
- Consumes: the deployed `agent-core/` docs (Task 3) and the stub shapes from Task 2 (render `{{AGENT_NAME}}`→`Echo`, `{{AGENT_SLUG}}`→`echo`, `{{MAILBOX}}`→ the `email` value in echo's `config/agent.json`).

- [ ] **Step 1: Worktree**

```bash
git -C ~/emdash/repositories/echo worktree add /tmp/echo-agent-core -b feat/agent-core-stubs
cd /tmp/echo-agent-core
```

- [ ] **Step 2: Read before writing.** Read echo's current `skills/turn/SKILL.md`, `skills/task-tracker/SKILL.md`, `skills/self-review/SKILL.md` IN FULL. List every section/step that is NOT in the corresponding `agent-core/` doc (use the deployed copies at `$CANOPY/agent-core/`). Those are echo's candidates for local notes.

- [ ] **Step 3: Write the stubs.** Replace `skills/turn/SKILL.md` and `skills/task-tracker/SKILL.md` with the Task 2 stub shapes, tokens rendered for echo. Under **local notes** put ONLY echo-unique content found in Step 2, as short pointers (e.g. story pipeline hooks → `skills/story-*`). Per PR #311's judgment, echo's legacy sheet machinery (`echo_sheet.py`, `ECHO_TASKS_SHEET_ID`, pipe/comma footgun, `echo_tasks.py` warnings) is **dropped**, not carried — the board DB is the source of truth. If unsure a line is legacy vs. live, keep it as a one-line local note.

- [ ] **Step 4: Stamp `skills/agent-turn-review/SKILL.md`** — the current factory template (`_AGENT_TURN_REVIEW_SKILL` in the deployed canopy source, `~/.claude/plugins/marketplaces/canopy/src/orchestrator/agent_factory.py`), rendered for Echo. Fold any echo-unique review notes from `self-review` (its story-grader lineage) into the `## Echo-specifics` section.

- [ ] **Step 5: Supersede `skills/self-review/SKILL.md`** with exactly (rendered):

```markdown
---
name: self-review
description: >
  Superseded — Echo's pre-send discipline is the fleet-wide `canopy:agent-turn-review`,
  invoked via skills/agent-turn-review. This stub remains so older references keep working.
---

# Self-review → agent-turn-review

Use `skills/agent-turn-review/SKILL.md` (it invokes the fleet-wide `canopy:agent-turn-review`
and carries Echo's specifics). Do not add content here.
```

- [ ] **Step 6: Verify the stub resolves.** Run the stub's step-1 bash for real; then confirm `$CANOPY/agent-core/turn.md` and `task-tracker.md` are readable and `bash "$CANOPY/scripts/canopy-update-check.sh"` prints `UP_TO_DATE <new version>`.

- [ ] **Step 7: PR + merge + clean up**

```bash
git add -A && git commit -m "refactor(skills): adopt canopy agent-core stubs (turn, task-tracker); supersede self-review; add agent-turn-review"
git push -u origin feat/agent-core-stubs
gh pr create --title "Adopt canopy agent-core stubs" --body "<what moved to local notes; what was dropped and why>"
gh pr merge <n> --squash          # NEVER --delete-branch (worktree)
cd ~ && git -C ~/emdash/repositories/echo worktree remove /tmp/echo-agent-core --force
```

---

### Task 5: Migrate eva (`~/emdash/repositories/eva`, remote `dimagi-internal/eva`)

Same procedure as Task 4 with tokens rendered for Eva, except:
- eva **already has** `skills/agent-turn-review` — verify it matches the current factory stub shape (thin, invokes `canopy:agent-turn-review`); update only if it's a stale full copy.
- eva's `turn` + `task-tracker` are current-factory copies (fleet-align showed zero eva findings), so local notes will likely be `- (none yet)` — do not invent content.
- eva's `self-review` gets the same supersession stub (rendered for Eva).

- [ ] **Step 1: Worktree** (`/tmp/eva-agent-core`, branch `feat/agent-core-stubs`)
- [ ] **Step 2: Read current files in full; list eva-unique content** (expected: none)
- [ ] **Step 3: Write stubs (turn, task-tracker), rendered for Eva**
- [ ] **Step 4: Verify/refresh `agent-turn-review`; supersede `self-review`**
- [ ] **Step 5: Verify stub resolution (as Task 4 Step 6)**
- [ ] **Step 6: PR + `--squash` merge + worktree remove**

---

### Task 6: Migrate hal (`~/emdash/repositories/hal`, remote `dimagi-internal/hal`)

Hal is the divergent lineage — **preserve its evolved content as local notes**, don't flatten it.

**Files (in hal's repo, via a fresh worktree):**
- Replace: `skills/turn/SKILL.md` (divergent copy → stub + rich local notes)
- Create: `skills/task-tracker/SKILL.md` (hal has none — net-new stub)
- Create: `skills/agent-turn-review/SKILL.md` (verify first: hal runs an equivalent review inline)
- Replace: `skills/self-review/SKILL.md` (→ supersession stub, rendered for Hal)
- Modify: `config/gating.json` (add the missing deny rail)

- [ ] **Step 1: Worktree** (`/tmp/hal-agent-core`, branch `feat/agent-core-stubs`)

- [ ] **Step 2: Read hal's `skills/turn/SKILL.md` IN FULL.** Hal-unique sections that MUST survive as local notes (fleet-align identified these; verify against the file): `§self-improve canopy` (hal's architect mandate), `§situational awareness across jonathan's repos`, `§propose, then do the high-leverage work (natively)`, `bin/hal-turn-close`, pointers to `skills/architect` / `skills/canopy-sweep`. Note: hal's `§skill self-check & close` generics were already promoted into the core (PR #311) — only the hal-specific residue goes in notes.

- [ ] **Step 3: Write hal's `turn` stub** (Task 2 shape, rendered for Hal) with local notes carrying each Step-2 item as a one-to-three-line pointer (link the detailed content's home — e.g. `skills/architect` — rather than pasting long prose into the stub).

- [ ] **Step 4: Create hal's `task-tracker` stub** (net-new; local notes `- (none yet)`).

- [ ] **Step 5: agent-turn-review.** Read hal's turn/close tooling for its inline "agent-turn-review" step. Stamp the factory `_AGENT_TURN_REVIEW_SKILL` rendered for Hal, and put hal's inline send-path specifics in `## Hal-specifics`. Supersede `skills/self-review` with the supersession stub (rendered for Hal).

- [ ] **Step 6: Add the missing gating rail.** In `config/gating.json`, append to `"deny"` (JSON as it appears in the file):

```json
{
  "tool": "Bash",
  "pattern": "canopy\\s+email\\s+send\\b(?=[^\\n]*--account)",
  "message": "BLOCKED: `canopy email send --account` overrides Hal's repo identity — one mailbox per agent, never shared (identity bleed is the fleet's one hard rule). Send via bin/hal-email, which pins this repo's identity."
}
```

Validate: `python3 -c "import json; json.load(open('config/gating.json'))"` → no error. If hal has no `bin/hal-email` shim, keep the rail (it blocks the bypass regardless) and adjust the message's "Send via" line to hal's actual sanctioned send path.

- [ ] **Step 7: Verify stub resolution (as Task 4 Step 6)**

- [ ] **Step 8: PR + `--squash` merge + worktree remove** (commit message: `refactor(skills): adopt canopy agent-core stubs; add task-tracker + agent-turn-review; add --account deny rail`)

---

### Task 7: Measure — the loop isn't closed until the findings collapse

- [ ] **Step 1: Re-run the deterministic fleet check**

Run: `canopy fleet-align --no-llm` (installed CLI — post-deploy it has the new taxonomy)
Expected: the 2026-07-13 findings collapse — no `distribute` findings for turn/task-tracker/agent-turn-review, no missing-skill finding for hal, no gating finding for hal. Divergent-lineage `reconcile` findings should also clear (stubs match the stub template). Any remaining finding: read it; if it's caused by this migration, fix forward in the responsible repo; if pre-existing/unrelated, report it.

- [ ] **Step 2: Report before → after** — before: 6 findings (1 evidence-backed); after: expected 0 (or enumerate what remains and why).

- [ ] **Step 3: Update the session task list** (task #4) and report: PR links (canopy + echo + eva + hal), what shipped, before→after finding counts, and that chunk B (gating engine centralization) is the follow-up plan.

---

## Self-Review (done at plan-writing time)

- **Spec coverage:** §3.1 core docs → Task 1; §3.2 stubs → Task 2; §3.3 factory → Task 2; §3.4 evolution loop → encoded in the core docs' header + turn Step 3; §4 gating → chunk B (separate plan) except hal's stopgap rail → Task 6 Step 6 (spec §5.3 allows this); §5 migration → Tasks 3–6; §6 fleet-align role → Task 7 verifies collapse (code changes to fleet-align itself: none needed, taxonomy is derived); §7 testing → Tasks 1–2 tests + Task 4–6 Step 6 verifications + Task 7 measure. `canopy agent doctor` "core resolvable" check: NOT in this plan — deferred to chunk B with the doctor's gating checks (noted in Task 7 report).
- **Placeholders:** none — full doc/stub texts included; per-agent local-notes content is intentionally judgment work with explicit read-first steps and named must-survive sections.
- **Type consistency:** stub file paths, `_TEMPLATES` keys, and test names consistent across tasks.
