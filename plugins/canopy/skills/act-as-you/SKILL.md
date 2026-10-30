---
name: act-as-you
description: |
  Act as the person who started your turn on canopy-web, for at most 30
  minutes, after they approve with their passkey — e.g. to read one of their
  private sessions your own login gets a 404/403 on. Ask with an honest,
  specific reason and the narrowest scope, put the approve page in front of
  them, wait for their yes IN THE SAME TURN when they are here, do only what
  you said, then stop. Never for what your own login can already do, a person
  who is not there, or a confined turn.
---

# Act as you — ask, show the link, do only that, stop

Some things on canopy-web belong to one person: their private sessions, their own settings, a
page only they can see. Your login (the turn's agent login) gets a 404 or 403 on them, and
that is correct. When the person who started this turn wants you to look anyway, **ask them**:
canopy-web pushes the request to them, they approve it with their passkey, and `canopy act-as`
holds a short-lived credential that acts as them.

The limits are canopy-web's, not yours to stretch: **30 minutes at most, never extended**; only
while **this turn is still running**; `read` scope is GET only; `write` scope excludes a
server-side list of routes. Every call you make with it is logged as **"<agent> as <person>"**
— they can see exactly what you did as them.

## When to use it

- You need to see or do something only the person who asked can, and your own login has just
  404'd or 403'd on it (e.g. "Ada, look at my session 4328 and tell me why it stalled").
- The person who started the turn is present in the conversation (`presence: PRESENT`) and can
  approve now — you will wait for them in this turn.

## When NOT to use it

- **Your own login suffices.** Try it first. Act-as is never a convenience or a shortcut.
- **The person is not there** — a scheduled turn, an inbound email, another agent asked. Nobody
  can approve; say what you could not reach instead. (canopy-web answers 403 when the turn has no
  verified human asker.)
- **A confined turn** (an `ask` session for a caller) or a **local turn** (it already acts as
  them) — `canopy act-as` refuses both.
- **For someone other than the asker.** It is always the person who started this turn.

## The flow

**Is the person here?** Your caller context says `presence: PRESENT` or `ABSENT` (canopy's
caller hook; `canopy act-as request` reads the same record). Present = they are in this
conversation now and can tap the link: **wait for them, in this turn.** Absent = nobody can
approve right now: ask, say so, and end the turn.

**Why two commands and not one blocking call.** A command's output reaches the chat only when
the command *ends*, so `request --wait` would hold the very link the person has to tap until
the wait is over. So: ask without waiting, *say the link as a message*, then wait.

1. **Ask, honestly and narrowly.** Say exactly what you will do as them, and pick the narrowest
   scope that does the job — `read` unless you must change something:

   ```bash
   canopy act-as request --reason "Read your session 4328 (transcript + turns) to find why it stalled" --scope read --no-wait
   ```

   It prints the **Approve at:** link and a ready line to say to the person, and returns. On a
   session running on the person's own computer it also **opens the approval page in their
   browser** (`--open`, automatic there, never on a cloud runner), so approving is one tap and
   their passkey. canopy-web has also already pushed the request to them (phone, supervisor feed,
   Settings), so they may approve from there.

2. **Say the link to the person as a message, then wait for the yes — same turn.** Write the
   message *before* the next command so it reaches them while you wait, with the link tappable
   and what you will do once approved:

   > I need to look at your session 4328 to find why it stalled (read-only, 30 minutes at most).
   > **[Tap to approve — it asks for your passkey](the-approve-url)**. I'll carry on as soon as you do.

   Then collect the answer without ending the turn:

   ```bash
   canopy act-as status --wait --timeout 240     # returns the moment they approve
   ```

   - **Approved** → the credential is stored; go to step 3, in this same turn.
   - **Exit 4, still pending** (no answer in 240 s) → tell them it is still waiting on them, and
     either run `status --wait` once more if they say they are on it, or end the turn; the request
     lives 30 minutes and survives your turn ending. **Don't re-request** (`request` reuses a
     pending one anyway). On your next turn (or when they say they approved):
     `canopy act-as status`.
   - **Declined or expired** → you do **not** act as them: carry on with your own login and tell
     them what you couldn't do.
   - **They are absent** (`presence: ABSENT`): `request` does not wait on its own. Show/say what
     you asked and why, say what you'll do once they approve, and end the turn; collect it with
     `canopy act-as status` on their next message.

   State is kept per Claude session, not per turn, and is recovered from canopy-web if the local
   copy is gone — so a new turn picks it up. (`request --wait` blocks until the end and is only
   for an unattended script with the person watching their terminal; in a chat use the two-step
   above.)

3. **Do only what you said.** Through the CLI, never by hand:

   ```bash
   canopy act-as get /api/sessions/4328/ [--param k=v] [--workspace SLUG]
   canopy act-as call PATCH /api/... --json '{"...": "..."}'   # write scope only
   ```

   A 403 is printed verbatim — it is the server saying this is outside the grant; don't route
   around it. The credential file under `~/.canopy/act-as/` is deny-railed: never read it, never
   put it in curl.

4. **Stop the moment you're done** — don't wait out the 30 minutes:

   ```bash
   canopy act-as stop
   ```

5. **Say in your reply what you did as them**: "As you (approved 14:02, read scope) I read
   session 4328's transcript and turn list; nothing else. Stopped at 14:06."

## Housekeeping

`canopy act-as status` shows this turn's grant (never the credential). The credential file is
0600, ignored and deleted once it expires, deleted by `stop`, and deleted when the session ends
(canopy's SessionEnd hook). Nothing else in canopy picks it up: every other `canopy` call and the
canopy-web MCP keep acting as you, the agent.

If `canopy act-as request` says **this canopy-web doesn't support act-as yet** (exit 3), the
server predates it: tell the person, and ask them to look or share it with you instead.
