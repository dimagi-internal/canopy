# Setting up an agent from Claude — the prompt to paste

For an agent **owner**: the person whose division an agent belongs to. You don't have to work
through the [onboarding doc](onboarding-a-new-operator.md) by hand. Install canopy once, then give
Claude Code the prompt below, and it does the setup, stopping only for the steps that need you.

## Is it just `canopy agent doctor`?

No. `doctor` is half of it:

| | What it covers | How Claude reaches it |
|---|---|---|
| **This laptop** | The agent's plugin, its `.env`, its mailbox login, its rails | `canopy agent bootstrap` sets it up and `canopy agent doctor` checks it and names each fix |
| **canopy-web** | The agent's vault key, workspace invites, turn mode, runners, board | The **canopy-web MCP tools**. Every canopy-web route is one ([canopy-web #1032](https://github.com/dimagi-internal/canopy-web/pull/1032)), and each call runs **as you** |

`doctor` never changes canopy-web, which is why owners kept getting stuck on "paste this key into
Settings". The MCP tools close that gap. They come with the canopy plugin and sign in with the
token `/canopy:setup` gives you, so there's nothing extra to connect.

## Before you paste it (once per laptop)

In Claude Code:

```
/plugin marketplace add dimagi-internal/canopy
/plugin install canopy@canopy
```

Restart Claude Code, then run `/canopy:setup`. Details and Windows notes are in
[§2 of the onboarding doc](onboarding-a-new-operator.md#2-install-canopy).

## The prompt

Replace the four values in angle brackets, then paste the whole thing:

```text
Set up <Agent>, our canopy agent, on this machine and on canopy-web.
Workspace: <workspace>. Repo: dimagi-internal/<slug>. Mailbox: <slug>@dimagi-ai.com.
1Password vault: Agent-<Agent>.

Use the canopy-web MCP tools for anything on canopy-web (pass workspace "<workspace>"),
and the canopy CLI for this machine. Skip any step that is already done, and show me
each result before moving on.

1. Clone dimagi-internal/<slug> and install it as a plugin
   (/plugin marketplace add dimagi-internal/<slug>, then /plugin install <slug>@<slug>).
2. Run `canopy agent bootstrap --slug <slug> --dry-run`, show me the plan, then run it.
3. Attach the agent's vault on canopy-web: vault Agent-<Agent>, with the token in the
   1Password item "canopy-web-service-account" in that vault. Read it with `op` and pass
   it straight to the tool, and never print it. Then read the vault status back and
   confirm key_set is true.
4. Invite <slug>@dimagi-ai.com to the "<workspace>" workspace as an editor, so the
   agent's own login can see its own board.
5. Run `canopy agent doctor --repo <the checkout>` and fix every failing check it names.
   Stop and walk me through any step that needs a person: signing in to Google as
   <slug>@dimagi-ai.com, accepting that invite as the agent, and minting the agent's own
   canopy-web token (do it in a private browser window signed in as the agent, and save
   the token to 1Password at Agent-<Agent>/canopy-pat, never to my own
   ~/.claude/canopy/workbench-token).
6. When doctor is all green, mark the matching setup cards done on the agent's board,
   then run /<slug>:turn.
```

## What stays with a person, and why

Claude stops at these on purpose. None of them is a gap in the tools.

- **Signing in as the agent.** One private browser window, signed in as `<slug>@dimagi-ai.com`,
  covers three things: the mailbox login (`gog login`), accepting the workspace invite, and
  minting the agent's own canopy-web token. Whoever holds the agent's Google login does this.
  If Google says it *"couldn't verify this account belongs to you"*, a dimagi-ai.com admin has to
  lift the login challenge for a few minutes first.
- **Changing an agent's owner or admins.** This is browser-only by design: the owner is whose
  GitHub access the agent borrows, so a person decides it in the canopy-web app. Attaching the
  vault needs you to be the agent's owner or admin, or an owner of its workspace.
- **Inviting someone to a workspace.** Only that workspace's owner can do it.

## Where the agents are

| Agent | Owner(s) | Workspace | Vault token item |
|---|---|---|---|
| Muse | Gillian Javetski, Amie Vaccaro | `commcare` | `Agent-Muse/canopy-web-service-account` |
| Fizzy | Shayoni Mazumdar | `strategy` | `AI-Agents/Service Account Auth Token: canopy-web-fizzy` |
| Jarvis | Andrea King | `connect` | `Agent-Jarvis/canopy-web-service-account` (repo not created yet, see [moving an OpenClaw](moving-an-openclaw-onto-canopy.md)) |
