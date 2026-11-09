# Onboarding a new operator — from nothing to a running agent

**Who this is for:** someone joining the canopy fleet who is comfortable with a
terminal and with AI coding tools, but is **not** a career software engineer, and
who has never set up canopy before.

Every other document here is written either for canopy's own developers
(`README.md`), for Claude (`plugins/canopy/skills/create-agent/SKILL.md`), or for
the person who already runs the fleet (`runner/canopy_runner/README.md` in
canopy-web). This one is written for the human doing it the first time, in the
order they will actually hit things.

> **Read `docs/agent-operating-model.md` for the *why*.** This doc is the *how*.

---

## 0. The mental model — five pieces, and which ones you need

The single most confusing thing about canopy is that "canopy" names a framework,
a CLI, a plugin, and a website. Here is the whole map:

| Piece | What it is | Where it runs | Do you need it on day 1? |
|---|---|---|---|
| **Claude Code** | The agent runtime. Everything else is scaffolding around it. | Your machine | **Yes** |
| **canopy** (this repo) | The framework: a Claude Code plugin + a `canopy` CLI. Holds the agent factory and the fleet-wide operating model. | Your machine | **Yes** |
| **Your agent's repo** | One git repo per agent — its persona, skills, gating rails, secrets. Generated for you by the factory. | Your machine + GitHub | **Yes** |
| **canopy-web** | The shared website: each agent's projects, tasks, turns. Lives at `canopy.dimagi.com`. | Already deployed — you just log in | **Yes** (read/write via CLI) |
| **A runner** | Runs an agent's turns *on its own*: the runner app on your Mac, or a fleet cloud box. | Your Mac, or a cloud box | **No** |

**There are two ways to run an agent** ([running-an-agent.md](running-an-agent.md)):

1. **Use it yourself in Claude Code** — no runner needed. `canopy agent use <slug>` once on
   your computer, then `/<slug>:turn` in a new session.
2. **Let it run on its own** — a runner starts its turns (schedules, email, dispatches): your
   Mac's runner app set **Private** (it does only your work), or a fleet cloud box. See §6.

---

## 1. Prerequisites

Install these first. Everything below assumes they exist.

