"""Recurring-work cursor — "what have I already looked at?", once, for every agent.

A skill that runs on a cadence (review the new runs, sweep the new sessions, brief on
what changed) has to know where it stopped, or every run re-reads the whole history and
the cadence becomes unaffordable. Four separate versions of this grew in the fleet before
this module, no two alike and no two correct in the same ways:

  - `ada/bin/ada-run-cursor` — Drive-persisted, timestamp + id list. The most complete,
    and the shape this module generalises. Two flaws: it dedupes on id ALONE, so a
    session reviewed at 10:00 and worked in until 18:00 is never revisited (its later,
    usually more interesting half is invisible); and it skips anything at or below the
    watermark, so an item that only became VISIBLE after the last run — one on the other
    macOS account, one caught by a wider window — is dropped silently and permanently.
  - `eva/bin/cos-state` — a repo-committed JSON file. Cadence policy and run-state
    entangled, and a file in the repo is invisible to a run on another machine until
    someone pushes.
  - `shareout.resolve_default_range` — no stored state at all; the previously published
    artifact IS the cursor. Elegant where a durable artifact exists; unavailable otherwise.
  - `ada/skills/self-review` — deliberately stateless, deriving "since last run" from the
    merge date of its own last PR, on the stated grounds that persisted artifacts rot.

That last objection is the real design constraint, and it is right: a state file that
drifts out of sync with reality is worse than none. This module answers it by keeping the
cursor **derived and self-describing** rather than authoritative — it records only what
was processed and when, carries `pruned_before` so it can say where its own memory ends,
and never suppresses an item it cannot positively account for.

## The shape

    {"version": 1, "key": "hal/ace-review",
     "cursor_ts": "2026-07-28T13:00:00Z",   # high-water mark, informational
     "seen": {"<item id>": "<ts when processed>"},
     "pruned_before": "2026-07-07T00:00:00Z",
     "last_run_at": "...", "runs": 12}

## The rule

An item is NEW when its id is unknown, or when its timestamp is newer than the one we
recorded for that id. Both stamps matter and neither alone is sufficient: the id says
*which* thing, the timestamp says *which version of it*. Below `pruned_before` the map
can no longer distinguish processed from never-seen, so items there are treated as seen
— bounded memory has to cost something, and re-reading the distant past every run is the
worse cost. Callers bound their own lookback window; that, not the watermark, is the
cost control.

Storage is the agent's own Drive `Process State` folder, reusing `agent_gdoc`'s per-agent
identity — so this works for any agent without per-agent code, and survives the throwaway
worktrees agents actually run in. Pure logic here is Drive-free and injectable; the I/O
lives in `DriveCursorStore`.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable

CURSOR_VERSION = 1
DEFAULT_KEEP = 800          # ids retained per cursor; ~2 months of a daily fleet sweep
STATE_AREA = "Process State"
UTC = dt.timezone.utc


class CursorError(Exception):
    """Malformed key, item, or cursor — always raised, never swallowed. A cursor bug
    that fails quietly shows up as work silently not done."""


def _now_iso(now: dt.datetime | None = None) -> str:
    return (now or dt.datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def slug_for_key(key: str) -> str:
    """`hal/ace-review` -> `hal--ace-review.cursor.json`.

    One file per key, not one file with many keys: two skills running concurrently in
    different worktrees would otherwise race on a shared document and one would lose its
    advance.
    """
    k = (key or "").strip().lower()
    if not k or k in ("/", "."):
        raise CursorError(f"empty cursor key: {key!r}")
    if ".." in k:
        raise CursorError(f"unsafe cursor key: {key!r}")
    # Slugify each `/`-segment separately, then join with `--`. Slugifying the whole
    # string at once collapses the segment separator into the same `-` as any other
    # punctuation, so `a/b-c` and `a-b/c` would land on one shared file.
    parts = [re.sub(r"[^a-z0-9]+", "-", p).strip("-") for p in k.split("/")]
    slug = "--".join(p for p in parts if p)
    if not slug:
        raise CursorError(f"cursor key slugs to nothing: {key!r}")
    return f"{slug}.cursor.json"


def empty_cursor(key: str) -> dict:
    slug_for_key(key)          # validate early
    return {
        "version": CURSOR_VERSION,
        "key": key,
        "cursor_ts": "",
        "seen": {},
        "pruned_before": "",
        "last_run_at": "",
        "runs": 0,
    }


def _checked(items: Iterable[dict]) -> list[tuple[str, str, dict]]:
    """(id, ts, item) for each, rejecting anything missing either stamp."""
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            raise CursorError(f"cursor item must be an object, got {type(it).__name__}")
        iid, ts = str(it.get("id") or "").strip(), str(it.get("ts") or "").strip()
        if not iid or not ts:
            raise CursorError(
                f"cursor item needs both an id and a ts (got id={it.get('id')!r} "
                f"ts={it.get('ts')!r}) — an item with one stamp cannot be deduped safely")
        out.append((iid, ts, it))
    return out


def filter_new(cursor: dict, items: Iterable[dict]) -> list[dict]:
    """The items worth processing: unknown ids, plus known ids that have changed since."""
    seen = (cursor or {}).get("seen") or {}
    horizon = (cursor or {}).get("pruned_before") or ""
    out = []
    for iid, ts, it in _checked(items):
        prev = seen.get(iid)
        if prev is None:
            # Unknown. New unless it predates what this cursor can still remember.
            if horizon and ts < horizon:
                continue
            out.append(it)
        elif ts > prev:
            out.append(it)          # same thing, but it has grown since we read it
    return out


def advance(cursor: dict, processed: Iterable[dict], *,
            keep: int = DEFAULT_KEEP, now: dt.datetime | None = None) -> dict:
    """Record what was just processed. Returns a NEW cursor; does not mutate the input."""
    cur = json.loads(json.dumps(cursor or {}))          # cheap deep copy
    cur.setdefault("version", CURSOR_VERSION)
    cur.setdefault("seen", {})
    seen = dict(cur["seen"])

    high = cur.get("cursor_ts") or ""
    for iid, ts, _ in _checked(processed):
        if seen.get(iid, "") < ts:
            seen[iid] = ts
        if ts > high:
            high = ts               # monotone: a late older item must not rewind it

    if len(seen) > keep:
        kept = sorted(seen.items(), key=lambda kv: kv[1], reverse=True)[:keep]
        # The horizon is the OLDEST timestamp we still remember; below it the map can no
        # longer answer "processed or never seen?", and filter_new says so by abstaining.
        cur["pruned_before"] = min(ts for _, ts in kept)
        seen = dict(kept)

    cur["seen"] = seen
    cur["cursor_ts"] = high
    cur["last_run_at"] = _now_iso(now)
    cur["runs"] = int(cur.get("runs", 0)) + 1
    return cur


# --------------------------------------------------------------------------- I/O

class LocalCursorStore:
    """Filesystem-backed store. For tests, and for a machine with no Drive identity."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def read(self, key: str) -> dict:
        p = self.root / slug_for_key(key)
        if not p.is_file():
            return empty_cursor(key)
        try:
            return json.loads(p.read_text(encoding="utf-8")) or empty_cursor(key)
        except (OSError, json.JSONDecodeError) as e:
            raise CursorError(f"cursor at {p} is unreadable: {e}") from e

    def write(self, key: str, cursor: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / slug_for_key(key)).write_text(json.dumps(cursor, indent=2, sort_keys=True), encoding="utf-8")

    def list_keys(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.glob("*.cursor.json"))


