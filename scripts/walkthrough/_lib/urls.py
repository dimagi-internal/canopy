"""One answer to "is the browser already showing this URL?".

Two callers need it and they must agree, or preflight passes recipes the
recorder breaks and fails recipes the recorder renders fine:

  * :class:`~scripts.walkthrough._lib.orchestrator.SkipSameUrlRecorder`, deciding
    whether to re-navigate for a scene (a re-navigation wipes the JS state the
    previous scene built — an open modal, a resolved table, picked checkboxes);
  * :mod:`scripts.ddd.recipe_preflight`, walking the same recipe ahead of a
    render to say whether its selectors will resolve.

It lives here rather than in ``orchestrator`` because that module imports
playwright at module scope, which made the shared rule untestable without a
browser toolchain — and an untested comparison between two subsystems is how
they drift apart in the first place.
"""
from __future__ import annotations


def normalize_url(u: str) -> str:
    """Compare-friendly URL: strip the fragment and any trailing slash.

    Deliberately does NOT strip the query string. ``?persona=ada`` and
    ``?persona=amina`` are different destinations — collapsing them would make
    a persona switch look like a no-op and leave the walkthrough logged in as
    the wrong user for the rest of the scene.
    """
    return (u or "").split("#")[0].rstrip("/")
