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
- **Key** — every task also has a workspace key (`DIM-142`, the `key` field), its name in
  canopy-web's UI and in links (`/w/<ws>/tasks/<KEY>`). Your `T<N>` still names it on your
  board; either works wherever a task ref is taken.
- **Status** — `suggested` (you proposed it; a human validates) → `todo` (accepted, next up)
  or `backlog` (accepted, not now) → `in_progress` → `done` / `declined`. A person moves your
  suggestion to `todo`/`backlog` when they accept it without starting it: treat `todo` as
  yours to start when you have room, `backlog` as parked until someone moves it. There is no
  "blocked": *waiting on a person* is expressed by **Assigned**.
- **Assignee** — a task is assigned to an agent or to a person (`agent_slug` /
  `assignee_email`). Your board lists the tasks you are assigned plus those you filed that no
  agent has; a task you filed but assigned to a person is theirs to do — follow it, do not work it.
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
- **Confidence** — `high` / `low`, for suggested tasks (how sure you are).
- **Due** — `YYYY-MM-DD`; past-due un-done tasks are flagged on the board.
- **Links** — every stable artifact: the thread, the doc, PRs, the project folder. Working state
  (maps, dossiers, notes) hangs off the task via links — NOT committed into target repos.
- **Ask** — what a task asks a person, if anything: `review` ("should I do this?") or
  `question` ("I need an answer"), with the ask text. Open until someone acts on it; the
  board's *Waiting on you* is every open ask plus every task parked on a person. Raise one
  by creating the task with `ask_kind` + `ask_body` (MCP `create_tasks`, or
  `canopy agent-publish tasks <file.json>`); a task with no ask is simply work in flight.

The board groups by **who has the ball**: Suggested · Waiting on a human · agent
working · Done.

## Verbs (installed canopy CLI — no bespoke script)
```
canopy agent add  --slug <slug> --title "…" --next-action "…" \
    --status in_progress --owner <human> --assigned <agent-name> \
    --links "Thread|https://…, Doc|https://…"          # create (auto T<N>)
canopy agent set  --slug <slug> --task-id T<N> \    # the ext_id off the card — tasks have no other id
    --rationale "why" --plan "first steps" --source-url <url>   # store context — never re-derive
canopy agent set  --slug <slug> --task-id T<N> \
    --append-notes "--- <date> TURN ---\n<what this turn did>"  # LOG a turn; --notes REPLACES the history
canopy agent add  --slug <slug> --title "…" --project "<Project>"   # file it into a project
canopy agent tasks --slug <slug> --open       # DRAIN the board: unresolved tasks only
canopy agent tasks --slug <slug>                # every task ever (needed to compute the next ext_id)
canopy agent tasks --slug <slug> --status done  # one status (repeatable; human spellings ok)
canopy agent actions --slug <slug>              # DRAIN the actions people took on your tasks
canopy agent applied --slug <slug> --id <N> --note "what I did"
```
Over MCP the same verbs are `list_tasks` / `create_tasks` / `patch_task` / `act_on_task` /
`list_task_actions` / `mark_task_action_applied` / `patch_project`.

## Acting on actions (the canopy-web DB is the source of truth)
The board at `/agents/<slug>` is a **control surface**: a person acts on a task — **approve**,
**decline** (the comment is the reason), **reply** (on a question, the reply is the answer),
**nudge** (editors, on a task already in progress) or **done**. An action that hands you work
**starts your turn** — you do not wait to find it on the queue (canopy-web, 2026-10-08):

- **approve** flips the task to in_progress, closes its ask, and enqueues a turn: the task's own
  `on_approve` specs, or — with none — one turn canopy-web writes from the card
  (`Work task T<N>: <title>` + next action, plan and the approver's note). That turn IS the
  approval: the row is already `applied`, so just do the work and update the task.
- **nudge** ("Nudge <Agent>") enqueues the same card turn on an in-progress task, status
  unchanged — someone wants you to look at it again now. It replaced `dispatch`
  ("do this now"), which is gone: it only queued a row and woke nobody.
- **reply** that ANSWERS your open question enqueues a turn carrying the answer
  (`ANSWERED BY …`) — the question's own `on_approve` specs, or, with none, one turn canopy-web
  writes from the card. Whoever answers (viewer included) wakes you: you asked for it. Act on
  the answer and update the task.
- **reply** from an editor on a live task enqueues a short turn carrying the note — answer it on
  the task (`set --append-notes`) and fold it into the work. A **viewer's** note still lands on
  your queue as a pending row.
- **decline** closed the task; read the comment, record anything worth keeping.

**At the start of every turn, drain what is still queued** — viewer notes, anything a turn did
not already carry:
```
canopy agent actions --slug <slug>      # pending actions, oldest first: #<id> <action> -> T<N>
# ... do the work (under the normal guardrails — outbound actions still need approval) ...
canopy agent applied --slug <slug> --id <N> --note "what I did"   # mark it carried out
```
A reply you post on your OWN card — a note, or an answer to your own question — never wakes you
(canopy-web skips it, so a turn that replies cannot loop) — record your own progress with `set --append-notes`.

When you *suggest* a task, store the context immediately (`set` — rationale, plan,
source url) so it is never re-derived later.

## The human why — their words, not your reading of them

`rationale` is YOUR inference. It does not say who wanted the work, and a reader cannot tell a
person's ask from your guess at one. So a task has a second, separate field, **`human_why`**: what
a person actually said that the task answers. The board shows it above your rationale and draws
it heavier, because when a person's view is a good one it outweighs your inference (the Supply
brief, Part 3, 2026-10-09).

