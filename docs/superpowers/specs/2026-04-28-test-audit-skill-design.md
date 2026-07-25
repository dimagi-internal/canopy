# Test Audit Skill

> **Status: shipped — `canopy test-audit` + `/canopy:test-audit` live. Historical record, not current-state.**

A canopy skill that audits a project's test suite, scores each test on
"is this pulling its weight," and (by default) opens a PR that prunes or
skips the dumb ones.

## Problem

From the user, in their own words:
> "I have no idea what any of the tests are doing in ace or ace-web... I'm
> frequently thinking 'oh I should go make sure tests are ideal and any
> non-meaningful tests are pruned.'"

Concrete pain in ace-web: 23 perpetually-failing tests baseline-acknowledged
as "test-environment fragility — not real bugs." The "just ignore those"
attitude erodes trust in the suite. Plus an unknown amount of redundant,
mock-heavy, or low-signal tests across the repo.

`canopy:health` already exists but is a dashboard wrapping pass/fail/timing
— it doesn't ask "is this test pulling its weight?" Nothing else in the
canopy / superpowers / code-review ecosystem does this.

## Design

### Behavior

`canopy test-audit` from any project root:

1. Runs the suite (with a configurable rerun count for flake detection).
2. Statically parses each test (AST → assertions, mocks, fixtures, the
   source funcs the test seems to exercise).
3. Dispatches a parallel LLM judge per test that scores 5 dimensions
   grounded in superpowers TDD principles:
   - **Meaningful assertion** (does it actually check something?)
   - **Behavior vs. implementation** (testing what, not how)
   - **Mock discipline** (no mocks of code under test)
   - **Name matches behavior** (the name and the assertions agree)
   - **Redundancy** (does a sibling test already cover this?)
4. Cross-test pass to cluster redundant tests.
5. Writes `audit-report.md` + `verdicts.yaml` to
   `.canopy/test-audits/<timestamp>/` (gitignored).
6. **Default: opens a PR** prune-and-skip changes for `verdict=prune` with
   `score ≤ 3`. PR body is the audit report. Runs
   `superpowers:requesting-code-review` on the PR before the agent reports
   done.

`--report-only` skips step 6 (read-only mode).
`--no-run` skips the dynamic phase (static-only).
`--reruns N` controls flake detection (default: 0 = no reruns).
`--scope changed|all` (default: `all`).

### Default = autonomous

The user's stated workflow: "A as the core but I usually run it in mode C."
So mode C is the default. The audit is always produced; the PR is the
default action on it. Conservative thresholds (`score ≤ 3`, `verdict=prune`
only) keep this safe; refactor and investigate verdicts always require
human work.

### Surface area

| File | Purpose |
|------|---------|
| `plugins/canopy/skills/test-audit/SKILL.md` | Agent-facing skill, follows existing canopy skill conventions |
| `plugins/canopy/commands/test-audit.md` | Slash command, Pattern B (reads SKILL.md from disk) |
| `src/orchestrator/test_audit/__init__.py` | Module entry |
| `src/orchestrator/test_audit/collector.py` | Walks tests/, builds `TestItem` inventory |
| `src/orchestrator/test_audit/runner.py` | Wraps pytest with `--json-report --durations=0`; optional reruns |
| `src/orchestrator/test_audit/parser.py` | AST analysis: assertions, mocks, fixtures, name vs body |
| `src/orchestrator/test_audit/judge.py` | Per-test parallel LLM judge via `skill_runner` + `rate_limiter` |
| `src/orchestrator/test_audit/aggregator.py` | Cross-test redundancy clustering, bucketing |
| `src/orchestrator/test_audit/report.py` | Writes `audit-report.md`, `verdicts.yaml`, `summary.md` |
| `src/orchestrator/test_audit/applier.py` | Mode-C: verdicts → git edits → `gh pr create` |
| `src/orchestrator/cli.py` | Add `canopy test-audit` Click command |

### Reuse

- `skill_runner.py` — for parallel `claude -p` judge dispatch
- `rate_limiter.py` — caps API calls per hour
- `circuit_breaker.py` — stops the run if N consecutive judges fail
- `paths.py` — for output dir
- `superpowers:requesting-code-review` skill — invoked on the apply-mode PR
- `superpowers:test-driven-development` principles — the rubric source
  (embedded in the judge prompt; we don't invoke that skill at runtime)

### Scope (v1)

- **Python / pytest only.** ace and ace-web are pytest. JS/TS adapters
  later if they earn it.
- Single repo at a time, run from project root.
- No incremental / cached mode in v1; if performance is a problem we add a
  cache later.

### Output (terse by default)

The user has indicated they won't read long reports. So:

- **Terminal output is one screen** — top 5 prunes, top 3 redundant
  clusters, count of failing/flaky tests. PR URL if applied.
- **`audit-report.md`** is the full human-readable report — but lives in
  the PR body, not as a thing the user has to open.
- **`verdicts.yaml`** is machine-readable and only consulted by the applier
  or future tooling.
- **`summary.md`** is what gets printed inline to terminal.

### Conservative apply rules

The applier only touches a test if:

| Verdict | Score | Action |
|---------|-------|--------|
| `prune`, reason=env-fragile | any | Add `@pytest.mark.skip(reason="…")` (don't delete) |
| `prune`, other reasons | 0–3 | `git rm` the test |
| `prune`, other reasons | 4–6 | **Reported, not applied.** Needs `--aggressive` |
| `refactor` | any | **Never applied.** Reported only |
| `investigate` | any | **Never applied.** Reported only |
| `keep` | any | No action |

Each git change includes a one-line commit message citing the audit
finding. The PR body is the full audit-report.md.

## Error handling

- Pytest collection errors → reported, run continues, those tests get
  `verdict=investigate`.
- Judge timeouts / rate-limit → `skill_runner` already retries; final
  failures get `verdict=error` and are excluded from the applier.
- Applier never pushes to main, never force-pushes, always opens a PR on a
  fresh branch. PR is opened *before* the agent reports complete so the
  user can review.
- If `gh` is not authenticated, the applier reports "would have opened PR
  with N changes" and writes a patch file; doesn't crash.

## Testing

- Unit tests for `parser.py` — AST extraction against fixture test files
  with known shapes (good test, mock-heavy test, no-assertion test, etc.).
- Unit tests for `aggregator.py` — redundancy clustering against synthetic
  verdicts.
- Integration test: a `tests/fixtures/synthetic_suite/` containing 8–10
  hand-crafted "good" and "bad" tests with documented expected verdicts;
  run the full pipeline (with the judge stubbed deterministically) and
  assert each verdict matches expectation.
- Run `canopy test-audit --report-only` against canopy itself as a
  smoke test.

## Out of scope (v1)

- JS/TS / jest / vitest support
- Coverage analysis (assertion-presence is a proxy)
- Multi-repo audits
- Caching of LLM judgments across runs
- A web UI for browsing reports (the PR body is the UI)

## Open questions resolved during brainstorm

- **Modes A vs C:** A is the engine, C is the default. `--report-only`
  flag for A-mode.
- **Static vs dynamic:** Hybrid. `--no-run` for static-only.
- **Frameworks:** Python/pytest only for v1.
- **Use superpowers/code-review skills:** Borrow rubric philosophy from
  superpowers TDD skill (embedded in judge prompt). Invoke
  `superpowers:requesting-code-review` on the apply-mode PR. Don't try to
  shoehorn `code-review:code-review` into per-test grading — it wants a PR
  diff, wrong shape.
