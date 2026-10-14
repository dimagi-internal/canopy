---
description: One-shot idempotent setup — sign in to canopy-web in the browser and register your agent catalog (canopy-agents, auto-updating) so you can install the agents you choose; plus the machine basics (state dir, CLI, hook)
allowed-tools: [Bash]
---

# Canopy Setup

Getting started with canopy is three steps, and this command is the first:

1. **Install canopy** — `/plugin marketplace add dimagi-internal/canopy`,
   `/plugin install canopy@canopy`, then this command (`/canopy:setup`).
2. **Install an agent** — the normal Claude Code way, e.g. `/plugin install ace@canopy-agents`
   (or the `/plugin` menu). Only the agents you choose. Installing needs your own GitHub read
   access to the agent's repo (ACE's is public; a private agent's owner grants access).
3. **Set up the agent** — that agent's own setup command, e.g. `/ace:setup`.

This command signs you in to canopy-web and registers ONE agent catalog, `canopy-agents`,
which lists every agent in every workspace you belong to. That registration is the only reason
a canopy command is involved: `claude plugin marketplace add` can't send your sign-in token, so
canopy writes the entry with a headers helper that does. It installs no agents. Runners, vault
keys, `gog login` and `op inject` are NOT part of it either — they belong to the operator who
*hosts* an agent (`canopy agent bootstrap`). The user-facing guide is
https://canopy.dimagi.com/get-started.

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

## 3. Register your agent catalog (auto-update on)

```bash
canopy agents connect
```

No workspace picking: this fetches `https://canopy.dimagi.com/plugins/marketplace.json` as you —
every agent in every workspace you belong to — and registers it in your user settings as
`canopy-agents` (`extraKnownMarketplaces`, `autoUpdate: true`, authenticated by canopy's headers
helper in `--archive` mode so the catalog fetch carries your token). It is idempotent, and it
removes any retired per-workspace `canopy-<ws>` entries from your settings, saying so. It
installs nothing: it ends by listing the agents, one line each, with the exact install line
(`/plugin install <agent>@canopy-agents`) and the agent's own setup command (`/<agent>:setup`).
Report its output, then tell the user:

- install only the agents they want, by running those lines (or picking from the `/plugin`
  menu) — don't run the installs for them unless they ask;
- each agent installs from its GitHub repo, so they need their own GitHub read access to it
  (ACE's repo is public; for a private agent, ask its owner) — canopy never lends access;
- after installing an agent, run **that agent's own setup command** (e.g. `/ace:setup`); it
  says what else that agent needs.

On a failure it prints the exact cause (not signed in, catalog not served yet) — surface that;
don't improvise around it. If the catalog lists no agents, the user isn't in a workspace with
agents yet and should ask a workspace admin for an invite. (`--install-all` exists for someone
who explicitly wants every agent; it is not the default.)

## 4. 1Password check (never fails setup)

Step 1 printed one `[op]` line. If it says not installed / not signed in, repeat that single fix
to the user. Whether an agent needs 1Password is up to that agent — its own setup command tells
you what it needs; canopy only needs it to **act as** an agent whose credentials live in
1Password (`canopy cred check --agent <slug>` says whether this session may).

## Done

Tell the user to restart Claude Code (or `/reload-plugins`) so the capture hook (and any agent
they install) loads, then `/canopy:canopy-doctor` to verify. Their next steps are the agent
install lines from step 3, then each installed agent's own setup command. Operators who will
*host* agents on this machine continue with `canopy agent bootstrap` (printed at the end of step 1).
