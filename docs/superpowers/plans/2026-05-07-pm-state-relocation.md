# PM State Relocation Implementation Plan

> **Status: shipped — `resolve_pm_dir.sh` + `<repo>/.canopy/pm/` live. Historical record, not current-state.**

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Relocate `canopy:product-management` per-project state from `~/.canopy/pm/<project>/` into `<repo>/.canopy/pm/` so it's portable across machines and accounts via git.

**Architecture:** A single shell script (`scripts/resolve_pm_dir.sh`) becomes the canonical PM-path resolver — one call site idiom across every PM markdown file. The script also handles one-shot, idempotent migration from the legacy home-dir location with an auto-commit on the current branch. All references to `CANOPY_PM_PROJECT` (the old origin-URL-derived project key) are removed; only display-name uses of an origin-derived label remain (e.g. `email.subject_prefix`).

**Tech Stack:** bash 4+, pytest, subprocess for testing the script. No new Python dependencies.

**Spec:** `docs/superpowers/specs/2026-05-07-pm-state-relocation-design.md`

---

## File Structure

**New files:**
- `plugins/canopy/skills/product-management/scripts/resolve_pm_dir.sh` — path resolver + migration
- `tests/test_pm_state_path_resolution.py` — resolver behavior tests
- `tests/test_pm_state_migration.py` — migration behavior tests

**Modified files:**
- `plugins/canopy/skills/product-management/SKILL.md`
- `plugins/canopy/skills/product-management/templates/autonomous/cycle.md`
- `plugins/canopy/skills/product-management/templates/autonomous/config-schema.md`
- `plugins/canopy/skills/product-management/scripts/validate_autonomous_config.py` (docstring only)
- `plugins/canopy/agents/pm-supervisor.md`
- `plugins/canopy/commands/pm-status.md`
- `plugins/canopy/commands/pm-scout.md`
- `plugins/canopy/commands/pm-autonomous.md`
- `VERSION`
- `plugins/canopy/.claude-plugin/plugin.json`
- `.claude/CLAUDE.md`

---

## Task 1: Resolver script — happy path (in-repo and outside-repo)

