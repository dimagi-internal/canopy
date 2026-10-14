---
description: One-shot idempotent setup — sign in to canopy-web in the browser and register every workspace's private agent marketplace (auto-updating) so you can install the agents you choose; plus the machine basics (state dir, CLI, hook)
allowed-tools: [Bash, AskUserQuestion]
---

# Canopy Setup

Getting started with canopy is three steps, and this command is the first:

1. **Install canopy** — `/plugin marketplace add dimagi-internal/canopy`,
   `/plugin install canopy@canopy`, then this command (`/canopy:setup`).
2. **Install an agent** — the normal Claude Code way, `/plugin install <agent>@canopy-<ws>`
   (or the `/plugin` menu). Only the agents you choose.
3. **Set up the agent** — that agent's own setup command, e.g. `/ace:setup`.

This command signs you in to canopy-web and registers the private agent marketplace
`canopy-<ws>` of every workspace you belong to (agents are split across workspaces, and each
catalog lists only its own). That registration is the only reason a
canopy command is involved: `claude plugin marketplace add` can't send your sign-in token, so
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

## 3. Your workspaces

```bash
canopy workspace list
```

- **None:** tell the user they aren't a member of any workspace yet and should ask a workspace
  admin for an invite; stop here (setup is otherwise complete).
- **Otherwise:** don't ask them to pick — step 4 connects all of them. Agents are split across
  workspaces (e.g. `dimagi` holds ada and eva, `connect` holds ace, echo, hal and jarvis), and
  each catalog lists only its own agents, so connecting one would hide the rest.

## 4. Register every workspace's agent marketplace (auto-update on)

```bash
canopy workspace connect --all
```

For each workspace this fetches `https://canopy.dimagi.com/w/<ws>/marketplace.json` as you and
registers it in your user settings as `canopy-<ws>` (`extraKnownMarketplaces`,
`autoUpdate: true`, authenticated by canopy's headers helper in `--archive` mode so catalog and
archive downloads carry your token). A workspace that can't be connected (no catalog served
yet, not reachable) prints a `skipped — <reason>` line and the rest continue. It installs
nothing: it ends with ONE combined list of the agents on offer, each with its exact install
line (`/plugin install <agent>@canopy-<ws>`) and its own setup command (`/<agent>:setup`).
Report its output, then tell the user:

- install only the agents they want, by running those lines (or picking from the `/plugin`
  menu) — don't run the installs for them unless they ask;
- after installing an agent, run **that agent's own setup command** (e.g. `/ace:setup`); it
  says what else that agent needs.

Surface any `skipped` line as-is; don't improvise around it. (`canopy workspace connect <ws>`
still connects a single workspace; `--install-all` exists for someone who explicitly wants
every agent, and is not the default.)

## 5. 1Password check (never fails setup)

Step 1 printed one `[op]` line. If it says not installed / not signed in, repeat that single fix
to the user. Whether an agent needs 1Password is up to that agent — its own setup command tells
you what it needs; canopy only needs it to **act as** an agent whose credentials live in
1Password (`canopy cred check --agent <slug>` says whether this session may).

## Done

Tell the user to restart Claude Code (or `/reload-plugins`) so the capture hook (and any agent
they install) loads, then `/canopy:canopy-doctor` to verify. Their next steps are the agent
install lines from step 4, then each installed agent's own setup command. Operators who will *host* agents on this
machine continue with `canopy agent bootstrap` (printed at the end of step 1).
