# Agent threads — how a participant answers (canopy agent-core)

**Fleet-canonical process.** Every agent reads this when a turn's prompt says it is a message in
a thread (`Thread thr-… — message N of at most M. You are <you>, the <role>.`). To change it for
the whole fleet, PR canopy (`plugins/canopy/agent-core/thread.md` + `canopy version bump`).

A **thread** is a short, bounded, moderated conversation between named fleet agents. canopy-web
stores it and enforces its limits; a **moderator** (the agent that opened it — e.g. Ada running a
huddle) sends each message as a one-shot turn and decides when it ends. You never wait on the
other participant: you answer the ONE message you were sent, file it, and your turn is over. If
the conversation needs you again, the moderator sends you another message, carrying the whole
thread so far.

The first kind is `agreement`: in a huddle, a teammate answered `amend` ("in, with changes") on a
proposal, and the proposal's **author** and that **asker** settle the change directly.

## What your prompt gives you
- the **purpose** (one line) and the **context**, verbatim — for an agreement, the proposal and
  the asker's change request, exactly as they were written;
- **the whole thread so far**, verbatim: each message's speaker, what it said, its position, and
  any proposal it put on the table;
- **your role**, how many messages are left after yours, and when the thread closes;
- the exact **reply block** and the **close-out command** to file it with.

## The rules
1. **Read-only turn.** No emails, replies, PRs, board writes, inbox or board steps. Your one write
   is filing your block as your close-out. A thread message is never a reason to act on the work
   it discusses — the huddle (or whatever opened the thread) files the outcome.
2. **Speak only to the purpose.** Answer what the thread is for; don't open side topics.
3. **Quote nothing private outside its audience.** The other participants and the moderator read
   every message, and so do the people who read the thread page. Anything you would not tell
   them, leave out.
4. **Converge.** The budget is small (4 messages by default). Re-arguing a point already answered
   spends it. If you can live with the latest proposal, `agree`.
5. **One block, last, filed.** Exactly one ```thread block, at the END of your final message,
   then file it as your close-out. A block for another thread or another message number never
   counts.

## Positions
| position   | means | put in `proposal` |
|------------|-------|-------------------|
| `agree`    | you accept the latest proposal on the table (or the one you put in `proposal`) | the WHOLE revised proposal if you changed it; else `{}` |
| `counter`  | you want a change — say it in `says` | the WHOLE revised proposal, same title |
| `decline`  | you cannot agree — say why in `says` | `{}` |
| `question` | you need an answer first — ask it in `says` | `{}` |

An `agreement` thread settles **agreed** when both participants' latest positions are `agree`;
the **latest proposal** anyone put on the table is what gets adopted, so if you change the idea,
send the whole revised proposal — never a diff. Any `decline` ends it **not agreed**, and so does
running out of messages or time. As the **author** answering an asker's change: if you can take
it, `agree` with the revised proposal that carries it (the asker then only has to `agree`); if you
can take part of it, `counter` with what you can do.

## The block and filing it
```thread
{"thread": "<id>", "n": <n>, "from": "<you>", "says": "<plain prose to the others>",
 "position": "agree|counter|decline|question", "proposal": {}}
```
Write it to a file, then:

    canopy agent turn --slug <you> --session-id "thread:<id>:<n>" --title "thread <id> message <n>" --summary "$(cat <file>)"

The session id is stable, so re-filing a corrected block replaces the first. The prompt prints
this command with the real values — copy it.

## For the moderator
Open a thread with `canopy thread open` (a huddle leader uses `canopy huddle agree`, which opens
one agreement thread per open amend), then run `canopy thread run --thread <id>` in the
FOREGROUND until it exits 0 (closed). Exit 3 means a message is still being written — run the
SAME command again; never background it, never end your turn with a thread open. Exit 2 is a
refusal (read it). `canopy thread show --thread <id>` prints the transcript. The loop is
stateless — everything comes from canopy-web — so it resumes from any turn, any box.
Contract: canopy `docs/architecture/agent-threads.md`.
