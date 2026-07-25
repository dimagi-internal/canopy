# Agent turn packaging — end-of-turn "unit of work" record with an optional transcript link

> **Status: shipped — `canopy agent turn` + `session_upload.py` live. Historical record, not current-state.**

Status: **design / awaiting approval** (2026-07-02)
Scope: canopy (framework: client + CLI + turn-skill template) + canopy-web (agents/sessions) + echo (backfill)

## Problem

Today an agent's turn ends with a prose close-out to the human. Nothing durable
captures *what unit of work the turn advanced* in a machine-navigable form, and
the only way to share the session is `canopy:share-session` — which mints a loose
`/share/<token>` tied to a User + a free-text `project_slug`, with **no link to
the agent or to the request/task it worked**. That is the "one-off session share"
we explicitly do not want.

The ask (reframed by the maintainer): the end-of-turn step should package a
**clear unit of work — a request/task** — as a first-class part of the agentic
framework and the agent web view (`/agents/<slug>`); uploading the turn's
transcript is an **optional** artifact of that package, not the point of it.

## What a "Turn" is

A `Turn` record binds together, for one turn of work:

- **the request** — the board task(s) (`AgentTask.ext_id`) the turn advanced, or a
  new ad-hoc task if the turn wasn't board-driven;
- **what the agent did** — the close-out summary;
- **the deliverables** — the work-products produced (by url);
- **the transcript** *(optional)* — the reduced session as a `/share/<token>` link.

This is the alignment constraint made concrete: on `/agents/<slug>` the chain
reads *request → the turn that worked it → deliverables + (optional) transcript*.

## Current state (verified 2026-07-02)

**canopy-web** (`/Users/acedimagi/emdash-projects/canopy-web`, Django + django-ninja):

- `apps/agents/models.py`: `Agent` (slug is the `/agents/<slug>` key) with child
  rows `AgentSync` (FK agent, idempotent per period+source, doc_url + self_grades),
  `AgentWorkProduct` (FK agent, unique per (agent,url), kind/title/url/tags),
  `AgentSkill`, `AgentTask` (FK agent, `ext_id` unique per agent — the board card:
  title/next_action/status/rationale/source_url/plan/links), `AgentTaskCommand`.
  **No turn/transcript/session model or FK exists.**
- `apps/agents/api.py`: routes for register / syncs / work-products / skills /
  tasks(+sync) / commands. `agent_detail` (`services.py`) returns the section
  counts that drive the left-nav badges.
- `apps/sessions/models.py`: `Session` (`shared_sessions`) FKs `owner`
  (User, `on_delete=PROTECT`, required) + free-text indexed `project_slug`;
  `Message` rows; `ShareToken` → `/share/<token>`; `SessionArc`/`SessionArcItem`
  (the existing "group sessions into one shared page" pattern). **A `Session` is
  NOT linked to any `Agent`.**
- `apps/sessions/api.py`: `POST /api/sessions/upload` (multipart; owner =
  `request.user` from the PAT; mints a `ShareToken` when `visibility=link`;
  returns `{slug, share_token, message_count, redaction_count, duplicate}`),
  and the public `GET /api/share/{token}` (auth=None) rendering via `MessageList`.
- Frontend: `/agents/:slug` (`AgentWorkspacePage` + `AgentLeftNav` + child routes
  `needs-you|overview|tasks|syncs|work-products|skills`). A new section is one more
  child route + a nav item + a `AgentTurnsSection` (clone of `AgentSyncsSection`).

**canopy** (framework):

- `src/orchestrator/agent_client.py` — `AgentClient` (register / post_sync /
  put_work_products / put_skills / sync_tasks / tasks / commands / apply / patch_task).
- `src/orchestrator/agent_cli.py` — `canopy agent {register|sync|work|skills|
  tasks-sync|tasks|commands|apply|set}`.
- `scripts/share-session/upload.py` — reduces a raw `.jsonl` to conversation-only
  via `orchestrator.turn_synthesis` (drops tool_use/tool_result/sidechain on the
  machine), uploads multipart to `/api/sessions/upload`, prints `/share/<token>`.
- `src/orchestrator/agent_factory.py` `_TURN_SKILL` — the operating-model turn
  template (Step 4 = Close the turn: `canopy agent skills`, optional `canopy agent work`).
- `docs/architecture/agent-client-rest-contract.md` — the operator-plane contract;
  explicitly notes it "carries no run/step/artifact/verdict surface (that is a
  separate wave)." **This design opens that wave, scoped to turns.**

