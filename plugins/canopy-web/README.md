# canopy-web (plugin)

Just the canopy-web MCP server, for people who want to work with canopy-web from
Claude — set up an agent, change its routing or turn mode, attach a vault, invite
someone to a workspace — without the rest of canopy (session capture, the `canopy`
CLI, DDD, the self-improvement loop).

```
/plugin marketplace add dimagi-internal/canopy
/plugin install canopy-web@canopy
```

The first time Claude uses it, a browser tab opens on canopy-web: sign in with your
Dimagi Google account and click **Allow**. That's all — the connection refreshes
itself, and it acts as you, with exactly the access you have in the app. See or
disconnect it under **Settings → Connected apps** on canopy-web.

**Every canopy-web route is a tool** (canopy-web #1032), so anything the app can do,
Claude can do through this. Pass `workspace` to act in a particular workspace.

## `canopy-web` or `canopy`?

Install **one**. The full `canopy` plugin already includes this same server (it signs
in with your canopy token when you have one, and with the browser sign-in when you
don't) plus canopy's skills, hooks and CLI. Installing both gives you the server twice.

## Without a plugin

- **Claude Code:** `claude mcp add --transport http canopy https://labs.connect.dimagi.com/canopy/api/mcp/`
- **Claude Desktop / claude.ai:** a Claude org admin can add
  `https://labs.connect.dimagi.com/canopy/api/mcp/` as a custom connector for the whole
  org; each person then connects it and signs in the same way.
