"""`canopy huddle-group …` — the old name of `canopy checkin …` (kept for one release).

A work group became a project check-in (canopy-web spec 2026-10-17-project-check-ins-design.md).
Everything lives in `checkin_cli`; this module re-exports it for callers that import the old
names.
"""
from orchestrator.checkin_cli import (  # noqa: F401
    ENDED,
    GROUPS_PATH,
    LEGACY_RUNS_PATH,
    RUNS_PATH,
    GroupRunError,
    already_finished,
    finish_run,
    get_run,
    huddle_group_group,
    list_runs,
)
