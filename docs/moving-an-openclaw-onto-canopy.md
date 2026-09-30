# Moving an OpenClaw agent onto canopy — who does what

**Who this is for:** the person whose agent runs on an OpenClaw droplet today and is moving to
canopy — Andrea with **Jarvis**, Shayoni with **Fizzy**, and the next round after them.

You do not have to do the migration yourself. **Hal does the bulk of it.** Your part is the
handful of steps that need *you* — your laptop, your logins, your judgment about what the agent
is for. This page says which is which, in order.

> **The mechanics already have a home.** Every command below is explained properly in
> [Onboarding a new operator](onboarding-a-new-operator.md); this page links into it rather than
> repeating it. Read this page for the *order and ownership*, that one for the *how*.

---

## What changes for the people who talk to your agent — read this first

| Today (OpenClaw) | After (canopy) |
|---|---|
| Email the agent's `@dimagi-ai.com` address | **Same address, same behaviour.** |
| **Telegram** bot | **Gone.** Canopy has no Telegram channel today (email, Slack and canopy-web chat only — see [the operating model](agent-operating-model.md), "Channels over time"). Tell your Telegram users before the switch. |
| One always-on process on a droplet | Turns run in Claude Code on **your machine** (or a cloud runner); every turn is visible on canopy-web. |
| Sends whenever it decides to | Outbound email is **drafted and waits for your approval** by default (turn mode `manual`), and hard rails block the raw send paths. |
| Memory, persona and skills live only on the droplet | They live in a **git repo** you control (`dimagi-internal/<slug>`), so nothing is lost when the droplet goes away. |

---

## The steps

### Phase 1 — Hal does this (no action from you)

Hal needs one thing to start: **SSH access to the droplet** — the key is in 1Password as
`AI-Agents/openclaw-<slug> - SSH Private Key`. Whoever holds the vault shares it with Hal (or runs
the snapshot once in Hal's session). Then Hal:

1. **Snapshots the agent's brain** — persona, skills, memory — off the droplet. Credentials are
   excluded by construction and never land in git.
   (`canopy openclaw-harvest snapshot` → `inventory` → `compare`)
2. **Bootstraps the agent's repo** at `dimagi-internal/<slug>`: the canopy factory scaffold, with
   the OpenClaw persona, memory and every skill ported in. (`canopy openclaw-harvest bootstrap`)
3. **Tidies what came across**: distils the raw persona into `persona.md`, reviews each ported
   skill (keeps the good ideas, rewrites them to canopy conventions), and sets the agent's gating
   rails — including the one that forces all email through the approval path.
4. **Registers the agent on canopy-web** in its division's workspace (Jarvis → `connect`) and
   mirrors its skills, so its page exists at `/canopy/w/<workspace>/agents/<slug>` with this
   checklist on its board.
5. **Makes you an editor (or owner) of that workspace** — you get an invite link.

### Phase 2 — You do this (about an hour, once)

These need your laptop and your logins, so they cannot be done for you. Each links to the exact
section of the onboarding doc. **Shortcut:** install canopy, then paste the prompt in
[Setting up an agent from Claude](setting-up-an-agent-from-claude.md), and Claude works through
steps 3–6 with you.

1. **Install Claude Code, canopy and the prerequisites** — [§1](onboarding-a-new-operator.md#1-prerequisites)
   and [§2](onboarding-a-new-operator.md#2-install-canopy). Works on macOS and Windows.
2. **Accept your workspace invite and mint your personal token** — [§3](onboarding-a-new-operator.md#3-get-your-canopy-web-access-sorted-before-you-build-anything).
   Skip §3a's "which workspace" decision: Hal already placed the agent.
3. **Clone the agent's repo and install it as a plugin** — [§4](onboarding-a-new-operator.md#4-create-the-agent)
   (the "push it to GitHub" step is already done; start at installing the plugin) and
   [§5](onboarding-a-new-operator.md#5-make-it-real--let-agent-doctor-drive).
4. **Re-authorise the mailbox.** The agent's Gmail consent does not move off the droplet —
   you (or Jonathan, who set the accounts up) sign in as `<slug>@dimagi-ai.com` once when
   `canopy agent doctor` asks for it. **On Windows**, read
   [§5a](onboarding-a-new-operator.md#5a-mailbox-setup-on-windows--four-things-that-bite)
   first. If Google refuses the sign-in with *"couldn't verify this account belongs to you"*,
   a dimagi-ai.com admin has to lift the login challenge for that account for a few minutes.
   Ask Jonathan.
5. **Run `canopy agent doctor --repo .` until it is all green.** It names every remaining fix.
   Anything it cannot explain is a bug — email it to hal@dimagi-ai.com.
6. **Run the first turn yourself**: `/<slug>:turn` — [§7](onboarding-a-new-operator.md#7-your-first-turn).

### Phase 3 — Only if you want email to trigger turns on its own

Install the runner on your machine — [§6](onboarding-a-new-operator.md#6-the-runner--including-on-windows).
Until you do, the agent works whenever you run a turn; it just does not wake up by itself.

### Phase 4 — Retire the droplet (Hal, once you say so)

When a canopy turn has handled real email correctly, Hal stops the OpenClaw gateway so the two
never answer the same thread, then the droplet is decommissioned (or kept as a cloud runner).
**Nothing on it is needed any more** — the repo holds the brain, and the droplet's credentials are
never copied anywhere.

---

## Where each agent is

The live status of each move is on that agent's board on canopy-web — the checklist above, as
tasks, with whose turn it is. Start there, not here.

| Agent | Operator | Workspace | Board |
|---|---|---|---|
| Jarvis | Andrea King | `connect` | [/canopy/w/connect/agents/jarvis](https://labs.connect.dimagi.com/canopy/w/connect/agents/jarvis) |
| Fizzy | Shayoni Mazumdar | `strategy` (when Shayoni registers it) | repo exists; Shayoni is mid-way through Phase 2 on Windows |
