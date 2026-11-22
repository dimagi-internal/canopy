# Stored agents — the canopy side

A **stored agent** has no GitHub repo. Its whole definition (skills, persona, rails, config) is a git
repository hosted by canopy-web: a draft is proposed, a human with agent-admin publishes it, there is a
live revision and rollback. canopy-web's half (models, file policy, API, UI) is its
`docs/architecture/stored-agents.md`. This page is what canopy does with it.

**Rule: a stored agent's definition is a real git repository hosted by canopy-web, and its dir on a machine is
an ordinary clone of it.** Everything downstream of "a clone at `AGENT_ROOT/<slug>`" stays as it is:
worktrees, `claude plugin marketplace add <dir>`, the fleet updater's `refresh_plugin` / `off_release_branch` /
`build_and_swap`, `agents_unprepared` all see a repo. canopy-web is the repo's only writer (drafts and publishes
go through its API); everyone else reads it with git.

## The repo

Served read-only over smart HTTP at `<canopy base>/git/<workspace>/<slug>.git` (upload-pack only; a push is
refused, pointing at `canopy agent definition edit`). canopy-web reports it as `definition.git_url` on the agent
detail, `/runtime` and `credentials/resolve`.

| Ref | Is |
|---|---|
| `refs/heads/main` | the LIVE published definition |
| `refs/heads/draft` | the one draft, if any |
| `refs/tags/r<N>` | published revision N |
| `refs/heads/imported` | history imported from GitHub by `convert --with-history` (people only) |

`revision` and `root_sha256` keep their API meaning: N is the `r<N>` tag on a commit, and the root is
`agent_definition.manifest_root` over that commit's tree (canopy recomputes it locally from the checkout).

**Auth.** `Authorization: Bearer <token>`, or Basic with the token as the password: a runner's token for an
agent it may hold, a person's with read on the agent, or the agent's own login. canopy passes the token for ONE
git command at a time, as the command-scoped config `http.<scheme://host>/.extraHeader` (git's
`GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n`/`GIT_CONFIG_VALUE_n` environment, the env form of `git -c`, so it is not in
`ps` either), with credential helpers and the terminal prompt off. **The token is never written to
`.git/config` or put in the remote url** (pinned in `tests/test_agent_materialize.py`). The header is scoped to
canopy's host, so a redirect elsewhere never carries it. Needs git >= 2.31.

## Materialize / sync (`src/orchestrator/agent_materialize.py`)

`canopy agent materialize --slug S --dest D [--revision N|live] [--runner]`:

1. Where: `git_url` from the caller (`agent prepare` passes the resolve payload's), else `GET /api/agents/S/runtime`,
   then the agent detail. With no `git_url` in either, `<base>/git/<workspace>/S.git` from the detail's workspace
   (or the workspace in a legacy `canopy://` origin). `--runner` reads with the runner's token (`$CANOPY_TOKEN`,
   then this box's runner session); otherwise the person's login (in a turn, the agent's). A 401/403 as a
   person's token on a runner box retries as the box's runner session.
2. `git init` (a new dest is built in a sibling staging dir and renamed into place), origin = `git_url`,
   `git fetch --prune --tags origin +refs/heads/main:refs/remotes/origin/main` with the token, `origin/HEAD` →
   `origin/main`.
3. `HEAD` → `main`, `git reset --hard origin/main` (or `r<N>`). Tracked files are replaced exactly; tracked files
   absent from the revision are deleted; untracked and ignored files (`.env`, `node_modules`, state) stay.
   Nothing in the working tree moves when the fetch fails.
4. `canopy.slug`, `canopy.revision`, `canopy.root-sha256` are recorded in the repo's git config. It prints one
   JSON line `{slug, revision, root_sha256, commit, changed}`; `changed: false` when HEAD was already there and
   nothing tracked was edited. One materialize per dest at a time (`.<slug>.materialize.lock` beside it).

**Migration.** A dir from canopy <= 0.2.897 (origin `canopy://<ws>/<slug>`, one deterministic orphan commit per
revision) is converted in place by the same steps: origin is set to the git url, fetched, reset. `.env` and
other untracked files stay; the first sync after the upgrade reports `changed: true` (HEAD moves from the
orphan commit to the real one), so the fleet updater reinstalls once.

**Exported agents.** When canopy-web says `source: repo` (the agent moved to GitHub), a stored dir at the dest is
repointed: origin becomes `https://github.com/<o>/<n>.git`, the `canopy.*` config goes, and the line says
`"source": "repo", "repo_url": …`; the caller's ordinary repo path (pull / fetch + reset) takes over. Export
pushes main with its whole history, so the local HEAD is an ancestor and the pull is a fast-forward. With no dir
at the dest it is an error naming the repo.

`canopy agent sync --slug S [--runner] [--dest D]` is the stored agent's `git pull`: the same thing into
`$AGENT_ROOT/S` (`--runner`) or `~/.canopy/agents/S`, creating it when missing, refusing a dir that is a GitHub
clone. Same JSON line, exit 0. (With `--doc-url --title --period-start --period-end` `agent sync` is still the
manager-sync post.)

A stored dir is recognised by its origin: under `<canopy base>/git/` (base = `$CANOPY_WEB_API_URL`, default
`https://canopy.dimagi.com`), the legacy `canopy://`, or (another canopy-web, e.g. staging) the shape
`…/git/<ws>/<slug>.git` together with a recorded `canopy.slug`.

## Where it is called

| Caller | What it does for a stored agent |
|---|---|
| `canopy agent prepare --runner` (`agent_prepare.py::step_repo`) | the resolve payload's `definition.source == "stored"` (or no repo and a stored dir) → materialize from `definition.git_url` into the clone dest instead of cloning GitHub. A stored dir whose agent is now `source: repo` is repointed at the GitHub repo and pulled. Later steps are unchanged. |
| canopy-web's runner (`cloud_runner.py`, the laptop runner) | `agent prepare --runner` to stage; `agent sync --runner` where a repo agent is `git pull`ed. Runner code that inspects an origin must accept `<base>/git/` as well as `canopy://`. |
| `fleet_session_start_update.py::update_one` | a stored origin → no bare `git fetch` (it would carry no token); `canopy agent sync --slug S --dest <clone>`, then the usual `build_and_swap` + `patch_registry` when HEAD moved (the cache dir is keyed by plugin.json's version, `0.<revision>.0`). If the sync repointed the dir at GitHub (exported), the ordinary fetch path runs. |
| `canopy agent use S` (`agent_use.py`) | agent detail says `definition.source == "stored"` → materialize into `~/.canopy/agents/S`, `claude plugin marketplace add` that dir and install `S@<its marketplace name>`; not the `canopy-agents` catalog |
| `canopy agent-publish register` / `skills` (`agent_web.py`) | refused, pointing at `canopy agent definition edit`; the sync/tasks verbs skip their implicit register |

## Changing one

- `canopy agent definition show|files|cat <path>|diff` read it; `edit --set <path> <file> --delete <path> -m <msg>`
  creates or replaces the single draft on the live base; `publish <n>` and `rollback <n>` are a human admin's;
  `discard <n>`. A stored agent edits ITSELF only by proposing a draft (canopy-web files an approval to its
  owner); it never publishes.
- `canopy agent definition clone [--slug S] [DIR] [--no-helper]` makes a person's working copy (main, draft, tags)
  with the token sent on that command only. It sets the clone's `credential.<canopy host>.helper` to
  `canopy agent definition credential` (hidden; answers git's `get` with the canopy-web login as the Basic
  password, for canopy's host only), so a later `git pull` works with nothing stored. `--no-helper` leaves the
  config alone and prints the one-off `git -c credential.<host>.helper=… pull` form instead.
- `canopy create-agent <slug> --mandate … --stored --workspace <ws>` creates one in canopy-web and writes nothing
  locally.

## Moving between canopy and GitHub

- **In:** `canopy agent definition convert --from <dir> [--with-history]` turns a repo agent into a stored one: its
  tracked files that the policy allows become revision 1. Paths canopy-web generates (`.claude-plugin/*`,
  `.claude/settings.json`, `bin/<slug>-email`, `.gitignore`) are not uploaded; refused ones (workflows, scripts,
  `.py` tests) stop it unless `--drop-refused`. `--with-history` adds `bundle_b64` = `git bundle create - main`
  (the repo must have `main`); canopy-web imports it to `refs/heads/imported` and makes revision 1's parent its
  tip. A body above ~2.4 MB (canopy-web's 2.5 MB request cap) is refused client-side, and a server 413 likewise,
  both with the files-only hint. `--dry-run` prints the plan (with the bundle's size).
- **Out:** `canopy agent definition export --to-github OWNER/NAME [--ref main]` calls
  `POST /definition/export {repo, ref}`: canopy-web pushes main + the `r<N>` tags to the EMPTY repo with the agent's
  GitHub delegation and switches the agent to `source=repo`. `--push-myself` does the push from here instead: it
  checks the repo is empty (`git ls-remote` with your git credentials), bare-clones the canopy repo (token on that
  command only), pushes `main` + tags with YOUR git credentials (no canopy header), then calls
  `POST /definition/detach {repo_url}`. Either way the canopy repo stays as a read-only archive; runners and the
  fleet updater move to GitHub on their next pass (above).

## The two helpers shared with canopy-web

`src/orchestrator/agent_definition.py` is a vendored copy of the SDK's `canopy_sdk.agent_definition`
(canopy does not depend on the SDK):

- `manifest_root(files)` — sha256 of `json.dumps(entries, sort_keys=True, separators=(",",":"))` over
  `{path, sha256, size, executable}` sorted by path. It MUST match the server byte for byte; the shared test
  vector is in `tests/test_agent_definition.py`.
- `check_tree(files, slug)` — the file policy as a pre-check, so `edit`/`convert` name the refused file before
  uploading. canopy-web is the authority on every write.