**echo** (`~/emdash/worktrees/echo/emdash/turn-u37ts`, repo `dimagi-internal/echo`):
its own forked `skills/turn/SKILL.md` Step 5 = Close the turn. Must be backfilled.

## Rejected alternatives

| Path | Why rejected |
|------|--------------|
| `canopy:share-session` (loose `/share/<token>`) | No agent link, no task link — the "one-off" the maintainer explicitly rejected. |
| `AgentWorkProduct` with `kind="transcript"`, `tags=[ext_id]` | Zero web change and ships fast, but the request↔turn binding is only a *tag*; renders as a generic deliverable, not "the turn that worked request N." Fakes the alignment instead of modeling it. |

## Recommended design — first-class `AgentTurn`, transcript-optional

Mirrors the `AgentSync` pattern (FK to `Agent`, idempotent, one API verb).

### canopy-web
- New `AgentTurn` model (`apps/agents/models.py`): `agent` FK
  (`related_name="turns"`); `title`; `summary`; `task_ext_ids` (JSON list — the
  requests advanced; matches how `AgentTask` already keys by `ext_id`, so no
  Session↔Task migration coupling); `work_product_urls` (JSON list, optional);
  `session_slug` + `share_token` (optional — the transcript link, null when the
  turn is packaged without upload); `period_start`/`period_end`; `source`;
  `created_at`. Idempotent by `(agent, cli_session_id)` — one turn per Claude
  session; re-running updates it.
- `POST /api/agents/{slug}/turns/` (idempotent, like `create_sync`) and
  `GET /api/agents/{slug}/turns/`.
- Add `turn_count` + `latest_turn_at` to `AgentDetailOut` / `agent_detail`.
- Session upload stays unchanged: the human's PAT owns the `Session`
  (satisfies `owner=PROTECT`); the `AgentTurn` carries the agent + task binding.
- Frontend: a **Turns** nav item (`AgentLeftNav`) + child route + `AgentTurnsSection`
  (clone `AgentSyncsSection`) rendering *request → summary → deliverables* cards
  that deep-link the transcript to the existing public `/share/<token>` view.

### canopy
- `agent_client.py`: `post_turn(...)` → `POST /api/agents/{slug}/turns/`.
- `canopy agent turn` CLI: one shot — package the turn; with `--upload`, reduce
  the transcript (reuse `scripts/share-session/upload.py`'s `turn_synthesis`
  reducer, conversation-only) → `POST /api/sessions/upload` → then `post_turn`
  with `task_ext_ids` + `share_token`. Without `--upload`, package the unit of
  work with no transcript.
- Update `docs/architecture/agent-client-rest-contract.md` with `/turns/`.

### turn skills (the "add to the end of turns" ask)
- `_TURN_SKILL` template Step 4 (Close) and echo's turn Step 5 gain an
  **optional, approval-gated** step: *"Package this turn? (records the request +
  what I did + deliverables; optionally uploads the reduced transcript and links
  it to task(s) N)."* On yes → `canopy agent turn …` → put the `/agents/<slug>`
  (or `/share/<token>`) link in the close-out response. Publishing = outbound, so
  it rides the same human-approval gate the operating model already enforces;
  transcript is reduced (conversation-only) by default.

## Open decision (recommended default in **bold**)

- **Model shape:** **`AgentTurn`, transcript-optional** — a turn record (request +
  summary + deliverables) is always packageable at close-out; the transcript link
  is the opt-in extra. This is the strongest fit for the maintainer's own words
  ("optional upload" + "align with a request/task/unit of work"). Alternative:
  the same `AgentTurn` but always paired with a transcript (drop optionality); or
  the rejected work-product tag.

## Phasing

1. **canopy-web:** `AgentTurn` model + migration + `POST/GET /turns/` + `turn_count`.
2. **canopy:** `agent_client.post_turn` + `canopy agent turn` CLI + REST-contract doc.
3. **canopy:** factory `_TURN_SKILL` step + echo turn-skill Step 5 backfill (usable end-to-end).
4. **canopy-web frontend:** Turns nav item + `AgentTurnsSection`.

Each phase ships behind its repo's normal PR-then-merge flow; canopy-web changes
require the manual deploy (`workflow_dispatch`) — see memory `canopy-web-deploy-manual`.

## Provenance

Surfaced during a `canopy agent-review` of echo (2026-07-02). The review's own
findings #1/#2 were already shipped (echo PR #46); finding #3 (`echo_gdoc.py`
urllib3 warning masking real errors) was fixed + merged as echo PR #47 (v0.1.6).
This turn-packaging feature is the follow-on the maintainer requested.
