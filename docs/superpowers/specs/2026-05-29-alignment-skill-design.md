# `canopy:alignment` — cross-system drift sweep

> **Status: shipped — `canopy:alignment` live. Historical record, not current-state.**

**Date:** 2026-05-29
**Status:** Design approved, pending spec review

## Problem

The portfolio contains paired systems that evolve in parallel: the `ace` and
`canopy` Claude Code plugins, and the `ace-web` and `canopy-web` web apps. When
a capability, convention, or utility lands in one side, the sibling often lags —
either missing the feature entirely or solving the same problem a different way.
Even when both approaches work, the divergence is a tax: it costs context to
hold two mental models, and it hides opportunities to standardize.

There is no routine that asks, across two sibling systems: *what did one build
that the other should bring over or reconcile?*

## Goal

A read-only canopy skill, `canopy:alignment <projectA> <projectB>`, that compares
two passed-in projects across four axes, reasons case-by-case about which side
(if any) should be the reference, and surfaces ranked, actionable findings as
cards on the existing canopy-web `/insights` feed.

Report-only: it never writes to either repo and opens no PRs.

## Scope decisions (from brainstorming)

| Decision | Choice |
|---|---|
| How far it goes | **Report only** — no repo writes, no proposals store, no PRs |
| What's compared | The **two projects passed as arguments**; no whole-portfolio sweep, no stored config |
| Comparison axes | **All four**: features/capabilities, patterns/conventions, shared code/utilities, docs/UX surface |
| Recency | Used only to **rank** findings, not to choose what's scanned |
| Directionality | **Reasoned case-by-case** — each finding names a reference + lagging side with reasoning, or explicitly "no clear winner — reconcile". Not a fixed rule. |
| Output | **canopy-web `/insights` feed**, `source=canopy:alignment`, new **`[alignment]`** category |
| Architecture | Skill orchestrates; **one comparison subagent** does the heavy repo reading + reasoning (keeps the main context clean) |
| No args | **Ask the user** which two repos to align |

## Invocation

```
/canopy:alignment ace canopy
/canopy:alignment ace-web canopy-web
/canopy:alignment            # → asks which two repos to align
```

Each argument is a **slug**. The skill:
- resolves slug → local path by checking known bases in order:
  `~/emdash/repositories/<slug>`, then `~/emdash-projects/<slug>`
- uses the slug as-is when posting insight cards to canopy-web
- errors clearly if a slug resolves to no local repo

## Flow

1. **Preamble + sanity.** Run the canopy update check. Verify
   `~/.claude/canopy/workbench-token` is non-empty and canopy-web `/health/`
   returns 200 (override base URL with `CANOPY_WEB_API_URL`). Resolve both
   slugs → paths. Stop on any failure.
2. **Clear stale.** `DELETE /api/insights/clear/?source=canopy:alignment`, with
   the same OAuth/401 fallback portfolio-review documents (dismiss one-by-one,
   or skip and let cards pile up).
3. **Dispatch one comparison subagent.** Inputs: both repos' local paths, the
   four axes as a checklist, and the case-by-case directionality instruction.
   The agent reads both repos, identifies divergences, and returns structured
   findings (schema below). A single subagent keeps heavy repo reading out of
   the main session context.
4. **Rank.** Order findings by recency (last-touched date of the affected area,
   via git) and impact. Recent work floats to the top.
5. **POST.** Each finding becomes a one-sentence card POSTed to the **lagging**
   side's slug at `/api/projects/<slug>/context/` with
   `context_type=insight`, `source=canopy:alignment`, and the **`[alignment]`**
   category prefix (see canopy-web changes below). Stop on the first 401.
6. **Summary.** Print counts (findings posted, failures with HTTP codes) and
   `View at: <CANOPY_WEB>/insights`.

## Finding schema (subagent return)

One row per finding:

```
axis:        features | patterns | shared-code | docs-ux
reference:   <slug>  | "none — reconcile"
lagging:     <slug>
reasoning:   why this side is the reference, or why there's no clear winner
evidence:    file / commit / path on BOTH sides
recency:     last-touched date of the area (for ranking)
card:        one sentence ending in an action verb (the text actually posted)
```

## Card discipline (reused from portfolio-review)

