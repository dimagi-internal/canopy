---
name: share-to-slack
description: |
  Summarize what THIS session is doing and post it to a Slack channel so
  teammates can see it — as a one-way broadcast, or bound so the Slack thread
  talks to this session from then on (replies reach it, its replies post
  there). Use when asked to "share this to slack", "summarize and share what
  we're doing", "post an update to #channel", or "start a slack thread from
  this session". Argument: a channel (`#dev` or an id), plus `bind` for a
  connected thread. Run it again on a bound session to post an update into
  its thread, or with `unbind` to disconnect it — use for "detach / disconnect
  / unbind this session from slack", "stop posting to slack".
---

# Share to Slack

Posts a summary of the current session into a Slack channel through canopy-web's
`share_session_to_slack` MCP tool (the `canopy-web` server this plugin declares).
You write the summary — you have the context — and canopy-web decides whether the
share is allowed, posts it as the Slack app, and in `bind` mode connects the
thread to this session.

## Step 1 — channel and mode

Parse the arguments: a channel (`#name`, `name`, or an id like `C0123ABCD`) and
an optional `bind` — or `unbind`.

- **`unbind`** (also "detach", "disconnect", "stop posting to Slack") → skip to
  [Unbind](#unbind) below. No channel or summary is needed.

- **This session was already shared with `bind`** (earlier in this conversation,
  or the user says "post an update" / "update the thread") → no channel needed:
  sharing a bound session posts the summary as an update in its own thread,
  whatever channel or mode is passed. This is how the thread stays current: work
  done here never reaches it on its own.
- **No channel given** → call the tool without one first (it posts an update if
  the session is bound). If it answers "Name a channel to share to", ask with
  `AskUserQuestion`. There is no default channel; never guess one.
- **Mode not stated** → `broadcast`. Use `bind` only when the user said `bind`,
  or asked for replies to come back to the session ("keep the thread connected",
  "let people reply to it here"). Binding lets anyone in the workspace who
  replies in the thread steer this session; if the request is ambiguous, ask.

## Step 2 — write the summary

For an **update** to a bound thread, write what changed since the last post
(done, found, next), not the whole story again.

Otherwise, write it for a teammate who has NOT seen this session, in Markdown, short enough
to read in Slack (aim for under ~150 words):

1. **What** we're working on, in one sentence.
2. **Why**: the problem or ask behind it.
3. **Where it stands**: done, in progress, next. Name PRs, branches and links
   that exist; never claim something shipped unless you verified it in this
   session.
4. **Where input would help**, if anywhere.

No secrets, tokens, customer data or internal URLs the channel should not see.
It posts to a shared channel.

## Step 3 — identify this session and post

Read the identifiers from the environment, then call the tool:

```bash
echo "claude=$CLAUDE_CODE_SESSION_ID task=$EMDASH_TASK_NAME project=$(basename "${EMDASH_ROOT_PATH:-}")"
```

Call `share_session_to_slack` with:

- `channel`: the channel as given
- `summary`: the Markdown from Step 2
- `mode`: `broadcast` or `bind`
- `claude_session_id`: `$CLAUDE_CODE_SESSION_ID`
- `emdash_task` / `emdash_project`: `$EMDASH_TASK_NAME` / basename of
  `$EMDASH_ROOT_PATH` (the fallback when canopy has not seen the Claude id yet)

## Step 4 — report

On success, reply with the permalink the tool returned. For `bind`, add one
line: replies in that thread now reach this session. A `status: updated` result
means it went into the existing thread as a reply.

The tool refuses in plain sentences. Relay them as they are; don't retry around
them:

- **not in channel**: the canopy app must be invited (`/invite @canopy`).
- **not linked**: mention @canopy once in Slack so canopy can link the account.
- **no session** (bind only): canopy doesn't know this session (e.g. a Claude
  Code session outside emdash). Offer a broadcast instead.
- **agent not on Slack**: the agent's owner must turn Slack on for it.

If the `canopy-web` MCP tools aren't available at all, say so. Don't fall back
to posting any other way.

## Unbind

Disconnects this session from the Slack thread it was bound to: its replies stop
posting there, and replies in the thread stop reaching it (they are ignored).
The thread gets one short note saying it is no longer connected; nothing already
posted is deleted. Use it when the user asks to detach, disconnect or unbind
this session from Slack.

Read the identifiers as in Step 3, then call canopy-web's
`detach_session_from_slack` tool with `claude_session_id` (and
`emdash_task` / `emdash_project` as the fallback). No summary, no channel.

Report what it returned:

- `status: detached` → "Disconnected from <#channel>; replies there no longer
  reach this session." If `notice_posted` is false, add that the note to the
  thread could not be posted (the disconnect still happened).
- `status: not_bound` → this session wasn't connected to a Slack thread; nothing
  to do.
- A 403 means the caller can read the session but not act in it (owners and
  editors may disconnect); a 404 means canopy can't find this session. Relay it;
  don't retry another way.

To connect it again later, share it with `bind`: that starts a fresh thread.
