---
name: test-audit
description: Audit a pytest or vitest test suite. Build a corpus, judge each test in-context against superpowers TDD principles, then (by default) open a PR pruning the dumb ones. Use when asked to "audit tests", "prune dumb tests", "clean up the test suite", or "test-audit".
---

## Preamble (run first)

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])" 2>/dev/null)"
_CANOPY_UPD=$(bash "$_CANOPY_PLUGIN/scripts/canopy-update-check.sh" 2>/dev/null || true)
case "$_CANOPY_UPD" in UPGRADE_AVAILABLE*) echo "$_CANOPY_UPD" ;; esac
```

If output shows `UPGRADE_AVAILABLE <old> <new>`: tell the user "canopy **v{new}** is available (you're on v{old}). Run `/canopy:update` to upgrade." Then continue with the skill — do not block on the upgrade.

# Test Audit

You are auditing a pytest or vitest test suite. The judging happens in YOUR
context — one pass over the whole corpus — not via per-test fan-out. That's
deliberate: cheaper, and you can spot redundancy by reasoning across tests
instead of guessing from name overlap.

Pytest and vitest are the two supported frameworks (v2). The collector
auto-detects from repo signal (`pyproject.toml`/`pytest.ini`/`conftest.py`
→ pytest, `vitest.config.*` or `"vitest"` in package.json → vitest). Pass
`--framework=pytest|vitest` to override. **The applier behaves differently
per framework — see step 7.**

## Flow

### 1. Confirm scope

Detect the framework. If you see jest/go test/rspec/etc., stop and report —
those aren't supported. (Vitest uses jest-compatible syntax for `it`/`test`/
`expect`, so a real Jest suite often works through the vitest adapter, but
don't rely on it without checking the corpus.)

### 2. Build the corpus

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
uv run --project "$CANOPY_ROOT" canopy test-audit collect .
```

(`uv run --project` does not change the working directory — `collect .` still
operates on the repo you're standing in.)

Add `--reruns 2` if the user mentions flaky tests. Add `--no-run` to skip
the test runner (faster, but misses flakiness and env-fragile tests — only
use if the suite takes >10 min to run or the user asks). Add
`--framework=vitest` if auto-detection picked the wrong backend.

The CLI prints `stamp_dir:`, `corpus:`, and `framework:` paths. Note them —
the framework determines how step 7 will behave.

**Non-conventional repo layouts (`--source-roots`).** The architecture pass
defaults to scanning `src/`, `lib/`, `app/`, `source/` for source modules.
Many real projects don't fit that shape — ACE has `lib/ mcp/ skills/ agents/
commands/ hooks/ scripts/ templates/`; CommCare Connect has its app-specific
layout. If the project has top-level dirs containing source code that aren't
in the defaults, pass them explicitly:

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
uv run --project "$CANOPY_ROOT" canopy test-audit collect . \
  --source-roots=lib,mcp,scripts
```

How to decide: `ls -d */ | grep -v -E '^(node_modules|dist|build|coverage|\.|test|tests|docs|migrations|fixtures)/'`
and inspect `package.json`'s `workspaces`/`exports`/`main` fields. Pass every
dir that contains code you'd expect to have tests for. Skip prompt-only dirs
(`skills/`, `agents/`, `commands/`) at this step — they're handled in 5c.

After collection, sanity-check: if `architecture.modules` in `corpus.yaml`
is empty or has fewer entries than `ls lib/*.ts | wc -l` suggests, the scan
missed the layout — re-collect with explicit `--source-roots`.

### 3. Read the corpus

`Read` the `corpus.yaml` from the stamp dir. It contains every test with:
- `nodeid`, `file`, `name`, `line`, `classname`
- `source`: full source of the test function
- `static`: assertion_count, has_real_assertion, mock_targets,
  fixtures_used, source_funcs_referenced, line_count
- `runtime`: status (passed/failed/error/skipped), duration_ms,
  flake_count, error message (if failing/erroring)

For very large suites (>300 tests) reading every body in your own context
is expensive (~25 lines/test × 300+ tests = 7K+ lines). Pick one of:

  **(a) Judge-by-file** (preferred above 500 tests). Read each test file
  whole, reason about the file's cluster (what behaviors does it cover,
  which tests are redundant, is any test mocking the CUT), and emit one
  verdict per test. The file is the unit of reasoning; the verdict is
  still per-test. Note in `audit-report.md` that you judged at file
  granularity.

  **(b) Sample-and-extrapolate.** Read all tests in 20% of files chosen
  to span every top-level test dir. Judge those carefully. For the
  remaining 80%, default `keep` and write `reason_code: not-sampled`
  with `score: null`. Document the sampling rate in `audit-report.md`
  so the user knows what wasn't audited.

  Do NOT shell out to a Python heuristic classifier — the audit's value
  is the in-context cross-test reasoning. A heuristic that scans for
  `expect()` count and assertion shape doesn't catch redundancy clusters
  or mock-of-CUT smells.

