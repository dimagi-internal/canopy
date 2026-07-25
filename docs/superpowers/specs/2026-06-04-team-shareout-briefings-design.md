# Team Shareout Briefings — Design

> **Status: shipped — `canopy shareout` + /shareouts feed live. Historical record, not current-state.**

**Date:** 2026-06-04
**Status:** Approved (design), building.

## Problem

The author is shipping fast across many projects and can't keep teammates current
on *what* changed and *why* it matters. Need a low-effort way to turn "what I did
over a date range" (default: yesterday) into a teammate-facing briefing — per
project, with the reasoning ("why") and "how you can leverage this" — and publish it
somewhere browsable.

## Solution overview

A `canopy:shareout` skill (CLI `canopy shareout`) that, for a date range:

1. **Gathers** all Claude Code sessions + the author's GitHub PRs/commits in the
   window, grouped by project (repo).
2. **Synthesizes** (agent-authored) one briefing per active project + a short
   cross-project roll-up, framed for teammates: what changed, why, how to leverage.
3. **Posts** to a new **`/shareouts`** feed on canopy-web, navigable by date.

Two sub-projects across two repos:

- **Part A — canopy-web**: the `/shareouts` feed (Django Ninja API + React page).
- **Part B — canopy**: the `shareout` skill + CLI (deterministic gather + post;
  agent does synthesis).

Build/run order: A (deploy first, feed must exist) → B (ship via PR + `/canopy:update`)
→ run for yesterday and publish.

---

## Part A — canopy-web `/shareouts` feed

canopy-web is Django 5 + Django Ninja (RFC 7807 problem+json, `apps/<app>/api.py`
routers registered in `apps/api/api.py`) + React 19 / React Router 7 / Vite, typed
off the generated OpenAPI schema (`frontend/src/api/generated.ts`). Auth: a Bearer
PAT is resolved to a real user by `apps.tokens.middleware.BearerTokenAuthMiddleware`,
which then satisfies `LoginRequiredMiddleware` — so a write endpoint needs nothing
special beyond `auth=session_auth`; a valid workbench PAT authenticates it.

### New Django app `apps/shareouts`

Mirrors `apps/walkthroughs` / `apps/runs` (own app, own migrations).

**Model `Shareout`:**

| field | type | notes |
|-------|------|-------|
| `project` | FK(Project, null=True, CASCADE, related_name="shareouts") | **null = cross-project roll-up** |
| `period_start` | DateField | inclusive |
| `period_end` | DateField | inclusive (== start for a single day) |
| `title` | CharField(200) | |
| `summary` | TextField | TL;DR shown on the feed card |
| `content` | TextField | full markdown body |
| `links` | JSONField(default=list) | `[{label, url}]` — PRs, commits |
| `author` | CharField(100, blank) | |
| `source` | CharField(100) | e.g. `canopy:shareout@<iso>` |
| `created_at` | DateTimeField(auto_now_add) | |

`Meta.ordering = ["-period_end", "-created_at"]`. Index on
`(period_start, period_end)`.

### API (`apps/shareouts/api.py`, router mounted at `/shareouts`)

- `POST /api/shareouts/` — **batch + idempotent**. Body `{ "shareouts": [ShareoutIn...] }`.
  Each item carries an optional `project_slug` (null/omitted → roll-up). On write, for
  each `(project, period_start, period_end, source)` group present in the batch, delete
  pre-existing rows in that group first, then create — so re-running the same day from
  the same source replaces rather than duplicates. Unknown project_slug → that item is
  skipped and counted in `skipped`. Returns `{created, replaced, skipped}`.
- `GET /api/shareouts/` — list, filters `?from=&to=&project=&limit=` (newest period
  first). `openapi_extra={"x-mcp-expose": True}` on both so they're available as MCP
  tools automatically (same mechanism as insights/slugs).

Schemas (`apps/shareouts/schemas.py`, `StrictModel` base): `ShareoutIn`
(project_slug optional, period_start/period_end as date, title, summary, content,
links, author, source), `ShareoutOut` (+ id, project_slug, project_name, created_at),
`ShareoutBatchIn` (`shareouts: list[ShareoutIn]`), `ShareoutBatchOut`
(`created/replaced/skipped: int`). Service helpers in `apps/shareouts/services.py`:
`upsert_shareouts(items)`, `list_shareouts(...)`.

