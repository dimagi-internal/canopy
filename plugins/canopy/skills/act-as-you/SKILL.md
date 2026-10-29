---
name: act-as-you
description: |
  Act as the person who started your turn on canopy-web, for at most 30
  minutes, after they approve with their passkey — e.g. to read one of their
  private sessions your own login gets a 404/403 on. Ask with an honest,
  specific reason and the narrowest scope, show them the approve link, do only
  what you said, then stop. Never for what your own login can already do, a
  person who is not there, or a confined turn.
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
- The person who started the turn is present in the conversation and can approve now.

## When NOT to use it

- **Your own login suffices.** Try it first. Act-as is never a convenience or a shortcut.
- **The person is not there** — a scheduled turn, an inbound email, another agent asked. Nobody
  can approve; say what you could not reach instead. (canopy-web answers 403 when the turn has no
  verified human asker.)
- **A confined turn** (an `ask` session for a caller) or a **local turn** (it already acts as
  them) — `canopy act-as` refuses both.
- **For someone other than the asker.** It is always the person who started this turn.

## The flow

1. **Ask, honestly and narrowly.** Say exactly what you will do as them, and pick the narrowest
   scope that does the job — `read` unless you must change something:

   ```bash
   canopy act-as request --reason "Read your session 4328 (transcript + turns) to find why it stalled" --scope read --no-wait
   ```

   It prints an **Approve at:** link and returns.

2. **Show the link to the person, verbatim, and END YOUR TURN.** In a chat, never block the
   turn waiting for them (no `--wait`, no background poll): post the link, say what you'll do
   once they approve, and stop. The request lives 30 minutes and survives your turn ending.
   **On your next turn** (or as soon as they say they approved), collect it:

   ```bash
   canopy act-as status        # approved → the credential is stored; pending → still waiting
   ```

   State is kept per Claude session, not per turn, and is recovered from canopy-web if the
   local copy is gone — so a new turn picks it up. Still pending: tell them it's waiting on
   their approval; don't re-request (`request` reuses a pending one anyway). Declined or
   expired means you do **not** act as them: carry on with your own login and tell them what
   you couldn't do. (Only outside a chat — an unattended script with the person watching —
   use `--wait`, which polls up to 240 s and retries network blips.)

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
