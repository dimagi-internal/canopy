# How canopy's docs stay true

Canopy changes daily and agents read these docs to answer people. A stale page is worse than a
missing one, so the rule is: **docs are a map, the code is the territory, and anything an agent
says about canopy is checked against the territory.**

Same shape as connect-labs and open-chat-studio (MkDocs-style Markdown in the repo, a glossary,
decision records, generated reference, a docs-impact signal on PRs, a review rule). No `llms.txt`:
neither of those uses one, and a pointer from each agent's `CLAUDE.md` plus readable pages does
the job.

## Structure (Diátaxis)

- **Tutorials / how-to** for people: "Use an agent you've been added to", "Set up an agent".
  Task-first, one page per task, no operator background.
- **Reference**: generated where possible (below).
- **Explanation**: `docs/architecture/` and design records (the *why*).
- **Operators** (`onboarding-a-new-operator.md`) stay separate and are linked only from there.
- `docs/GLOSSARY.md` fixes the vocabulary. `plugins/canopy/agent-core/using-canopy.md` is the one
  page every agent is pointed at; it routes to the rest.

## Keeping it true

1. **Generate what can be generated** (CLI reference from `--help`, API surface from the schema).
   Never hand-edit a generated page.
2. **Verify before asserting.** Agents read the page, then check the claim against `--help`, live
   state or the code (`using-canopy.md`), and say which. A mismatch is filed as a docs fix.
3. **Name the source of a load-bearing claim** ("checked by `src/orchestrator/agent_use.py`"), so
   verifying costs one file.
4. **Docs-impact on PRs.** A PR that changes behavior a doc describes updates the doc in the same
   PR, or says why not. Reviewers (human or agent) cross-check a doc's claims against the diff.
5. **Decision records for the *why*;** reference pages state current behavior and link to them.

## Not built yet (follow-ups)

- CI that runs the commands shown in end-user pages and fails when a flag or URL disappears.
- A source-file-changed-but-doc-untouched check, and a scheduled freshness sweep that files drift.
- A plain-text/markdown rendering of canopy-web's `/get-started` (it is a JavaScript page that
  agents cannot read today).
- The end-user page "Use an agent you've been added to" in canopy-web's docs.
