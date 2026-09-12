# Agent factory extraction — report

Branch: `feat/agent-factory-package`, worktree
`/Users/jjackson/emdash/worktrees/canopy/agent-factory-package`. Cut fresh from
`origin/main` (`cba84fe`, PR #632 merged).

## What moved

- `src/orchestrator/agent_factory.py` (856 lines) → deleted. Its body moved verbatim
  (no reformatting) to `packages/canopy_agent_factory/canopy_agent_factory/_factory.py`,
  with two additions at module level:
  - `from types import MappingProxyType` / `from typing import Mapping` (stdlib, zero new
    dependency)
  - two new public functions appended at the end of the file: `templates()` (returns
    `MappingProxyType(dict(_TEMPLATES))` — a fresh, read-only copy each call) and
    `gating_config()` (returns `_GATING_JSON`).
- `packages/canopy_agent_factory/canopy_agent_factory/__init__.py` re-exports exactly:
  `AgentSpec`, `create_agent`, `normalize_slug`, `AgentFactoryError`, `templates`,
  `gating_config`. `__version__ = "1.0.0"`.
- `packages/canopy_agent_factory/pyproject.toml`: `name = "canopy-agent-factory"`,
  `version = "1.0.0"`, `dependencies = []`, `requires-python = ">=3.11"`, hatchling
  build backend (matching canopy-web's `packages/canopy_agent_runs` precedent). No
  package-data needed — confirmed all 20 templates are inline string constants, no
  external file reads.
- Consumers updated: `src/orchestrator/cli.py` (2 import sites),
  `src/orchestrator/fleet_align.py`, `src/orchestrator/openclaw_harvest.py` — a THIRD
  in-repo consumer of `agent_factory.{AgentSpec,create_agent}` the brief's "only two
  consumers" didn't mention (verified: uses only public API, so no extra promotion
  needed, just an import-path fix).
- `fleet_align.py` now calls `canopy_agent_factory.templates()` /
  `canopy_agent_factory.gating_config()` instead of
  `getattr(agent_factory, "_TEMPLATES"/"_GATING_JSON", {}/None)`.
- Root `pyproject.toml`: added `"canopy-agent-factory>=1.0.0"` to `[project]
  dependencies`, and `[tool.uv.sources] canopy-agent-factory = { path =
  "packages/canopy_agent_factory", editable = true }` (mirrors canopy-web's
  `canopy-agent-runs` entry exactly).
- Doc reference updated: `plugins/canopy/skills/create-agent/SKILL.md:105` now points at
  `packages/canopy_agent_factory/`.
- `src/orchestrator/TIERS.md` and `tests/test_plugin_boundary.py`: removed
  `agent_factory` from the FRAMEWORK module set (the module no longer exists under
  `orchestrator/`; `canopy_agent_factory` is now an external dependency, not a tiered
  submodule, so it carries no tier).
- `tests/test_text_encoding_explicit.py`: added `"packages"` to `SEARCH_ROOTS` (so the
  moved factory module stays covered by the Windows-encoding gate) and added `"tests"`
  to `SKIP_DIR_PARTS` (test code was never in scope — the top-level `tests/` dir was
  never in `SEARCH_ROOTS` either — and without this, `packages/*/tests/` would newly
  fail the gate on test-only `read_text()`/`write_text()` calls that pre-date this
  change and were never meant to be covered).

## Tests moved / split (judgment call — flagged, not asked)

The brief says "move any existing factory tests into the package's own suite... no
orchestrator import and no Django." `tests/test_agent_factory.py` (33 tests) split
based on what each test actually exercises:

- **24 tests → `packages/canopy_agent_factory/tests/test_factory.py`**: everything
  that only exercises `create_agent`/`AgentSpec`/`normalize_slug` output (file layout,
  token substitution, JSON validity, rollback, non-ASCII round-trip) plus the tests of
  the generated hook's SELF-CONTAINED degrade-safely behavior (it's a stdlib loader;
  these tests always point `CANOPY_PLUGIN_DIR` at a nonexistent dir, so they need no
  external fixture).
