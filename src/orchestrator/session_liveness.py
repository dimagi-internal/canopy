"""Is a Claude Code session STILL BEING WRITTEN TO?

`agent_review` grades a turn against a checklist of expected steps. That verdict is
only meaningful over a session that has ENDED: a live session has not skipped the
steps it simply has not reached yet. Scoring one anyway produces a confident false
finding, and `agent_review`'s own module comments record twice what that costs —
"a wrong metric doesn't just mislead, it dispatches work", once literally dispatching
an agent to build the wrong fix (canopy#593).

## Two sources, because either alone is wrong

This mirrors `plugins/canopy/scripts/live-turns.sh`, deliberately: the fleet's
duplicate check and its self-review must agree about what "live" means, or a turn
stands down for a sibling that agent-review has already written off as finished.
That script's header records six wrong versions; the two that survived are:

1. **A live process** — `claude --session-id <id>` or `--resume <id>` in the process
   table. `--resume` is load-bearing: a resumed session carries no other scope.
2. **A recently-written transcript** — covers the window where a session is being
   resumed and has NO process at all (the old one exited, the new one is not exec'd
   yet). That gap is precisely when a recovery dispatch fires.

**Neither source is redundant, and the failure directions are opposite.**

Source 2 alone — which is what canopy#593 proposed — misses the single most common
live-but-idle state there is: a MANUAL-mode turn parked at an approval gate. It has
drafted a reply, run its review, and is waiting on a human, so nothing writes to its
transcript for as long as the human takes. Measured 2026-09-04 while fixing #593:
hal session `96444cd8` sat 56.8 minutes stale at an approval gate and was a live
process the whole time. A 10-minute mtime window calls that finished and scores it —
reintroducing the exact bug, via the fix.

Source 1 alone misses the resume gap, which is failure six in `live-turns.sh`.

## Which way to be wrong

The two mistakes do not cost the same, so the tie-break is not symmetric:

- Treating a FINISHED session as live costs one deferred finding. The next review,
  after it ends, sees the same transcript and reports it.
- Treating a LIVE session as finished costs a confident false finding that dispatches
  work at an agent.

So when liveness cannot be determined, prefer "live". But prefer it OUT LOUD:
`degraded` is set when the process table could not be read, so a caller can say
"liveness undetermined" instead of silently suppressing every checklist in the run —
which would turn the whole check into a no-op that looks like a clean bill of health.
That is `live-turns.sh`'s invariant, and it is the one to test any future version
against: **a check that cannot see something must SAY SO.**
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

# Same env var and default as `live-turns.sh`, so the duplicate check and the review
# cannot drift apart in a way that makes a turn stand down for a session this module
# has already declared finished.
RECENT_MIN_ENV = "CANOPY_LIVE_TURNS_RECENT_MIN"
DEFAULT_RECENT_MIN = 10

# Test seam, shared with `live-turns.sh`: enumerate these ids instead of reading the
# process table, so the matcher can be exercised against fixtures with no live fleet.
SESSION_IDS_ENV = "CANOPY_LIVE_TURNS_SESSION_IDS"

_SESSION_ID_RE = re.compile(r"--(?:session-id|resume)\s+([0-9a-f-]{36})")


def recent_window_seconds(env: Optional[dict] = None) -> float:
    """The mtime window, in seconds. Unparseable or negative values fall back to the
    default rather than raising — a malformed env var must not take down a review."""
    raw = (env if env is not None else os.environ).get(RECENT_MIN_ENV, "")
    try:
        minutes = int(str(raw).strip())
    except (TypeError, ValueError):
        minutes = DEFAULT_RECENT_MIN
    if minutes < 0:
        minutes = DEFAULT_RECENT_MIN
    return minutes * 60.0


@dataclass(frozen=True)
class Liveness:
    """Which sessions are live, and whether we could actually look.

    `degraded` True means the process table was unreadable, so `live_ids` is a lower
    bound and mtime is carrying the whole check. Callers must surface it rather than
    treating the result as authoritative.
    """
    live_ids: frozenset[str]
    degraded: bool

    def is_live(self, transcript: Path, now: Optional[float] = None,
                window: Optional[float] = None) -> bool:
        """True if this transcript's session is still being written to.

        `transcript.stem` is the session id (`<session-id>.jsonl`), which is how the
        process table's `--session-id`/`--resume` value joins to a file on disk.
        """
        if str(Path(transcript).stem) in self.live_ids:
            return True
        win = recent_window_seconds() if window is None else window
        try:
            mtime = Path(transcript).stat().st_mtime
        except OSError:
            # Cannot stat it. Per the asymmetry above, an undetermined session is
            # treated as live so it is never scored on a checklist it may not have
            # finished; `degraded` is not set here because the process table was fine.
            return True
        return ((now if now is not None else time.time()) - mtime) < win


def live_session_ids(runner: Optional[Callable[[], str]] = None,
                     env: Optional[dict] = None) -> Liveness:
    """Session ids of every live `claude` process.

    Matches BOTH `--session-id` and `--resume`: a resumed session runs as
    `claude --resume <uuid>` and carries no other scope, which is what made the
    original argv-based duplicate check silently return "nobody is here".
    """
    environ = env if env is not None else os.environ
    override = (environ.get(SESSION_IDS_ENV) or "").split()
    if override:
        return Liveness(frozenset(override), degraded=False)

    if runner is None:
        def runner() -> str:
            proc = subprocess.run(
                ["ps", "ax", "-o", "args="],
                capture_output=True, text=True, timeout=15,
            )
            return proc.stdout or ""

    try:
        out = runner()
    except (subprocess.SubprocessError, OSError, ValueError):
        # "I could not look" must never render as "nobody is there".
        return Liveness(frozenset(), degraded=True)

    ids = {m.group(1) for line in out.splitlines() if "claude" in line.lower()
           for m in _SESSION_ID_RE.finditer(line)}
    return Liveness(frozenset(ids), degraded=False)


def mark_live(transcripts: Iterable[Path],
              liveness: Optional[Liveness] = None,
              now: Optional[float] = None) -> dict[str, bool]:
    """Map each transcript path -> is-it-still-running. Enumerates the process table
    ONCE for the whole batch; `agent_review` scores every transcript in a run, and a
    per-transcript `ps` would be both slow and internally inconsistent."""
    lv = liveness if liveness is not None else live_session_ids()
    window = recent_window_seconds()
    stamp = time.time() if now is None else now
    return {str(t): lv.is_live(Path(t), now=stamp, window=window) for t in transcripts}
