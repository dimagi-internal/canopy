# How canopy's docs stay true

Canopy changes daily and agents read these docs to answer people. A stale page is worse than a
missing one, so the rule is: **docs are a map, the code is the territory, and anything an agent
says about canopy is checked against the territory.**

Shape: Markdown in the repo, a glossary, generated reference, a docs-impact signal on PRs, a review
rule. No `llms.txt`: a pointer from each agent's `CLAUDE.md` plus readable pages does the job.

A doc holds only what the code cannot say: what a subsystem is for, how its parts connect, the
invariants, refusals and gotchas (each with a pointer to the file and symbol that enforces it), and where to start reading.
Route lists, field lists, defaults, enum values, UI copy, counts and flag lists are stated by the
code, the schema or `--help`: point at them, never copy them. History (dates, PR numbers, "used
to", specs, plans, changelogs) is not kept as a doc; git and GitHub keep it. The reason behind a
rule is part of the rule, in the present tense ("do not relax X: Y depends on it"). If a doc exists
because the code is hard to follow, fix the code (a module docstring, a comment, a clearer name).

## Structure (Diátaxis)

- **Tutorials / how-to** for people: "Use an agent you've been added to", "Set up an agent".
  Task-first, one page per task, no operator background.
- **Reference**: generated where possible (below).
- **Explanation**: `docs/architecture/` (orientation, invariants, gotchas).
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
5. **Commands in end-user pages are checked in CI.** `.github/scripts/docs_commands_check.py` (the
   `docs-commands` job in `docs-sync.yml`) extracts every `canopy ...` command from `README.md`,
   `CONTRIBUTING.md` and `docs/*.md`, resolves it in the real CLI and fails when the command is gone
   or a long flag the page shows is not in its `--help`. Placeholders go in
   `.github/scripts/docs_commands_allowlist.yaml`, each with a reason. URLs are not checked.
6. **Screens the pages name are pinned to canopy-web.** `tests/docs_ui_labels.yaml` lists each button,
   tab and runner-installer detail an end-user page names, with the canopy-web file that defines it;
   `tests/test_docs_ui_labels.py` fails when a page drops one, and (with `CANOPY_WEB_PATH` set to a
   canopy-web checkout) when canopy-web changes one.
7. **Help text and CLI docstrings are current state.** `tests/test_cli_text_is_current.py` fails on an
   ISO date, a PR or issue reference or a person's name in any `--help` text or CLI module docstring.
8. **False positives of the regeneration scripts are named once.** `.docs-ignore` lists what
   `check_refs.py` and `find_history.py` must not flag, each with a reason; `tests/test_docs_ignore.py`
   runs both scripts on the repo's docs and fails on a flag or on an entry nothing matches.
9. **The docs tests run in a clean environment.** `scripts/test-docs.sh` (see `CONTRIBUTING.md`).
10. **History docs do not exist.** `tests/test_docs_ignore.py` fails when a specs, plans or archive directory under
    `docs/`, or a `CHANGELOG.md`, is present, and runs `find_history.py` (dates, PR numbers, history phrases)
    and `check_refs.py` over `README.md`, `CONTRIBUTING.md`, every `docs/*.md` and
    `docs/architecture/*.md`, and the `plugins/canopy/agent-core/*.md` pages agents read at runtime.

## Not built yet (follow-ups)

- A source-file-changed-but-doc-untouched check, and a scheduled freshness sweep that files drift.
- A plain-text/markdown rendering of canopy-web's `/get-started` (it is a JavaScript page that
  agents cannot read today).
- The end-user page "Use an agent you've been added to" in canopy-web's docs.
