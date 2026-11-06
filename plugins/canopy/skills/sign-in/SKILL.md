---
name: sign-in
description: |
  Sign this session in to a third party with the person's own approval, for this session
  only — today Claude Design, so `DesignSync` / `/design-sync` works on a cloud runner. Use
  when a tool says it needs a sign-in that cannot run in this session (e.g. "DesignSync needs
  design-system authorization" and `/design-login` cannot run here) and the person is in the
  conversation. Give them the link, they approve on the provider's own page and paste the code
  back, you hand the code to `canopy sign-in code`, do the work, then `stop`. Never for a
  person who is not there, and never to keep a sign-in: it is removed at stop, at session end
  and after an hour.
---

# Sign in — the person approves, you finish it, then it is gone

Some sign-ins can't be done by an agent and shouldn't be kept. The person does the one thing
only they can (approve on the provider's consent page, paste back the code it shows); this box
runs the provider's own sign-in as a separate process and removes what it wrote afterwards.
The code is useless without the verifier held by that process, so it is safe to paste in chat,
and canopy-web never holds anything (this is not `act-as`: no canopy-web login is involved).

## When

- A tool refuses for lack of a sign-in that can't run in your session, **and the person is in
  the conversation** (`presence: PRESENT`). An emailed or scheduled turn has nobody to open the
  link: say what you could not do and stop; `canopy sign-in start` refuses there anyway.
- Not when the provider is already signed in on the box (it says so, and leaves it alone).

## The flow (one turn, with the person)

1. `canopy sign-in start design` — prints the link. **Say it to the person as a message** with
   what you will do (e.g. "sync the design system to claude.ai/design") and that they paste back
   the code the page shows. The link is good for about 5 minutes.
2. They paste `<code>#<state>`. Hand it over without echoing it back:
   `canopy sign-in code design -` with the code on stdin.
3. Do only what you said (e.g. `DesignSync`).
4. `canopy sign-in stop design` as soon as you are done — don't wait for the hour.
5. Say what you did: "Signed in to Claude Design at 14:02 with your approval; synced X; signed
   out at 14:06. It stays valid at claude.ai until you revoke it (`/design-revoke`)."

## Limits to say out loud

- The credential sits where the provider's CLI keeps it, so **every session on this box can use
  it while it lives**, not only yours. Keep the window short.
- Removing it here does not revoke it at the provider; `/design-revoke` does.
- A `designOauth` already on the box before you started is never touched.
- On a laptop (macOS) the credential is in the keychain; `start` refuses — sign in with
  `/design-login` yourself.

Backed by `src/orchestrator/sign_in_cli.py`; cleanup by `hooks/sign_in_cleanup.py`.