### Frontend

- `frontend/src/api/shareouts.ts` — `shareoutsApi.list(params)` via `apiV2.GET`.
- `frontend/src/pages/ShareoutsPage.tsx` — group rows by `period_start..period_end`;
  per period render the roll-up (project null) pinned on top, then a card per project
  with rendered markdown (reuse the markdown renderer used by GuidePage), `summary` as
  the card lead, and `links` as PR chips.
- `frontend/src/router.tsx` — add `{ path: '/shareouts', element: <ShareoutsPage /> }`.
- `AppLayout.tsx` — add a "Shareouts" nav link.
- Regenerate `generated.ts` from the live OpenAPI (`npm run gen:api`) after the
  backend is up so the page is type-checked against real schemas.

### Tests (pytest)

`tests/test_shareouts.py`: model defaults; `POST` create; `POST` idempotent replace
(same group twice → one row, `replaced` counted); roll-up row (null project) round-trips;
`GET` date + project filters; unknown slug skipped; auth (no PAT → 401).

### Ship

Branch in `~/emdash-projects/canopy-web`, PR, merge, `./deploy.sh`, then
`gcloud run jobs execute canopy-web-migrate` to apply the migration.

---

## Part B — canopy `shareout` skill + CLI

Deterministic Python does IO; the agent writes the prose.

### `src/orchestrator/shareout.py`

- `resolve_range(from_date, to_date, days, today)` → `(date, date)`. Default (all
  None) = yesterday (single day). `--days N` = last N days ending yesterday.
- `gather(projects_dir, repo_map, labels, start, end, author, project_filter)` →
  `{ "period": {...}, "projects": { repo: { sessions: [...], prs: [...], commits: [...] } } }`:
  - Sessions: `scan_all_transcripts()` filtered by date overlap on `first_ts`/`last_ts`,
    grouped by resolved `repo`. Per session: prompts (`extract_user_messages`),
    assistant summary text, notable tool calls. Caps to keep the corpus bounded.
  - PRs: `gh pr list --author @me --json ...` per repo, filtered to the window
    (created/merged within range). Title, number, url, body, state.
  - Commits: `git log --author=<me> --since/--until` per local repo if resolvable
    (best-effort; skipped when the repo isn't checked out locally).
- `build_post_payload(corpus, briefings)` → the `ShareoutBatchIn` body.
- `post(payload, api_url, token)` → POST to canopy-web (urllib, Bearer token from
  `~/.claude/canopy/workbench-token` or `CANOPY_WEB_PAT`), mirrors
  `scripts/walkthrough-share/upload.py`. Returns the `/shareouts` URL.

### CLI (`src/orchestrator/cli.py`)

`canopy shareout` group:
- `gather --from --to --days --project [--json-out PATH]` → prints/writes the corpus JSON.
- `post <briefings.json> [--api-url]` → posts; prints the feed URL.

### Skill `plugins/canopy/skills/shareout/SKILL.md`

Autonomous by default (per "just works"): run `gather` → agent reads the corpus and
writes per-project briefings + roll-up (concise, teammate-facing: *what / why / how
you can leverage*; honest, no marketing fluff) into the batch JSON → `post` →
print the `/shareouts` URL. Document `--from/--to/--days/--project`.

### Tests (pytest)

`tests/test_shareout.py`: `resolve_range` (yesterday default, explicit range, days);
`gather` date filtering + repo grouping against synthetic transcripts;
`build_post_payload` shape (roll-up has null project_slug). gh/network mocked.

### Ship

Version bump (`uv run canopy version bump`), PR, merge, `/canopy:update`, `/reload-plugins`.

---

## Decisions / non-goals (v1)

- **Synthesis lives in the skill (agent), not a `claude -p` in Python** — best-quality
  "why", no double model cost. A headless `synthesize` subcommand can come later for cron.
- **Idempotent replace, not destructive clear** — briefings are a dated log; re-running a
  day cleanly replaces that day's rows from this source.
- **Author scoping**: PRs via `gh --author @me`; sessions are all local sessions in range
  (already the author's). Commits best-effort by author.
- **No file uploads** — content is markdown + link JSON, so no blob storage needed.
- Not building per-date detail routes, email/Slack delivery, or scheduling in v1.
