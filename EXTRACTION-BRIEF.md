# Extract the agent factory into a published, zero-dependency package

## Why

canopy-web needs to scaffold an agent repo from a browser. Today the factory is
`src/orchestrator/agent_factory.py` inside the `canopy` distribution — and `canopy`'s
runtime deps include **Pillow and NumPy** (the DDD screenshot/video gates). Depending on
`canopy` wholesale would pull image and array libraries into a Django web image to render
text templates, and — worse, given both repos commit heavily — canopy-web's lockfile would
churn every time canopy's *video tooling* deps moved.

The factory itself imports **only stdlib** (`re`, `shutil`, `sys`, `dataclasses`,
`pathlib`). So it can become its own package with zero runtime dependencies, released on
its own clock, and consumed by version.

## Verified facts — do not re-derive these, but DO re-check anything you're about to rely on

- `src/orchestrator/agent_factory.py` is **856 lines** on `origin/main`.
- Module-level imports are stdlib only. (Many `import` lines deeper in the file sit inside
  template strings — code the factory *emits* — not its own imports. Don't be fooled.)
- Existing public API, already the right shape:
  - `AgentSpec` (dataclass)
  - `create_agent(spec: AgentSpec, target_dir: Path, *, force: bool = False) -> list[Path]`
  - `normalize_slug(raw: str) -> str`
  - `AgentFactoryError`
- **`create_agent` already takes a caller-supplied `target_dir` and returns written paths.**
  It does NOT do `git init` — that happens in the CLI wrapper (`src/orchestrator/cli.py`
  around line 1410, which does `import subprocess` separately). So there is **no I/O
  inversion to do**; the factory is already pure in the way a second consumer needs.
- Two in-repo consumers, and only two:
  - `src/orchestrator/cli.py:1410` — imports the four public names above.
  - `src/orchestrator/fleet_align.py:32,58,67,68` — imports the module and reaches into
    **privates**: `getattr(agent_factory, "_TEMPLATES", {})` and
    `getattr(agent_factory, "_GATING_JSON", None)`. Verified at runtime: `_TEMPLATES` has
    **20 entries**, `_GATING_JSON` is present. So this works today — but the `getattr`
    defaults mean a rename would silently yield `{}` / `None` and `fleet_align`'s
    "ground truth" template baseline would quietly become empty.
- The plugin skill (`plugins/canopy/skills/create-agent/SKILL.md:34`) invokes the factory
  **through the CLI** (`uv run --project "$CANOPY_ROOT" canopy create-agent <slug>`), not by
  importing it. So the skill needs no change.

## What to build

### 1. `packages/canopy_agent_factory/`

A standalone distribution inside this repo:

```
packages/canopy_agent_factory/
  pyproject.toml
  canopy_agent_factory/
    __init__.py          # the public API, re-exported
    _factory.py          # the moved module body
  tests/
    test_*.py
```

- `pyproject.toml`: `name = "canopy-agent-factory"`, **zero runtime dependencies**,
  `requires-python` matching this repo's floor. Start at `version = "1.0.0"` — it is a new
  distribution with a stable API from day one, and it must version on its OWN clock, not
  canopy's (which is at 0.2.4xx and moves constantly). Include package data if any template
  lives outside the .py file (check — I believe they are all inline string constants).
- `__init__.py` exports exactly: `AgentSpec`, `create_agent`, `normalize_slug`,
  `AgentFactoryError`, plus the two promotions below. Everything else stays private.

### 2. Promote the two privates `fleet_align` legitimately needs

Reaching into a private across a package boundary is bad; reaching into a *published*
package's privates is worse. `fleet_align` needs the canonical template set as a baseline to
diff the fleet against — that is a legitimate second use, so it becomes public API:

```python
def templates() -> Mapping[str, str]:
    """The canonical stamp table: repo-relative path -> rendered-template source.

    Public because the fleet-alignment lens diffs real agent repos against this baseline —
    it is the ground truth for "what a factory-stamped agent looks like", not an
    implementation detail.
    """

def gating_config() -> str:
    """The canonical `config/gating.json` body a stamped agent ships with."""
```

Return a copy or a `MappingProxyType` from `templates()` so a caller cannot mutate the
table. Then update `fleet_align.py` to call these instead of `getattr` — which also removes
the silent-empty-default fragility.

### 3. Wire the orchestrator to it

- Add `canopy-agent-factory` to this repo's `[project] dependencies` and a
  `[tool.uv.sources]` editable path entry, mirroring how canopy-web does its in-repo
  packages.
- Replace `src/orchestrator/agent_factory.py` with **nothing** — delete it. Do not leave a
  shim re-exporting the package: a shim is a second import path that will silently become
  the one people use, and this repo's own docs argue against exactly that pattern.
- Update the two consumers (`cli.py`, `fleet_align.py`) to import from
  `canopy_agent_factory`.
- Update the one doc reference at `plugins/canopy/skills/create-agent/SKILL.md:105`
  ("The factory is `src/orchestrator/agent_factory.py`") to the new location.

### 4. Tests

- Move any existing factory tests into the package's own suite and make them run there with
  **no orchestrator import and no Django** — the package must stand alone. (canopy-web's
  `packages/canopy_agent_runs` sets this precedent: its own plain-pytest suite.)
- Add a test asserting `templates()` is non-empty and returns the expected count, and that
  its result cannot mutate the internal table. That is the test that would have caught the
  silent-empty class of bug.
- Keep the repo's existing test command green.

### 5. Publish it from the existing release pipeline

`.github/workflows/release.yml` already builds a wheel, **verifies the built wheel reports
its declared version from a clean venv with no repo on the path**, and publishes to GitHub
Releases. Extend it (or add a sibling job) to publish `canopy-agent-factory` to **PyPI**.

- PyPI rather than a GitHub Release asset, because only PyPI gives a consumer real version
  *resolution*. A Release asset means pinning an exact URL, which is untenable against a repo
  that has cut 436 releases.
- Use PyPI Trusted Publishing (OIDC) if you can — no long-lived token in the repo. If that
  needs configuration this repo does not have, **stop and say so** rather than adding a
  `PYPI_API_TOKEN` secret on your own initiative; that is a credential decision for a human.
- Carry over the existing pipeline's discipline: the published artifact must not be able to
  lie about its version. Reuse the clean-venv verification idea for this package.
- The two distributions version independently. Do not couple `canopy-agent-factory`'s version
  to `VERSION` / `plugin.json` / `marketplace.json` — `version-check.yml` enforces those three
  agree, and this package is deliberately outside that set. If that workflow would now fail or
  needs a carve-out, handle it and say what you did.

## Constraints

- **Zero runtime dependencies** on the new package. If you find you need one, stop and
  report — it invalidates the reason for the split.
- Do not change the factory's behaviour. This is a move plus two promotions. The rendered
  output for a given `AgentSpec` must be byte-identical before and after; prove it (scaffold
  into a temp dir on `origin/main`, again after, and diff).
- Do not reformat or "improve" the moved code. A large incidental diff defeats review.
- Work only in this worktree (`/Users/jjackson/emdash/worktrees/canopy/agent-factory-package`,
  branch `feat/agent-factory-package`). The canopy main checkout is on someone else's feature
  branch with uncommitted work — do not touch it.
- Commit when green. Do NOT push and do NOT open a PR.

## Report

Write your report to `EXTRACTION-REPORT.md` in this worktree. Include: the byte-identical
proof from the constraint above, every command with its output, what you did about
`version-check.yml`, and whether PyPI Trusted Publishing was available or needs a human.