**Files:**
- Create: `tests/test_pm_state_path_resolution.py`
- Create: `plugins/canopy/skills/product-management/scripts/resolve_pm_dir.sh`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pm_state_path_resolution.py`:

```python
"""Tests for the resolve_pm_dir.sh path resolver."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = (
    REPO_ROOT
    / "plugins"
    / "canopy"
    / "skills"
    / "product-management"
    / "scripts"
    / "resolve_pm_dir.sh"
)


def _run(cwd: Path, home: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "HOME": str(home),
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=str(repo), check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(repo),
        check=True,
    )
    # initial commit so HEAD exists
    (repo / "README.md").write_text("test\n")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True
    )


class TestResolveInsideGitRepo:
    def test_resolves_to_repo_canopy_pm(self, tmp_path):
        repo = tmp_path / "repo"
        home = tmp_path / "home"
        repo.mkdir()
        home.mkdir()
        _git_init(repo)

        result = _run(cwd=repo, home=home)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == str(repo / ".canopy" / "pm")

    def test_creates_canopy_pm_dir(self, tmp_path):
        repo = tmp_path / "repo"
        home = tmp_path / "home"
        repo.mkdir()
        home.mkdir()
        _git_init(repo)

        _run(cwd=repo, home=home)
        assert (repo / ".canopy" / "pm").is_dir()

    def test_resolves_from_subdirectory(self, tmp_path):
        repo = tmp_path / "repo"
        home = tmp_path / "home"
        repo.mkdir()
        home.mkdir()
        _git_init(repo)
        sub = repo / "src" / "deep"
        sub.mkdir(parents=True)

        result = _run(cwd=sub, home=home)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == str(repo / ".canopy" / "pm")


class TestResolveOutsideGitRepo:
    def test_falls_back_to_home_canopy_pm(self, tmp_path):
        cwd = tmp_path / "not-a-repo"
        home = tmp_path / "home"
        cwd.mkdir()
        home.mkdir()

        result = _run(cwd=cwd, home=home)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == str(
            home / ".canopy" / "pm" / "not-a-repo"
        )

    def test_creates_home_fallback_dir(self, tmp_path):
        cwd = tmp_path / "not-a-repo"
        home = tmp_path / "home"
        cwd.mkdir()
        home.mkdir()

        _run(cwd=cwd, home=home)
        assert (home / ".canopy" / "pm" / "not-a-repo").is_dir()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pm_state_path_resolution.py -v`
Expected: FAIL — `bash: <path>/resolve_pm_dir.sh: No such file or directory`

- [ ] **Step 3: Write the resolver script**

Create `plugins/canopy/skills/product-management/scripts/resolve_pm_dir.sh`:

```bash
#!/usr/bin/env bash
# Resolve $CANOPY_PM_DIR for the current cwd.
#
# Inside a git repo: <repo-root>/.canopy/pm
# Outside a git repo: $HOME/.canopy/pm/<basename-of-cwd>
#
# Side effect: if running inside a git repo and the destination is empty AND
# the legacy ~/.canopy/pm/<derived-project>/ directory exists with state,
# copy files in and commit on the current branch (best-effort).
#
# Stdout: the resolved path.
# Stderr: human-readable migration notes, if any.

set -euo pipefail

if REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null); then
  CANOPY_PM_DIR="$REPO_ROOT/.canopy/pm"

  # Migration: only if destination is empty (no tracked or untracked files).
  if [ ! -d "$CANOPY_PM_DIR" ] || \
     [ -z "$(find "$CANOPY_PM_DIR" -type f -print -quit 2>/dev/null)" ]; then
    LEGACY_PROJECT=$(git config --get remote.origin.url 2>/dev/null \
      | sed 's|.*[/:]||;s|\.git$||' || true)
    if [ -z "${LEGACY_PROJECT:-}" ]; then
      LEGACY_PROJECT=$(basename "$(dirname "$(git rev-parse --git-common-dir 2>/dev/null)")")
    fi
    LEGACY_DIR="$HOME/.canopy/pm/$LEGACY_PROJECT"
    LEGACY_MARKER="$LEGACY_DIR/.migrated"

    if [ -d "$LEGACY_DIR" ] && [ ! -e "$LEGACY_MARKER" ] && \
       { [ -e "$LEGACY_DIR/context.md" ] || \
         [ -e "$LEGACY_DIR/learnings.md" ] || \
         [ -e "$LEGACY_DIR/autonomous.yaml" ]; }; then
      mkdir -p "$CANOPY_PM_DIR"
      [ -e "$LEGACY_DIR/autonomous.yaml" ] && cp "$LEGACY_DIR/autonomous.yaml" "$CANOPY_PM_DIR/"
      [ -e "$LEGACY_DIR/context.md" ]      && cp "$LEGACY_DIR/context.md" "$CANOPY_PM_DIR/"
      [ -e "$LEGACY_DIR/learnings.md" ]    && cp "$LEGACY_DIR/learnings.md" "$CANOPY_PM_DIR/"
      [ -d "$LEGACY_DIR/runs" ]            && cp -R "$LEGACY_DIR/runs" "$CANOPY_PM_DIR/"

      # Best-effort commit. If git commit fails (no identity, hook rejects,
      # etc.) we leave the files staged-or-unstaged for the user to handle
      # and continue. Migration is still considered "done".
      if git -C "$REPO_ROOT" add -- ".canopy/pm" >/dev/null 2>&1; then
        if git -C "$REPO_ROOT" commit -m \
          "chore(canopy-pm): migrate state from ~/.canopy/pm/$LEGACY_PROJECT/" \
          -- ".canopy/pm" >/dev/null 2>&1; then
          echo "resolve_pm_dir: committed migrated state to current branch" >&2
        else
          echo "resolve_pm_dir: copied files but commit failed; review with 'git status' and commit manually" >&2
        fi
      fi

      {
        printf 'migrated_to: %s\n' "$REPO_ROOT/.canopy/pm"
        printf 'timestamp: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
      } > "$LEGACY_MARKER"
      echo "resolve_pm_dir: migrated PM state from $LEGACY_DIR" >&2
    fi
  fi

  mkdir -p "$CANOPY_PM_DIR"
else
  CANOPY_PM_DIR="$HOME/.canopy/pm/$(basename "$(pwd)")"
  mkdir -p "$CANOPY_PM_DIR"
fi

echo "$CANOPY_PM_DIR"
```

- [ ] **Step 4: Make it executable**

Run: `chmod +x plugins/canopy/skills/product-management/scripts/resolve_pm_dir.sh`

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_pm_state_path_resolution.py -v`
Expected: PASS — all 5 tests in `TestResolveInsideGitRepo` and `TestResolveOutsideGitRepo`

- [ ] **Step 6: Commit**

```bash
git add tests/test_pm_state_path_resolution.py plugins/canopy/skills/product-management/scripts/resolve_pm_dir.sh
git commit -m "feat(pm): add resolve_pm_dir.sh — path resolver with repo + home-dir fallback"
```

---

## Task 2: Resolver script — migration from legacy `~/.canopy/pm/<project>/`

**Files:**
- Create: `tests/test_pm_state_migration.py`
- Modify: (resolver already wrote in Task 1; verify migration paths via tests)

- [ ] **Step 1: Write the failing migration tests**

Create `tests/test_pm_state_migration.py`:

```python
"""Tests for the legacy-state migration baked into resolve_pm_dir.sh."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = (
    REPO_ROOT
    / "plugins"
    / "canopy"
    / "skills"
    / "product-management"
    / "scripts"
    / "resolve_pm_dir.sh"
)


