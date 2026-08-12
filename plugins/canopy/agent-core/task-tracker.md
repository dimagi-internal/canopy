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
- **`[MANUAL — …]`** — a **Next action** beginning with this literal marker means the human has
  taken the task **off your queue entirely**: they are doing it themselves. It is NOT the same as
  Assigned. Assigned says *"you're next after they move"*; `[MANUAL — …]` says *"stop bringing
  this to me at all."* A marked task is invisible to your turn: do not work it, do not propose
  next steps on it, do not list it in a close-out recommendation, and **do not run the drain-time
  blocker re-check on it** (the rule below is explicitly exempted). It stays `in_progress`
  because it is still live *for the human* — `declined` would be a lie that invites a later turn
  to close the underlying issue. Only a direct request re-opens it; when one comes, drop the
  marker. Set it with the human's words in the card's Notes so the reason survives:
  `canopy agent set --slug <slug> --task-id <T> --next-action "[MANUAL — <who> handles this] …"`
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
- **Drain-time: re-check the BLOCKER on every task parked on a human** — *except* one whose Next
  action carries the `[MANUAL — …]` marker, which is off your queue entirely and must be skipped
  here (see the vocabulary above). Without that exemption this rule is precisely what drags a
  hand-back task into every subsequent turn: the human says "I'll handle it," and the next drain
  dutifully re-checks its blockers and re-surfaces it as the top recommendation, because a card
  parked on a human is exactly what this rule is built to hunt. A card whose **Next
  action** names something it waits on — an open PR in someone else's lane, an unmerged
  dependency, an expired credential, an upstream decision — asserts that blocker is *still true*,
  and nothing on the board expires that claim. The card reads identically the day the blocker
  clears and a month later. Worse, the re-validate rule below never fires to catch it, because
  that rule triggers on *resuming* a task and this task is parked on someone else: it is not
  waiting on you, so you never pick it up, so nobody ever re-reads it. A stale blocker is
  therefore invisible by construction — it can only be found by checking it on purpose.
  So check it: each named blocker is usually **one command** (`gh pr view <n> --json state`,
  `gh issue view <n>`, an auth probe). If it cleared, update **Next action** to the real next
  step and say so in the close-out — the human has been sitting on a decision that stopped being
  blocked. Do this before the situational-awareness scan; a parked task that just became
  actionable outranks anything the scan will turn up.
  (2026-08-11: a connect-labs task had sat since 08-03 reading "blocked on ctsims's open PR
  #1070 + expired AWS SSO." Both had cleared — #1070 merged 08-03, the SSO was valid — and the
  in-repo half had been shippable the whole week. The two `gh`/`aws` calls that proved it took
  under a minute; the card had been wrong for eight days, and its human read it as still blocked.)
- **Taking on multi-turn work:** create the task (status `in_progress` if a human asked for it,
  `suggested` if you are proposing it), immediately `set` rationale + plan + links,
  and give it a **project folder** whose link goes in Links.
- **Resuming an existing task: re-validate its brief against current reality BEFORE building.**
  A task's `rationale` / `plan` / dispatch brief is a *snapshot of when it was written*, and the
  board gives it no expiry — it reads equally authoritative on day 1 and day 30. Re-check the
  specific claims it rests on (the code it cites still says that? the issue still open? nothing
  merged in the meantime?) and write what changed into the task before you touch anything. If the
  premise moved, **say so and re-scope — do not build the stale brief.** Cheap: minutes. The
  failure it prevents is expensive and silent, because building the wrong thing competently looks
  exactly like progress. (2026-07-31: a task dispatched three days earlier said "delete this
  linear scan"; three PRs had merged against it in the interim, and re-reading the target file
  first showed the flag already gone, the scan memoised, and the issue's own done-when no longer
  achievable in that repo at all. The re-read took ten minutes and replaced a day of wrong work.)
- **During work:** keep **Next action** current — it is the card headline a human scans.
- **Close of turn:** package every turn that advanced a task —
  `canopy agent turn --slug <slug> --title "…" --task <ext_id> --work-product-url <url>`.
  This builds the per-task history spine: which turn did what, with which deliverables.
