---
name: openclaw-harvest
description: >
  Bridge a live OpenClaw instance into the canopy fleet. Snapshot an OpenClaw's readable workspace
  (persona/skills/memory — never its credentials), compare it to the agent's GitHub repo, and
  either bootstrap a NEW canopy agent repo from it or reconcile its latest-and-greatest skills/ideas
  into the existing repo as a PR. Use when asked to "harvest an openclaw", "migrate eva/hal off
  openclaw", "compare the openclaw to github", or "salvage the openclaw". Backed by
  src/orchestrator/openclaw_harvest.py.
---

# OpenClaw Harvest — salvage a dead-end brain into the fleet

The OpenClaw droplets are dead ends, but real ideas evolved on them (persona, skills, memory).
This rescues that and folds it into the canopy agent (a git repo you control). **OpenClaw content
is safe to read** (assume the droplet is compromised) — but **credentials never land in git**: the
snapshot excludes `auth-profiles.json`, `channels.json`, and `*token*`/key files by default.

## Step 1 — Snapshot the OpenClaw
Get the readable workspace onto local disk. `HOST` is anything `ssh` can reach — reef resolves DO
droplet IPs + the 1Password SSH key, so point ssh at that (or use an ssh-config alias):
```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
uv run --project "$CANOPY_ROOT" canopy openclaw-harvest snapshot <user@host> --into /tmp/oc-<slug>
```
(Pure rsync of `~/.openclaw/workspace/`, minus secrets. If ssh isn't wired, copy the workspace
dir over by hand — the rest of the flow only needs the local dir.)

## Step 2 — Inventory + compare to GitHub
```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
uv run --project "$CANOPY_ROOT" canopy openclaw-harvest inventory /tmp/oc-<slug>
uv run --project "$CANOPY_ROOT" canopy openclaw-harvest compare   /tmp/oc-<slug> <slug>     # slug or path to the canopy repo
```
`compare` resolves the agent's canopy repo and recommends **BOOTSTRAP** (no repo yet → create one)
or **RECONCILE** (repo exists → port the skills that only live on the OpenClaw).

## Step 3a — Bootstrap (no canopy repo yet)
Create a NEW agent repo seeded from the OpenClaw — factory scaffold + the OpenClaw persona seeded
into `persona.md` + every OpenClaw skill ported in:
```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
uv run --project "$CANOPY_ROOT" canopy openclaw-harvest bootstrap /tmp/oc-<slug> <slug> --mandate "<one line>" --into ~/emdash/repositories/<slug>
```
Then **refine `persona.md`** (the raw SOUL/IDENTITY is appended for you to distill, then delete the
note), sanity-check the ported skills, set `config/gating.json` rules, `gh repo create`, push.

## Step 3b — Reconcile (canopy repo already exists)
Port the OpenClaw skills missing from the repo, for review:
```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
uv run --project "$CANOPY_ROOT" canopy openclaw-harvest reconcile /tmp/oc-<slug> <slug>
```
Then in the agent repo: review each ported skill body (the OpenClaw version may be cruftier or
better than what you'd write today — keep the good ideas, rewrite to canopy conventions), and for
divergent skills already in both, diff bodies by hand to pull the "latest and greatest." Ship as a
branch → PR → merge in the agent repo.

## Step 4 — Retire the OpenClaw
Once the ideas are in git, the droplet's brain has no unique value left. Decommission it, or (per
the operating model) repurpose the droplet as a *runner* for the canopy agent. Never copy its
credentials anywhere.

## Notes
- The engine (inventory/compare/bootstrap/reconcile) is pure and offline — only `snapshot` needs
  the network. You can run the whole flow on a hand-copied workspace dir.
- Skills are matched by folder name; bodies are never auto-merged — a human keeps the good parts.
