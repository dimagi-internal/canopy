"""On YOUR private runner, your local sessions sign in with the box's own key — no PAT.

canopy-web#1705. When this machine's runner is your private runner (canopy-web marked it
private and the runner wrote ``~/.canopy/private-runner.json`` naming itself), the
calls a local session makes as you — opening, renewing and closing its local turn, and
reading the agent's credentials under your license — use the runner's own token
(`runner_session.token`, bought with this machine's approved device key). There is no
personal access token to expire unnoticed (jj-mbp-cdp's did, 2026-10-20). canopy-web judges
the session as you, the runner's owner — capped only when the turn or the client asks
for a lower role (a local turn opened with `acts_as`, or `CANOPY_ACTS_AS`; canopy-web#1756),
never by a setting on the runner. Anything else uses your PAT.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def _read(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def linked_runner_id() -> str:
    """This machine's runner id when it is the private runner the registration names,
    else "" (no runner, a shared runner, or a person's own `register` with no link)."""
    from orchestrator.runner_pin import _machine_runner_id

    named = str(_read(Path.home() / ".canopy" / "private-runner.json").get("runner_id") or "")
    return named if named and named == _machine_runner_id() else ""


def token(*, mint=None) -> str:
    """The runner token your local sessions sign in with, or "" when this machine is not
    your linked private runner or the box cannot get one (no approved key). Never raises."""
    if not linked_runner_id():
        return ""
    from orchestrator import canopy_web, runner_session

    mint = mint or runner_session.token
    try:
        return mint(canopy_web.resolve_base_url(None)) or ""
    except Exception:  # noqa: BLE001 — no token: the caller falls back to the PAT
        return ""


def in_local_turn() -> bool:
    """This process belongs to a local session's turn (the SessionStart hook exports it)."""
    return os.environ.get("CANOPY_LOCAL_TURN", "").strip() == "1"
