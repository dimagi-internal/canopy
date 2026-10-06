"""Where a huddle's clean outcomes live: the leader's Drive `Process State/Huddles/<id>.json`.

The one thing a huddle writes outside canopy-web (spec §1). No messages, prompts or round
state — canopy-web already holds those — only what came OUT: each outcome, its fate (filed /
held / declined), and who was not reached. The next huddle of the same team reads the last few
so a declined item is not re-raised without new evidence and work in flight is known.

Single writer (the leader), through the same per-agent Drive identity `agent_gdoc` and
`work_cursor` use, so any agent can lead a huddle with no per-agent code. `LocalHuddleStore` is
the same interface over a directory (tests, `--local`).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

STATE_AREA = "Process State"
HUDDLES_FOLDER = "Huddles"
_ID = re.compile(r"^[a-z0-9][a-z0-9_]*-[a-z0-9_.]+-\d{8}(-\d+)?$")


class HuddleStoreError(Exception):
    """Drive (or the local dir) could not be read or written — never swallowed: a huddle
    whose record silently failed to land would re-raise everything next time."""


def _name(huddle_id: str) -> str:
    if not _ID.match(str(huddle_id or "")):
        raise HuddleStoreError(f"not a huddle id: {huddle_id!r}")
    return f"{huddle_id}.json"


def _recent_key(team: str, name: str):
    """(date, suffix) for `<type>-<team>-<YYYYMMDD>[-N].json`, or None if not this team's."""
    m = re.match(rf"^[a-z0-9_]+-{re.escape(team)}-(\d{{8}})(?:-(\d+))?\.json$", name)
    if not m:
        return None
    return (m.group(1), int(m.group(2) or 1))


def _newest(team: str, names, limit: int) -> list[str]:
    keyed = [(k, n) for n in names if (k := _recent_key(team, n)) is not None]
    return [n for _, n in sorted(keyed, reverse=True)][: max(0, int(limit))]


class LocalHuddleStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def read(self, huddle_id: str) -> dict | None:
        p = self.root / _name(huddle_id)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise HuddleStoreError(f"huddle record {p} is unreadable: {e}") from e

    def write(self, record: dict) -> str:
        p = self.root / _name(record.get("id"))
        self.root.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
        return str(p)

    def recent(self, team: str, limit: int = 3) -> list[dict]:
        if not self.root.exists():
            return []
        names = _newest(team, (p.name for p in self.root.glob("*.json")), limit)
        return [r for r in (self.read(n[:-5]) for n in names) if r is not None]


class DriveHuddleStore:
    """The leader's own Drive `Process State/Huddles/` folder via `agent_gdoc`'s identity."""

    def __init__(self, repo: Path, runner=subprocess.run):
        from orchestrator.agent_email import reconcile_client
        from orchestrator.agent_gdoc import resolve_gdoc_identity
        self.identity = resolve_gdoc_identity(Path(repo))
        # The client rule every gog caller follows (see work_cursor.DriveCursorStore).
        reconcile_client(self.identity, runner=runner, apply=True)
        self.runner = runner
        self._folder: str | None = None

    def _gog(self, *args: str) -> Any:
        cmd = ["gog", *args, "--account", self.identity.account,
               "--client", self.identity.client, "-j", "--results-only"]
        r = self.runner(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise HuddleStoreError(f"`{' '.join(cmd[:3])}` failed: "
                                   f"{(r.stderr or r.stdout or '').strip()[:300]}")
        out = (r.stdout or "").strip()
        try:
            return json.loads(out) if out else None
        except json.JSONDecodeError:
            return out

    def folder_id(self) -> str:
        if self._folder is None:
            from orchestrator.agent_gdoc import AgentGdocError, resolve_subfolder
            try:
                self._folder = resolve_subfolder(self.identity, area=STATE_AREA,
                                                 project=HUDDLES_FOLDER, runner=self.runner)
            except AgentGdocError as e:
                raise HuddleStoreError(str(e)) from e
        return self._folder

    def _ls(self) -> list[dict]:
        rows = self._gog("drive", "ls", "--parent", self.folder_id(),
                         "--query", "trashed = false") or []
        return [r for r in rows if isinstance(r, dict)]

    def _find(self, name: str) -> str | None:
        rows = self._gog("drive", "ls", "--parent", self.folder_id(),
                         "--query", f"name = '{name}' and trashed = false") or []
        rows = [r for r in rows if isinstance(r, dict)]
        return rows[0]["id"] if rows else None

    def _download(self, fid: str) -> dict:
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "huddle.json")
            self._gog("drive", "download", fid, "--out", out)
            try:
                with open(out, encoding="utf-8") as fh:
                    return json.load(fh)
            except (OSError, json.JSONDecodeError) as e:
                raise HuddleStoreError(f"huddle record {fid} in Drive is unreadable: {e}") from e

    def read(self, huddle_id: str) -> dict | None:
        fid = self._find(_name(huddle_id))
        return self._download(fid) if fid else None

    def write(self, record: dict) -> str:
        """Create or REPLACE `<id>.json` (a re-run of `huddle file` never appends a twin)."""
        name = _name(record.get("id"))
        fid = self._find(name)
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, name)
            with open(out, "w", encoding="utf-8") as fh:
                json.dump(record, fh, indent=2, sort_keys=True)
            if fid:
                self._gog("drive", "upload", out, "--replace", fid)
                return fid
            res = self._gog("drive", "upload", out, "--name", name, "--parent", self.folder_id())
        return str((res or {}).get("id") or "") if isinstance(res, dict) else str(res or "")

    def recent(self, team: str, limit: int = 3) -> list[dict]:
        by_name = {r.get("name"): r.get("id") for r in self._ls()}
        return [self._download(by_name[n]) for n in _newest(team, by_name, limit)]
