# Agent threads — contract between canopy-web and canopy

Agents talk to each other directly through bounded, moderated threads. The first use is the huddle's
agreement step: when a teammate answers "In, with changes" (`amend`) on an idea, the idea's author and
that teammate settle it in a direct thread instead of one relayed hop.

## Concept
A **thread** is a bounded, moderated conversation between named fleet agents.
- canopy-web STORES the thread record (`/api/threads/`, `/api/w/{ws}/threads`) and ENFORCES its limits
  server-side, not by trust. The record's fields, routes and the frontend page are canopy-web's (its
  OpenAPI schema); this repo reads them through `src/orchestrator/thread_cli.py`.
- Each MESSAGE is an ordinary one-shot harness turn for the speaking agent, tagged
  `origin_ref = {"kind": "thread_message", "thread": "<id>", "n": <1-based>, "speaker": "<slug>"}`
  (`src/orchestrator/thread.py` builds it). The message text is NOT stored separately: canopy-web DERIVES
  messages from the tagged turns, reading each turn's reply block exactly as the huddle reads a round
  reply (report summary, then report-only close-out, then transcript).
- No agent ever waits on another inside its own turn. The MODERATOR (a CLI loop run by the thread's
  opener, e.g. Ada running a huddle) sends the next message and decides when the thread ends.

## Invariants canopy-web holds (the turn-create guard)

When a harness turn is created with `origin_ref.kind == "thread_message"`, canopy-web refuses unless:
- the thread exists and is `open` (409);
- now is before `deadline_at` (409; the status is left alone, the moderator closes it);
- `messages_used < max_messages` (409);
- `origin_ref.speaker` is a participant AND is the turn's target agent (422);
- `origin_ref.n == messages_used + 1` (409, strict ordering). An idempotent retry with the same
  idempotency key still returns the existing turn, so idempotency is checked first.

Also: opening a thread while the calling turn is itself a `thread_message` turn is refused (depth limit
1), and opening a second OPEN thread with the same parent and participant set returns the existing one,
which makes the moderator loop resumable. `messages_used` counts tagged turns of any status.

## Reply block

The speaking agent files a fenced ```` ```thread ```` block with `{thread, n, from, says, position,
proposal?}` (`position` is one of `agree | counter | decline | question`) as its turn close-out, via
`canopy agent turn --session-id "thread:<id>:<n>"`. canopy-web's parser takes the last `thread` block
whose `thread` == id and `n` == n, and leniently accepts a bare ```` ```json ```` block or bare JSON
object with those keys. The prompt that tells a participant this is rendered by
`src/orchestrator/thread.py`; the participant-side rules (speak only to the purpose, quote nothing
private outside its audience, one block, file it) are in `plugins/canopy/agent-core/thread.md`.

## canopy (engine, CLI, huddle)
- `src/orchestrator/thread.py` is the pure half (ids, tags, prompt, reply-block extraction, the
  moderator's decision); `src/orchestrator/thread_cli.py` is `canopy thread open | say | run | show`
  (`--help` for flags). `run` is stateless and resumable (everything derives from `GET`); exit 0 =
  closed, 3 = run it again (still waiting), 2 = refusal.
- `thread.py::decide` is the moderator rule, including these non-obvious choices:
  - A message that never yields a usable block (turn failed, block malformed) still spends its budget
    (canopy-web counts every tagged turn), and the SAME speaker is asked again.
  - "Agreed" means every participant's latest position is `agree`; any `decline` closes
    `settled` / `not_agreed` and the outcome carries `declined_by`. The settle check runs before the
    deadline check, so agreement reached in time is never lost.
  - Order for `kind=agreement`: message 1 goes to the author, then participants alternate.
- Huddle integration (`plugins/canopy/agent-core/huddle.md` step 4b): `canopy huddle agree` opens one
  agreement thread per open amend; `huddle proposals` / `file` treat a settled-agreed thread as
  amend accepted (adopting its proposal) and not-agreed / out-of-budget / timed-out as rejected or held.
  An open thread holds the proposal; a thread never overrides a relayed round-4 resolution. Reading
  threads is advisory: a canopy-web without `/api/threads/` warns and the huddle proceeds
  (`src/orchestrator/huddle_cli.py`, the "agreement threads" read).
