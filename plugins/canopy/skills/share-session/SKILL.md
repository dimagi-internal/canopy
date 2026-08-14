---
name: share-session
description: |
  Share a Claude Code session as a pretty, read-only web page on canopy-web.
  Asks whether to share THIS session or find a DIFFERENT one (the common case —
  you're usually running this from a live session to share a past one), then
  best-effort-scrubs secrets and returns a link-by-default /share/<token> URL
  anyone can open (no dimagi login). The generic counterpart to ACE's
  upload-transcript. Use when asked to "share this session", "share that
  session from earlier", "make a link for a chat", or "share-session".
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])" 2>/dev/null)"
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
```

If output shows `UPGRADE_AVAILABLE <old> <new>`, mention it once and continue.

# Share Session

Uploads a Claude Code session to canopy-web, which renders it as a chat-style
view, scrubs obvious secrets, and (by default) mints an anyone-with-link share
URL rendered at `/share/<token>`. The generic version of
`ace:upload-transcript` — not scoped to any opportunity, just "share what I (or
some earlier session) did".

**By default the uploader reduces the transcript client-side BEFORE upload** to
just the conversation — what the human typed plus Claude's final reply per turn.
Tool calls, tool results, and intermediate steps are dropped on your machine and
never leave it (smaller, far more readable, and tool output that may carry
sensitive data isn't transmitted). Pass `--full` to upload the raw transcript.

## Step 1 — which session?

**Unless the user already named a specific session or transcript path**, ask
with the `AskUserQuestion` tool before doing anything:

- Question: "Which session do you want to share?"
- Option A — **"A different session"** *(usually the right one — you're running
  this from a live session to share a past one)*: go to Step 2B.
- Option B — **"This session"** (the one you're typing in right now): go to
  Step 2A.

List "A different session" **first** — it's the common case.

If the user already pointed at a session (a repo name, "the one from this
morning", or a `.jsonl` path), skip the question and go straight to 2B (or pass
the path to the uploader directly).

## Resolve the helpers (run in the same shell as the steps below)

`upload.py` lives in the canopy runtime (resolved via `scripts/canopy-runtime.sh`);
`find_session.py` (reused from `canopy:find-session`) ships in the installed
plugin and lists candidate sessions across all projects.

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
UPLOAD="$CANOPY_ROOT/scripts/share-session/upload.py"
FIND="$_CANOPY_PLUGIN/skills/find-session/scripts/find_session.py"
```

## Step 2A — share THIS session