**Known data-quality issues to work around when reading `corpus.yaml`:**

- **`static.has_real_assertion`**: pre-0.2.88 vitest collector mis-flagged
  ~40% of tests with nested-paren `expect()` args (e.g.
  `expect(obj.has('x')).toBe(true)`) as having no real assertion. If you're
  on a stamp_dir collected before 0.2.88 OR the field's `false` count
  looks implausibly high, ignore the flag and read the body. Fixed in
  0.2.88 — also affected `expect(...).rejects.toThrow()` style.
- **`runtime: null` on every test, vitest only**: pre-0.2.88 the vitest
  runner only parsed the legacy `testResults` JSON shape and missed
  vitest 4.x's `files`/`tasks` tree. The `investigate` verdict and
  `env-fragile` special case can't fire when runtime is null suite-wide
  — either re-collect with 0.2.88+ or skip those verdicts for the run.
- **Duplicate nodeids**: when two `it(...)` blocks under different
  `describe` parents share the same leaf name, they collapse to the same
  nodeid. Disambiguate in `verdicts.yaml` by suffixing `#L<line>` to the
  nodeid (e.g. `foo.test.ts::handles X#L42`). Don't silently drop one.

### 4. Judge each test

For every test in the corpus, decide a **verdict** and a **score 0-10**.

**Rubric (grounded in superpowers TDD principles):**

Score these 5 dimensions in your head, then assign an overall score:

1. **meaningful_assertion** — does it actually verify something? `assert
   True` / no assertion = 0.
2. **behavior_vs_implementation** — does it test what (the contract) or
   how (the internals)?
3. **mock_discipline** — mocking dependencies is fine; mocking the code
   under test is not.
4. **name_match** — does the test name describe what it actually verifies?
5. **clarity** — could a new reader understand it in <30s?

**Verdicts:**

| Verdict | Use when |
|---------|----------|
| `keep` | score ≥ 7, OR the test is fine as-is |
| `refactor` | score 4–6 with a clear improvement path (mention in `reason`) |
| `prune` | score ≤ 3, OR redundant with a sibling that already covers this, OR no real value |
| `investigate` | runtime status=failed/error AND the assertion itself is meaningful (not env-fragile) |

**Special case — environment-fragile tests:**
If `runtime.status` is `error` AND the error mentions things like `no
module named …`, `fixture not found`, `Docker`, `connection refused`, or
similar: set `verdict=prune` AND `reason_code=env-fragile`. The applier
will skip-mark these (not delete them).

**Cross-test redundancy — read sibling tests, don't pattern-match.**
This is the part where you reason across the suite. The default trap is
to default everything to `keep` because no test is *obviously* broken.
That's a rubber stamp, not an audit. Counter it like this:

For every TestCase class or every cluster of tests in the same file that
target the same source function, **read the sibling tests** and ask:

> Does this test exercise a code path that is not already exercised by
> another test in this cluster?

If the answer is no, mark it `verdict=prune`,
`reason_code=redundant-with-sibling`, score 2–3, cite the keeper in
`reason`. Common shapes:

