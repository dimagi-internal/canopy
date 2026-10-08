# Agent threads — contract between canopy-web and canopy (v1)

Jonathan, 2026-10-07: agents should talk to each other directly, as "the start of a robust
approach" to increasingly complicated agent→agent interactions. First use: the huddle's
agreement step — when a teammate answers "In, with changes" (`amend`) on an idea, the idea's
author and that teammate settle it in a direct thread instead of one relayed hop.

## Concept
A **thread** is a bounded, moderated conversation between named fleet agents.
- canopy-web STORES the thread record and ENFORCES its limits (server-side, not by trust).
- Each MESSAGE is an ordinary one-shot harness turn for the speaking agent, tagged
  `origin_ref = {"kind": "thread_message", "thread": "<id>", "n": <1-based>, "speaker": "<slug>"}`
  and parented on nothing special. The message's text is NOT stored separately: canopy-web
  DERIVES messages from those tagged turns, reading each turn's reply block exactly the way
  apps/huddles reads a round reply (report_summary → report-only close-out → transcript).
- No agent ever waits on another inside its own turn. The MODERATOR (a CLI loop run by the
  thread's opener, e.g. Ada running a huddle) sends the next message and decides when it ends.

## canopy-web (new app `apps/threads`, mounted at /api/threads and /api/w/{ws}/threads)

Model `AgentThread`:
- `id` str PK, `thr-<12 hex>` (server-generated)
- `workspace` FK (the opener's workspace, as other per-workspace rows do)
- `kind` str (e.g. "agreement"; free text, max 40)
- `purpose` str (one line, max 300)
- `participants` JSON list of `{"agent": "<slug>", "role": "<free text>"}` (2..6, distinct agents)
- `moderator` str (agent slug that opened/moderates it)
- `parent` JSON dict (e.g. `{"huddle": "<huddle id>", "title": "<idea title>", "lead": "<slug>"}`), may be {}
- `context` text (the opening material every message prompt quotes verbatim; max 20k)
- `max_messages` int (default 4, 1..12)
- `deadline_at` datetime (default created_at + 90 min)
- `status`: open | settled | out_of_budget | timed_out | cancelled
- `outcome` JSON dict (set on close; for kind=agreement: `{"result": "agreed"|"not_agreed", "proposal": {...revised idea, optional}, "why": "..."}`)
- `created_by` user FK, `created_at`, `closed_at`

Endpoints (auth: the usual PAT/session; write needs editor on the workspace like task writes):
- `POST /api/threads/` body `{kind, purpose, participants, moderator, parent?, context?, max_messages?, deadline_minutes?}`
  → 201 ThreadOut. Refuse 409 if an OPEN thread already exists with the same parent and the
  same participant set (idempotent re-open: return the existing one with 200 instead of 409 —
  pick 200+existing, it makes the moderator loop resumable).
  Refuse 422 if the request's parent turn (X-Canopy-Parent-Turn header / `parent.turn` of the
  calling turn, if present) is itself a `thread_message` turn — depth limit 1 for v1.
- `GET /api/threads/?parent_key=<k>&parent_value=<v>&agent=<slug>&status=<s>` → list ThreadOut (newest first, limit 50).
  (parent filter: e.g. parent_key=huddle&parent_value=<huddle id>)
- `GET /api/threads/{id}` → ThreadOut with `messages`.
- `POST /api/threads/{id}/close` body `{status: settled|out_of_budget|timed_out|cancelled, outcome?: {...}}` → ThreadOut. Only an open thread can close (409 otherwise).

ThreadOut: `{id, kind, purpose, participants, moderator, parent, context, max_messages,
messages_used, deadline_at, status, outcome, created_at, closed_at, messages: [MessageOut]}`
MessageOut: `{n, speaker, turn_id, status (turn status), created_at, finished_at,
content_hidden, prompt (if readable), block (parsed reply dict or null), reply_source, reply_error}`
— `messages_used` counts tagged turns (any status) for the thread.

GUARD (the important part) — in harness turn creation (`POST /api/harness/turns/` and any other
path that creates a turn with an origin_ref), when `origin_ref.kind == "thread_message"`:
- thread exists and is `open`, else 409 "thread <id> is <status>"
- now < deadline_at, else 409 "thread <id> passed its deadline" (and leave status alone; the moderator closes it)
- messages_used < max_messages, else 409 "thread <id> used its <N> messages"
- `origin_ref.speaker` is a participant AND equals the target agent of the turn, else 422
- `origin_ref.n` == messages_used + 1, else 409 (keeps ordering strict; an idempotent retry with the
  same idempotency key must still return the existing turn — check idempotency FIRST)
Unit tests for every refusal.

Reply block (what the speaking agent files): a fenced block
```thread
{"thread": "<id>", "n": <n>, "from": "<slug>", "says": "<what it says to the others, plain prose>",
 "position": "agree" | "counter" | "decline" | "question", "proposal": {<optional revised idea, same shape as a huddle proposal>}}
```
filed as the turn's close-out via `canopy agent turn --slug <me> --session-id "thread:<id>:<n>" --title "thread <id> message <n>" --summary "$(cat <file>)"`.
canopy-web's parser: last ```thread fenced block whose "thread" == id and "n" == n; also accept a bare ```json block / bare JSON object with those keys (same leniency as huddles' extract_block).

