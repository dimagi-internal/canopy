# Agent storage → the agent's shared Drive root — the fleet-canonical filing standard

> **Fleet-canonical process (canopy agent-core).** Every agent files what it produces and what it
> must remember into **one shared Google Drive**, in a fixed per-agent layout. Your agent's
> `gdoc-writer` (or equivalent publishing skill) is a thin stub that (a) points here and (b)
> declares your agent-specifics: your Drive root (vault-resolved, see below) and the exact
> publishing mechanism. To change THIS standard for the whole fleet, PR canopy
> (`plugins/canopy/agent-core/deliverables.md` + `canopy version bump`).

There are two kinds of thing an agent keeps outside its repo, and both live in the same shared
place so the team can find them and they survive across turns/worktrees:

- **Deliverables** — anything a human is meant to read, review, or keep (a brief, a draft,
  a concept note, a research summary, a form submission). The point of publishing is that the team
  can **find it, comment on it, and rely on it surviving.**
- **Process state** — the durable operational memory a recurring job needs across turns (a
  meeting-prep tracker so prep isn't duplicated, a run log, a registry). A repo-local file is the
  wrong home: a fresh worktree/branch each turn recreates or loses it.

A doc in the agent's personal My Drive root, or a loose local file, serves neither: it's invisible
to the team and dies with the agent identity / the worktree.

## The layout (non-negotiable)

Every agent has exactly **one Drive root of its own** — `$GDRIVE_ROOT_FOLDER`, the folder named for
the agent (`Eva`, `Echo`, …). **That root is the only Drive location an agent needs to know.**
Everything it produces or remembers goes underneath it, in two standing areas (more may be added as
we learn what agents need to keep):

```
$GDRIVE_ROOT_FOLDER/             ← your own root folder — the one id you care about
├── Projects/                    ← deliverables, ONE subfolder per project/task
│   └── <Project or counterpart>/   ← reuse across turns; never dump flat in Projects/
└── Process State/               ← durable trackers / run logs / registries
```

**Don't reason about the parent.** Where your root physically sits — under the shared `AI-Agents`
drive, under a division's drive like Connect Marketing, anywhere else — is an administrative choice
recorded once in your 1Password vault (`op://Agent-<Slug>/gdrive-root-folder`). It differs per agent
and per environment, it can be moved without touching any code, and **nothing in a skill or repo
should reference it or anything above it.** You resolve `$GDRIVE_ROOT_FOLDER` and file beneath it;
that's the whole contract.

1. **Never My Drive root. Never a loose local file. Never above your root.** Everything lands in
   your root's `Projects/` or `Process State/`, owned/co-owned so the team can reach it. Work dumped
   beside your root instead of inside it is the exact mistake this layout fixes.
2. **One subfolder per project.** Deliverables file into a **per-project subfolder** under
   your root's `Projects/`, NOT flat in `Projects/`.
   - **Reuse** the project's existing folder if one exists (search first); **iterate the same doc
     in place** rather than spawning a new doc each turn (`--replace <id>` keeps the link stable).
   - **Create** the subfolder if it doesn't — name it for the project / counterpart / initiative,
     stable across sessions so the next turn re-uses it.
   - **Register it on canopy-web under the SAME name** — `canopy agent project-add --slug <slug>
     --name "<Project>" --drive-folder-url <link>`, then file the work in with
     `canopy agent add --project "<Project>"`. The folder holds the files; the project holds what
     a folder cannot state (what is open, what is parked on a person, whether it is still
     running). One name keeps them pointing at each other — it is the same string
     `canopy gdoc publish --project` resolves the folder from. See `task-tracker.md`.
   - **Put each deliverable on the project's links** once it is shared —
     `canopy agent project-set --slug <slug> --project "<Project>" --append-link "Label|<url>"`
     (MCP: `patch_project` with `links`). The folder holds the file; the project's links are
     what the board shows as the work's output, so a deliverable that is only in Drive is one
     nobody looking at the project can find. A url already there is not duplicated.
3. **Process state goes in `Process State/`.** A recurring job's tracker/registry/run-log is a
   Drive artifact under your root's `Process State/`, so it persists across turns and isn't duplicated.
4. **Share with the requester, then CONFIRM — before you hand over the link.** A raw Drive link
   grants no access; and the agent's `@dimagi-ai.com` mailbox is a *different domain* from a
   `@dimagi.com` recipient, so a doc is a **dead link** to them until explicitly shared. Share the
   subfolder (or at least the doc) with the requester + any named recipients, then verify the
   recipient actually appears in the permission list before sending the link. Broader/link-anyone
   sharing still follows the outbound gate; sharing the deliverable with the human who requested it
   is part of delivery, not a separate favor.
5. **Link, don't paste.** Chat/email carry the doc **link** + a 1–2 line summary — never a wall of
   pasted text, never "it's in a local file."

**ACE keeps its own opportunity locations.** ACE files opportunity artifacts through its own
pipeline into its own working Drive paths — that stays as it is. Its *agentic* storage (ad-hoc
projects, process state) follows this standard under its own root like everyone else.

## How you file (the publishing engines do the layout for you)

The shared engines resolve the layout from your Drive root — you name the project, they
find-or-create the subfolder, file, share, and verify the share landed:

```bash
# run the engines from the plugin's runtime bundle, never the global CLI on PATH (see below)
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }

# a deliverable → <your root>/Projects/<project>/  (find-or-create, reused next turn)
uv run --project "$CANOPY_ROOT" canopy gdoc publish --md <file>.md --name "<Doc title>" --project "<Project>" --share domain

# a durable tracker → <your root>/Process State/
uv run --project "$CANOPY_ROOT" canopy gdoc publish --md <file>.md --name "<Tracker>" --area "Process State"

# iterate in place — same id, same link, same permissions
uv run --project "$CANOPY_ROOT" canopy gdoc publish --md <file>.md --replace <docId>
```

**Why the runtime bundle and not `canopy` on PATH.** The session-start hook keeps
`<plugin>/runtime` at the installed plugin's version on every machine. Nothing updates the
global CLI: it is whatever was last `uv tool install`ed, so on a runner nobody reinstalled on,
it silently lags behind or lacks a subcommand entirely. Below, `canopy gdoc|gsheet` is
shorthand for this `uv run --project "$CANOPY_ROOT" canopy …` form.

**Tabular deliverable? Use `canopy gsheet`, never raw `gog sheets create`.** Same contract,
same flags — one `--tab` per worksheet, as `"Name=path.tsv"` (`.csv` is comma-delimited,
anything else tab-delimited):

```bash
# a roster / grid a human works in → <your root>/Projects/<project>/
uv run --project "$CANOPY_ROOT" canopy gsheet publish --name "<Sheet title>" --project "<Project>" \
  --tab "Targets=roster.tsv" --tab "Clean-up=cleanup.tsv" --share domain
```

- `--project` files into `Projects/<project>`; `--area "Process State"` (optionally with
  `--project`) files a tracker. `--parent <id>` bypasses resolution when you already have the id.
- Emits JSON `{id, url, shared, verified}` — share the `url`; `verified: true` means the share
  landed. Agents that still publish with raw `gog drive upload` pass `--parent <subfolder-id>`
  explicitly (resolve/create the `Projects/<project>` folder under your root first).
- **`gsheet` refuses to create with no destination** — unlike a Doc there is no sensible root
  fallback for a tracker, and the fallback is what produced the incident below.

**A deck? Use `canopy gslides`.** It converts a `.pptx` into a native Google Slides deck
authored as you, filed, shared, and read back (the converted slide count must equal the
pptx's). It needs no claude.ai Google Drive connector, which an org can disable — the
`gslides-export` skill is the whole procedure for a Claude Slides deck (download its .pptx,
then publish). Drive cannot overwrite a native deck in place, so every export is a NEW file
and the link changes; pass `--supersede <previous id>` to trash the old copy once the new
one verifies.

```bash
uv run --project "$CANOPY_ROOT" canopy gslides publish --pptx ~/Downloads/deck.pptx \
  --project "<Project>" --share domain [--supersede <previous deck id>]
```

### Landing on prior work is reported — read it

When the engine REUSES a project folder that already holds files, it says so on stderr and
names them. That note is not decoration: **the layout's whole value is that the next turn
finds the last one's work.** On 2026-08-12 an agent built a trip target roster while a
project folder for that same trip already existed, holding a doc that contradicted the
trip window the roster was built on. Nothing surfaced it, and the deliverable was wrong.

So when you see the note: open what is there before treating the task as new, and prefer
updating the existing artifact in place (`--replace <id>`) over adding a parallel one.

## Why this is enforced, not just written

Per the operating model, hard behavioral rules don't live in prose alone — prose relies on the
model choosing to comply, which fails under load (origin: 2026-07-20, an agent created a brief in
My Drive root and handed the requester a link they couldn't open — the doc was fine, the filing +
share were skipped; 2026-07-23, a fresh session dumped work beside the agent roots instead of
under its agent folder). So the "never My Drive root / never flat at root" invariant is a
**fleet-baseline gating rail** for the tool where the mistake happens:

**Every agent that touches Drive mounts the `gws` channel** in its `config/gating.json`
`channels` list. The baseline rails then cover the whole creation surface:

- **raw `gog`** — any `docs|sheets|slides|forms create`, `drive mkdir`, or `drive upload`
  with no `--parent` (`drive upload --replace <id>` is exempt: it overwrites in place).
- **the engines** — `canopy gdoc|gsheet|gslides publish` with no `--project` / `--area` / `--parent` /
  `--replace`, which would otherwise fall back to your Drive *root*, beside `Projects/`
  rather than inside it.
- **MCP** — any gdrive-server `drive_create_*` / `docs_create*` / `sheets_create` whose
  arguments carry no parent/folder id, matched on tool-name *shape* so it holds under any
  plugin mount.

`--help` is exempt throughout, so a rail can never stop you reading the usage of the command
it just told you to use.

> **Do not mount `gws` selectively.** Until 2026-08-13 the rails were narrower than the tool
> surface — the baseline carried exactly one (`gog drive upload --convert`), agents were told
> they could skip the channel if their helper "always parents", and any extra verbs were
> hand-added per agent. The result: `gog sheets create` was in nobody's list, hal/ada/echo
> mounted no Drive rails at all, and on 2026-08-12 an agent built a 45-row target roster
> straight into its own My Drive root, unshared — a dead link to the human who asked for it.
> Nothing errored. **A denylist of verbs always trails the tool surface**, and "compliant by
> construction" only holds until someone reaches past the helper for a file type the helper
> never covered. Mount the channel; the rails are shaped so a compliant command passes.

The other half of the lesson: **a rail can only say no.** Blocking `gog sheets create` would
have been pure friction while there was no sanctioned way to publish a spreadsheet at all —
which is why `canopy gsheet` shipped in the same change as the rail that forbids the
alternative. If you add a rail, make sure the path it names actually exists.

## Review before you share the link — `canopy gdoc check`

"It published" is not "it looks right". `canopy gdoc publish` and `canopy gdoc email-blocks`
now **review the doc themselves** after writing it: they read its HTML export (the rendered
view) and fail on an emptied body, italic bleed, leaked markdown (`**`, `|---|`, `&amp;`), or
dropped links, and they warn on off-style fonts and sizes. A failure lands in `degraded` and the
command exits non-zero — **do not hand out that link**.

After any OTHER kind of edit (raw `gog docs` verbs, a Docs API batch, an MCP `docs_*` write),
review it yourself before sharing:

```bash
uv run --project "$CANOPY_ROOT" canopy gdoc check <docId> [--expect-links N] [--expect-blocks N]
```

- **Count `--expect-links` from your source** (`grep -oE '\]\(https?://' src.md | wc -l`); a
  guessed number fails a good doc and teaches you to drop the flag.
- **House style** (font / body size) comes from `config/agent.json`
  `"gdoc_style": {"font": "Arial", "body_size": 11}`; without it those checks are skipped.
- Tables are fine now: the create path renders GFM pipe tables as real Doc tables (2026-10-04),
  and `--replace` unwraps soft-wrapped lines before gog converts them, so a `**bold**` span
  that your editor wrapped across two lines no longer ships its asterisks.

**It is enforced on the Stop event, not just written here.** A pass writes a receipt to
`$CANOPY_AGENT_HOME/gdoc-gate/passed/<docId>`, and `agent-core/gdoc_gate.py` refuses to let a
turn end on a link to a doc you wrote and have not reviewed since your last write (at most once
per write; a link you only READ never trips it). Wire it with a thin loader, exactly like
`gating_guard.py`:

```json
"PostToolUse": [{"matcher": "Bash|^mcp__", "hooks": [{"type": "command",
  "command": "python3 \"$CLAUDE_PROJECT_DIR/hooks/gdoc_gate.py\" record"}]}],
"Stop": [{"hooks": [{"type": "command",
  "command": "python3 \"$CLAUDE_PROJECT_DIR/hooks/gdoc_gate.py\" check"}]}]
```

Origin: eva, 2026-08-28 — three docs published, three links handed over, the checker run zero
times with the rule in context. Promoted from eva to the fleet 2026-10-04.

## Editing a doc that already exists — what breaks

Every rule here cost a real deliverable somewhere in the fleet (mostly eva and echo, which
each rediscovered the same `--replace` failures independently). `canopy gdoc check` now
catches most of the damage after the fact; these keep you from causing it.

**`publish --replace`** (same id, same link) runs through gog's markdown converter, not ours:
- **It can empty the doc.** It clears the body and then injects; gog's find-replace has a hard
  ~120s limit, and a timeout in between leaves only `__CANOPY_GDOC_BODY_SENTINEL__`. The old
  version is gone and a retry fails the same way. Keep the source markdown, and fall back to a
  fresh publish (rename the wreck `ZZ SUPERSEDED …`).
- **Never `--replace` a doc whose link is already out.** If it fails, you have handed someone
  a live link to a broken or empty doc. Publish fresh and send the new link.
- **It reuses the old doc's run styling**, which is how one italic line became an all-italic doc.
- **It can flatten lists and wipe headings** (echo, 2026-08-13: all 11 headings gone, every
  paragraph a bullet; eva, 2026-08-17: every list flattened). The engine reports this in
  `degraded` — believe it. For list-heavy docs, publish fresh.
- **It wipes email blocks.** Insert blocks last; to change one, republish and re-insert.
- A fresh publish has a new id, so **re-grant any per-person shares** the old doc had.

**A doc that is not yours → edit it as TRACKED SUGGESTIONS.** When a human asks for changes
"in edit mode" / "with track changes", or the doc belongs to someone else, they mean Suggesting
mode: every change visible and accepted or rejected one by one. A direct write lands silently;
its only trace is version history. Use:

```bash
uv run --project "$CANOPY_ROOT" canopy gdoc suggest <docId> --edits edits.json [--dry-run]
# edits.json: [{"find": "<text that occurs once>", "replace": "<new>"},
#              {"find": "...", "insert_after": "..."}, {"find": "...", "insert_before": "..."},
#              {"find": "...", "delete": true}]
```

Each `find` must match exactly once in the doc as it reads now (text already suggested for
deletion is skipped); everything goes in one batch, and the command fails loudly if anything
went in as a direct edit. `--dry-run` here is honest: it reads the doc and prints the batch.
If it reports the Developer Preview error, the account isn't enrolled. **Do not fall back to
direct edits on someone else's doc** — ask. (eva, 2026-10-02: eight direct edits to a
teammate's concept note, asked for as suggestions, undone by hand.)

**Raw `gog docs` edits:**
- **No `--dry-run`.** On gog v0.12.0 the flag performed the write, so a dry-run-then-run pair
  applied it twice. It is a fleet deny rail (gws channel).
- **Capture before you edit** (`gog docs cat <id> > "$WD/before.txt"`), take indices from
  `gog docs structure <id> --json`, write **once**, then re-read and `canopy gdoc check`.
- **Scratch files go in `WD=$(mktemp -d)`, never a fixed `/tmp/<name>`.** Agents share `/tmp`;
  a fixed path can silently hand you a sibling session's structure dump for the same doc, and a
  stale index writes to the wrong offset in a live doc (eva, 2026-09-09).
- **`find-replace` drops hyperlinks** on the text it rewrites. Check the span afterwards and
  re-link with `gog docs format <id> --match "<unique phrase>" --link <url>` (`--match` styles
  only the first hit, so make the phrase unique).
- **For anything multi-step or index-based, send ONE Docs API batch as yourself** — it runs
  exactly once and is atomic:
  `gog api call docs v1 documents.batchUpdate --params '{"documentId":"<id>"}' --body
  '{"requests":[…]}' --allow-write --force -a <acct> --client <client> --json`.
- **`gog docs export` writes a FILE and prints its path** — it does not stream the doc. Read the
  file you named with `--out`; grepping the command's stdout reads metadata and "finds" zero
  headings in a perfectly good doc.
- **A version that lacks a verb is usually just old.** Check `gog --version` and
  `gog docs --help` before designing around a gap (eva ran gog 0.12 against an upstream 0.38).

**"Permission denied" from a service-account Drive MCP means the wrong identity, not missing
access.** gog runs as you; a gdrive MCP (chrome-sales, ace-gdrive) runs as a shared service
account that starts with access to nothing. Do not share a doc with the service account to get
past the error — on 2026-08-28 that put a writer grant on an externally-shared funder doc to
change four margins that `gog docs page-layout` could do as the agent. Use gog.

## Sharing with people outside the agent's domain

Agent mailboxes are on `@dimagi-ai.com`; the team is on `@dimagi.com`. `--share domain` shares
with the AGENT's domain, so to a `@dimagi.com` reader the link is dead until you share with
them by address:

```bash
gog drive share <id> --to user --email "<person>" --role writer -a <acct> --client "$(canopy email client --repo .)"
gog drive permissions <id> -a <acct> --client <client>    # confirm it landed
```

- **Groups** (e.g. a team list) work as a share target, and they do NOT inherit folder access,
  so add them to the doc explicitly.
- **Resolve the address from your roster/records, never type it from memory**, and assert it is
  non-empty before you use it.
- Sharing with **the person who asked** is part of delivery. Wider or link-anyone sharing, and
  **sending the link to anyone**, is outbound and follows your agent's approval gate.

## What each agent's `gdoc-writer` stub declares

- **Your Drive root** — the id of your own agent folder. It is **environment-specific, so it lives
  in your agent's 1Password vault**, never in git: `op://Agent-<Slug>/gdrive-root-folder/credential`
  (use the angle-bracket placeholder in any doc/comment — a literal `op://…` gets resolved too),
  referenced from your `.env.tpl` (the standard) — or the legacy `config/secrets.yaml` — and
  resolved into `~/.<agent>/.env` as `$GDRIVE_ROOT_FOLDER` via `op inject` (or `canopy provision`
  for the legacy path). Everything files beneath it via `Projects/` + `Process State/`. You never
  declare, or reason about, what that root sits inside. See `agent-core/agent-runtime.md`.

## Related

- `turn.md` — the turn procedure; its reply-quality rules already say deliverables are gdocs, not
  local files. This doc is the *filing* standard behind that.
- your agent's `gdoc-writer` — the thin per-agent stub that implements this.
- `email-drafts.md` — when the deliverable is emails a person will send: email blocks with the Gmail icon.
