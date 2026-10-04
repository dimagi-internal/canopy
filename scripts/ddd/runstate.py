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
import sys
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

    The project name is the MAIN repo's name, never the worktree's — see
    ``_repo_identity``.
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
            runs_dir = Path.home() / ".canopy" / "ddd" / "runs" / _repo_identity(repo_root)
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


def _repo_identity(repo_root: Path) -> str:
    """The name keying this repo's runs root — the MAIN checkout's name.

    ``_enclosing_repo`` stops at the first path with a ``.git``, which in a
    ``git worktree add`` checkout is the WORKTREE root. Keying the runs root on
    its basename gave every worktree a private, empty runs dir: the harness
    hands agents temp-named worktrees, so ``score_history`` silently restarted
    at zero on each one. That does not merely lose history — ``compute_auto_iterate``
    reads the series to decide stall/plateau/convergence, so an empty one changes
    the loop's TERMINAL VERDICT, not just its bookkeeping.

    In a worktree, ``.git`` is a FILE reading ``gitdir: <main>/.git/worktrees/<name>``,
    so the main root is recoverable by pure path parsing — no subprocess, which
    keeps this unit-testable and safe to call when git is absent. Falls back to
    the basename whenever the file is missing, unreadable, or not in that shape,
    because a slightly-wrong key beats an exception in a path resolver.
    """
    dot_git = repo_root / ".git"
    if dot_git.is_dir():
        return repo_root.name          # ordinary checkout: already the main root
    try:
        content = dot_git.read_text().strip()
    except OSError:
        return repo_root.name
    if not content.startswith("gitdir:"):
        return repo_root.name
    gitdir = Path(content.split("gitdir:", 1)[1].strip())
    # <main>/.git/worktrees/<name>  ->  <main>
    parts = gitdir.parts
    if "worktrees" in parts:
        idx = parts.index("worktrees")
        if idx >= 2 and parts[idx - 1] == ".git":
            return Path(*parts[: idx - 1]).name
    return repo_root.name


def _legacy_runs_dir(ddd_dir: Path) -> Path:
    """The pre-2026-07 in-repo location. Read-only compatibility."""
    return ddd_dir / "runs"


# Files only a render/judge writes. A legacy in-repo run dir holding any of these
# is an in-flight run that already rendered there, and keeps living there so its
# artifacts are never split across two roots. One holding only Phase-0 files
# (evidence, why-brief, narrative agreement — typically COMMITTED to the target
# repo) has not rendered yet.
_RENDER_MARKERS = (
    "run-report.json",
    "walkthrough-run-data.json",
    "verdict-concept.yaml",
    "verdict-user.yaml",
    "snapshots",
    "passes",
    "judge-cache",
)


def _has_rendered(run_dir: Path) -> bool:
    return any((run_dir / m).exists() for m in _RENDER_MARKERS)


def _run_dir_for(ddd_dir: Path, run_id: str) -> Path:
    """Resolve ONE run's directory.

    * The external root wins whenever the run already exists there.
    * A legacy in-repo run that has RENDERED keeps living there, so resuming an
      in-flight pre-2026-07 run never splits its artifacts across two roots.
    * A legacy in-repo run that holds only Phase-0 files is MIGRATED: its files
      are copied (never moved — they may be committed to the target repo) into
      the external root, and the run continues there. Before this, a Phase 0
      committed to ``.canopy/ddd/runs/<run_id>/`` pulled every render artifact
      (snapshots, clips, decks, judge passes) into the target repo (0.2.528, M1).
    * Otherwise: the external root.
    """
    external = _resolve_runs_dir(ddd_dir) / run_id
    if external.exists():
        return external
    legacy = _legacy_runs_dir(ddd_dir) / run_id
    if legacy.is_dir():
        if _has_rendered(legacy):
            return legacy
        _migrate_phase0(legacy, external)
        return external
    return external


def _migrate_phase0(legacy: Path, external: Path) -> None:
    """Copy a Phase-0-only legacy run dir into the external root (idempotent)."""
    import shutil

    external.parent.mkdir(parents=True, exist_ok=True)
    tmp = external.with_name(external.name + ".migrating")
    if tmp.exists():
        shutil.rmtree(tmp)
    shutil.copytree(legacy, tmp)
    tmp.rename(external)
    print(
        f"[ddd] run {legacy.name}: Phase-0 files copied from the repo ({legacy}) to the "
        f"external runs root ({external}); render artifacts will land there. The repo "
        "copy is left untouched.",
        file=sys.stderr,
    )


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