**Whenever a task comes from a person, record their words**, one entry per quote:
- **`quote`** — their words **verbatim**: copied from the email, the doc comment, the prompt, the
  Slack message. Trim to the part that carries the ask; never reword, summarise or fix typos.
- **`who`** — `{email, name}` (or `person_id`). canopy links the real Person when it knows them in
  your workspace; someone it has never met still gets their name and address on the card.
- **`source_url`** — where they said it: the thread, the doc (a `#heading=` anchor beats the bare
  doc), the session. **`said_at`** — when, if you know it.

**Leave it EMPTY rather than paraphrase.** No quote you can point at means no `human_why` — your
reading of what they meant belongs in `rationale`, labelled as yours. A paraphrase in
`human_why` is worse than nothing: it puts your words in a person's mouth on the one field built
to keep the two apart. Work you proposed yourself has no human why until someone says something
about it; when they do, add it.
```
canopy agent add --slug <slug> --title "…" --quote "<their exact words>" \
    --quote-by "Jonathan Jackson <jj@dimagi.com>" --quote-url <where> [--quote-at 2026-10-09]
canopy agent set --slug <slug> --task-id T<N> --append-quote "<their exact words>" \
    --quote-by "…" --quote-url <where>          # keeps the quotes already on the card
```
Over MCP: `create_tasks` / `patch_task` take `human_why: [{quote, who: {email, name}, source_url,
said_at}]` — a PATCH **replaces** the list, so send the ones to keep.

## Projects — one name, two halves

A **project** is a real piece of work that outlives a turn: the conference you are planning,
the partnership you are chasing, the initiative with four PRs in it. It has two halves and
**one name**:

- **The folder** — `$GDRIVE_ROOT_FOLDER/Projects/<name>/` holds the files (see
  `deliverables.md`, the non-negotiable layout).
- **The project on canopy-web** — holds what the folder cannot state: what is open, what is
  parked on a person, whether the thing is still running. Visible at `/agents/<slug>` under
  Projects.

**Use the same string for both.** It is what `canopy gdoc publish --project "<name>"` already
resolves the folder from, so one name keeps the folder, its deliverables and its state pointing
at each other. A project registered under a different name than its folder is a second place to
look instead of one place to look.

**The folder is a property of the project.** `project-add` finds or creates
`Projects/<name>` and links it; `project-folder` links one to an existing project; and
`canopy gdoc|gsheet publish --project <P<N>|name>` files into the folder the project links
(linking it on first use). A session in ANOTHER repo working on an agent's project reads the
folder off the project (`get_project` → `drive_folder_url`) and publishes with
`--agent <slug>` — no agent checkout, no sourced env, no asking the human where it is.

```
canopy agent projects --slug <slug> --active           # what is running (JSON)
canopy agent project-add --slug <slug> --name "<Project>" \
    --outcome "what DONE looks like"                  # links Projects/<name> too
canopy agent project-folder --slug <slug> --project P<N>   # link a folder (idempotent)
canopy agent project-folder --slug <slug> --all            # backfill every unlinked one
canopy gdoc publish --agent <slug> --project P<N> --md x.md --name "…"   # from any repo
canopy gdoc publish --agent <slug> --task T<N> --md x.md --name "…"      # into the TASK's folder
canopy agent project-set --slug <slug> --project "<Project>" --status done
canopy agent project-audit --slug <slug> [--json]      # what the board is missing (read-only)
```