| Tool | Why | Check it works |
|---|---|---|
| [Claude Code](https://code.claude.com/docs) | The runtime | `claude --version` |
| [git](https://git-scm.com/downloads) | Your agent is a repo | `git --version` |
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | Runs the `canopy` CLI | `uv --version` |
| [GitHub CLI](https://cli.github.com/) | Creating the agent's repo | `gh auth status` |
| [Node.js 20+](https://nodejs.org/en/download) | `npx` runs the token-mint script | `node --version` |
| [gog](https://github.com/openclaw/gogcli) | Your agent's Gmail/Drive access — §5 uses it | `gog --version` |

**`gog` on Windows.** There is no Homebrew, so install from the release
archive: download the latest `gogcli_<version>_windows_amd64.zip` from
[the releases page](https://github.com/openclaw/gogcli/releases),
unzip it somewhere permanent (e.g. `%LOCALAPPDATA%\gog\`), and add that folder
to your `PATH` (Settings → *Edit environment variables for your account* →
`Path` → New). Open a NEW terminal afterwards — `PATH` changes do not reach
already-running shells. On macOS/Linux: `brew install gogcli` (it is in homebrew-core — no tap, and
nothing private to request access to).

You also need **membership in the `dimagi-internal` GitHub org** and a
**canopy-web login**. Ask a Dimagi canopy admin for both if `gh repo list dimagi-internal`
comes back empty.

> **Python note:** canopy targets Python 3.11+. You do not need to install Python
> separately — `uv` provides it.

---

## 2. Install canopy

### 2a. Install the plugin FIRST — `/canopy:setup` does not exist until you do

This step is easy to miss and nothing below works without it: `/canopy:setup` is
a command *provided by the canopy plugin*, so until the plugin is installed the
command simply is not there. Run these in Claude Code:

```
/plugin marketplace add dimagi-internal/canopy
/plugin install canopy@canopy
```

> **Make sure it is THIS canopy.** There is another repo called
> `jjackson/canopy-skills` which carries only the PM skills. Installing that one
> leaves you with no `/canopy:setup`, no `canopy` CLI and no agent commands — and
> the symptom is just "the command doesn't exist", which looks like a typo rather
> than the wrong marketplace. The source you want is **`dimagi-internal/canopy`**.

> **If `marketplace add` fails with `EPERM ... rename` on Windows, just run it
> again.** Defender scans the freshly-cloned files and briefly locks them; a
> plain retry succeeds. It is not a corrupted install.

Restart Claude Code (or `/reload-plugins`) so the new commands register.

### 2b. Run the setup script

```
/canopy:setup
```

It is idempotent — safe to re-run — and provisions the state directory and
the `canopy` CLI, signs you in to canopy-web in the browser, and
registers your one agent catalog `canopy-agents` (every agent in every workspace you
belong to; auto-updating) — `canopy agents connect`. It installs no agents: it lists them
with their install lines (e.g. `/plugin install ace@canopy-agents`); install the ones you
want (each needs your own GitHub read access to its repo), then run each agent's own
setup command (e.g. `/ace:setup`) — or simply run `canopy agent use <slug>`, which installs
the plugin and does the rest. As an operator you continue below — hosting an agent (runners,
its canopy vault, `gog`) is not part of a user's setup.

> To use canopy-web the product (board, sessions, settings) from claude.ai, add it as a
> **custom connector**: URL `https://canopy.dimagi.com/api/mcp/`. Agents are not used that
> way — you use an agent by installing its plugin.

To update later: `/canopy:update`.

---

## 3. Get your canopy-web access sorted BEFORE you build anything

Two things live here, and mixing them up is the most common early confusion.

### 3a. Your workspace ("team")

A **workspace** is canopy-web's tenant: the "team" whose agents, tasks and work products you can see. An
agent lives in exactly one workspace **at a time**, and the choice is reversible: setting an explicit
`workspace` on an already-homed agent **moves** it (`canopy agent move --slug <x> --to <ws> --dry-run`
lists what moves, what stays and what will stop working, e.g. a mail address on a domain the new
workspace cannot use; `canopy agent register --slug <x> --workspace <ws>` runs the same move).

Which workspace is the one choice here worth thirty seconds of thought:

| Workspace | Who gets in | Notes |
|---|---|---|
| `dimagi` | a `@dimagi.com` login may **request** access; an admin approves, or the workspace's auto-approve role grants it | the widest audience; everyone it admits can see the agent's board |
| **your division**: `connect`, `strategy`, `operations`, `commcare`, `global-solutions` | by invite or an approved request; each has its own admins | **the usual answer** for an agent that belongs to a team. To see where an existing agent lives, read `workspace` off its record (the `get_agent` canopy-web MCP tool, or `GET /api/agents/<slug>/`) rather than assuming |
| **your own** | nobody until you invite them; you are the owner | the only one that starts empty and stays that way |

**Workspaces nest.** The divisions sit *under* `dimagi`, and the tree grants one thing: an **owner or
admin** of a workspace holds that role in everything below it (`WorkspaceMembership.INHERITS_DOWN` in
canopy-web). Nothing else flows down, so a member of `dimagi` sees **nothing** in a division workspace.
That is why a division is the right home: private to its team, still administrable from the top. Nest a
new workspace by passing `parent` when creating it (you must own the parent). What each role may do is in
canopy-web's `docs/architecture/roles.md`; read it before putting an agent somewhere wide, because an
editor can create and edit agents and send them turns.

Prefer your own workspace when the agent's board is yours rather than the fleet's; prefer a division
when the agent is part of the shared fleet. Whichever you pick, say so explicitly when you register:

```bash
canopy agent register --slug <your-agent> --workspace <workspace-slug>
```

<details>
<summary>Creating your own workspace, and inviting people to it</summary>

Create one from the workspace switcher in canopy-web (its last option), the `create_workspace` canopy-web
MCP tool, or `POST /api/workspaces/` (canopy-web's OpenAPI has the body). Pick the slug deliberately:
lowercase letters, digits and hyphens only, and it appears in every URL your team uses.

> **Both names are permanent: there is no rename.** The workspace routes have no `PATCH`/`PUT` for the
> slug or `display_name`, so the only route to a different name is delete-and-recreate, and delete
> returns 409 while the workspace still owns agents (or child workspaces, or project history).

Creating it makes you its **owner**. Only a domain-allowlisted account (any `@dimagi.com` address) or an
existing workspace member may create a workspace: a purely invite-admitted user cannot, which is a
deliberate security boundary (otherwise invite-admission would be delegable). To add someone, use the
workspace's **Settings → Members** page, or the `create_invite` MCP tool (`role` is `viewer`, `editor`,
`admin` or `owner`, default `viewer`). canopy emails them the invite link; it also comes back as `token`
(`https://canopy.dimagi.com/invite/<token>`) in case the email does not arrive. Deleting is owner-only and
returns 409, naming what is in the way, while the workspace still owns agents: move or delete those first.
</details>

### 3b. Your personal access token

The CLI talks to canopy-web as **you**, using a token minted once per machine:

```
/canopy:canopy-web-pat-mint
```

This opens a browser, and writes the token to `~/.claude/canopy/workbench-token`.

> **The MCP tools don't need this file.** Claude Code's canopy-web MCP server signs you in
> through the browser on first use when there is no token. The token is
> for the `canopy` CLI and skills, which call canopy-web's REST API directly and cannot
> borrow Claude Code's sign-in.

> **The identity rule that will bite you later.** That file is *your human token*.
> An **agent** gets its own token in `~/.<slug>/.env` as `CANOPY_WEB_PAT`. When you
> run canopy tooling from inside an agent's repo, canopy prefers the agent's token
> and falls back to yours **with a loud warning**. If you see
> *"falling back to the operator's workbench-token"*, that is canopy telling you
> the agent has no identity of its own yet — expected before §5, a problem after.
>
> **Giving the agent its token:** on the agent's Settings → Security tab in canopy-web,
> press **Give it its canopy login**. canopy-web mints the agent's own token and stores it in
> the agent's canopy vault (sealed — it is never shown). The next `canopy cred env --agent
> <slug>` (or session) puts it in `~/.<slug>/.env`; the fallback warning should be gone.

---

## 4. Create the agent

> **Does this agent already exist as an OpenClaw? Then you want a different command.**
> `create-agent` scaffolds from scratch; pointed at an agent that lives on an OpenClaw droplet it leaves
> the persona, memory and skills behind on a box you are about to decommission. Use the harvest route
> (`canopy openclaw-harvest snapshot | inventory | compare | bootstrap`, `--help` for flags), as laid out
> in [Moving an OpenClaw agent onto canopy](moving-an-openclaw-onto-canopy.md). `compare` tells you which
> branch you are on: **bootstrap** when no repo exists yet, **reconcile** when the repo exists and only
> the OpenClaw's newer skills need porting in. Section 5 then arrives out of order: you can register on
> canopy-web before the repo exists, and `canopy agent doctor --repo .` refuses until it does. `snapshot`
> needs `ssh` (it uses `rsync` when present and falls back to `ssh … tar`), so Windows is fine, but the
> key has to actually load: load it with `ssh-add` and confirm with `ssh-add -l`.

Two decisions first, and only two:

- **slug** — lowercase, 2–31 chars, starts with a letter (e.g. `scout`). It becomes
  the repo name, the plugin name, and the `/scout:...` command prefix. Hard to
  change later.
- **mandate** — one line: what this agent is *for*.

Then, from Claude Code, just ask: **"create a canopy agent called `<slug>` whose
mandate is `<one line>`"**. That invokes the `create-agent` skill, which runs the
factory for you.

To run it by hand instead:

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")"
uv run --project "$CANOPY_ROOT" canopy create-agent <slug> \
  --name "<Display Name>" \
  --mandate "<one-line mission>" \
  --mailbox "<slug>@dimagi-ai.com" \
  --stakeholders "<who it serves>" \
  --into ./<slug>
```

You get ~20 files and an initialised git repo with one commit. **This is a
skeleton, not a working agent** — §5 is the part that makes it real.

Push it to GitHub now, so the plugin has a source to install from:

```bash
cd <slug>
gh repo create dimagi-internal/<slug> --private --source=. --push
```

---

## 5. Make it real — let `agent doctor` drive

> **Setting up an agent someone else created for you?** Paste the prompt in
> [Setting up an agent from Claude](setting-up-an-agent-from-claude.md) instead. It runs this
> section, and does the canopy-web side (vault, invites) through the canopy-web MCP tools,
> which `doctor` never touches. **Just want to use it?** `canopy agent use <slug>`.

This is the step that replaces guesswork. From inside the agent's repo:

```bash
uv run --project "$CANOPY_ROOT" canopy agent doctor --repo .
```

It prints one line per check (`src/orchestrator/agent_doctor.py` defines them) and, for anything failing,
**the exact command that fixes it**. On a fresh scaffold, expect four failures: the plugin install, the
email auth and auth client (no gog token yet), and the canopy-web setup check (the agent is not
registered). They are the things the factory cannot do for you. Work them in this order:

1. **Plugin install** — makes `/scout:turn` invocable at all:
   `/plugin marketplace add dimagi-internal/scout` then `/plugin install scout@scout`.
2. **canopy-web setup** — registers the agent so it has a board:
   `canopy agent-publish register --repo .`
   In more than one workspace? Name the agent's home with `--workspace <slug>` (or `"workspace"` in
   `config/agent.json`, or `CANOPY_WEB_WORKSPACE=<slug>`): canopy-web refuses to guess, and the register
   fails until you do. Registration is reversible: only an agent's owner or a workspace owner can delete it in
   canopy-web, not an editor who does not own it (it takes the agent's projects, tasks, turns and skills with it). Deleting the GitHub repo
   is `gh repo delete dimagi-internal/<slug>`.
3. **Email auth** — only if the agent has a mailbox. Needs a real Google account provisioned first; ask a
   dimagi-ai.com admin. Then sign the mailbox in EITHER way: they are one Google app (`canopy` /
   `canopy-web`, `agent_email.FLEET_CLIENTS`) and a token under either serves the agent's turns:
   - browser: canopy-web `/w/<ws>/agents/<slug>/settings/accounts` → **Connect Google** (mints under
     `canopy-web`, stored in canopy-web; boxes pick it up on their next bootstrap: `canopy agent
     bootstrap` here, **Refresh runner** on a cloud runner's page);
   - terminal: `gog login <slug>@dimagi-ai.com --client canopy --services gmail,drive,docs,sheets,forms,appscript`
     (never `--client canopy-web`: that Web client cannot run gog's loopback login).
4. **Secrets** — they live in the agent's **canopy vault**. Set the values on its Settings → Security tab
   (or `canopy vault zk set --agent <slug> NAME`), turn zero-knowledge on (`canopy vault zk enable
   --agent <slug>`; keep the recovery code it prints offline), and press **Give them access** (Settings →
   Security) for each device the agent's page shows as *Grant pending*. Then `canopy cred env --agent
   <slug>` writes `~/.<slug>/.env` here; `doctor`'s *Secrets backend* line says whether this device has
   access.

Re-run `agent doctor` after each. **Do not move on until it is all green** — and
re-run it on any new machine, since it catches setup that only ever existed on
the old one.

**Existing agents on a new machine or macOS account?** To use one yourself, `canopy agent
use <slug>`. To host them all, `canopy agent bootstrap --dry-run` to preview, then
`canopy agent bootstrap` (add `--slug <x>` to limit it). It installs each agent's
plugin + `required_plugins`, writes `~/.<slug>/.env`, imports the gog token from canopy-web
under the client the token names,
and verifies Gmail. Either fleet client serves a fleet declaration; it flags loudly only
when the token is under a NON-fleet client the agent's turns would not use.

Then fill in the two files that carry the actual judgment:

- **`persona.md`** — voice, mandate detail, what is worth remembering.
- **`skills/<name>/SKILL.md`** — the agent's first real job. A skill is already a
  slash command (`/<slug>:<name>`); you do **not** write a separate command file.

### 5a. Mailbox setup on Windows

- **The shared OAuth client.** `agent bootstrap` reads it from the workspace's canopy vault "Shared" on
  canopy-web (`gog-oauth-client`). If bootstrap says it cannot read it, this machine needs a key to that
  vault: ask a workspace owner to give it one on canopy-web.
- **Where gog keeps things.** gog on Windows defaults to `%APPDATA%\gogcli`. With `GOG_HOME` set, gog
  0.40+ puts the client credentials in a `data\` subfolder (`<GOG_HOME>\data\credentials-canopy.json`)
  and `config.json` in `config\`. `agent doctor` looks in all of these.
- **PowerShell, not canopy:** write `--client=canopy`, not `--client canopy`. A long command pasted from
  chat can break at the space.

---

## 6. Letting it run on its own — a runner (macOS and Windows)

Everything above works without a runner: you use the agent yourself in Claude Code
([the two ways](running-an-agent.md)). A runner is the second way: it runs the agent's turns
**without you** — the turns canopy-web starts from email, from a schedule, or from a dispatch.
Schedules fire from canopy-web, so any runner the agent routes to picks them up.

- **Your own Mac** — install the Canopy runner app (**Add a runner** on canopy-web's Runners page) and pair
  it. Set it **Private** (`canopy private-runner set private`) and it does only your work: your
  pinned turns and your own sessions, never fleet work.
- **A fleet cloud box** — already running; an admin routes the agent to it.

Either way the runner opens the agent's canopy vault with its own device key, which an admin
grants once (shown as *Grant pending* on `/agents/<slug>`; **Give them access** on its
Settings → Security tab).

### Windows

Install the runner with the PowerShell installer rather than the bash one:

```powershell
.\runner\canopy_runner\scripts\install-runner.ps1
```

It mirrors the macOS install (snapshot the ref, build the wheels, `uv tool install`, provision the CDP
sidecar) and registers two **Scheduled Tasks** (`\Canopy\canopy-runner` and
`\Canopy\canopy-runner-updater`) where macOS uses launchd jobs. Bring it up in two passes: run it with
`-NoTasks` first, check `canopy-runner update-check --config <your config>` answers, then re-run without
the flag to register the tasks. Two Windows-specific setup notes:

- **emdash needs its debug port.** On macOS that is the "Emdash CDP" app; on Windows, make a shortcut to
  `emdash.exe` whose Target ends with `--remote-debugging-port=9222`.
- **The emdash DB path differs.** In `%USERPROFILE%\.canopy\runner.json`:
  `"emdash_db": "C:\\Users\\<you>\\AppData\\Roaming\\Emdash\\emdash4.db"`

**If you need to SSH anywhere (harvesting an OpenClaw, reaching a droplet),** an exported `.pem` is a
failure mode worth ruling out early: `ssh-keygen -l -f <file>` says `is not a key file` for a corrupt
export, and if the host offers `publickey` only there is no way past it. Load the key into `ssh-agent` so
it never lands on disk unprotected.

For the full detail (the two-job design, the log-redirection wrapper, the session boundary) see
`runner/canopy_runner/README.md` in canopy-web.

## 7. Your first turn

```bash
canopy agent use <slug>     # once, in the project you work from
```

then, in a new Claude Code session there:

```
/<slug>:turn
```

That is it. The turn procedure is fleet-canonical: the agent reads its inbound
channels, decides one action per counterpart, and closes with an explicit status
line. Outbound actions (email, public writes) pause for your approval by default —
that is the agent's **turn mode**, and it is board-side state you can read with
`canopy agent mode --slug <slug>`. A routing rule in the agent's Settings → Overview → Routing
can override it for one channel or one sender (e.g. a trusted colleague's email runs
auto); a turn reads its own mode with `--caller <path>`.

---

## 8. When something is wrong

Reach for these in order:

| Symptom | Command |
|---|---|
| Anything at all, on any machine | `canopy agent doctor --repo .` |
| canopy itself misbehaving | `/canopy:canopy-doctor` |
| Email auth failing | `canopy email preflight --repo .` |
| Using an agent on your own computer | `canopy agent use <slug>` — each step says ok / done / NEEDS YOU |
| `/<slug>:...` commands missing | The plugin is not installed — see §5 step 1 |
| "falling back to the operator's workbench-token" | The agent has no `CANOPY_WEB_PAT` — see §3b |
| An MCP server shows `Connection closed` | Read the MCP log — the real error is in the server's **stderr**, which `/mcp` does not show you (below) |

**`Connection closed` is never the actual error.** Claude Code reports the symptom;
the cause is one line of stderr in the MCP log, and reading it turns a debugging
session into a one-line fix. The logs live under
`~/Library/Caches/claude-cli-nodejs/<project>/mcp-logs-<server>/` on macOS and
`%LOCALAPPDATA%\claude-cli-nodejs\<project>\mcp-logs-<server>\` on Windows;
the newest `.jsonl` there, last few lines.

There is no `canopy-gws` MCP server: agents reach Google through `gog` and `canopy gdoc`.

---

## Appendix — where the rest of the documentation lives

| Doc | Read it when |
|---|---|
| `docs/agent-operating-model.md` | You want the *why* — primitives, topology, gating |
| `plugins/canopy/skills/create-agent/SKILL.md` | You want the factory's own reference |
| `plugins/canopy/agent-core/turn.md` | You want the exact turn procedure agents follow |
| `plugins/canopy/agent-core/task-tracker.md` | Multi-turn work and the board |
| canopy-web `runner/README.md` | You are actually pairing a runner |
| `README.md` (this repo) | You are developing canopy itself |
