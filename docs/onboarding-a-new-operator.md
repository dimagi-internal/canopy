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
| **canopy-web** | The shared website: each agent's board, tasks, turns, work products. Lives at `labs.connect.dimagi.com/canopy`. | Already deployed — you just log in | **Yes** (read/write via CLI) |
| **The runner** | A daemon that fires an agent's turns *unattended* (on a schedule, or when email arrives). Works on macOS and Windows. | Your machine, or a cloud box | **No — skip it at first**, unless you need email-triggered turns |

**The runner is the piece to skip on day 1.** An agent is fully usable without
it: you invoke `/<slug>:turn` in Claude Code yourself. The runner only removes
*you* from the loop — which matters as soon as you want turns triggered by
incoming email or by a schedule. See §6.

---

## 1. Prerequisites

Install these first. Everything below assumes they exist.

| Tool | Why | Check it works |
|---|---|---|
| [Claude Code](https://code.claude.com/docs) | The runtime | `claude --version` |
| [git](https://git-scm.com/downloads) | Your agent is a repo | `git --version` |
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | Runs the `canopy` CLI | `uv --version` |
| [GitHub CLI](https://cli.github.com/) | Creating the agent's repo | `gh auth status` |
| [1Password CLI](https://developer.1password.com/docs/cli/get-started/) | Resolving the agent's secrets | `op whoami` |
| [Node.js 20+](https://nodejs.org/en/download) | `npx` runs the token-mint script | `node --version` |
| [gog](https://github.com/dimagi-internal/gogcli) | Your agent's Gmail/Drive access — §5 uses it | `gog --version` |

**`gog` on Windows.** There is no Homebrew, so install from the release
archive: download the latest `gog_windows_amd64.zip` from
[the releases page](https://github.com/dimagi-internal/gogcli/releases),
unzip it somewhere permanent (e.g. `%LOCALAPPDATA%\gog\`), and add that folder
to your `PATH` (Settings → *Edit environment variables for your account* →
`Path` → New). Open a NEW terminal afterwards — `PATH` changes do not reach
already-running shells. On macOS/Linux: `brew install dimagi-internal/tap/gog`.

You also need **membership in the `dimagi-internal` GitHub org** and a
**canopy-web login**. Ask Jonathan for both if `gh repo list dimagi-internal`
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

It is idempotent — safe to re-run — and provisions the state directory, the
capture hook, your canopy-web token, and the `canopy` CLI in one pass.

To update later: `/canopy:update`.

---

## 3. Get your canopy-web access sorted BEFORE you build anything

Two things live here, and mixing them up is the most common early confusion.

### 3a. Your workspace ("team")

A **workspace** is canopy-web's tenant — the "team" whose agents, tasks and work
products you can see. An agent lives in exactly one workspace **at a time**, but
that is not a one-way door: setting an explicit `workspace` on an
already-homed agent **moves** it (`apps/agents/schemas.py`), so this is a
reversible decision, not a commitment.

**You almost certainly do not need to create one. But do NOT just take the
default either** — this is the one choice in this doc worth thirty seconds of
thought, because the default is the *less* private option.

Two workspaces exist today, and they differ in exactly the way that matters:

| Workspace | Who gets in | Who lives there |
|---|---|---|
| `dimagi` | **auto-admits every `@dimagi.com` address as an EDITOR** on first login | the default for a new agent |
| `connect` | no auto-join — membership is explicit | `hal`, `ace`, `ada`, `echo` |

Editor is not a read-only role: `DELETE /api/agents/{slug}` accepts editor or
owner. So an agent left in `dimagi` can have its board, tasks, turns and work
products read — **and can be deleted outright** — by any Dimagi employee who has
ever logged into canopy-web. Nobody has done this, but it is not a boundary you
want to be relying on politeness for.

**Recommendation: put your agent in `connect`**, where the rest of the fleet
already is. It costs one field at registration:

```bash
canopy agent register --slug <your-agent> --workspace connect
```

That gets you explicit membership rather than domain auto-join, and it keeps
your agent alongside the others for anything that reasons across the fleet.

A **third, separate workspace** is available if you genuinely want isolation
(below) — but prefer it only when you have a real reason, since every person who
should see your agent then has to be invited individually.

> If you have already registered into `dimagi`, re-running `register` with
> `--workspace connect` moves it. Nothing is lost.

<details>
<summary>Only if you genuinely need a separate workspace</summary>

There is **no UI for creating a workspace** — it is API-only:

```bash
curl -X POST https://labs.connect.dimagi.com/canopy/api/workspaces/ \
  -H "Authorization: Bearer $(cat ~/.claude/canopy/workbench-token)" \
  -H 'Content-Type: application/json' \
  -d '{"slug":"my-team","display_name":"My Team"}'
```

Still pick the slug deliberately — lowercase letters, digits and hyphens only,
and it appears in every URL your team uses. But it is **no longer a one-way
door**: an owner can delete a workspace once it is empty.

```bash
curl -X DELETE https://labs.connect.dimagi.com/canopy/api/workspaces/my-team/ \
  -H "Authorization: Bearer $(cat ~/.claude/canopy/workbench-token)"
```

Owner-only, and it returns **409 while the workspace still owns agents**, naming
them — delete those first (below) and retry.

You must be either on the email allowlist (any `@dimagi.com` address is) or
already a member of some workspace. A purely invite-admitted user cannot bootstrap
one — that is a deliberate security boundary, not a bug.
</details>

### 3b. Your personal access token

The CLI talks to canopy-web as **you**, using a token minted once per machine:

```
/canopy:canopy-web-pat-mint
```

This opens a browser, and writes the token to `~/.claude/canopy/workbench-token`.

> **The identity rule that will bite you later.** That file is *your human token*.
> An **agent** gets its own token in `~/.<slug>/.env` as `CANOPY_WEB_PAT`. When you
> run canopy tooling from inside an agent's repo, canopy prefers the agent's token
> and falls back to yours **with a loud warning**. If you see
> *"falling back to the operator's workbench-token"*, that is canopy telling you
> the agent has no identity of its own yet — expected before §5, a problem after.

---

## 4. Create the agent

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

This is the step that replaces guesswork. From inside the agent's repo:

```bash
uv run --project "$CANOPY_ROOT" canopy agent doctor --repo .
```

It prints one line per check and, for anything failing, **the exact command that
fixes it**. On a fresh scaffold you should expect roughly this:

```
[OK  ] Identity              slug=scout mailbox=scout@dimagi-ai.com gog_client=canopy
[FAIL] Plugin install        plugin 'scout' is NOT installed — ...
[OK  ] Gating rails          2 effective deny rail(s)
[OK  ] Hook wiring           gating_guard.py registered as a PreToolUse hook
[OK  ] Secrets manifest      .env.tpl (1 var(s), 0 op:// ref(s))
[OK  ] Rails enforced        guard blocked the raw-send probe (exit 2)
[FAIL] Email auth (gog)      ... does not map scout@dimagi-ai.com -> canopy
[FAIL] Auth client           no gog token on this machine for scout@dimagi-ai.com
[FAIL] canopy-web board      agent 'scout' not registered
```

**Four failures on a brand-new agent is correct.** They are the four things the
factory cannot do for you. Work them in this order:

1. **Plugin install** — makes `/scout:turn` invocable at all:
   `/plugin marketplace add dimagi-internal/scout` then `/plugin install scout@scout`.
2. **canopy-web board** — registers the agent so it has a board:
   `canopy agent-publish register --repo .`
   The agent lands in the workspace your token defaults to; override with
   `CANOPY_WEB_WORKSPACE=<slug>` if that is not what you want.
   Made a typo in the slug, or just trying one out? Registration is reversible:
   `curl -X DELETE .../api/agents/<slug>/ -H "Authorization: Bearer $(cat ~/.claude/canopy/workbench-token)"`
   (editor or owner; takes the agent's tasks, turns, skills and work products
   with it). Deleting the GitHub repo is `gh repo delete dimagi-internal/<slug>`.
3. **Email auth** — only if the agent has a mailbox. Needs a real Google account
   provisioned first; ask Jonathan. Then
   `gog login <slug>@dimagi-ai.com --client canopy --services gmail,drive,docs,sheets,forms`.
4. **Secrets** — `op inject -i .env.tpl -o ~/.<slug>/.env --account dimagi.1password.com`.

Re-run `agent doctor` after each. **Do not move on until it is all green** — and
re-run it on any new machine, since it catches setup that only ever existed on
the old one.

Then fill in the two files that carry the actual judgment:

- **`persona.md`** — voice, mandate detail, what is worth remembering.
- **`skills/<name>/SKILL.md`** — the agent's first real job. A skill is already a
  slash command (`/<slug>:<name>`); you do **not** write a separate command file.

---

## 6. The runner — including on Windows

The runner is what fires an agent's turns **without you**: it polls the agent's
mailbox and enqueues a turn per new email thread, and it fires scheduled turns.
Everything above works without it — you invoke `/<slug>:turn` yourself — so skip
this section until turns-you-run-by-hand become the bottleneck.

But if **email-triggered or scheduled turns matter to you, you need a runner**,
and specifically the *laptop* runner: the cloud runner claims turns that
something else queued, and has no inbox polling or schedule firing of its own.

Two runners, and what each is for:

| | **Laptop runner** | **Cloud runner** |
|---|---|---|
| Triggers turns from email / schedules | **yes** | no |
| Executes turns | drives the emdash app over CDP | headless `claude -p` |
| Runs on | macOS **and Windows** | a Linux server |

### Windows

Supported. Install it with the PowerShell installer rather than the bash one:

```powershell
.\runner\canopy_runner\scripts\install-runner.ps1
```

It mirrors the macOS install exactly — snapshot the ref, build the wheels,
`uv tool install`, provision the CDP sidecar — and registers two **Scheduled
Tasks** where macOS uses launchd jobs:

| | macOS | Windows |
|---|---|---|
| supervisor | launchd | Task Scheduler |
| runner job | `com.canopy.runner` | `\Canopy\canopy-runner` |
| updater job | `com.canopy.runner.updater` | `\Canopy\canopy-runner-updater` |

Two Windows-specific setup notes:

- **emdash needs its debug port.** On macOS that is the "Emdash CDP" app; on
  Windows, make a shortcut to `emdash.exe` whose Target ends with
  `--remote-debugging-port=9222`.
- **The emdash DB path differs.** In `%USERPROFILE%\.canopy\runner.json`:
  `"emdash_db": "C:\\Users\\<you>\\AppData\\Roaming\\Emdash\\emdash4.db"`

Everything else — Claude Code, emdash (stable ships `emdash-x64.exe` and
`.msi`), the canopy CLI, canopy-web — is cross-platform already.

**One honest caveat.** The Windows runner's logic is shared with the macOS one
and unit-tested on both platforms' branches, and the installer is lint-clean —
but it has not yet been run on a real Windows machine, because there wasn't one
in the fleet when it was written. Treat your first install as a bring-up: run it
with `-NoTasks` first, check `canopy-runner update-check --config <your config>`
answers, then re-run without the flag to register the tasks. If something breaks,
that is a bug worth reporting, not something you did wrong.

For the full detail — the two-job design, the log-redirection wrapper, the
session boundary — see `runner/canopy_runner/README.md` in canopy-web.

## 7. Your first turn

```
/<slug>:turn
```

That is it. The turn procedure is fleet-canonical: the agent reads its inbound
channels, decides one action per counterpart, and closes with an explicit status
line. Outbound actions (email, public writes) pause for your approval by default —
that is the agent's **turn mode**, and it is board-side state you can read with
`canopy agent mode --slug <slug>`.

---

## 8. When something is wrong

Reach for these in order:

| Symptom | Command |
|---|---|
| Anything at all, on any machine | `canopy agent doctor --repo .` |
| canopy itself misbehaving | `/canopy:canopy-doctor` |
| Email auth failing | `canopy email preflight --repo .` |
| `/<slug>:...` commands missing | The plugin is not installed — see §5 step 1 |
| "falling back to the operator's workbench-token" | The agent has no `CANOPY_WEB_PAT` — see §3b |

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