Auto-discovers the newest transcript for the current working directory (the live
session you're in):

```bash
# Link-by-default — anyone with the URL can view.
uv run --project "$CANOPY_ROOT" python "$UPLOAD" --title "<short title>"

# Or private — see the warning below. NOT "dimagi-only": private is OWNER-only,
# and an agent owns its own uploads, so nobody else can open it.
uv run --project "$CANOPY_ROOT" python "$UPLOAD" --private --title "<short title>"
```

## Step 2B — find a DIFFERENT session

1. List candidates (newest first, across every project, last ~2 weeks). Pass a
   repo-slug substring as the first arg to narrow to one project when the user
   named one (e.g. `"$FIND" connect-labs --json …`):

   ```bash
   python3 "$FIND" --json --hours 336 --top 0 2>/dev/null
   ```

   Each entry has `transcript` (the `.jsonl` path to upload), `cwd`, `branch`,
   `age_minutes`, and `prompts` (recent human prompts). The current session is
   excluded automatically.

2. **Present the choices to the user.** Show a concise list — for each
   candidate: the project (cwd basename) + branch, how long ago, and the latest
   human prompt as a one-line preview. Use `AskUserQuestion` when there are ≤4
   good matches; otherwise show a short numbered markdown list (top ~8) and ask
   which number, or to name a project to filter and re-run step 1.

3. Upload the chosen candidate's `transcript`. Derive a readable `--title` from
   its branch or first human prompt, and `--project` from its cwd basename:

   ```bash
   uv run --project "$CANOPY_ROOT" python "$UPLOAD" "<chosen transcript path>" \
     --title "<branch or first-prompt summary>" \
     --project "<cwd basename>"
   ```

## Output

The uploader prints:

```
using transcript: <path>                        # stderr — only on auto-discovery
reduced to <N> conversation turn(s) …           # stderr — unless --full
uploading <N> KB to <api>…                      # stderr
Share: <api>/share/<token>                      # stdout — the link to hand out
<N> messages · <N> secrets redacted (best-effort)   # stderr
```

Pass the `Share:` line back to the user verbatim.

## Required state

- **PAT** at `~/.claude/canopy/workbench-token` (or `CANOPY_WEB_PAT`). Mint with
  `/canopy:canopy-web-pat-mint` — gh-style loopback flow, one click. The PAT
  identifies the caller; shared sessions are owned by whoever minted it.
- **Canopy-web reachability**: defaults to the production deploy. Override with
  `CANOPY_WEB_API_URL` (e.g. `http://localhost:8000` for local dev).

## upload.py argument map

| Flag | Required | Notes |
|---|---|---|
| `<path>` (positional) | no | A transcript `.jsonl`. Default (Step 2A): newest for the current dir's session. In Step 2B, pass the chosen candidate's `transcript`. |
| `--title <str>` | no | Defaults to the transcript filename stem. Max 500 chars server-side. |
| `--project <slug>` | no | Groups the session under a project in the `/sessions` feed. Defaults to the current directory name (set it explicitly in 2B to the shared session's project). |
| `--private` | no | Upload **owner-only** — no link, no page for anyone else. NOT "dimagi-only". Default is link-by-default; see the warning below before using this. |
| `--full` | no | Upload the raw transcript (all tool calls). Default reduces to the conversation (prompts + final replies) client-side; tool output never leaves the machine. |
| `--api-url <url>` | no | Override canopy-web base URL (also via `CANOPY_WEB_API_URL`). |
| `--arc` | no | Stitch MULTIPLE transcripts into one shared **arc** page (see below). |

## Arc mode — share a whole build, not just one session

When a feature was built across several sessions (often across machine
accounts), share the **arc** — all of them, in order, on one page:

```bash
uv run --project "$CANOPY_ROOT" python "$UPLOAD" --arc \
  /path/to/session-1.jsonl /path/to/session-2.jsonl /path/to/session-3.jsonl \
  --title "Campaign tool build" --project connect-labs
```

Each transcript is reduced to its turn-synthesis and uploaded as a **private**
member session (no per-session public link); then they're stitched into one
arc that carries the single `/share/<token>` link the command prints. Section
headings default to each session's first human prompt. Order is the order you
pass the paths — list them oldest-first to read as the build unfolded.

To gather the member transcripts for an initiative across users first, use
`uv run --project "$CANOPY_ROOT" canopy harvest map <initiative> --match <terms>`
(same resolution lines as above; it lists every matching session path,
cross-user, oldest-first), then pass those paths to `--arc`.

## Secret scrubbing (best-effort, not a guarantee)

On upload the server scrubs every turn: provider API keys (`sk-…`, `ghp_…`,
AWS/Google/Slack tokens), JWTs, PEM private-key blocks, `Authorization: Bearer …`
headers, and sensitive `KEY=value` assignments → `‹redacted:…›`. The printed
count says how many fired.

This catches the common foot-guns but is **not** a guarantee — unusual formats
can slip through. Treat a `link` URL like any other secret-link share
(unguessable but unlisted). If a transcript is too sensitive for that, **don't
upload it** — `--private` is not a safer way to share it, it is a way to not
share it (below).

## `--private` does not share anything — read this before using it

**`--private` is owner-only, and when an agent runs this command the owner is the
AGENT.** Its PAT resolves from the repo it is standing in, so an agent's upload is
owned by e.g. `echo@dimagi-ai.com`, and a private session is readable by its owner
and nobody else — not the human who asked for it, not any other dimagi login.
A private **arc** is worse still: canopy-web has no arcs UI at all, so `/share/<token>`
is the only arc surface there is and a private arc has no token.

So: **if a human asked you to share a session, do not pass `--private`.** The
link default is what makes the artifact readable. Reach for `--private` only when
the intent is genuinely "upload it to my own account and show nobody" — and say
so plainly, because the command will otherwise report a success that produced
something the requester cannot open. (dimagi-internal/canopy#484: a private share
was handed over as a link, rendered a black screen, and appeared on nobody's
`/sessions` page.)

## Managing shared sessions

Owners manage their sessions at `<api>/sessions` (their own login — the list shows
everyone's link-shared sessions plus **your own** private ones): copy the link,
**Rotate** (invalidate the current link, mint a new one), **Make private**
(revoke sharing), or **Delete**. Re-running the skill on the same session is
idempotent — it returns the existing link instead of duplicating.

## First-time setup

If `~/.claude/canopy/workbench-token` doesn't exist (or the token is expired):

```
/canopy:canopy-web-pat-mint
```

That opens canopy-web's authorize page (signing you in via Google if needed),
captures the minted PAT on a one-shot localhost listener, and writes it to
`~/.claude/canopy/workbench-token` (chmod 600).

## Error handling

The uploader exits non-zero with a one-line `error: …` on stderr:

- Missing PAT → run `/canopy:canopy-web-pat-mint`.
- HTTP 401 → PAT expired/revoked. Re-mint via the slash command.
- HTTP 413 → transcript exceeds the server cap (50 MB). Unusual for a session.
- "no Claude session log dir" (Step 2A) → run from the project where the session
  ran, or pick a different session (Step 2B), or pass the transcript path.
