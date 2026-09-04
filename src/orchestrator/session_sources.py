"""Injectable session-corpus sources — the seam that stops the cross-user blind spot.

WHY this exists: `agent_coverage.coverage_report` reports which of an agent's
declared skills ever FIRED, by scanning that agent's Claude Code transcripts. It
found them via `agent_review.find_turn_transcripts(repo, hours,
projects_dir=CLAUDE_PROJECTS)` where `CLAUDE_PROJECTS = Path.home() / ".claude" /
"projects"` — ONE place: the CURRENT user's home. This machine has TWO macOS
accounts (jjackson + acedimagi) JJ alternates between when he runs out of
tokens. Scanning only one inverts the report's conclusions: `hal/architect` was
reported `never_live` ("13 commits, never fired") when it had actually fired
three times — on the OTHER account, in a worktree checkout invisible to the
single-home scan. Every "never fired" claim was untrustworthy until this fix.

`harvest.py`'s module docstring already states the rule this module encodes:
"Cross-user or the conclusion inverts. JJ alternates macOS accounts (acedimagi +
jjackson)" and "Flag own blindness. `confidence: half-blind` is a first-class
field whenever any user's corpus is unreadable." `harvest.user_session_roots`
already did the local glob + readable check for exactly one caller — this
module pulls that logic out from under its one hardcoded call site into a
typed, N-source SEAM, so the next reader doesn't collapse it back into a single
`/Users/*/.claude/projects` glob duplicated across modules.

The human's requirement: "our skills should have N places they can look,
including cloud runtimes when we add those." So this is a typed list of
sources, one adapter per `kind` — NOT a hardcoded two-account glob. Only the
`local` adapter is implemented today (no speculative cloud code). A configured
source whose `kind` has no registered adapter comes back `readable=False` with
a non-empty `reason` — NEVER silently dropped. That is the forward-compat
property: someone configures a cloud runtime before its adapter exists and the
report degrades LOUD (half-blind) instead of quietly under-reporting.
"""
from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from orchestrator.paths import CANOPY_DIR

# Canopy already keeps state under ~/.claude/canopy/ (workbench-token, campaigns/,
# repo-map.json, ...) -- this config lives alongside it, not in a repo-local file.
DEFAULT_CONFIG_PATH = CANOPY_DIR / "session-sources.json"


@dataclass
class SessionSource:
    name: str        # "local:jjackson"
    kind: str        # "local"  (future: "cloud")
    location: str    # a path today; a URI for future kinds
    readable: bool
    reason: str = ""  # why unreadable, when it isn't


def _dir_listable(path: str) -> bool:
    try:
        os.listdir(path)
        return True
    except OSError:
        return False


def discover_local_sources(users_root: str = "/Users") -> list[SessionSource]:
    """Every macOS user's ~/.claude/projects on this box, with a readability flag.

    The local glob + readable check (moved here from harvest.user_session_roots,
    which is now a thin wrapper over this — see its docstring).
    """
    out: list[SessionSource] = []
    for home in sorted(glob.glob(os.path.join(users_root, "*"))):
        p = os.path.join(home, ".claude", "projects")
        if not os.path.isdir(p):
            continue
        readable = _dir_listable(p)
        out.append(SessionSource(
            name=f"local:{os.path.basename(home)}", kind="local", location=p,
            readable=readable, reason="" if readable else "not readable",
        ))
    return out


def _adapt_local(entry: dict) -> SessionSource:
    location = str(entry.get("location") or "")
    name = entry.get("name") or f"local:{location}"
    readable = os.path.isdir(location) and _dir_listable(location)
    return SessionSource(name=name, kind="local", location=location,
                         readable=readable, reason="" if readable else "not readable")


def _adapt_unknown(entry: dict) -> SessionSource:
    kind = str(entry.get("kind") or "")
    location = str(entry.get("location") or "")
    name = entry.get("name") or f"{kind}:{location}"
    # Degrade LOUD, never silent: an unrecognized kind still produces a source
    # row (so it shows up in corpus.sources / drives confidence to half-blind)
    # instead of being dropped, which would quietly under-report the corpus.
    return SessionSource(name=name, kind=kind, location=location, readable=False,
                         reason=f"no adapter for kind {kind!r}")


# One adapter per `kind`. Registering a future "cloud" adapter here is the
# whole extension point -- no caller of `session_sources()` needs to change.
_ADAPTERS: dict[str, Callable[[dict], SessionSource]] = {"local": _adapt_local}


def _from_config_entry(entry: dict) -> SessionSource:
    adapter = _ADAPTERS.get(entry.get("kind"))
    return adapter(entry) if adapter else _adapt_unknown(entry)


def session_sources(config_path: Optional[Path] = None,
                    users_root: str = "/Users") -> list[SessionSource]:
    """All configured sources, or local auto-discovery when no config exists.

    Config lives at ~/.claude/canopy/session-sources.json (override via
    `config_path`, mainly for tests):
        {"sources": [{"name": "...", "kind": "local", "location": "/path"}, ...]}

    A configured source wins over auto-discovery entirely (it's an explicit
    statement of where to look); with no config file, auto-discover local
    sources so this works out of the box on a fresh machine.
    """
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        return [_from_config_entry(e) for e in data.get("sources", [])]
    return discover_local_sources(users_root=users_root)


def local_transcript_dirs(sources: list[SessionSource]) -> list[Path]:
    """The readable local sources' paths -- ready for
    `find_turn_transcripts(repo, hours, projects_dir=d)`."""
    return [Path(s.location) for s in sources if s.kind == "local" and s.readable]


def corpus_confidence(sources: list[SessionSource]) -> str:
    """`"whole-corpus"` if every source is readable, else `"half-blind"` --
    harvest's established convention for flagging its own blindness."""
    return "whole-corpus" if all(s.readable for s in sources) else "half-blind"