Frontend: `/w/:ws/threads/:id` page: purpose, participants, status/outcome at top, then the
messages as a conversation (speaker avatar, says, position pill: agree→"Agrees", counter→"Suggests a change",
decline→"Doesn't agree", question→"Asks"), each expandable to the full block/prompt. Plain words,
no engine jargon. The huddle page shows the huddle's threads (GET /api/threads/?parent_key=huddle&parent_value=<id>):
in the Diagram as a "Step 4 · Settling changes" section with DIRECT arrows between the two agents
(speaker → the other participant(s)), and in the Story/outcome, an idea whose agreement thread
settled `agreed` counts as agreed (amend accepted) and links "the conversation" to the thread page.

## canopy (engine + CLI + huddle integration)
- `src/orchestrator/thread.py` (pure: prompt rendering, reply-block extraction/validation, the
  moderator's next-step decision) + `src/orchestrator/thread_cli.py` → `canopy thread …`:
  - `open --kind --purpose --participant slug:role (repeatable) --moderator --parent k=v (repeatable) --context-file --max-messages --deadline-minutes` → prints thread JSON
  - `say --thread <id> --to <slug>` → renders the message prompt (purpose, context, the WHOLE thread so far verbatim, your role, messages left, the reply-block instructions) and dispatches the tagged turn via the existing build_turn_payload path (thread_key `thread:<id>`, idempotency key `thread-<id>-n<n>`)
  - `run --thread <id>` → the MODERATOR loop, stateless and resumable (derives everything from GET):
    if no message is outstanding, decide next: for kind=agreement with participants [author, asker]:
    message 1 → author (who sees the asker's change request in `context`); then alternate.
    End when: the two latest positions from DIFFERENT speakers are both `agree` → close settled
    {result: agreed, proposal: latest proposal offered}; any `decline` → close settled {result: not_agreed, why};
    budget used without that → close out_of_budget {result: not_agreed, why: "ran out of messages"};
    deadline passed → close timed_out. Foreground polling like `huddle await`: exit 0 = closed,
    3 = run it again (still waiting), 2 = refusal.
  - `show --thread <id>` → a readable transcript.
- Huddle integration: replace the relayed round 4 for amends with agreement threads:
  `canopy huddle agree --plan plan.json` opens one agreement thread per open amend
  (participants: lead as "author", amender as "asker"; parent {huddle, title, lead};
  context = the proposal verbatim + the amend note verbatim), then the leader runs
  `canopy thread run` on each. `huddle proposals`/`file` treat a settled-agreed thread as
  amend→accepted (adopting its proposal if given) and not_agreed/out_of_budget/timed_out as
  amend→rejected/held. Round 4 relay stays available but agent-core/huddle.md now uses threads.
- `plugins/canopy/agent-core/thread.md`: how a participant answers a thread message (and the
  rules: speak only to the purpose, quote nothing private outside its audience, one block, file it).
- Ada binding: ada `skills/huddle` step for amends → `canopy huddle agree` + `canopy thread run`.

## canopy — as built (v1)
- `canopy thread open --kind K --purpose P --participant slug:role … [--moderator S] [--parent k=v …] [--context-file F] [--max-messages 4] [--deadline-minutes 90]`
- `canopy thread say --thread ID --to SLUG [--mode auto|manual|none] [--prompt-out F]` — refuses while a message is still out.
- `canopy thread run --thread ID [--budget-seconds 540] [--poll 30] [--mode auto|manual|none]` — exit 0 closed / 3 run again / 2 refusal.
- `canopy thread show --thread ID [--json]`
- `canopy huddle agree --plan plan.json [--max-messages 4] [--deadline-minutes 90] [--dry-run]`

Decisions the contract left open, as implemented in `src/orchestrator/thread.py::decide`:
- A message that never yields a usable block (turn failed, block malformed) still spends its
  budget (canopy-web counts every tagged turn), and the SAME speaker is asked again.
- "Agreed" for N participants = every participant's latest position is `agree` (for the
  two-party agreement kind this is exactly "the two latest positions from different speakers").
  A settle check runs before the deadline check, so agreement reached in time is never lost.
- `outcome` for a decline also carries `declined_by`.
- In a huddle, the amend's author is the proposal's lead — unless the lead itself amended (a
  teammate named it lead), then the teammate who proposed it. An open thread holds the proposal
  ("amend still being settled in thread <id>"); a closed not-agreed one holds it as
  "<Author> and <Asker> didn't agree: <why>"; a thread never overrides a relayed round-4
  resolution. Reading threads is advisory: a canopy-web without `/api/threads/` warns and the
  huddle behaves as before.