class DriveCursorStore:
    """The agent's own Drive `Process State` folder, via `agent_gdoc`'s identity.

    Drive rather than the repo or a local file because agents run each turn in a fresh
    throwaway worktree, often on a different account or machine than the last turn — the
    two places a cursor most needs to be readable are exactly the two a local file isn't.
    """

    def __init__(self, repo: Path, runner=subprocess.run):
        from orchestrator.agent_gdoc import resolve_gdoc_identity
        self.identity = resolve_gdoc_identity(Path(repo))
        self.runner = runner
        self._folder: str | None = None

    def _gog(self, *args: str) -> Any:
        cmd = ["gog", *args, "--account", self.identity.account,
               "--client", self.identity.client, "-j", "--results-only"]
        r = self.runner(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise CursorError(f"`{' '.join(cmd[:3])}` failed: "
                              f"{(r.stderr or r.stdout or '').strip()[:300]}")
        out = (r.stdout or "").strip()
        try:
            return json.loads(out) if out else None
        except json.JSONDecodeError:
            return out

    def folder_id(self) -> str:
        if self._folder is None:
            from orchestrator.agent_gdoc import resolve_subfolder
            self._folder = resolve_subfolder(self.identity, area=STATE_AREA,
                                             runner=self.runner)
        return self._folder

    def _find(self, name: str) -> str | None:
        rows = self._gog("drive", "ls", "--parent", self.folder_id(),
                         "--query", f"name = '{name}' and trashed = false") or []
        return rows[0]["id"] if rows else None

    def read(self, key: str) -> dict:
        fid = self._find(slug_for_key(key))
        if not fid:
            return empty_cursor(key)
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "cursor.json")
            self._gog("drive", "download", fid, "--out", out)
            try:
                with open(out, encoding="utf-8") as fh:
                    return json.load(fh) or empty_cursor(key)
            except (OSError, json.JSONDecodeError) as e:
                raise CursorError(f"cursor {key!r} in Drive is unreadable: {e}") from e

    def write(self, key: str, cursor: dict) -> None:
        name = slug_for_key(key)
        fid = self._find(name)
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, name)
            with open(out, "w", encoding="utf-8") as fh:
                json.dump(cursor, fh, indent=2, sort_keys=True)
            if fid:
                self._gog("drive", "upload", out, "--replace", fid)
            else:
                self._gog("drive", "upload", out, "--name", name,
                          "--parent", self.folder_id())

    def list_keys(self) -> list[str]:
        rows = self._gog("drive", "ls", "--parent", self.folder_id(),
                         "--query", "trashed = false") or []
        return sorted(r["name"] for r in rows if str(r.get("name", "")).endswith(".cursor.json"))
