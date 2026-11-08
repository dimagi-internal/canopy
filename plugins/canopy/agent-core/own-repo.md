# Changing your own repo (the `agent_edit` rail)

**The rule.** Only an asker who holds `agent_edit` on you (the `editor` role and above: editors,
admins, the owner; canopy-web lists it in the envelope's `permissions`) may change YOU, including
your own repo. Everyone else may read and write GitHub freely, but not your repo. Issues,
comments, other repos and a PR from a fork are all fine.

canopy-web enforces `agent_edit` for changes made through canopy-web. It cannot see what a session
does with `git` and `gh` directly, so the fleet gating hook (`gating_guard.py --session`) carries
the session-side half.

## What is refused (when the turn's asker lacks `agent_edit`)

The matcher is `gating_guard.py::own_repo_violation` (its block comment above `own_repo_refusal`
is the design note); this list is its summary. Only writes to YOUR OWN repo (the `origin` of your clone, the repo URL in `config/agent.json` /
`.claude-plugin/plugin.json`, or the envelope's `agent_repo`, which holds wherever the session's
directory is):

- `git push` of any branch or tag to it (your clone's origin, a remote whose push URL is it, its
  URL with or without a token in it, or `git -C <dir> push`),
- `gh pr create|merge|edit|close|review|ready|reopen|lock|unlock|update-branch` on it,
- `gh release create|edit|delete|upload|delete-asset` and `gh repo edit|delete|rename|archive|unarchive|sync|deploy-key` on it,
- `gh workflow run|enable|disable`, `gh secret set|delete` and `gh variable set|delete` on it
  (`--org` / `--user` secrets and variables are not the repo's and stay open),
- `gh api` with `-X POST|PUT|PATCH|DELETE`, or with `-f/-F/--field/--raw-field/--input` and no
  `-X`, against `repos/<you>/...` (except `issues`, a PR's review comments and `forks`),
- a PATCH of `repos/<you>/issues/<n>` that sets `state`: a PR is an issue, so this closes a PR,
- the same REST calls through `curl` or `wget` to `api.github.com` / `uploads.github.com`, whether
  or not a token is on the line,
- a `gh api graphql` (or curl/wget `/graphql`) **mutation that names your repo or does not say which
  repo it targets**, and any GraphQL request whose query the hook cannot read. See the next section.
- `gh <alias>` when the alias runs any of the above (read from gh's config file), and setting one
  with `gh alias set`. `gh alias import` and an expansion from a variable or stdin are refused,
  because what they set cannot be read.

### GraphQL fails closed

GraphQL mutations mostly name their target by opaque node id (`pullRequestId: "PR_..."`). The hook
makes no network call, so it cannot map one to a repo. It therefore treats a mutation it cannot place
as if it were on your repo, **on every repo, not only yours**:

| Request | While the rail is active |
|---|---|
| A query (a read), inline or in a variable set by `NAME='...'` in the same command | allowed |
| `addComment`, `createIssue`, `closeIssue` and the other issue and comment mutations | allowed |
| `createCommitOnBranch` whose `repositoryNameWithOwner` is another repo | allowed |
| `createCommitOnBranch` whose `repositoryNameWithOwner` is your repo | refused |
| Any other mutation (`mergePullRequest`, `enablePullRequestAutoMerge`, `closePullRequest`, `enqueuePullRequest`, `dequeuePullRequest`, `deleteRef`...) | refused |
| A query from `@file`, `--input`, stdin, or a variable or substitution the hook cannot resolve | refused |

The operation is read from the query text, so the word `mutation` in a string or comment does not
count. Naming a repo somewhere else (a spare `-f r=other/repo`) does not unlock a mutation, because
that field does nothing to where the mutation lands.

What to use instead, on any repo: `gh pr merge|close|edit ... -R <owner/repo>` (auto-merge and the
merge queue are `gh pr merge <n> --auto -R <owner/repo>`) or REST (`gh api repos/<owner>/<repo>/...`).
Both name the repo, so the rail allows them on other repos. **Taking a PR out of a merge queue has
no gh or REST form** (`gh pr merge --disable-auto` exits 0 and leaves a queued PR in the queue), so a
turn whose asker lacks `agent_edit` has to use the PR's page, or ask someone who holds it.

## What stays open

- `gh issue create|comment|close ...` on your repo, and `gh api repos/<you>/issues/...` (comments,
  labels, title and body edits; only a `state` change above is refused). The same through curl/wget,
  and GraphQL's issue and comment mutations (`addComment`, `createIssue`, `closeIssue`...).
- A PR **from a fork**: `gh repo fork <you>`, push to the fork, then
  `gh pr create -R <you> --head <fork-owner>:<branch>`.
- Any write to ANY OTHER repo that says which repo it is, and every read. A GraphQL mutation that
  does not say is the one exception, above.
- Editing local files and committing in your clone: nothing leaves the machine until a push.
  (The Write/Edit tools are not railed on `skills/`, `persona.md`, `CLAUDE.md` or `config/`:
  agents legitimately write state under `config/`, and a path rail cannot tell a state file
  from an edit to yourself. The push is the line.)

When you are refused, the message says so in plain words. Do not look for another route to the
same write; tell the asker they need the editor role (or offer the issue / fork-PR path).

## When it applies

Only to a session that belongs to an agent AND is a canopy turn: a runner's `$CANOPY_TURN_ID`, a
local turn (`~/.canopy/local/<session>.json`), or the laptop's by-task record
(`~/.canopy/caller/by-task/<task>.json`, unless the owner has taken over the keyboard). A person
running Claude Code in your repo by hand has no turn and is untouched; so is canopy's own repo.

The envelope is read from `~/.canopy/caller/<turn_id>.json` only (the file's own `turn_id` must
match its name, no symlink out). For a turn that has one and wants an own-repo write, a missing,
unreadable or mismatched envelope fails CLOSED. An envelope with no
`permissions` list falls back to `relationship` (owner, admin, system allowed).

### A turn with no agent, in your clone

A repo chat or repo dispatch has no agent, so its asker holds no permission on you. It is allowed
only when the envelope's `own_box_turn` is true: canopy-web's word that a verified person, unconfined,
is working on a box they own, which is the same as that person by hand. Everything else stays refused:

- a teammate's own session on someone else's box, though its `relationship` also says `owner`,
- an agent's own login, even on its owner's box (`system`), or anyone with no edit on an agent could
  have it dispatch a repo turn into its own clone. The owner can run that turn themselves,
- a scheduled repo turn, a confined turn, and an envelope without the field.

### When no repo is known

If neither the session's directory, your installed copy nor the envelope names your repo, the rail
cannot compare a target to anything. What happens depends on what the envelope says:

| Envelope | Asker lacks `agent_edit` | Asker holds it |
|---|---|---|
| `agent_repo` is `""`: canopy-web has no repo set for you | **fails closed**: every write above is refused, on any repo | allowed |
| No `agent_repo` key in the envelope | rail off | allowed |
| `agent_repo` is `null`: the turn has no agent | rail off | rail off |
| No envelope at all | rail off | rail off |

A missing envelope cannot fail closed here: the runner prunes envelopes after a week and a laptop's
by-task record outlives them, so it is routine, and refusing would lock out the owner's own turns.
Failing closed still leaves reads, issues and comments, a PR from a fork, and the commands of
your read mode and every host site. The fix is for the owner to set your repo on canopy-web.

## Forging

The fleet baseline also refuses, for any agent session: assigning `CANOPY_TURN_ID`,
`CANOPY_CALLER` or `CANOPY_SCOPED_TURN` in a command; writing into `~/.canopy/caller/` or
`~/.canopy/local/` (shell, Edit, Write). Read the envelope with `canopy caller tier`.

## What it does not cover

A guard rail, not a boundary: it reads the command's own words, and failing closed on GraphQL covers
only the GraphQL it can recognise as GraphQL. It does not see:

- **a script file, library or other program** that pushes or calls the API. This is the widest gap
  and failing closed does not touch it: `bash merge.sh`, `python -c`, a `gh` extension, `hub`,
  `httpie`, `find -exec`, `xargs -I`, `parallel`, or a Claude tool that is not the shell. A script
  that runs `gh api graphql` with a mutation passes, and so does a query from a file it wrote,
- a directory, URL, program or subcommand held in a variable (`git -C "$WT" push`, `curl "$URL"`,
  `$GH api graphql`, `gh "$SUB" ...`). `gh api $ALL_ARGS`, with no field on the line, is read as a GET,
- a shell expansion inside a query's parentheses or strings (`pullRequest(number:$N)` is how reads
  are normally written, and GraphQL's own `$n` looks the same once quotes are gone), so a value
  that closes the parentheses and adds a mutation is not seen. Outside them, an expansion is refused,
- a shell alias (`shopt -s expand_aliases; alias g='gh api graphql --input x'; g`),
- a gh alias whose config lives somewhere other than `GH_CONFIG_DIR`, `XDG_CONFIG_HOME/gh` or
  `~/.config/gh`, or set by editing that file in the same command that runs it,
- a request body from a file or stdin on a REST path: `gh api --input`, `curl -d @file`,
  `wget --post-file` (the mutating call to a repo path is still refused; the unseen body only decides
  a PATCH of an issue's `state`),
- a turn in a session outside your clone, with no installed copy of you, whose envelope is missing
  or has no `agent_repo` key, where the whole rail is off (see above),
- a session that is not known to be yours at all: no `CANOPY_AGENT_SLUG` or `CANOPY_AGENT` in its
  env and a directory that is no agent's repo, which no rail of yours applies to,
- `gh issue close|reopen <n>` when `<n>` is a PR: gh closes it, and a number alone does not say
  which it is, so the issue allowance stands,
- `gh run rerun|cancel|delete`, `gh cache delete` and `gh label` writes, which do not change the
  repo's contents,
- a direct edit of the repo on the runner's disk by another process.

canopy-web (not this hook) is the boundary for changes made through canopy-web.
