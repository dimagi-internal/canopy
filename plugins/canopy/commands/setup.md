---
description: One-shot idempotent setup — sign in to canopy-web in the browser, pick your workspace, and install that workspace's agents (auto-updating); plus the machine basics (state dir, CLI, hook)
allowed-tools: [Bash, AskUserQuestion]
---

# Canopy Setup

The new-user path (canopy#851): **sign in → pick a workspace → that workspace's agents are
installed and keep themselves up to date.** Runners, vault keys, `gog login` and `op inject`
are NOT part of it — they belong to the operator who *hosts* an agent (`canopy agent bootstrap`).

> **First step worth trying — no install at all:** add canopy-web to claude.ai as a **custom
> connector** (claude.ai → Settings → Connectors → Add custom connector) with the URL
> `https://canopy.dimagi.com/api/mcp/`. You sign in with your canopy account in the browser and
> can talk to your workspace's agents from claude.ai or the Claude app. Come back to this
> command when you want the agents inside Claude Code.

Run each step in order. Every step is idempotent — re-running `/canopy:setup` skips what's done.

## 1. Machine basics

```bash
PLUGIN_PATH=$(python3 -c "import json; d=json.load(open('$HOME/.claude/plugins/installed_plugins.json')); print(d['plugins']['canopy@canopy'][0]['installPath'])")
SETUP="$PLUGIN_PATH/runtime/scripts/canopy-setup.sh"
# Bootstrap fallback: a cache that predates the runtime bundle doesn't carry the
# script yet — the marketplace clone (the install channel, always present) does.
[ -f "$SETUP" ] || SETUP="$HOME/.claude/plugins/marketplaces/canopy/scripts/canopy-setup.sh"
bash "$SETUP"
```

Report the script's output verbatim. A `NOT SIGNED IN` workbench token is expected on a new
machine — step 2 fixes it. If the `canopy CLI` step failed, surface its remediation and stop:
the steps below need the CLI.

## 2. Sign in to canopy-web (browser)

Skip if `~/.claude/canopy/workbench-token` exists and is non-empty. Otherwise tell the user a
browser tab is about to open and they should click **Authorize** (signing in first if asked),
then run the loopback mint — it waits for the click (Bash timeout 600000):

```bash
SCRIPT="$(sed -n '/"canopy@canopy"/,/\]/{ s/.*"installPath": *"\([^"]*\)".*/\1/p; }' "$HOME/.claude/plugins/installed_plugins.json" | head -1)/scripts/canopy-web-pat-mint.ts"
npx --yes tsx "$SCRIPT"
```

If the browser didn't open, give the user the URL the script printed in step `[2/3]`.

## 3. Pick a workspace

```bash
canopy workspace list --json
```

- **None:** tell the user they aren't a member of any workspace yet and should ask a workspace
  admin for an invite; stop here (setup is otherwise complete).
- **Exactly one:** use it, and say which.
- **Several:** ask with AskUserQuestion (one option per workspace, label = name, description =
  slug).

## 4. Connect it — register its marketplace (auto-update on) and install its agents

```bash
canopy workspace connect <slug>
```

This fetches `https://canopy.dimagi.com/w/<slug>/marketplace.json` as you, registers it in your
user settings (`extraKnownMarketplaces`, `autoUpdate: true`, authenticated by canopy's headers
helper in `--archive` mode so catalog and archive downloads carry your token), and runs
`claude plugin install <agent>@<marketplace> --yes` for each agent. Report its output. On a
failure it prints the exact cause (not signed in, not a member, no marketplace served yet) —
surface that; don't improvise around it.

## 5. 1Password check (never fails setup)

Step 1 printed one `[op]` line. If it says not installed / not signed in, repeat that single fix
to the user: it's only needed to **act as** an agent whose credentials live in 1Password
(`canopy cred check --agent <slug>` says whether this session may). Using agents doesn't need it.

## Done

Tell the user to restart Claude Code (or `/reload-plugins`) so the new agents and the capture
hook load, then `/canopy:canopy-doctor` to verify. Operators who will *host* agents on this
machine continue with `canopy agent bootstrap` (printed at the end of step 1).
