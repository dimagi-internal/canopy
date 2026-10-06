"""Discover git repos under ~/emdash/{worktrees,repositories} (and the legacy
~/emdash-projects) with recent activity that aren't in canopy-web's curated
project list.

Surfaced from the canopy:improve cycle observation that newly-active repos
(e.g. a freshly created `expense-helper`) stay invisible to the canopy
portfolio feed until manually registered. This module provides the
discovery half: scan local emdash roots, ask canopy-web which slugs are
already curated, and return the difference. Registration itself stays a
human-in-the-loop step on canopy-web.
"""
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestrator.provenance import with_provenance

EMDASH_ROOTS = [
    Path.home() / "emdash" / "worktrees",
    Path.home() / "emdash" / "repositories",
    Path.home() / "emdash-projects",
]


def discover_active_repos(
    roots: list[Path] | None = None,
    max_age_days: int = 30,
) -> list[dict]:
    """Find git repos under the given roots with a HEAD commit newer than the
    cutoff. Returns a list of {slug, path, last_commit} dicts sorted newest-first.

    Slugs are deduplicated across roots — when the same project name appears
    under both ~/emdash/worktrees/ and ~/emdash/repositories/ (the common case
    for emdash-managed checkouts), the first occurrence wins.
    """
    if roots is None:
        roots = EMDASH_ROOTS
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    found: dict[str, dict] = {}
    for root in roots:
        if not root.exists():
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if not (child / ".git").exists():
                continue
            slug = child.name
            if slug in found:
                continue
            try:
                ts = subprocess.check_output(
                    ["git", "-C", str(child), "log", "-1", "--format=%cI"],
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=5,
                ).strip()
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                continue
            if not ts:
                continue
            try:
                last = datetime.fromisoformat(ts)
            except ValueError:
                continue
            if last < cutoff:
                continue
            found[slug] = {"slug": slug, "path": str(child), "last_commit": ts}
    return sorted(found.values(), key=lambda x: x["last_commit"], reverse=True)


def fetch_curated_slugs(api_url: str, token: str, timeout: float = 10.0) -> set[str]:
    """Ask canopy-web for the curated project slugs. Returns an empty set on
    any error — the caller should treat that as "couldn't reach canopy-web,
    show all active repos as candidates".

    canopy-web's Ninja-based API returns a bare JSON array of
    {slug, name, status, visibility} objects — no {success, data} envelope.
    """
    req = urllib.request.Request(
        f"{api_url.rstrip('/')}/api/projects/slugs/",
        headers=with_provenance({"Authorization": f"Bearer {token}"}),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return set()
    # Bare list response (Ninja API — no {success, data} envelope).
    items = data if isinstance(data, list) else None
    if not isinstance(items, list):
        return set()
    return {p["slug"] for p in items if isinstance(p, dict) and "slug" in p}


def diff_against_curated(active: list[dict], curated: set[str]) -> list[dict]:
    """Return the subset of active repos whose slug is NOT in the curated set."""
    return [a for a in active if a["slug"] not in curated]