- **8 tests stay in `tests/test_agent_factory.py`** (repo-side, import updated to
  `canopy_agent_factory`): tests that spawn the generated hook against the REAL
  `plugins/canopy/agent-core/*` engine — baseline deny rails, the agent-core docs, MCP
  Drive rails, `per_statement`, shared-engine delegation. That fixture tree lives in
  this repo's plugin tree, not in the standalone package; moving these tests would
  either break them (wrong relative path) or require copying `plugins/canopy/agent-core`
  into the package, which is real coupling the brief didn't ask for and the package
  doesn't need. Documented in both files' module docstrings so the split is legible
  later, not just remembered by me.
- **New**: `packages/canopy_agent_factory/tests/test_templates_baseline.py` — asserts
  `templates()` returns exactly 20 entries, is read-only (`TypeError` on item
  assignment), and returns a fresh copy per call; `gating_config()` is non-empty. This
  is the test the brief asked for — it's what would have caught the
  `getattr(..., {})`-silently-empty class of bug.
- `tests/test_fleet_align.py`: 3 sites that did
  `monkeypatch.setattr(fa.agent_factory, "_TEMPLATES", ...)` now monkeypatch
  `fa.canopy_agent_factory.templates` (a callable) instead, since `templates()` returns
  a fresh dict per call rather than exposing a mutable module attribute. Verified this
  preserves exact original semantics: `_template_text()`/`ARTIFACTS` were already
  evaluating the template table at the times they always did (module-import time for
  `ARTIFACTS`, call time for `_template_text`) — unchanged by the refactor.

## Byte-identical scaffold proof

```
$ uv run python -c "from orchestrator.agent_factory import AgentSpec, create_agent; ..."   # BEFORE, origin/main
20 files written
$ uv run python -c "from canopy_agent_factory import AgentSpec, create_agent; ..."          # AFTER
20 files written
$ diff -r /tmp/scaffold-before/testagent /tmp/scaffold-after/testagent
(no output, exit 0)
$ diff <(sha256sum-per-file BEFORE) <(sha256sum-per-file AFTER)
(no output, exit 0)
$ diff <(find BEFORE -printf '%P %m\n' | sort) <(find AFTER -printf '%P %m\n' | sort)   # perms/modes
(no output, exit 0)
```
Identical file set, identical bytes (sha256 per file), identical permissions
(0o755 on `.py`/`bin/*`, 0o644 elsewhere) — same `AgentSpec`, same `create_agent`,
same output, before and after.

## Test runs

- **Repo's own test command** (`uv run pytest -q -p no:cacheprovider`, per
  `.github/workflows/python-tests.yml`): `3763 passed, 2 skipped` (2 pre-existing
  skips, 2 pre-existing deprecation warnings in `test_openclaw_harvest.py`, unrelated
  to this change).
- **New package's standalone suite**, run from `packages/canopy_agent_factory/` via
  `uv run --with pytest pytest -q` (fresh ephemeral venv, no orchestrator/repo on
  path): `28 passed`.
- **Wheel build + isolation proof**: `uv build` inside the package dir → zero
  `Requires-Dist` lines in `METADATA` (confirmed via `unzip -p dist/*.whl "*/METADATA"`)
  → installed into `/tmp/verify-factory` (clean venv) → from `cd /` (no repo on path):
  `import canopy_agent_factory as f; f.__version__` → `1.0.0`; `f.__all__` matches the
  declared exports; `len(f.templates())` → `20`.
- `uv run canopy create-agent --help` — works, unchanged output.
- `uv run python -c "from orchestrator import fleet_align"` — imports cleanly,
  `fleet_align.ARTIFACTS` has 6 entries (5 stamped skills + gating), matching pre-change
  behavior.
- `uv run python -c "from orchestrator import openclaw_harvest"` — imports cleanly (the
  third, brief-unmentioned consumer).

## Zero runtime dependencies

Confirmed: `packages/canopy_agent_factory/pyproject.toml` declares `dependencies = []`;
the built wheel's METADATA carries no `Requires-Dist` line at all. No dependency was
added — the constraint held without needing to stop and report.

## VERSION / version-check.yml

