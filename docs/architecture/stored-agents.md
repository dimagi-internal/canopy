# Stored agents — the canopy side

A **stored agent** has no GitHub repo. Its whole definition (skills, persona, rails, config) is a file
tree with revisions in canopy-web: a draft is proposed, a human with agent-admin publishes it, there is a
live revision and rollback. canopy-web's half (models, file policy, API, UI) is its
`docs/architecture/stored-agents.md`. This page is what canopy does with it.

**Rule: a stored agent is MATERIALIZED into an ordinary git repo, and everything downstream of "a clone at
`AGENT_ROOT/<slug>`" stays as it is.** Worktrees, `claude plugin marketplace add <dir>`, the fleet
updater's `refresh_plugin` / `off_release_branch` / `build_and_swap`, `agents_unprepared` all see a repo.

## Materialize (`src/orchestrator/agent_materialize.py`)

`canopy agent materialize --slug S --dest D [--revision N|live] [--runner]`:

1. `GET /api/agents/S/definition/archive?revision=N|live` — a gzip tar of regular files (0644/0755) and
   headers `X-Canopy-Revision`, `X-Canopy-Root-Sha256`, `X-Canopy-Published-At` (optionally
   `X-Canopy-Workspace`). `--runner` reads with the runner's token (`$CANOPY_TOKEN`, then this box's runner
   session); otherwise the person's login (in a turn, the agent's).
2. The root is recomputed with `agent_definition.manifest_root` and compared **before anything is written**.
   A link, an escaping path, `.git/`, a duplicate or an oversized archive is refused.
3. Blobs, a tree and ONE orphan commit go into the dest's object store: author and committer
   `canopy <agents@canopy.dimagi.com>`, both dates = Published-At (whole seconds, UTC), message
   `canopy revision <N>`, modes 100644/100755. The commit object is written byte for byte by canopy (not
   `git commit`), so gpg-signing or encoding config cannot change it: **one revision, one sha, every
   machine** (pinned in `tests/test_agent_materialize.py`).
4. `refs/remotes/origin/main` (and `origin/HEAD`) point at it; origin's url is `canopy://<workspace>/<slug>`,
   which git can never fetch. Only then the swap: `HEAD` → `main`, `git reset --hard <commit>`. Tracked files
   are replaced exactly; tracked files absent from the revision are deleted; untracked and ignored files
   (`.env`, `node_modules`, state) stay. A dest that does not exist is built in a sibling staging dir and
   renamed into place.
5. `canopy.slug`, `canopy.revision`, `canopy.root-sha256` are recorded in the repo's git config. It prints
   one JSON line `{slug, revision, root_sha256, commit, changed}`; a dest already at the revision is left
   alone (`changed: false`; with `live`, a matching `GET /definition` skips the download entirely).

`canopy agent sync --slug S [--runner] [--dest D]` is the stored agent's `git pull --ff-only`: materialize the
live revision into `$AGENT_ROOT/S` (`--runner`) or `~/.canopy/agents/S`, creating it when missing, refusing a
dir that is a GitHub clone. Same JSON line, exit 0. (With `--doc-url --title --period-start --period-end`
`agent sync` is still the manager-sync post.)

## Where it is called

| Caller | What it does for a stored agent |
|---|---|
| `canopy agent prepare --runner` (`agent_prepare.py::step_repo`) | the resolve payload's `definition.source == "stored"` (or no repo and a `canopy://` dir) → materialize into the clone dest instead of cloning. Later steps are unchanged. |
| canopy-web's runner (`cloud_runner.py`, the laptop runner) | `agent prepare --runner` to stage; `agent sync --runner` where a repo agent is `git pull`ed |
| `fleet_session_start_update.py::update_one` | an origin `canopy://…` → no `git fetch`; `canopy agent sync --slug S --dest <clone>`, then the usual `build_and_swap` + `patch_registry` when HEAD moved (the cache dir is keyed by plugin.json's version, `0.<revision>.0`) |
| `canopy agent use S` (`agent_use.py`) | agent detail says `definition.source == "stored"` → materialize into `~/.canopy/agents/S`, `claude plugin marketplace add` that dir and install `S@<its marketplace name>`; not the `canopy-agents` catalog |
| `canopy agent-publish register` / `skills` (`agent_web.py`) | refused, pointing at `canopy agent definition edit`; the sync/tasks verbs skip their implicit register |

## Changing one

- `canopy agent definition show|files|cat <path>|diff` read it; `edit --set <path> <file> --delete <path> -m <msg>`
  creates or replaces the single draft on the live base; `publish <n>` and `rollback <n>` are a human admin's;
  `discard <n>`. A stored agent edits ITSELF only by proposing a draft (canopy-web files an approval to its
  owner); it never publishes.
- `canopy agent definition convert --from <dir>` turns a repo agent into a stored one: its tracked files that
  the policy allows become revision 1. Paths canopy-web generates (`.claude-plugin/*`, `.claude/settings.json`,
  `bin/<slug>-email`, `.gitignore`) are not uploaded; refused ones (workflows, scripts, `.py` tests) stop it
  unless `--drop-refused`. `--dry-run` prints the plan.
- `canopy create-agent <slug> --mandate … --stored --workspace <ws>` creates one in canopy-web and writes nothing
  locally.

## The two helpers shared with canopy-web

`src/orchestrator/agent_definition.py` is a vendored copy of the SDK's `canopy_sdk.agent_definition`
(canopy does not depend on the SDK):

- `manifest_root(files)` — sha256 of `json.dumps(entries, sort_keys=True, separators=(",",":"))` over
  `{path, sha256, size, executable}` sorted by path. It MUST match the server byte for byte; the shared test
  vector is in `tests/test_agent_definition.py`.
- `check_tree(files, slug)` — the file policy as a pre-check, so `edit`/`convert` name the refused file before
  uploading. canopy-web is the authority on every write.
