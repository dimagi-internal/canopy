# Setting up an agent from Claude — the prompt to paste

For an agent **owner**: the person whose division an agent belongs to. You don't have to work
through the [onboarding doc](onboarding-a-new-operator.md) by hand. Install canopy once, then give
Claude Code the prompt below, and it does the setup, stopping only for the steps that need you.

## Is it just `canopy agent doctor`?

No. `doctor` is half of it:

| | What it covers | How Claude reaches it |
|---|---|---|
| **This laptop** | The agent's plugin, its `.env`, its mailbox login, its rails | `canopy agent bootstrap` sets it up and `canopy agent doctor` checks it and names each fix |
| **canopy-web** | The agent's canopy vault, workspace invites, turn mode, runners, board | The **canopy-web MCP tools**. Every canopy-web route is one, and each call runs **as you** |

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
   the agent's Settings → Security tab, or with `canopy vault zk set --agent <slug>
   NAME` in my own terminal.
3. If the agent has no canopy login yet, tell me to press "Give it its canopy login" on
   its Settings → Security tab (it goes straight into the vault, never shown; the login
   joins the "<workspace>" workspace as an editor, so it sees its own board).
4. Run `canopy agent doctor --repo <the checkout>` (clone dimagi-internal/<slug> if there
   is none) and fix every failing check it names. Stop and walk me through any step that
   needs a person: signing in to Google as <slug>@dimagi-ai.com, and pressing
   "Give them access" on the Settings → Security tab for a device the agent's page shows
   as Grant pending.
5. Run `canopy agent use <slug>` again. When it ends with "Start a new Claude Code
   session …", mark the matching setup cards done on the agent's board and tell me to
   start that session and run /<slug>:turn.
```

## What stays with a person, and why

Claude stops at these on purpose. None of them is a gap in the tools.

- **Signing in as the agent.** One private browser window, signed in as `<slug>@dimagi-ai.com`,
  for the mailbox login (the agent's Settings → External accounts → **Connect Google**
  in canopy-web, or `gog login <mailbox> --client canopy` at a terminal — either fleet
  client serves the agent's turns). Whoever holds the agent's Google login does this. (The agent's own canopy-web token is not minted by hand:
  **Give it its canopy login** puts it in the vault.)
  If Google says it *"couldn't verify this account belongs to you"*, a dimagi-ai.com admin has to
  lift the login challenge for a few minutes first.
- **Changing an agent's owner or admins.** Only the agent's owner or a workspace owner may, in the
  canopy-web app or with the `transfer_owner` / `grant_admin` MCP tools: an admin can read the agent's
  credentials, so a person with that standing decides it. Changing the agent's credentials needs you to
  be its owner or admin, or an owner of its workspace.
- **Granting a device.** A device the agent's page marks *Grant pending* waits for **Give them
  access** (Settings → Security). Pressing it wraps the vault key for a new device with
  your passkey; only someone who already holds a grant can.
- **Inviting someone to a workspace.** A workspace admin or owner can, at a role below their own
  (an owner at any role).

## Where the agents are

Each agent's owner, workspace and setup state are on its page in canopy-web
(`/w/<workspace>/agents/<slug>`) and in `list_agents` / `agent_setup`; this page does not keep a list.
For an agent moving off an OpenClaw droplet, see [moving an OpenClaw](moving-an-openclaw-onto-canopy.md).
