---
description: One-shot idempotent setup — sign in to canopy-web in the browser, register your agent catalog (canopy-agents, auto-updating) and offer to set up each agent you can use in your own Claude Code (`canopy agent use`); plus the machine basics (state dir, CLI, hook)
allowed-tools: [Bash]
---

# Canopy Setup

Getting started with canopy is three steps, and this command does the first and offers the
second:

1. **Install canopy** — `/plugin marketplace add dimagi-internal/canopy`,
   `/plugin install canopy@canopy`, then this command (`/canopy:setup`).
2. **Use an agent yourself** — `canopy agent use <agent> --dir <folder>`, once per agent, in
   the folder where you'll work with it. It installs the agent's plugin, asks for your licence,
   points that folder's Claude Code sessions at the agent and syncs its env. Installing needs
   your own GitHub read access to the agent's repo (ACE's is public; a private agent's owner
   grants access).
3. **Set up the agent** — that agent's own setup command, e.g. `/ace:setup`.

**A person who can use an agent never needs `canopy agent dispatch` to reach it.** Ask it in
your own Claude Code session. Dispatch hands work to a runner to do unattended; every dispatch
is a fresh cloud session with no memory of the last (canopy-web#1955).

This command signs you in to canopy-web and registers ONE agent catalog, `canopy-agents`,
which lists every agent in every workspace you belong to. That registration is the only reason
a canopy command is involved: `claude plugin marketplace add` can't send your sign-in token, so
canopy writes the entry with a headers helper that does. It installs no agents by itself; step
3 offers `canopy agent use` for each agent you can use and runs it for the ones you pick. Runners, vault
keys and `gog login` are NOT part of it either — they belong to the operator who
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

## 3. Register your agent catalog and offer the agents you can use

```bash
canopy agents connect
```

No workspace picking: this fetches `https://canopy.dimagi.com/plugins/marketplace.json` as you —
every agent in every workspace you belong to — and registers it in your user settings as
`canopy-agents` (`extraKnownMarketplaces`, `autoUpdate: true`, authenticated by canopy's headers
helper in `--archive` mode so the catalog fetch carries your token). It is idempotent, and it
removes any retired per-workspace `canopy-<ws>` entries from your settings, saying so. It
installs nothing by itself: it ends by listing **the agents canopy-web says you can use**
(`GET /api/me/agents/`; the catalog's names if that can't be read), one line each:
`canopy agent use <agent> --dir <folder>`, then the agent's own setup command (`/<agent>:setup`).

Report its output, then **offer to set up each listed agent now**, in one question (e.g. "Set
up ace and eva for you to use here? Which folder for each?"):

- `canopy agent use` points ONE folder's sessions at ONE agent (`$CANOPY_AGENT` in that folder's
  `.claude/settings.local.json`), so each agent needs its own folder. Default to the folder the
  user already works in for that agent (their checkout of the agent's repo, or the project they
  named); for a second agent, propose `~/<agent>` and create it. Never run two agents into the
  same folder.
- For each agent they say yes to, run `canopy agent use <agent> --dir <folder>` (Bash timeout
  600000 — it may install the plugin). It is idempotent: an agent already set up reports `ok`
  on every step. Report its step list verbatim.
- It exits 1 when something NEEDS YOU (a licence an admin must approve, a device key, GitHub
  access). That does **not** fail setup: relay each NEEDS YOU line with its fix, and go on to
  the next agent and the next step.
- If they decline, or name none, skip it — the lines stay in the output for later.
- each agent installs from its GitHub repo, so they need their own GitHub read access to it
  (ACE's repo is public; for a private agent, ask its owner) — canopy never lends access;
- after an agent is set up, they run **that agent's own setup command** (e.g. `/ace:setup`)
  in its folder; it says what else that agent needs.

On a failure it prints the exact cause (not signed in, catalog not served yet) — surface that;
don't improvise around it. If the catalog lists no agents, the user isn't in a workspace with
agents yet and should ask a workspace admin for an invite. (`--install-all` exists for someone
who explicitly wants every agent; it is not the default.)

## Done

Tell the user to restart Claude Code (or `/reload-plugins`) so the plugin's hooks (and any agent
set up in step 3) load, then `/canopy:canopy-doctor` to verify. Their next steps: open Claude
Code in each agent's folder, run that agent's own setup command, and ask it things there —
not through `canopy agent dispatch`. Any agent they skipped in step 3 is one
`canopy agent use <agent> --dir <folder>` away. Operators who will
*host* agents on this machine continue with `canopy agent bootstrap` (printed at the end of step 1).
