"""DDD run-state lifecycle helpers (SP0.4).

Public API
----------
new_run(narrative_slug: str, ddd_dir: Path | None = None) -> str
    Creates a new run directory + run_state.yaml, returns the run_id.

load(run_id: str, ddd_dir: Path | None = None) -> RunState
    Loads and returns a RunState from disk.

save(state: RunState, ddd_dir: Path | None = None) -> None
    Persists a RunState back to disk (overwrites run_state.yaml).

When ``ddd_dir`` is passed it is used directly, bypassing _resolve_ddd_dir().

append_learning(text: str) -> None
    Appends a learning entry to <ddd_dir>/learnings.md.

The DDD directory is resolved by _resolve_ddd_dir() in this precedence order:
  1. explicit ``ddd_dir`` arg on load/save/new_run — used directly, no resolution
  2. ``repo_root`` arg to _resolve_ddd_dir → <repo_root>/.canopy/ddd/
  3. ``DDD_DIR`` env var → used directly
  4. git toplevel of cwd → <repo-root>/.canopy/ddd/
  5. fallback (git absent / not a repo) → $HOME/.canopy/ddd/<cwd-basename>/

Per-RUN artifacts do NOT live under that directory. _resolve_runs_dir() puts them
outside the project repo ($HOME/.canopy/ddd/runs/<project>/) because run dirs
accumulate large generated files and were silently bloating project repos. Only
context.md / learnings.md remain repo-local. Runs created before this change are
still read (and kept) in the legacy in-repo location — see _run_dir_for().
"""

from __future__ import annotations

import os
import subprocess
from datetime import date
from pathlib import Path

import yaml

from scripts.ddd.schemas.models import RunState


# ---------------------------------------------------------------------------
# Dir resolver (shells out to git; keep in sync with resolve_ddd_dir.sh)
# ---------------------------------------------------------------------------


def _resolve_ddd_dir(repo_root: Path | None = None) -> Path:
    """Return (and create) the canonical DDD state directory.

    Resolution order (highest precedence first):
      1. ``repo_root`` arg → ``<repo_root>/.canopy/ddd`` (decouples from cwd; the
         caller already knows the repo root, so don't shell out to git).
      2. ``DDD_DIR`` env var → used directly (an explicit operator override).
      3. git toplevel of cwd → ``<repo-root>/.canopy/ddd``.
      4. fallback (git absent / not a repo) → ``$HOME/.canopy/ddd/<cwd-basename>``.

    The cwd-based branches (3/4) mirror resolve_ddd_dir.sh — keep the two in
    sync when changing the fallback path or git invocation.
    """
    if repo_root is not None:
        ddd_dir = Path(repo_root) / ".canopy" / "ddd"
        ddd_dir.mkdir(parents=True, exist_ok=True)
        return ddd_dir

    env_dir = os.environ.get("DDD_DIR", "").strip()
    if env_dir:
        ddd_dir = Path(env_dir)
        ddd_dir.mkdir(parents=True, exist_ok=True)
        return ddd_dir

    try:
        toplevel = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        ddd_dir = Path(toplevel) / ".canopy" / "ddd"
    except (subprocess.CalledProcessError, FileNotFoundError):
        # CalledProcessError: git ran but we're not in a repo
        # FileNotFoundError: git is not installed / not on PATH
        cwd_name = Path(os.getcwd()).name
        ddd_dir = Path.home() / ".canopy" / "ddd" / cwd_name

    ddd_dir.mkdir(parents=True, exist_ok=True)
    return ddd_dir


# ---------------------------------------------------------------------------
# Run-ID generation
# ---------------------------------------------------------------------------


def _resolve_runs_dir(ddd_dir: Path) -> Path:
    """Return (and create) the root that per-run artifacts are written under.

    OUTSIDE the project repo by default. A run dir accumulates large,
    machine-generated artifacts — decks that inline their own images, per-scene
    page dumps, rendered clips — and writing them under
    ``<repo>/.canopy/ddd/runs/`` grows the project repo on every DDD cycle.
    connect-labs reached 107MB of tracked run artifacts exactly that way: no one
    decided to commit them, each author just followed the previous one's
    precedent, and .gitignore covered mp4/webm/png while the two biggest
    offenders were a .html and a .json.

    ``context.md`` / ``learnings.md`` deliberately STAY repo-local — they are
    small, durable, and belong in the project's history. Only ``runs/`` moves.

    Precedence:
      1. ``CANOPY_DDD_RUNS_DIR`` env var → used directly (operator override).
      2. ``ddd_dir`` already outside a repo (the $HOME fallback) → ``<ddd_dir>/runs``.
      3. otherwise → ``$HOME/.canopy/ddd/runs/<project-name>``.
    """
    env_dir = os.environ.get("CANOPY_DDD_RUNS_DIR", "").strip()
    if env_dir:
        runs_dir = Path(env_dir)
    else:
        repo_root = _enclosing_repo(ddd_dir)
        if repo_root is None:
            # Not inside a repo — an explicit/operator dir or the $HOME
            # fallback. Nothing to protect, so keep runs alongside it.
            runs_dir = ddd_dir / "runs"
        else:
            runs_dir = Path.home() / ".canopy" / "ddd" / "runs" / repo_root.name
    runs_dir.mkdir(parents=True, exist_ok=True)
    return runs_dir


