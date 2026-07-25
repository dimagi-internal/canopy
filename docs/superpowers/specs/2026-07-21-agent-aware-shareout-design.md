# Agent-aware shareout ("on behalf of the user")

> **Status: shipped (PR #346) — historical record, not current-state.**

**Date:** 2026-07-21
**Status:** approved (design), pending implementation
**Repos:** `canopy` (CLI + skill), `canopy-web` (storage + display)

## Problem

An agent (Eva, Ada, Hal, …) can already run `canopy:shareout` for its human — it
gathers the human's Claude Code sessions + merged PRs and posts the briefing to
the `/shareouts` feed using the human's PAT. That is correct: the report is
*about the human's work*, so it should stay attributed to the human, and the
human's PAT is the right credential.

What's missing is that the post is **indistinguishable from the human running it
by hand** — there's no record that an agent assembled it. We want the report to
remain the human's, but to *transparently record which agent produced it*, and to
establish a small reusable "an agent uploaded this on behalf of the user" pattern
that later canopy-web uploads can copy.

Explicitly **not** wanted: attributing the report to the agent, giving the agent
its own canopy-web identity/token, or building a general "agents upload anything
on behalf of users" framework. (YAGNI — one field, one feature, now.)

## Semantics (the fixed principle)

| Concept | Value | Meaning |
|---|---|---|
| `author` | the human (e.g. `jjackson`) | whose work the report is *about* — unchanged |
| auth / PAT | the human's | correct, because it's the human's work |
| `produced_by_agent` (new) | agent slug (e.g. `eva`) or empty | who *assembled* it — optional, subtle |

A shareout with no `produced_by_agent` looks exactly as it does today (a human
posted it). One with it set renders a quiet "produced by Eva" byline.

## Changes

### 1. canopy-web — `apps/shareouts/` (storage + display)

- **Model** (`models.py`): add `produced_by_agent = CharField(max_length=80,
  blank=True, default="")`. `author` unchanged. Migration `0006`.
- **Schemas** (`schemas.py`): `ShareoutIn` and `ShareoutOut` gain
  `produced_by_agent: str = Field(default="", max_length=80)`.
- **Services** (`services.py`): `upsert_shareouts` passes it into
  `Shareout.objects.create(...)`; `list_shareouts` includes it in the row dict.
  It is **NOT** part of the idempotency group — the dedup key stays
  `(workspace, project, period_start, period_end, source)`, so the producer just
  rides along and a re-post from the same source still replaces cleanly.
- **Frontend** (`frontend/src/api/shareouts.ts`, `pages/ShareoutsPage.tsx`): add
  `produced_by_agent` to the `Shareout` type; when present, render a subtle
  "· produced by <slug>" next to the author/period line, linked to
  `/agents/<slug>`. Absent → no change to the current rendering.
- **Test** (`apps/shareouts/tests/test_api.py`): `produced_by_agent` round-trips
  through POST → GET, and two posts that differ ONLY in `produced_by_agent`
  (same period + source) still dedupe to one row (replace, not append).

### 2. canopy — `src/orchestrator/` (the agent-aware pipe)

- **`shareout.py`**:
  - `build_post_payload(authoring, source, produced_by_agent="")` — thread the
    slug onto every `_item(...)`. **Omit the key entirely when empty** (don't send
    `produced_by_agent: ""`): canopy-web's `ShareoutIn` is a `StrictModel`, so a
    human run stays compatible with a not-yet-deployed canopy-web, and only an
    agent run (non-empty slug) requires the new field.
  - New pure helper `detect_agent_slug(cwd, env)` with precedence:
    explicit arg (handled in cli) → `$CANOPY_AGENT_SLUG` → a `config/agent.json`
    with a `slug`/`name` in `cwd` → `""`. Unit-testable, no I/O beyond reading
    the one JSON file.
- **`cli.py`** (`shareout post`): add `--produced-by-agent <slug>`. Resolution:
  the flag if given, else `detect_agent_slug(...)`. Pass into
  `build_post_payload`. Echo "produced by <slug>" in the success line when set.
- **Skill** (`plugins/canopy/skills/shareout/SKILL.md`): add a short **"Running
  on behalf of the user (agent-aware)"** section. Because the CLI is normally
  invoked from the canopy repo dir (not the agent's repo), auto-detect from
  `cwd` is unreliable — so the skill instructs a running agent to **pass
  `--produced-by-agent <its own slug>`** (read from its repo's
  `config/agent.json`). State the reusable rule plainly: *author is the subject,
  producer is the agent, auth is the subject's PAT.*
- **Test** (`tests/test_shareout.py`): `build_post_payload` carries
  `produced_by_agent` onto every item; `detect_agent_slug` honors the precedence
  (env over file, explicit over both, empty default).

## Rollout

Two PRs, each additive and independently shippable:
1. **canopy-web**: field + migration + schema + service + frontend + test. Deploy
   migrates the new nullable/blank column (no backfill needed).
2. **canopy**: CLI flag + `detect_agent_slug` + skill doc + test. Depends on (1)
   being deployed only for the byline to *display*; posting the field is
   forward-compatible (canopy-web ignores unknown fields? — no: StrictModel, so
   canopy-web PR must land/deploy first). **Order: ship + deploy canopy-web
   first, then canopy.**

## Out of scope

- Eva-specific wrapper skill (rejected earlier as overkill).
- Agent-owned canopy-web identity / token (the report stays the human's).
- General "agents upload on behalf of users" framework beyond this one field.
