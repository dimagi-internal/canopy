"""Back-compat shim — a finished run's summary is :mod:`scripts.ddd.run_summary` now.

Until 2026-10-16 this module appended a block per run to
``<repo>/.canopy/ddd/learnings.md`` (canopy#788). That file is retired (it did
not reach the next run across machines or ACE's per-run DDD dirs, and product
lessons in it went stale — see :mod:`scripts.ddd.run_summary`). The summary is
now ``run-summary.json`` + ``.md`` in the run dir and part of the run document
on canopy-web; owned findings go to their owners as issues.

``python -m scripts.ddd.run_learnings <run_id>`` still works: it (re)writes the
run summary, exactly like ``python -m scripts.ddd.run_summary <run_id>``.
Nothing writes ``learnings.md`` any more.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from scripts.ddd.run_summary import ends_run  # noqa: F401 — re-exported for old callers


def summarize(state: Any) -> str:
    """The run's summary as markdown (built from run state, no routing)."""
    from scripts.ddd import run_summary

    return run_summary.markdown(run_summary.build(state))


def record(state: Any, ddd_dir: str | Path) -> Path:
    """Write the run summary into the run's dir; returns ``run-summary.json``.

    Kept so an old caller does not break; it no longer touches ``learnings.md``.
    """
    from scripts.ddd import run_summary
    from scripts.ddd.runstate import _run_dir_for

    run_dir = _run_dir_for(Path(ddd_dir), getattr(state, "run_id", "?"))
    run_summary.ensure(state, run_dir)
    return run_summary.write(state, run_dir)[0]


def _main(argv: list[str] | None = None) -> int:
    from scripts.ddd import run_summary

    return run_summary._main(argv)


if __name__ == "__main__":
    sys.exit(_main())