Editing `plugins/canopy/skills/create-agent/SKILL.md` (a `plugins/canopy/` file) trips
`version-check.yml`'s "plugin changed → VERSION must bump" gate, so I ran
`uv run canopy version bump` (the repo's own tool) rather than hand-editing: bumped
`VERSION`, `plugins/canopy/.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`
(both version fields), and the `[project] version` line in root `pyproject.toml` from
`0.2.485` → `0.2.487` (it fetched `origin/main`, found it had advanced to `0.2.486`
in the meantime, and correctly bumped from the higher of the two). Verified
`canopy version verify` reports OK and `canopy version verify-bump` will see the
`plugins/canopy/` diff once committed (it diffs against committed HEAD, so it read
"no changes" pre-commit — expected, not a bug).

`canopy-agent-factory`'s own version (`1.0.0`) is NOT part of this trio and is not
touched by `version-check.yml` — confirmed by reading the workflow: it only compares
`VERSION` / `plugin.json` / `marketplace.json` to each other. No carve-out was needed.

## Publishing (release.yml)

Added a sibling job `release-agent-factory` in `.github/workflows/release.yml`,
independent of canopy's own `release` job (own concurrency group, own tag namespace
`agent-factory-v<version>` read from `packages/canopy_agent_factory/pyproject.toml`,
own idempotent tag-exists check). It builds the wheel+sdist, verifies the built wheel
reports its declared version from a clean venv with no repo on the path (same
discipline as canopy's own release job), then publishes to PyPI via
`pypa/gh-action-pypi-publish@release/v1` using OIDC Trusted Publishing
(`permissions: id-token: write`, no token input), then tags.

**PyPI Trusted Publishing is NOT yet configured** — this is the human-decision item
the brief flagged. Verified, did not assume:
- `gh secret list --repo dimagi-internal/canopy` → empty (no `PYPI_API_TOKEN` or
  similar).
- `gh api repos/dimagi-internal/canopy/environments` → `{"total_count":0,...}` (no
  `pypi` environment).
- `canopy-agent-factory` does not exist on PyPI yet (`GET
  https://pypi.org/pypi/canopy-agent-factory/json` → 404), so there's no existing
  "pending publisher" to have registered against a not-yet-existing project name.

Per the brief, I did **not** invent a `PYPI_API_TOKEN` secret. **A human needs to**,
before the `release-agent-factory` job's first real run, register a PyPI Trusted
Publisher (pypi.org → your account/org → "Publishing" → add pending publisher) with:
- PyPI project name: `canopy-agent-factory`
- GitHub owner/repo: `dimagi-internal/canopy`
- Workflow filename: `release.yml`
- Job name: `release-agent-factory`
- Environment name: (none configured — leave blank, or add one and I'll add
  `environment:` to the job to match)

Until that's done, the job's build + version-verification steps still run and pass on
every push to `main`; only the PyPI publish step will fail (visibly, as an OIDC/403,
not silently) and the tag step is gated behind it (`if: steps.v.outputs.already ==
'0'` covers publish; tagging happens after publish in the same conditional block, so a
failed publish does not tag a version that never actually reached PyPI).

Also added a CI step to `.github/workflows/python-tests.yml` running the package's own
suite in its own directory (`working-directory: packages/canopy_agent_factory`,
`uv run --with pytest pytest -q`) — the root `uv run pytest` never collects it, since
the package has its own `testpaths` and root's `pyproject.toml` restricts
`testpaths = ["tests"]`. This mirrors canopy-web's per-package CI step precedent
exactly.

## Constraints honored

- Zero runtime dependencies: yes (verified above).
- No I/O refactor: `create_agent(spec, target_dir, *, force=False)` unchanged, still no
  `git init`.
- No reformatting of moved code: `_factory.py` is the original file's body verbatim,
  plus two new imports and two new functions appended at the end.
- Worked only in this worktree; did not touch
  `/Users/jjackson/emdash-projects/canopy`.
- Did not push, did not open a PR.

## Commit

Single commit on `feat/agent-factory-package`. See status line below for SHA (this
report is written before the commit; the caller's response will carry the SHA from the
commit step that follows).