**A project's toolbox: open the project, then use it.** A project on canopy-web can carry a
knowledge base, skills, shared MCP servers and plugins, for everyone working on it. The `projects`
server reaches every project's with the same few tools: `projects` lists what you may open;
`open_project "<workspace>/<handle>"` gives its card (a task's brief says `Its toolbox (…): open
it first with open_project("…")` when there is one); the `kb_` tools (`kb_search`, `kb_read`,
`kb_table`, …) read its knowledge base, each taking `project`; `project_skill` reads a skill when
the work needs it. Never copy a project's own server into your plugin's MCP config: that adds its
tools to every session you run.

Then **file the work into it** — a task takes the project's name or its `P<N>`:
```
canopy agent add --slug <slug> --title "…" --project "<Project>" --next-action "…"
canopy agent set --slug <slug> --task-id T<N> --project "<Project>"   # file an existing one
canopy agent set --slug <slug> --task-id T<N> --project ""            # take it out again
```
An unknown `--project` is **rejected and writes nothing** — it names what exists so you can
pick or create. (The API itself keeps the task and files it nowhere, which is right for an API
and would be silent here.)

**One project per real project.** A project per task gives you a directory of single-task
projects, which tells you less than the task list already did — and it is the same mistake the
Drive layout warns about. A genuine one-off needs no project: create the task without one.
Conversely, once a thread has produced deliverables and more than one task, it is a project:
register it, put the folder link on it, and file its tasks in.

**Subtasks, not subprojects.** A big piece of work INSIDE a project — a tender, a study, a
multi-PR feature — is a task with subtasks, not a project of its own: one board per product area
(Jonathan, 2026-10-09). A subtask lives in its parent's project (it inherits it), never in a
cycle, and at most three levels deep; canopy-web refuses anything else. The board nests subtasks
inside their parent's card.
```
canopy agent add --slug <slug> --title "…" --parent T<N>            # a subtask of T<N>
canopy agent set --slug <slug> --task-id T<M> --parent T<N>         # make an existing task one
canopy agent set --slug <slug> --task-id T<M> --parent ""           # top-level again
canopy agent tasks --slug <slug>   # rows carry parent_ext_id; REST/MCP list filter: parent=T<N>|none
```
Over MCP: `create_tasks` / `patch_task` take `parent`. Moving a parent to another project moves
its subtasks with it.

**A task with files gets its own folder, inside the project's.** `canopy gdoc|gsheet publish
--task T<N>` files into the folder the task links — else it finds or creates
`Projects/<project>/<T<N> title>/` (a subtask's inside its parent task's folder), records it on
the task (`drive_folder_id` / `drive_folder_url`, shown as a folder link on the card), and
publishes there. The folder is found by its `T<N>` prefix, so rewording the title never makes a
second one. A task in no project is refused: file it into one first.

**The board holds what is ACTIVE; Drive is the archive.** When work goes quiet, set the project
`--status done` (or `archived`) and **leave its folder exactly where it is — never delete or move
a `Projects/<name>/` folder.** Folders are expected to outlive their board entry; a dormant one
is not a defect. When the work comes back, re-register it **against the existing folder, under
the same name** (`project-add --name "<folder name>" --drive-folder-url <that folder>`), rather
than starting a new folder.

**Checked every turn, not remembered:** `canopy agent project-audit --slug <slug>` reports the
gaps in the active direction — open project-like tasks filed nowhere, active projects with no
folder, project/folder name drift, tasks advanced with no turn record — plus open tasks whose
GitHub issues/PRs are all closed or merged (finished, but the card was never closed) — and feeds the REQUIRED
`projects:` close-out line (`turn.md` Step 4). A task counts as project-like when it has ≥2
links, ≥1 turn record, or ≥2 appended note entries.

**A project moving between agents** ("take this over from <agent>") is `canopy agent handoff`,
run by the receiver — it finds the source agent's sessions and opens the project here with them in
its notes. The procedure (files, grants, thread) is `agent-core/handoff.md`.

## When to use (turn-loop wiring)
- **Start of every turn:** drain `actions` → act → `applied`. The board is a trigger surface
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
  `suggested` if you are proposing it) and immediately `set` rationale + plan + links. If a
  person asked for it, record their words too (`--quote` / `human_why`, verbatim — see "The
  human why" above); if nobody did, leave it empty. If it
  belongs to a project, pass `--project "<name>"`; if it IS a new project, register it first
  (`project-add`, with its `Projects/<name>` folder link) so the folder and its state share
  one name.
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
  `canopy agent turn --slug <slug> --title "…" --task <ext_id>`.
  This builds the per-task history spine: which turn did what. A deliverable the turn produced
  goes on its project's links: `canopy agent project-set --slug <slug> --project "<Project>"
  --append-link "Label|<url>"` (or the task's, `agent set --append-link`, for a one-off).
  Then run `canopy agent project-audit --slug <slug>`, fix the cheap gaps, and close with its
  `projects:` line (`turn.md` Step 4) — that is what catches a turn you forgot to record.