- **One claim per card.** Multiple claims → multiple cards.
- **Cite the handle on both sides.** File path, commit SHA, command name, etc.
- **Action verb at the end.** "adopt", "reconcile", "extract", "consider".
- **One sentence.** Hard cap.
- **Empty is allowed.** If the two systems are well-aligned on an axis, emit
  nothing for it. Don't pad.

Example cards:
- `[alignment] ace ships PAT-mint loopback auth (commands/*-pat-mint.md); canopy-web uses an older shared-secret flow — adopt the loopback shape.`
- `[alignment] ace and canopy resolve plugin install paths differently (ace: lib/, canopy: inline python one-liner) — standardize on canopy's installed_plugins.json reader.`

## canopy-web changes (a second repo / second PR)

The insights backend is **category-agnostic**: an insight is a `ProjectContext`
row (`context_type=insight`, `content`, `source`, `created_at`); the category is
parsed client-side from the `[prefix]` of `content`, and the list endpoint
filters by `content__startswith=f"[{category}]"`. So a new category is a
**frontend-only** change — no model field, no migration, no backend code.

v1 (build now):
- `frontend/src/api/insights.ts` — add `'alignment'` to the `InsightCategory`
  union; add a `CATEGORY_RANK` weight (proposed `3`, peer of `opportunity` —
  tunable).
- `frontend/src/components/InsightChip.tsx` — add an `alignment` entry to
  `CATEGORY_STYLES` with a distinct color (`sky`/blue; unused today — existing
  badges are amber/orange/violet/stone/emerald). Label: "Alignment".

Card body stays a single `[alignment] sentence`, so alignment cards ride the
existing feed, ranking, dismiss, and clear-by-source unchanged.

### Divergence forward-path (explicitly out of v1)

The user anticipates alignment cards diverging from generic insight cards over
time. The frontend already branches rendering by category, so the seam is an
**alignment-specific card renderer** keyed on `category === 'alignment'`. When
the single sentence becomes limiting and the card needs structured fields
(axis, reference vs lagging side, dual evidence handles), the path is:

1. add a nullable `meta = models.JSONField(null=True, blank=True)` to
   `ProjectContext` (+ migration),
2. surface it through `ProjectContextCreateIn` / the insight schemas,
3. render alignment cards from `meta` instead of parsing the sentence.

Not built in v1. v1 ships the badge only; the structured payload is added the
first time a card actually needs it.

## Files

- `plugins/canopy/skills/alignment/SKILL.md` — the orchestration procedure above
- `plugins/canopy/commands/alignment.md` — **Pattern B**: reads the SKILL.md from
  the resolved plugin install path and follows it, to avoid the command/skill
  name-collision foot-gun documented in CLAUDE.md
- VERSION + `plugins/canopy/.claude-plugin/plugin.json` bumped via
  `canopy version bump`; ship, then `/canopy:update`

**canopy-web** (separate repo, separate PR):
- `frontend/src/api/insights.ts` — `'alignment'` in the union + `CATEGORY_RANK`
- `frontend/src/components/InsightChip.tsx` — `alignment` in `CATEGORY_STYLES`

This is a **two-repo delivery**: one PR in `canopy` (the skill), one in
`canopy-web` (the badge). The skill can ship first — an unknown `[alignment]`
prefix simply renders with no badge until the canopy-web PR lands, so there's no
hard ordering dependency.

## Non-goals (YAGNI)

- No config file and no bootstrap (projects are passed as args).
- No writes to the **compared** repos, no PRs against them, no canopy
  proposals/observations entries. (The canopy-web badge is a one-time tooling
  change, not a per-run write.)
- No canopy-web backend/model/migration change in v1 — the new category is
  frontend-only. No structured `meta` payload until a card needs it.
- No persistent findings DB — the insights feed is the store, cleared per
  `source` each run.
- No auto-discovery of sibling pairs.
- No N-way / >2 project comparison (exactly two projects per run).

## Open risks

- **Slug ≠ canopy-web project.** If a passed slug isn't a curated project on
  canopy-web, the POST will fail. The skill should surface this clearly rather
  than silently dropping the card (report the HTTP code in the summary).
- **Large-repo context.** Even two repos across four axes is substantial; the
  comparison subagent must read selectively (entry points, command/skill
  manifests, lib/ utilities, CLAUDE.md, recent diffs) rather than exhaustively.