def run_dir_for(run_id: str, ddd_dir: Path | None = None) -> Path:
    """Public: THE way to resolve one run's directory. Use this, never a join.

    ``load``/``save`` have always gone through ``_run_dir_for``, which checks the
    legacy in-repo location before the external root. Three callers instead built
    ``ddd_dir / "runs" / run_id`` by hand, which is neither: it misses the project
    segment the external root carries, so every one of them raised
    FileNotFoundError on any run created after the split. ``ddd-upload`` could not
    open a run that ``new_run`` had just made.
    """
    return _run_dir_for(ddd_dir if ddd_dir is not None else _resolve_ddd_dir(), run_id)


def new_run(
    narrative_slug: str,
    ddd_dir: Path | None = None,
    *,
    agent: str | None = None,
    project: str | None = None,
) -> str:
    """Create a new run and return the run_id.

    With canopy-web reachable (``scripts.ddd.run_store``) the run is a document on
    an AGENT's PROJECT and canopy-web mints the id. The project is ``agent`` /
    ``project`` when given; else the narrative's existing binding (the project of
    its newest run); else, unattended inside an agent's turn, a new project of
    ``$CANOPY_AGENT_SLUG``; else :class:`run_store.NeedsBinding` — the human
    says which agent owns the work. Local-only otherwise, as before.

    If *ddd_dir* is given it is used directly (no cwd/git resolution).
    """
    from scripts.ddd import run_store

    if ddd_dir is None:
        ddd_dir = _resolve_ddd_dir()
    runs_dir = _resolve_runs_dir(ddd_dir)
    local_next = _next_run_id([runs_dir, _legacy_runs_dir(ddd_dir)], narrative_slug)

    store = None
    run_id = local_next
    if run_store.enabled(ddd_dir):
        try:
            if not agent:
                agent, project = _bind(narrative_slug, ddd_dir)
            doc = run_store.mint(
                narrative_slug, agent=agent, project=project, min_seq=int(local_next.rsplit("-", 1)[1])
            )
            run_id = doc["ext_id"]
            store = {
                "agent": agent, "project": project, "version": doc.get("state_version", 0),
                "synced_at": _now_iso(), "pending": False, "error": None,
            }
        except run_store.NeedsBinding:
            raise  # the human's call — never guessed, never silently local
        except run_store.RunStoreError as exc:
            # canopy-web unreachable or not yet serving run documents: the run
            # still starts, LOCAL-ONLY, and says so. A later save adopts it once
            # its narrative is bound and canopy-web answers.
            print(
                f"[ddd run store] could not start {narrative_slug} on canopy-web ({exc}); "
                f"starting LOCAL-ONLY run {local_next}.",
                file=sys.stderr,
            )
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    state = RunState(run_id=run_id, narrative_slug=narrative_slug, store=store)
    # Pin the canopy version this run starts on (M7/M18) — every later call
    # resolves the runtime by this path (scripts.ddd.pin).
    try:
        from scripts.ddd import pin

        pin.ensure(state)
    except Exception:  # pinning must never stop a run from starting
        pass
    _write_state(run_dir, state)
    if store is not None:
        _push(state, run_dir)

    return run_id


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _bind(narrative_slug: str, ddd_dir: Path) -> tuple[str, str | None]:
    """The agent + project a new run of *narrative_slug* belongs to."""
    from scripts.ddd import run_store

    found = run_store.resolve(narrative_slug, ddd_dir)
    if found["status"] == "bound":
        return found["agent"], found["project"]
    hint = run_store.agent_hint()
    from scripts.ddd import gates

    if hint and gates.is_unattended():
        project = run_store.create_project(
            hint, narrative_slug, repo=found.get("repo") or "",
            outcome=f"DDD narrative {narrative_slug}",
        )
        print(
            f"[ddd run store] {narrative_slug}: unattended in {hint}'s turn — started project "
            f"{project} on {hint}. Rebind by starting the next run with `run_store start`.",
            file=sys.stderr,
        )
        return hint, project
    raise run_store.NeedsBinding(narrative_slug, found)

def load(run_id: str, ddd_dir: Path | None = None, *, sync: bool = True) -> RunState:
    """Load a run's state.

    canopy-web holds the run when it is bound (``state.store``): a run never seen
    on this machine is HYDRATED from it, and a local copy older than the web's
    ``state_version`` (another runner advanced the run) is replaced by the web's.
    Unreachable canopy-web -> the local copy, with a warning. ``sync=False`` reads
    the local file only.

    If *ddd_dir* is given it is used directly (no cwd/git resolution).
    """
    from scripts.ddd import run_store

    if ddd_dir is None:
        ddd_dir = _resolve_ddd_dir()
    state_file = _run_dir_for(ddd_dir, run_id) / "run_state.yaml"
    if not state_file.exists() and sync and run_store.enabled(ddd_dir):
        return hydrate(run_id, ddd_dir=ddd_dir)
    raw = yaml.safe_load(state_file.read_text())  # FileNotFoundError for an unknown run
    state = RunState.model_validate(raw)
    if sync and state.store and run_store.enabled(ddd_dir):
        try:
            remote = run_store.pull(run_id)
        except run_store.RunStoreError as exc:
            run_store._warn_once(f"pull:{run_id}", f"using the local copy of {run_id}: {exc}")
            return state
        if remote and int(remote.get("state_version") or 0) > int(state.store.get("version") or 0):
            print(
                f"[ddd run store] {run_id}: canopy-web is at state_version {remote['state_version']} "
                f"(written by {remote.get('holder') or 'unknown'}), this runner had "
                f"{state.store.get('version')} — taking the web copy.",
                file=sys.stderr,
            )
            return _adopt_remote(remote, state_file.parent)
    return state


