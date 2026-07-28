"""How much of the page a scene's snapshot should cover.

Pure decision logic, deliberately kept out of ``record_video`` — that module
imports playwright at load time, so anything defined there is unreachable from a
test on a machine without a browser. A rule that decides what every judge looks
at should not be one of the untestable parts.
"""
from __future__ import annotations

from typing import Any


def snapshot_full_page(scene: dict, args: Any) -> bool | None:
    """Per-scene capture mode, with the DDD loop defaulting to the VIEWPORT.

    A full-page strip is not what a user sees. Every judge in the first
    four-way dispatch opened by saying so — "this is a 1280x2748 strip; a user
    never sees this composite" — and each then discounted it by hand before
    scoring. That is judge attention spent on an artifact of our capture, on
    every scene, of every round.

    An explicit ``full_page`` on the scene always wins. Absent one, a
    ``--ddd-orchestrated`` run captures the viewport — what the viewer would
    have on screen after the scene's own scrolls — and every other caller keeps
    the historical full-page default, so existing narratives are untouched.
    ``--full-page-snapshots`` restores the strip for a DDD run that wants it.

    Returns None to mean "no override", which the orchestrator reads as
    full-page.
    """
    explicit = scene.get("full_page")
    if explicit is not None:
        return bool(explicit)
    if getattr(args, "full_page_snapshots", False):
        return True
    if getattr(args, "ddd_orchestrated", False):
        return False
    return None