def _enclosing_repo(path: Path) -> Path | None:
    """The git work-tree root containing *path*, or None.

    Checked with .exists() rather than .is_dir(): in a git worktree or submodule
    `.git` is a FILE pointing at the real gitdir, and treating those as "not a
    repo" would put run artifacts right back inside the checkout.
    """
    for parent in [path, *path.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def _legacy_runs_dir(ddd_dir: Path) -> Path:
    """The pre-2026-07 in-repo location. Read-only compatibility."""
    return ddd_dir / "runs"


def _run_dir_for(ddd_dir: Path, run_id: str) -> Path:
    """Resolve ONE run's directory.

    A run that already exists in the legacy in-repo location keeps living there,
    so resuming or re-saving an in-flight run never splits its artifacts across
    two roots. Only genuinely new runs land in the external root.
    """
    legacy = _legacy_runs_dir(ddd_dir) / run_id
    if legacy.exists():
        return legacy
    return _resolve_runs_dir(ddd_dir) / run_id


def _next_run_id(runs_dir: Path | list[Path], narrative_slug: str) -> str:
    """Return the next available run_id of the form <narrative_slug>-<YYYY-MM-DD>-NNN.

    Accepts SEVERAL roots and takes the max across all of them. Runs now live
    outside the repo while older ones remain in the legacy in-repo dir, so
    numbering off a single root would re-mint an id that already exists — and
    load() would then resolve to the wrong run.
    """
    today = date.today().strftime("%Y-%m-%d")
    prefix = f"{narrative_slug}-{today}-"

    roots = [runs_dir] if isinstance(runs_dir, Path) else list(runs_dir)
    existing = [
        d.name
        for root in roots
        if root.is_dir()
        for d in root.iterdir()
        if d.is_dir() and d.name.startswith(prefix)
    ]
    nums = []
    for name in existing:
        suffix = name[len(prefix):]
        if suffix.isdigit():
            nums.append(int(suffix))

    next_num = (max(nums) + 1) if nums else 1
    return f"{prefix}{next_num:03d}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def runs_dir(ddd_dir: Path | None = None) -> Path:
    """Public: the root new run artifacts are written under (outside the repo).

    Callers that scan for runs should also consult ``legacy_runs_dir()`` — runs
    created before the split still live in the project repo.
    """
    return _resolve_runs_dir(ddd_dir if ddd_dir is not None else _resolve_ddd_dir())


def legacy_runs_dir(ddd_dir: Path | None = None) -> Path:
    """Public: the pre-split in-repo location, for read/scan compatibility."""
    return _legacy_runs_dir(ddd_dir if ddd_dir is not None else _resolve_ddd_dir())


def new_run(narrative_slug: str, ddd_dir: Path | None = None) -> str:
    """Create a new run under the runs root (see _resolve_runs_dir) and return the run_id.

    If *ddd_dir* is given it is used directly (no cwd/git resolution).
    """
    if ddd_dir is None:
        ddd_dir = _resolve_ddd_dir()
    runs_dir = _resolve_runs_dir(ddd_dir)

    run_id = _next_run_id([runs_dir, _legacy_runs_dir(ddd_dir)], narrative_slug)
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    state = RunState(run_id=run_id, narrative_slug=narrative_slug)
    _write_state(run_dir, state)

    return run_id


def load(run_id: str, ddd_dir: Path | None = None) -> RunState:
    """Load a RunState from <run_dir>/run_state.yaml (external root, or legacy in-repo).

    If *ddd_dir* is given it is used directly (no cwd/git resolution).
    """
    if ddd_dir is None:
        ddd_dir = _resolve_ddd_dir()
    state_file = _run_dir_for(ddd_dir, run_id) / "run_state.yaml"
    raw = yaml.safe_load(state_file.read_text())
    return RunState.model_validate(raw)


def save(state: RunState, ddd_dir: Path | None = None) -> None:
    """Persist *state* to <run_dir>/run_state.yaml (external root, or legacy in-repo).

    If *ddd_dir* is given it is used directly (no cwd/git resolution).
    """
    if ddd_dir is None:
        ddd_dir = _resolve_ddd_dir()
    run_dir = _run_dir_for(ddd_dir, state.run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_state(run_dir, state)


def append_learning(text: str) -> None:
    """Append *text* as a new bullet to <ddd_dir>/learnings.md."""
    ddd_dir = _resolve_ddd_dir()
    learnings_file = ddd_dir / "learnings.md"

    from datetime import datetime, timezone

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = f"- [{timestamp}] {text}\n"

    with learnings_file.open("a", encoding="utf-8") as fh:
        fh.write(entry)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _write_state(run_dir: Path, state: RunState) -> None:
    state_file = run_dir / "run_state.yaml"
    # Dump via model_dump to get Python-native types (no pydantic objects)
    data = state.model_dump()
    # Write-back contract (canopy#265 item 4): Pydantic v2 does not validate on
    # assignment, so an in-place mutation can break the schema silently. Validate
    # the dumped dict BEFORE touching the file — a bad state must never reach
    # disk, or the next load() fails and resume breaks.
    RunState.model_validate(data)
    state_file.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True))
