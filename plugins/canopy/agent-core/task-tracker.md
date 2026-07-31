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
canopy agent set  --slug <slug> --task-id T<N> \    # the ext_id off the card (or the numeric id)
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