- `test_three_servers_returns_X` next to `test_two_servers_returns_X` —
  both hit the same `len(distinct) >= 2` branch. Prune the larger one.
- `test_one_entry_returns_X` next to `test_single_session_returns_X` —
  both hit the single-server path. Prune the degenerate.
- `test_returns_path_object` checking `isinstance(result, Path)` when
  the function signature is `-> Path` — tautology, prune.
- `test_result_has_both_keys` that only asserts dict shape, never
  behavior — weak-assertion, score 4 refactor (or prune if a sibling
  proves the same shape implicitly).
- Two tests that build the same input via different surface syntax (kwargs
  vs positional) and assert the same output — refactor/prune the second.

Pick the most expressive test as the keeper. The keeper should be the one
whose name and body together communicate the most about the contract.

**reason_code suggestions** (use these slugs for consistency):
`ok`, `tautology`, `no-meaningful-assertion`, `mock-of-cut`,
`name-mismatch`, `redundant-with-sibling`, `env-fragile`,
`unclear-purpose`, `weak-assertion`, `over-mocked`, `slow-and-low-value`.

### 5. Write `verdicts.yaml`

Write to `<stamp_dir>/verdicts.yaml`. Format:

```yaml
verdicts:
  - nodeid: tests/test_foo.py::test_bar
    score: 8
    verdict: keep
    reason_code: ok
    reason: Clear assertion of the documented contract.
  - nodeid: tests/test_foo.py::test_always_true
    score: 1
    verdict: prune
    reason_code: tautology
    reason: assert True with no behavior under test.
  - nodeid: tests/test_foo.py::test_with_missing_dep
    score: 6
    verdict: prune
    reason_code: env-fragile
    reason: Imports `nonexistent_module` — fails outside Docker. Will be skip-marked.
```

Every test in the corpus must have an entry.

### 5b. Architecture review pass

The per-test audit catches dumb tests. It does NOT catch suite-architecture
problems: untested modules, over-mocked test files (often a CUT-design
smell), slow-test hot lists, fixture sprawl, framework hygiene, missing
test-pyramid balance. **You must do this pass too.** Skipping it produces
the rubber-stamp failure mode: "every test is OK, suite is fine."

Read the `architecture` key in `corpus.yaml`:

```yaml
architecture:
  modules: [...]              # one entry per src/*.py: name, line count,
                              # public_func_count, has_test_file
  untested_modules: [...]     # bare names with no test_<name>.py
  mock_density: [...]         # per-file: total_mocks, total_assertions, ratio
  overmocked_files: [...]     # files where mocks > assertions, n>=2
  slow_tests: [...]           # tests above 1s, sorted descending
```

Then **read the actual config + setup files** in the repo. For pytest:
Glob `**/conftest.py`, Read `pyproject.toml`/`pytest.ini`. For vitest:
Read `vitest.config.{ts,js,mjs}` + any `**/setup*.{ts,js}` files referenced
from it. The grist gives you counts; the config tells you the *shape* of
the testing approach.

Score the suite on these architectural dimensions:

1. **Coverage architecture** — are there load-bearing modules with no
   tests? (List them with their src_lines and public_func_count so the
   user can see which gaps are most painful.)
2. **Mock discipline** — are specific test files over-mocked? Heavy mocks
   in a unit test usually means the CUT has too many dependencies — flag
   it as a *design* smell, not just a test smell.
3. **Test pyramid balance** — guess unit vs integration vs e2e from the
   fixtures/setup used. **Pytest:** `tmp_path` / pure → unit; real DB /
   network / playwright → integration/e2e. **Vitest:** pure imports + `vi.mock`
   → unit; `setup.ts` with real network/DB/browser → integration/e2e. Is
   the ratio sensible for the project?
4. **Framework hygiene** — **Pytest:** is conftest.py organized by domain
   or one blob? Are markers used? Are fixtures layered (session/module/
   function) or all `function`? Are slow tests gated behind markers?
   **Vitest:** does `vitest.config.*` use `test.environment` correctly?
   Are setup files organized? Is `globals: true` used (anti-pattern for
   library code)?