def _run(cwd: Path, home: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "HOME": str(home),
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _git_init_with_origin(repo: Path, origin_url: str) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=str(repo), check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(repo),
        check=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", origin_url],
        cwd=str(repo),
        check=True,
    )
    (repo / "README.md").write_text("test\n")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True
    )


def _seed_legacy_state(home: Path, project: str) -> Path:
    legacy = home / ".canopy" / "pm" / project
    legacy.mkdir(parents=True)
    (legacy / "context.md").write_text("# context\nlegacy content\n")
    (legacy / "learnings.md").write_text("# learnings\nlegacy items\n")
    (legacy / "autonomous.yaml").write_text("email:\n  to: x@y.com\n")
    runs = legacy / "runs"
    runs.mkdir()
    (runs / "2026-01-01-user-value.md").write_text("# run\n")
    return legacy


class TestMigrationFromLegacyOriginUrl:
    def test_copies_all_files(self, tmp_path):
        repo = tmp_path / "repo"
        home = tmp_path / "home"
        repo.mkdir()
        home.mkdir()
        _git_init_with_origin(repo, "https://github.com/u/foo-proj.git")
        _seed_legacy_state(home, "foo-proj")

        result = _run(cwd=repo, home=home)
        assert result.returncode == 0, result.stderr

        new = repo / ".canopy" / "pm"
        assert (new / "context.md").read_text() == "# context\nlegacy content\n"
        assert (new / "learnings.md").read_text() == "# learnings\nlegacy items\n"
        assert (new / "autonomous.yaml").read_text() == "email:\n  to: x@y.com\n"
        assert (new / "runs" / "2026-01-01-user-value.md").is_file()

    def test_creates_migrated_marker(self, tmp_path):
        repo = tmp_path / "repo"
        home = tmp_path / "home"
        repo.mkdir()
        home.mkdir()
        _git_init_with_origin(repo, "https://github.com/u/foo-proj.git")
        _seed_legacy_state(home, "foo-proj")

        _run(cwd=repo, home=home)

        marker = home / ".canopy" / "pm" / "foo-proj" / ".migrated"
        assert marker.is_file()
        body = marker.read_text()
        assert "migrated_to:" in body
        assert str(repo / ".canopy" / "pm") in body
        assert "timestamp:" in body

    def test_creates_migration_commit(self, tmp_path):
        repo = tmp_path / "repo"
        home = tmp_path / "home"
        repo.mkdir()
        home.mkdir()
        _git_init_with_origin(repo, "https://github.com/u/foo-proj.git")
        _seed_legacy_state(home, "foo-proj")

        _run(cwd=repo, home=home)

        log = subprocess.run(
            ["git", "log", "--pretty=format:%s", "-n", "2"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        assert log[0].startswith("chore(canopy-pm): migrate state from")
        assert "foo-proj" in log[0]

    def test_migration_is_idempotent(self, tmp_path):
        repo = tmp_path / "repo"
        home = tmp_path / "home"
        repo.mkdir()
        home.mkdir()
        _git_init_with_origin(repo, "https://github.com/u/foo-proj.git")
        _seed_legacy_state(home, "foo-proj")

        _run(cwd=repo, home=home)
        _run(cwd=repo, home=home)

        log = subprocess.run(
            ["git", "log", "--pretty=format:%s"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        migration_commits = [
            line for line in log if line.startswith("chore(canopy-pm): migrate")
        ]
        assert len(migration_commits) == 1


class TestMigrationSkippedWhenDestNonEmpty:
    def test_skips_when_dest_has_content(self, tmp_path):
        repo = tmp_path / "repo"
        home = tmp_path / "home"
        repo.mkdir()
        home.mkdir()
        _git_init_with_origin(repo, "https://github.com/u/foo-proj.git")
        _seed_legacy_state(home, "foo-proj")
        (repo / ".canopy" / "pm").mkdir(parents=True)
        (repo / ".canopy" / "pm" / "context.md").write_text("# pre-existing\n")

        _run(cwd=repo, home=home)

        # dest content must be untouched
        assert (
            (repo / ".canopy" / "pm" / "context.md").read_text()
            == "# pre-existing\n"
        )
        # marker NOT created since migration skipped
        marker = home / ".canopy" / "pm" / "foo-proj" / ".migrated"
        assert not marker.exists()


class TestMigrationSkippedWhenMarkerPresent:
    def test_skips_when_marker_exists(self, tmp_path):
        repo = tmp_path / "repo"
        home = tmp_path / "home"
        repo.mkdir()
        home.mkdir()
        _git_init_with_origin(repo, "https://github.com/u/foo-proj.git")
        legacy = _seed_legacy_state(home, "foo-proj")
        (legacy / ".migrated").write_text("migrated_to: somewhere\n")

        _run(cwd=repo, home=home)

        # Dest dir created but no copies happened
        assert (repo / ".canopy" / "pm").is_dir()
        assert not (repo / ".canopy" / "pm" / "context.md").exists()


class TestMigrationFallbackProjectName:
    def test_uses_git_common_dir_basename_when_no_origin(self, tmp_path):
        # Repo has NO origin remote; fall back to dirname of git-common-dir.
        # In a non-worktree setup this is the parent of `.git`, i.e. the repo's
        # own basename.
        repo = tmp_path / "fallback-proj"
        home = tmp_path / "home"
        repo.mkdir()
        home.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
        subprocess.run(
            ["git", "config", "user.name", "Test"], cwd=str(repo), check=True
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=str(repo),
            check=True,
        )
        (repo / "README.md").write_text("test\n")
        subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True
        )
        _seed_legacy_state(home, "fallback-proj")

        result = _run(cwd=repo, home=home)
        assert result.returncode == 0, result.stderr

        new = repo / ".canopy" / "pm"
        assert (new / "context.md").is_file()
        assert (home / ".canopy" / "pm" / "fallback-proj" / ".migrated").is_file()


class TestNoMigrationOutsideGitRepo:
    def test_no_migration_attempted_outside_repo(self, tmp_path):
        cwd = tmp_path / "loose"
        home = tmp_path / "home"
        cwd.mkdir()
        home.mkdir()
        _seed_legacy_state(home, "loose")

        result = _run(cwd=cwd, home=home)
        assert result.returncode == 0, result.stderr
        # Resolver returns home-dir fallback, NOT migrated content
        assert result.stdout.strip() == str(home / ".canopy" / "pm" / "loose")
        # Legacy dir untouched (no marker created)
        assert not (home / ".canopy" / "pm" / "loose" / ".migrated").exists()
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `uv run pytest tests/test_pm_state_migration.py -v`
Expected: PASS — all 8 tests across the five test classes

(Note: the migration logic was written in Task 1 alongside the script. These tests verify it. If any fail, fix the script in `plugins/canopy/skills/product-management/scripts/resolve_pm_dir.sh` and re-run.)

- [ ] **Step 3: Commit**

```bash
git add tests/test_pm_state_migration.py
git commit -m "test(pm): cover legacy-state migration in resolve_pm_dir.sh"
```

---

## Task 3: Update SKILL.md to use the new resolver

**Files:**
- Modify: `plugins/canopy/skills/product-management/SKILL.md` (lines ~47-80, ~157-170)

- [ ] **Step 1: Replace the "Project State Convention" section**

Find the heading `## Project State Convention` (line ~47) and replace from that heading through (and including) the "Legacy migration" section (ending around line ~80, before `**Every run:**`).

Replace with:

```markdown
## Project State Convention

All project-level PM state lives at `<repo>/.canopy/pm/` — committed to the project's git repo so it's portable across machines and accounts. This shares the `.canopy/` namespace established by PR #37 (`<repo>/.canopy/lenses/`, `<repo>/.canopy/run-artifacts.yaml`, etc.).

```
<repo>/.canopy/pm/
├── context.md          ← what this project is, who uses it, what matters
├── learnings.md        ← project-specific learnings ("don't propose X again")
├── autonomous.yaml     ← autonomous-mode config (auto-bootstrapped on first run)
└── runs/               ← cycle logs (one per run)
    └── YYYY-MM-DD-<lens>.md
```

**Resolving the path** — call the resolver script once at the start of each run and capture its stdout:

```bash
PLUGIN_PATH=$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")
CANOPY_PM_DIR=$(bash "$PLUGIN_PATH/skills/product-management/scripts/resolve_pm_dir.sh")
```

Inside a git repo, this returns `<repo-root>/.canopy/pm` (created if missing). On the rare case of running outside a git repo, it falls back to `$HOME/.canopy/pm/<basename-of-cwd>/`.

**Auto-migration:** the resolver also performs a one-shot, idempotent migration from the legacy `~/.canopy/pm/<project>/` location. The first time PM runs in a project after this change, if `<repo>/.canopy/pm/` is empty AND a legacy directory exists, the resolver copies the files in, commits them on the current branch (`chore(canopy-pm): migrate state from ~/.canopy/pm/<project>/`), and writes a `.migrated` marker into the old location. Subsequent runs are no-ops. The user can delete `~/.canopy/pm/<project>/` whenever — nothing reads it after migration.

**Committing ongoing writes:** in autonomous mode, `.canopy/pm/` updates ride along with the cycle's PR commits, so they land on `main` when the PR merges. In interactive `/canopy:pm-scout` mode, treat `.canopy/pm/` updates like any other working-tree change — review with `git status` and commit alongside (or separately from) your feature work.
```

- [ ] **Step 2: Replace the Phase 0 bash snippet**

Find the Phase 0 code block (around line 162-168, starts with `CANOPY_PM_PROJECT=$(git config...`).

Replace the whole code block with:

```bash
PLUGIN_PATH=$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")
CANOPY_PM_DIR=$(bash "$PLUGIN_PATH/skills/product-management/scripts/resolve_pm_dir.sh")
[ -f "$CANOPY_PM_DIR/context.md" ] && echo "PM_STATE: ready" || echo "PM_STATE: missing"
```

- [ ] **Step 3: Verify other references stay coherent**

Search SKILL.md for `~/.canopy/pm` (e.g. line 361 in the principles section). Update any prose references from `~/.canopy/pm/<project>/autonomous.yaml` to `<repo>/.canopy/pm/autonomous.yaml`.

Run: `grep -n "~/.canopy/pm\|CANOPY_PM_PROJECT" plugins/canopy/skills/product-management/SKILL.md`
Expected: no matches

- [ ] **Step 4: Commit**

```bash
git add plugins/canopy/skills/product-management/SKILL.md
git commit -m "docs(pm): SKILL.md — switch path resolution to resolve_pm_dir.sh"
```

---

## Task 4: Update autonomous-mode templates

**Files:**
- Modify: `plugins/canopy/skills/product-management/templates/autonomous/cycle.md`
- Modify: `plugins/canopy/skills/product-management/templates/autonomous/config-schema.md`

- [ ] **Step 1: Update cycle.md Phase 0 step 1 (path resolution)**

In `templates/autonomous/cycle.md`, find the block starting around line 13 ("Resolve `$PLUGIN_PATH` and `$CANOPY_PM_DIR` once and reuse"). Replace the bash snippet (the `CANOPY_PM_PROJECT=...; ...; CANOPY_PM_DIR="$HOME/.canopy/pm/$CANOPY_PM_PROJECT"; mkdir -p "$CANOPY_PM_DIR"` block) with:

```bash
PLUGIN_PATH=$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")
CANOPY_PM_DIR=$(bash "$PLUGIN_PATH/skills/product-management/scripts/resolve_pm_dir.sh")
```

The opening prose `This skill is project-agnostic. ALL project-specific knobs live in ~/.canopy/pm/<project>/autonomous.yaml — see config-schema.md.` becomes:

```markdown
This skill is project-agnostic. ALL project-specific knobs live in `<repo>/.canopy/pm/autonomous.yaml` — see `config-schema.md`.
```

- [ ] **Step 2: Update cycle.md Phase 0 step 2 (autonomous.yaml bootstrap)**

The bootstrap block (around line 23-39) references `$CANOPY_PM_PROJECT` to seed `email.subject_prefix` and `shipping.branch_prefix`. We need a project-name string for those *display* uses, but it's no longer the path key.

Find the bullet items:
- `email.subject_prefix` = `[$CANOPY_PM_PROJECT]` …
- `shipping.branch_prefix` = `$CANOPY_PM_PROJECT/auto/`

Add a derivation step immediately above the bullet list:

```bash
# Derive a display name for subject prefix and branch namespace.
# This is for cosmetic strings only — NOT used for path resolution.
PROJECT_NAME=$(git config --get remote.origin.url 2>/dev/null \
  | sed 's|.*[/:]||;s|\.git$||' || true)
[ -z "$PROJECT_NAME" ] && PROJECT_NAME=$(basename "$(git rev-parse --show-toplevel 2>/dev/null)" \
  || basename "$(pwd)")
```

Then update the two bullets to reference `$PROJECT_NAME`:

```markdown
   - `email.subject_prefix` = `[$PROJECT_NAME]` (origin URL → repo basename fallback; cosmetic only — path resolution is handled by `resolve_pm_dir.sh`)
   - `shipping.branch_prefix` = `$PROJECT_NAME/auto/`
```

- [ ] **Step 3: Update cycle.md remaining `~/.canopy/pm/` prose mentions**

Find the comment block around line 240 that says `~/.canopy/pm/<project>/    ← per-machine, outlives any worktree`. Replace with:

```
<repo>/.canopy/pm/                  ← committed to the project repo, portable across machines
```

Find the legacy `sent-emails` mention around line 262 (`If a project has a legacy ~/.canopy/pm/<project>/sent-emails/ ...`) and update the path to `<repo>/.canopy/pm/sent-emails/` if it exists in the new location, OR keep it as `~/.canopy/pm/<project>/sent-emails/` since it refers specifically to the *legacy* location. (Keep as-is — the prose is about the legacy location, which is correct.)

Run: `grep -n "CANOPY_PM_PROJECT" plugins/canopy/skills/product-management/templates/autonomous/cycle.md`
Expected: no matches.

- [ ] **Step 4: Update config-schema.md header**

In `templates/autonomous/config-schema.md` line 3, change the opening sentence:

From:
```
This file lives at `~/.canopy/pm/<project>/autonomous.yaml` for any project that adopts the autonomous mode of `canopy:product-management`. The `<project>` part is `basename` of the repo root (resolved as `$CANOPY_PM_DIR` in Phase 0). If it's missing on first run, ...
```

To:
```
This file lives at `<repo>/.canopy/pm/autonomous.yaml` for any project that adopts the autonomous mode of `canopy:product-management`. PM resolves the path via `scripts/resolve_pm_dir.sh` (committed source-controlled state, portable across machines). If it's missing on first run, ...
```

- [ ] **Step 5: Update config-schema.md validation snippet**

Find the bash block under "## Validation" (around line 36-41). Replace with:

```bash
PLUGIN_PATH=$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")
CANOPY_PM_DIR=$(bash "$PLUGIN_PATH/skills/product-management/scripts/resolve_pm_dir.sh")
uv run --script "$PLUGIN_PATH/skills/product-management/scripts/validate_autonomous_config.py" "$CANOPY_PM_DIR/autonomous.yaml"
```

Run: `grep -n "CANOPY_PM_PROJECT" plugins/canopy/skills/product-management/templates/autonomous/config-schema.md`
Expected: no matches.

- [ ] **Step 6: Commit**

```bash
git add plugins/canopy/skills/product-management/templates/autonomous/cycle.md plugins/canopy/skills/product-management/templates/autonomous/config-schema.md
git commit -m "docs(pm): autonomous templates — switch path resolution to resolve_pm_dir.sh"
```

---

## Task 5: Update agent and command files

**Files:**
- Modify: `plugins/canopy/agents/pm-supervisor.md`
- Modify: `plugins/canopy/commands/pm-status.md`
- Modify: `plugins/canopy/commands/pm-scout.md`
- Modify: `plugins/canopy/commands/pm-autonomous.md`

- [ ] **Step 1: Update pm-supervisor.md**

In `plugins/canopy/agents/pm-supervisor.md`, find line 14:

```markdown
2. Resolve `CANOPY_PM_DIR="$HOME/.canopy/pm/$(basename "$(git rev-parse --show-toplevel)")"` then read `$CANOPY_PM_DIR/context.md` for project context (bootstrap if it doesn't exist)
```

Replace with:

```markdown
2. Resolve the PM state dir by running:
   ```bash
   PLUGIN_PATH=$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")
   CANOPY_PM_DIR=$(bash "$PLUGIN_PATH/skills/product-management/scripts/resolve_pm_dir.sh")
   ```
   Then read `$CANOPY_PM_DIR/context.md` for project context (bootstrap if it doesn't exist).
```

- [ ] **Step 2: Update pm-status.md**

In `plugins/canopy/commands/pm-status.md`, find line 15:

```markdown
   CANOPY_PM_DIR="$HOME/.canopy/pm/$(basename "$(git rev-parse --show-toplevel)")"
```

Replace with:

```markdown
   PLUGIN_PATH=$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")
   CANOPY_PM_DIR=$(bash "$PLUGIN_PATH/skills/product-management/scripts/resolve_pm_dir.sh")
```

- [ ] **Step 3: Update pm-scout.md**

In `plugins/canopy/commands/pm-scout.md`, find line 13. The text contains an inline bash snippet `CANOPY_PM_DIR="$HOME/.canopy/pm/$(basename "$(git rev-parse --show-toplevel)")"`. Replace that inline snippet with:

```
resolve `CANOPY_PM_DIR=$(bash "$PLUGIN_PATH/skills/product-management/scripts/resolve_pm_dir.sh")` (after computing `PLUGIN_PATH` from `~/.claude/plugins/installed_plugins.json`)
```

- [ ] **Step 4: Update pm-autonomous.md prose**

In `plugins/canopy/commands/pm-autonomous.md`:
- Line 2 (frontmatter `description`): change `~/.canopy/pm/<project>/autonomous.yaml` to `<repo>/.canopy/pm/autonomous.yaml`.
- Line 10: same change in the body prose.
- Line 36: change `Fix ~/.canopy/pm/<project>/autonomous.yaml and try again.` to `Fix <repo>/.canopy/pm/autonomous.yaml and try again.`
- Line 37: change `No ~/.canopy/pm/<project>/context.md → bootstrap interactively first ...` to `No <repo>/.canopy/pm/context.md → bootstrap interactively first ...`

- [ ] **Step 5: Verify all PM files are clean**

Run: `grep -rn "~/.canopy/pm\|CANOPY_PM_PROJECT" plugins/canopy/skills/product-management/ plugins/canopy/agents/pm-supervisor.md plugins/canopy/commands/pm-*.md`

Expected: only matches inside the `resolve_pm_dir.sh` script (which legitimately references the legacy path during migration) and in `validate_autonomous_config.py`'s docstring (will fix in Task 6).

- [ ] **Step 6: Commit**

```bash
git add plugins/canopy/agents/pm-supervisor.md plugins/canopy/commands/pm-status.md plugins/canopy/commands/pm-scout.md plugins/canopy/commands/pm-autonomous.md
git commit -m "docs(pm): agent + commands — switch path resolution to resolve_pm_dir.sh"
```

---

## Task 6: Update validate_autonomous_config.py docstring

**Files:**
- Modify: `plugins/canopy/skills/product-management/scripts/validate_autonomous_config.py:5`

- [ ] **Step 1: Update the docstring**

In `plugins/canopy/skills/product-management/scripts/validate_autonomous_config.py`, change line 5:

From:
```python
"""Validate `~/.canopy/pm/<project>/autonomous.yaml` (spec §2, Phase 0).
```

To:
```python
"""Validate `<repo>/.canopy/pm/autonomous.yaml` (spec §2, Phase 0).
```

- [ ] **Step 2: Confirm no other code-level references to the legacy path**

Run: `grep -n "\.canopy/pm" plugins/canopy/skills/product-management/scripts/validate_autonomous_config.py`
Expected: only the updated docstring line.

- [ ] **Step 3: Run existing tests to confirm no regressions**

Run: `uv run pytest tests/ -x -q`
Expected: all tests pass (notably `test_pm_state_path_resolution.py` and `test_pm_state_migration.py`).

- [ ] **Step 4: Commit**

```bash
git add plugins/canopy/skills/product-management/scripts/validate_autonomous_config.py
git commit -m "docs(pm): validator docstring — refresh path reference"
```

---

## Task 7: Update CLAUDE.md and bump version

**Files:**
- Modify: `.claude/CLAUDE.md`
- Modify: `VERSION`
- Modify: `plugins/canopy/.claude-plugin/plugin.json`

- [ ] **Step 1: Add `.canopy/pm/` reference to CLAUDE.md**

In `.claude/CLAUDE.md`, insert a new section **between** the existing `## Plugin Updates — NEVER locally patch` section (ends around line 297) and `## Testing` (line 298). Add:

```markdown
## Per-Project Canopy State

Per-project canopy state lives at `<repo>/.canopy/`, committed to the project's git repo:

- `<repo>/.canopy/pm/` — `canopy:product-management` state (autonomous.yaml, context.md, learnings.md, runs/). Resolved via `plugins/canopy/skills/product-management/scripts/resolve_pm_dir.sh` from any PM markdown file or agent.
- `<repo>/.canopy/lenses/` — per-project lens descriptors (PR #37).
- `<repo>/.canopy/run-artifacts.yaml`, `<repo>/.canopy/README.md` — project run-artifact map and onboarding.

Outside a git repo, the PM resolver falls back to `$HOME/.canopy/pm/<basename-of-cwd>/`.

The global "self-improvement brain" (`~/.claude/canopy/observations/`, `proposals/`, `session-log.jsonl`, etc.) stays under `$HOME/.claude/canopy/` — that data is intentionally cross-project on a single machine.
```

- [ ] **Step 2: Determine the next patch version**

Run: `bash -c 'cd /Users/acedimagi/emdash/worktrees/canopy/emdash/projects-vkspe && uv run canopy version verify'`
Expected: confirms VERSION and plugin.json agree (currently both `0.2.80`).

Run: `uv run canopy version bump`
Expected: prints the new version (e.g. `0.2.81`) and updates both files. (This wraps the "max(local, origin/main) + patch+1" logic per CLAUDE.md.)

If `uv run canopy version bump` fails for any reason, manually edit:
- `VERSION` → `0.2.81`
- `plugins/canopy/.claude-plugin/plugin.json` → `"version": "0.2.81"`

- [ ] **Step 3: Verify the bump applied**

Run: `cat VERSION && grep '"version"' plugins/canopy/.claude-plugin/plugin.json`
Expected: both show the same new version (`0.2.81` or whatever the bump command produced).

Run: `uv run canopy version verify`
Expected: success.

- [ ] **Step 4: Commit**

```bash
git add .claude/CLAUDE.md VERSION plugins/canopy/.claude-plugin/plugin.json
git commit -m "feat(pm): relocate state to <repo>/.canopy/pm/, bump version"
```

---

## Task 8: Final verification + branch hygiene

**Files:** none (read-only checks)

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest tests/ -q`
Expected: all tests pass (the existing 420 tests plus ~13 new ones from Tasks 1 & 2).

- [ ] **Step 2: Confirm the resolver works in this very repo**

Run:
```bash
PLUGIN_PATH=$(python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json'))); print(d['plugins']['canopy@canopy'][0]['installPath'])")
bash "$PLUGIN_PATH/skills/product-management/scripts/resolve_pm_dir.sh"
```
**WAIT — this will read from the *installed* plugin cache, which still has the old version. Use the worktree's copy instead:**

```bash
bash plugins/canopy/skills/product-management/scripts/resolve_pm_dir.sh
```
Expected: prints the path to `<this-worktree>/.canopy/pm` and creates the dir. (Since this canopy repo doesn't have legacy `~/.canopy/pm/canopy/` state, no migration commit fires.)

- [ ] **Step 3: Confirm grep cleanliness**

Run:
```bash
grep -rn "CANOPY_PM_PROJECT" plugins/canopy/ .claude/CLAUDE.md docs/ tests/ 2>/dev/null
```
Expected: no matches except possibly in the spec/plan docs themselves, which legitimately reference the old name.

Run:
```bash
grep -rn 'HOME/.canopy/pm' plugins/canopy/ 2>/dev/null
```
Expected: matches ONLY in `resolve_pm_dir.sh` (legacy migration source) and in prose strings about the legacy path (the script's own messages). No reference inside SKILL.md, cycle.md, config-schema.md, or any agent/command file.

- [ ] **Step 4: Push the branch and prepare PR**

```bash
git push -u origin emdash/projects-vkspe
gh pr create --title "feat(pm): relocate PM state to <repo>/.canopy/pm/" --body "$(cat <<'EOF'
## Summary

Moves `canopy:product-management` per-project state from `~/.canopy/pm/<project>/` (per-machine, per-account) into `<repo>/.canopy/pm/` (committed to git, portable across machines and accounts). Plugs into the `.canopy/` namespace established by PR #37.

## What changed

- New: `plugins/canopy/skills/product-management/scripts/resolve_pm_dir.sh` — single canonical resolver. Returns `<repo>/.canopy/pm` inside a repo, `~/.canopy/pm/<basename>` outside one.
- New: one-shot, idempotent migration baked into the resolver. First run on a project copies legacy state in, commits it on the current branch, drops a `.migrated` marker.
- All PM markdown files (SKILL.md, cycle.md, config-schema.md, pm-supervisor.md, pm-status.md, pm-scout.md, pm-autonomous.md) switch to invoking the resolver. `CANOPY_PM_PROJECT` (the origin-URL-derived path key) is gone.
- Tests: 13 new tests across `tests/test_pm_state_path_resolution.py` and `tests/test_pm_state_migration.py`.
- VERSION bumped per `plugins/canopy/` change rule.

## Test plan

- [ ] After merge, run `/canopy:update` then `/reload-plugins`
- [ ] In a project with legacy `~/.canopy/pm/<project>/` state, run `/canopy:pm-status` — verify auto-migration fires once with a `chore(canopy-pm): migrate state from …` commit on the current branch
- [ ] Re-run `/canopy:pm-status` — verify no second migration commit (idempotent)
- [ ] On a second mac account, `git pull` and run `/canopy:pm-status` — verify the migrated state is visible without any local setup

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Expected: PR URL printed.

---

## Notes for the implementer

- **Don't push until Task 8.** Each task's commit stays local; the branch pushes only at the end.
- **VERSION bump must be in the SAME PR.** The CI check (PR #36's version-bump-check) fails if `plugins/canopy/` changes without a VERSION bump.
- **No new dependencies.** `pyaml` is already in the project; `bash`, `git`, `find`, `cp`, `sed` are all stdlib of macOS/Linux.
- **The resolver script intentionally swallows commit failures.** If `git commit` fails during migration (no identity, hook rejects), the script logs to stderr and continues. This is by design — migration is best-effort, the user can `git status` and commit manually.
- **The fallback case (outside git repo) is rare but real.** Some users may run PM-style operations in scratch dirs. Don't crash there; the home-dir fallback preserves today's behavior.
- **The `.git/canopy/` shared-state idea was rejected during brainstorming.** Don't reintroduce it — the simple "treat `.canopy/pm/` as committed source" model is the agreed answer.
