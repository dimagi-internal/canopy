# Setting up an agent from Claude — the prompt to paste

For an agent **owner**: the person whose division an agent belongs to. You don't have to work
through the [onboarding doc](onboarding-a-new-operator.md) by hand. Install canopy once, then give
Claude Code the prompt below, and it does the setup, stopping only for the steps that need you.

## Is it just `canopy agent doctor`?

No. `doctor` is half of it:

| | What it covers | How Claude reaches it |
|---|---|---|
| **This laptop** | The agent's plugin, its `.env`, its mailbox login, its rails | `canopy agent bootstrap` sets it up and `canopy agent doctor` checks it and names each fix |
| **canopy-web** | The agent's canopy vault, workspace invites, turn mode, runners, board | The **canopy-web MCP tools**. Every canopy-web route is one ([canopy-web #1032](https://github.com/dimagi-internal/canopy-web/pull/1032)), and each call runs **as you** |

`doctor` never changes canopy-web. The MCP tools close that gap. They come with the canopy plugin and sign in with the
token `/canopy:setup` gives you, so there's nothing extra to connect. (Without that token, the
first MCP call opens a canopy-web sign-in in your browser instead: approve it once.)

Only need the canopy-web half — no agent on this laptop, just configuring one? Install the slim
`canopy-web` plugin instead (`/plugin install canopy-web@canopy`): the same MCP tools, signed in
through the browser, without canopy's CLI, hooks or skills. See
[`plugins/canopy-web/README.md`](../plugins/canopy-web/README.md).

## Before you paste it (once per laptop)

In Claude Code:

```
/plugin marketplace add dimagi-internal/canopy
/plugin install canopy@canopy
```

Restart Claude Code, then run `/canopy:setup`. Details and Windows notes are in
[§2 of the onboarding doc](onboarding-a-new-operator.md#2-install-canopy).

## The prompt

Every agent's secrets live in its **canopy vault** on canopy-web (see
[running an agent](running-an-agent.md)). Replace the values in angle brackets, then paste the
whole thing:

```text
Set up <Agent>, our canopy agent, on this machine and on canopy-web.
Workspace: <workspace>. Repo: dimagi-internal/<slug>. Mailbox: <slug>@dimagi-ai.com.

Use the canopy-web MCP tools for anything on canopy-web (pass workspace "<workspace>"),
and the canopy CLI for this machine. Never read a secret into a tool call or print one.
Skip any step that is already done, and show me each result before moving on.

1. Run `canopy agent use <slug>` and show me its output. It installs the agent's plugin,
   registers this computer, enrolls its device key and sets up the env. Do what it can't
   only where it names a command; for anything it says NEEDS YOU, tell me exactly what
   to do and wait.
2. Read the agent's setup checklist (agent_setup) and its vault state
   (get_agent_zk_state). Tell me which credentials are still missing. I set values on
   the agent's Settings → Credentials page, or with `canopy vault zk set --agent <slug>
   NAME` in my own terminal.
3. If the agent has no canopy login yet, tell me to press "Give it its canopy login" on
   its Settings → Credentials page (it goes straight into the vault, never shown).
4. Invite <slug>@dimagi-ai.com to the "<workspace>" workspace as an editor, so the
   agent's own login can see its own board.
5. Run `canopy agent doctor --repo <the checkout>` (clone dimagi-internal/<slug> if there
   is none) and fix every failing check it names. Stop and walk me through any step that
   needs a person: signing in to Google as <slug>@dimagi-ai.com and accepting that
   invite as the agent, and pressing Grant pending for a device that needs access.
6. Run `canopy agent use <slug>` again. When it ends with "Start a new Claude Code
   session …", mark the matching setup cards done on the agent's board and tell me to
   start that session and run /<slug>:turn.
```

## What stays with a person, and why

Claude stops at these on purpose. None of them is a gap in the tools.

- **Signing in as the agent.** One private browser window, signed in as `<slug>@dimagi-ai.com`,
  covers two things: the mailbox login (the agent's Settings → Credentials → **Connect Google
  mailbox** in canopy-web, or `gog login <mailbox> --client canopy` at a terminal — either fleet
  client serves the agent's turns) and accepting the workspace invite. Whoever holds the
  agent's Google login does this. (The agent's own canopy-web token is not minted by hand any
  more: **Give it its canopy login** puts it in the vault.)
  If Google says it *"couldn't verify this account belongs to you"*, a dimagi-ai.com admin has to
  lift the login challenge for a few minutes first.
- **Changing an agent's owner or admins.** This is browser-only by design: the owner is whose
  GitHub access the agent borrows, so a person decides it in the canopy-web app. Changing the
  agent's credentials needs you to be its owner or admin, or an owner of its workspace.
- **Granting a device.** Pressing **Grant pending** wraps the vault key for a new device with
  your passkey; only someone who already holds a grant can.
- **Inviting someone to a workspace.** Only that workspace's owner can do it.

## Where the agents are

| Agent | Owner(s) | Workspace | Secrets |
|---|---|---|---|
| Muse | Gillian Javetski, Amie Vaccaro | `commcare` | canopy vault |
| Fizzy | Shayoni Mazumdar | `strategy` | canopy vault |
| Jarvis | Andrea King | `connect` | not created yet, see [moving an OpenClaw](moving-an-openclaw-onto-canopy.md) |