5. **Approach consistency** — does the codebase test behavior or
   implementation? (Heavy mocking + assertions about call counts is a
   tell. In vitest, look for excessive `vi.mock(...)` of the module under
   test.)

Write `architecture-review.md` to `<stamp_dir>/`. Suggested structure:

```markdown
# Architecture Review

## Coverage gaps
- `<module>` (<lines>L, <N> public funcs) — no test file. Recommend ...

## Over-mocked test files
- `tests/test_X.py` — N mocks vs M assertions. Likely CUT-design smell;
  consider extracting <thing> from <module>.

## Slow tests (top hot list)
- `nodeid` — <duration>ms. Recommend <move to integration marker | parametrize | …>

## Framework hygiene
<conftest organization, marker usage, fixture layering observations>

## Approach
<are tests testing behavior or implementation? specific examples>

## Recommendations (top 3)
1. ...
2. ...
3. ...
```

Keep it terse but specific — "module X is undertested" is not actionable;
"module X has 12 public functions, 0 tests, last touched in commit Y" is.

### 5c. Subsystem coverage pass (load-bearing for non-traditional projects)

The architecture data in step 5b only inventories source code that lives
in scanned source roots and has a file extension the adapter recognizes
(`.ts/.tsx/.js/.jsx/.mjs` for vitest; `.py` for pytest). For projects whose
load-bearing units are **prompt files, manifests, or agent definitions**
— skill markdown, agent markdown, MCP server entry points, slash command
files — `architecture.untested_modules` will be empty or misleading even
when 80%+ of the project's behavior is uncovered.

This is the failure mode the user is most likely complaining about when
they say "the audit rubber-stamped my suite." The per-test rubric grades
the tests that exist; the architecture pass grades source modules that
exist; neither sees the gap between "subsystems that ship product value"
and "subsystems with any test/eval coverage at all."

For projects with these subsystem dirs, enumerate them manually:

  - `skills/*/SKILL.md` (canopy/ace skill plugins) — does each have a
    companion `test/skills/<name>/` dir, a `-qa` eval skill, or a
    fixture-driven test in `test/`?
  - `agents/*.md` (autonomous agents) — same question.
  - `commands/*.md` (slash commands) — usually have no automated
    coverage; flag the ones that mutate state.
  - `mcp/*-server.ts` or `mcp/*/server.ts` (MCP server entry points) —
    each should have at least one integration test in `test/mcp/<server>/`.
  - `hooks/*.py` or `hooks/*.ts` — runtime-loaded; typically untested
    because they're side-effecty. Flag the high-blast-radius ones.

```bash
ls -d skills/*/ 2>/dev/null | wc -l    # total skill count
ls -d test/skills/*/ 2>/dev/null | wc -l  # tested skill count
```

For each subsystem with >25% untested, write a section in
`architecture-review.md`:

```markdown
## Subsystem coverage gaps (manual enumeration)

- **skills/**: 84 of 93 SKILL.md have no companion test or eval. Tested:
  decisions-render, decisions-sync, idea-to-pdd-qa, ... Untested includes
  load-bearing skills: idea-to-pdd, pdd-to-deliver-app, app-deploy,
  solicitation-management, ... (full list in stamp_dir/untested-skills.txt).
- **agents/**: 11 of 11 agent definitions have no test or fixture.
- **mcp/**: 0 of 5 MCP servers have a wholistic integration test
  exercising the full server boot/handshake/tool-call path.
```

When the project does coverage via *runtime evals* instead of test-time
assertions (common for LLM-driven codebases), say so explicitly and name
the eval mechanism. Don't pretend they're untested if they aren't.

### 6. Write `audit-report.md`

Write to `<stamp_dir>/audit-report.md`. This becomes the PR body, so it
should be human-readable. Suggested structure:

```markdown
# Test Audit — <repo name>

**N tests** — keep K, refactor R, prune P, investigate I.

## Top prune candidates
- `nodeid` — score X, reason_code: reason

## Refactor candidates
- ...

## Investigate (failing or unclear)
- ...

## Redundancy clusters
- **Cluster: function `add()`** (3 tests)
  - keeper: `test_add_returns_sum`
  - prune: `test_add_basic`, `test_add_simple` (cite reason)

## Deletion candidates (vitest, manual)
- `nodeid` — score X, reason. *Vitest applier only `.skip()`s; delete
  these by hand in the same PR.*

## Notes
Anything the user should know (suite hygiene observations,
environment-fragility patterns, etc.)

## Honest suite verdict

A single 1-10 score for the suite as a whole, with **one** of these as the
limiting factor cited explicitly:
  - **per-test quality** (tests that exist are weak/redundant/tautological)
  - **subsystem coverage** (load-bearing units have no test/eval)
  - **architecture fit** (mock density, pyramid balance, fixture sprawl)

Resist defaulting to 8+. If &gt;50% of named subsystems from 5c have no
coverage, the ceiling is 6 regardless of how good the existing tests are.
```

Keep this terse. The user has indicated they won't read carefully.

### 7. Apply

By default, open a PR:

```bash
_CANOPY_PLUGIN="$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")"
CANOPY_ROOT="$(bash "$_CANOPY_PLUGIN/scripts/canopy-runtime.sh")" || { echo "ERROR: canopy runtime not found — run /canopy:update"; exit 1; }
uv run --project "$CANOPY_ROOT" canopy test-audit apply <stamp_dir>
```

Add `--aggressive` only if the user explicitly asks (this also applies
prunes with score 4–6, not just 0–3).

Use `--dry-run` if the user asked for a report-only audit — this plans the
changes without touching files or opening a PR.

**Framework asymmetry — vitest does not delete.** The pytest applier
deletes test functions outright when verdict=prune & score≤3. The vitest
applier does NOT — it only marks `.skip(...)` with an `// audit: <reason>`
comment, even for low-score prunes. Reason: JS/TS deletion needs a real
parser to be safe (regex literals + JSX + nested template strings break
brace counting), and a test-audit applier that occasionally corrupts source
is worse than useless. The audit-report.md still lists deletion candidates
prominently so the user can delete them manually in the same PR.

### 8. Print the summary

The `apply` CLI prints a one-screen summary (counts, branch, PR URL). Just
relay that to the user. Don't paraphrase the audit-report.md — the PR body
IS the report. If `gh` wasn't authenticated, the apply step writes a
`.patch` file instead and prints its path.

For vitest audits, also remind the user: "Deletions are flagged in the PR
body but not auto-applied — delete by hand in the same branch if you agree."

## Conservative apply rules

The pytest applier only touches a test if:

| Verdict + reason | Action |
|------------------|--------|
| `prune`, env-fragile | `@pytest.mark.skip(reason="audit: ...")` |
| `prune`, score ≤ 3 | `git rm` the test |
| `prune`, score 4–6 | reported only (need `--aggressive`) |
| `refactor` / `investigate` | reported only, never applied |
| `keep` | no action |

The vitest applier:

| Verdict + reason | Action |
|------------------|--------|
| `prune`, env-fragile | `it.skip(...)` + `// audit: ...` comment |
| `prune`, score ≤ 3 | `it.skip(...)` (NOT deletion) — flagged for human in audit-report.md |
| `prune`, score 4–6 | reported only |
| `refactor` / `investigate` | reported only |
| `keep` | no action |

## Rules

- Pytest and vitest only. Other frameworks unsupported.
- Never push to main, never force-push. The applier always opens a PR on
  a fresh branch.
- If `gh` isn't authenticated, the applier writes a patch file — don't
  crash, don't ask the user to install gh.
- Don't dump the full report to chat. The user wants terse output and the
  PR body is the audit.
- If the suite is huge (>1000 tests), batch your reading of corpus.yaml
  by `offset`/`limit` and judge in passes by directory. Still write one
  combined `verdicts.yaml` at the end.
