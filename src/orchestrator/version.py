"""What version of canopy is installed right now.

The first of the three questions any auto-update has to answer (the other two being what
SHOULD be installed and whether now is a safe moment — see canopy-web's
`runner/canopy_runner/canopy_runner/update.py`, which answers all three for the runner).
The CLI could not answer even this one: `canopy --version` did not exist, so
`bootstrap_agents.sh`'s tooling step printed `?` for the version on every cloud boot, and
nothing anywhere could compare installed against wanted. That is half of why the CLI sat
eight days stale on the box running four of the five agents while the runner beside it
updated itself on a 30-minute timer.

INSTALLED METADATA IS THE ANSWER, NOT THE REPO FILE. A released wheel carries its version
in package metadata and ships no VERSION file; a source checkout has the file and no
metadata. Preferring the file would mean a CLI installed weeks ago reports whatever number
the checkout you happen to be standing in declares — i.e. a stale install claiming to be
current, which is precisely the failure mode an updater exists to catch. So: metadata
first, the repo file only as a source-checkout fallback, and `unknown` rather than an
exception, because a probe that raises takes the CLI down with it.
"""
from __future__ import annotations

import re
from pathlib import Path

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

UNKNOWN = "unknown"


def _dist_version() -> str | None:
    """The installed distribution's version, or None when running from source.

    Split out as a seam so tests can drive both worlds without installing anything.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("canopy")
    except Exception:  # PackageNotFoundError, or a broken metadata dir
        return None


def _file_version(repo_root: Path) -> str | None:
    try:
        raw = (Path(repo_root) / "VERSION").read_text().strip()
    except OSError:
        return None
    return raw or None


def resolve(*, repo_root: Path | None = None) -> str:
    """The version of canopy this process is running, or `unknown`."""
    installed = _dist_version()
    if installed:
        return installed
    root = repo_root if repo_root is not None else Path(__file__).resolve().parents[2]
    return _file_version(root) or UNKNOWN