def hydrate(run_id: str, ddd_dir: Path | None = None) -> RunState:
    """Write canopy-web's copy of *run_id* onto this runner and return it."""
    from scripts.ddd import run_store

    if ddd_dir is None:
        ddd_dir = _resolve_ddd_dir()
    remote = run_store.pull(run_id)
    if remote is None:
        raise FileNotFoundError(f"run {run_id} is neither on this runner nor on canopy-web")
    run_dir = _run_dir_for(ddd_dir, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    return _adopt_remote(remote, run_dir)


def _adopt_remote(remote: dict, run_dir: Path) -> RunState:
    state = RunState.model_validate(remote.get("state") or {})
    state.store = {
        "agent": remote.get("agent_slug"),
        "project": (remote.get("project") or {}).get("ext_id"),
        "version": int(remote.get("state_version") or 0),
        "synced_at": _now_iso(), "pending": False, "error": None,
    }
    _write_state(run_dir, state)
    return state


def peek_store(state_file: Path) -> dict | None:
    """The ``store`` block of a local run_state.yaml, without validating the rest."""
    if not state_file.exists():
        return None
    try:
        return (yaml.safe_load(state_file.read_text()) or {}).get("store") or {"bound": False}
    except Exception:
        return {"error": "unreadable"}


def save(state: RunState, ddd_dir: Path | None = None, *, force: bool = False) -> None:
    """Persist *state* locally and, when the run is bound, write it through to
    canopy-web (``state.store.version`` is the optimistic-concurrency base).

    A conflict (another runner advanced the run) raises
    :class:`run_store.RunConflict` BEFORE the local file is touched. An
    unreachable canopy-web leaves the local write in place, marks the store
    ``pending`` and says so; the next save retries.

    If *ddd_dir* is given it is used directly (no cwd/git resolution).
    """
    from scripts.ddd import run_store

    if ddd_dir is None:
        ddd_dir = _resolve_ddd_dir()
    run_dir = _run_dir_for(ddd_dir, state.run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    if run_store.enabled(ddd_dir):
        if not state.store:
            _try_adopt(state, ddd_dir)
        if state.store:
            _push(state, run_dir, force=force)
            return
    _write_state(run_dir, state)


def _try_adopt(state: RunState, ddd_dir: Path) -> None:
    """A run started before runs lived on canopy-web: adopt it under its own id
    into its narrative's project when the narrative is already bound; otherwise
    say how to bind it (once) and stay local — never break an in-flight run."""
    from scripts.ddd import run_store

    try:
        found = run_store.resolve(state.narrative_slug, ddd_dir)
        if found["status"] != "bound":
            run_store._warn_once(
                f"adopt:{state.run_id}",
                f"{state.run_id} is local-only: its narrative has no agent project yet. Bind it: "
                f"`python -m scripts.ddd.run_store adopt {state.run_id} --agent <agent> "
                "(--project <P> | --new-project <name>)`.",
            )
            return
        run_store.mint(
            state.narrative_slug, agent=found["agent"], project=found["project"], ext_id=state.run_id,
        )
        state.store = {"agent": found["agent"], "project": found["project"], "version": 0,
                       "synced_at": _now_iso(), "pending": False, "error": None}
    except run_store.RunStoreError as exc:
        run_store._warn_once(f"adopt:{state.run_id}", f"could not adopt {state.run_id}: {exc}")


def _push(state: RunState, run_dir: Path, *, force: bool = False) -> None:
    from scripts.ddd import run_store

    RunState.model_validate(state.model_dump())  # never push what could not be saved
    try:
        version = run_store.push(
            state.model_dump(mode="json"),
            base_version=int((state.store or {}).get("version") or 0),
            force=force,
        )
        state.store = {**(state.store or {}), "version": version, "synced_at": _now_iso(),
                       "pending": False, "error": None}
    except run_store.RunConflict:
        raise
    except run_store.RunStoreError as exc:
        state.store = {**(state.store or {}), "pending": True, "error": str(exc)[:300]}
        run_store._warn_once(
            f"push:{state.run_id}",
            f"could not write {state.run_id} to canopy-web ({exc}); saved locally, will retry on the next save.",
        )
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
