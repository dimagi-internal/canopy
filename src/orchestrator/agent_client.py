"""Shared client for canopy-web's agent workspace (/api/agents). Operator-plane
only (identity, syncs, work-products, skills, tasks, commands) — NO run lifecycle."""
from __future__ import annotations

import glob
import os
import re
from pathlib import Path
from typing import Callable, Optional
from pydantic import BaseModel, ConfigDict

from orchestrator import canopy_web
from orchestrator.canopy_web import CanopyError, Transport  # re-export

__all__ = ["AgentIdentity", "BoardCommand", "AgentClient", "catalog_from_repo", "CanopyError",
          "list_agent_slugs", "emdash_task_from_cwd"]


# A harness-dispatched emdash session is named `<subject>-<disc>-<MMDD>-<HHMM>` by
# the runner (execute._task_name), and emdash gives its worktree that name plus a
# short suffix: `.../hal/emdash/hal-api-df02-0810-0805-7ohfp`.
#
# The `-\d{4}-\d{4}` tail is what makes this safe to infer. A hand-made session
# ("audit-76bl3", "labs-9i3mk") has no timestamp and simply does not match, which
# is the correct answer for it — there is no dispatch row to join to. Greedy `.*`
# anchors on the RIGHTMOST timestamp pair, so a subject containing four digits of
# its own does not truncate the name.
_EMDASH_TASK = re.compile(r"^(?P<task>.*-\d{4}-\d{4})(?:-[a-z0-9]+)?$")


def emdash_task_from_cwd(cwd: "Optional[Path]" = None) -> str:
    """The emdash session this process is running in, or "" if it can't be told.

    Returns "" rather than guessing: a WRONG task id would attach a close-out to
    another turn's row, which is worse than leaving it unattached.
    """
    name = (cwd or Path.cwd()).name
    match = _EMDASH_TASK.match(name)
    return match.group("task") if match else ""


class AgentIdentity(BaseModel):
    slug: str
    name: str = ""
    email: str = ""
    description: str = ""
    persona: str = ""
    avatar_url: str = ""
    # Which canopy-web workspace the agent lives in. Empty (the default) keeps
    # the server's existing behaviour — a new agent lands in the default
    # workspace and an already-homed one is left where it is — so sending this
    # field changes nothing until someone actually sets it.
    #
    # It is worth setting. The default workspace (`dimagi`) auto-admits every
    # @dimagi.com address as an EDITOR, and DELETE /api/agents/{slug} accepts
    # editor — so an agent left on the default can be deleted by anyone in the
    # company who has logged in once. Until now the API supported placing an
    # agent but no CLI verb exposed it, which left a raw curl as the only route.
    workspace: str = ""


class BoardCommand(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    kind: str
    task_title: Optional[str] = None
    created_by: str = ""
    payload: Optional[dict] = None


def _rows(raw) -> "list[dict]":
    """Unwrap a list endpoint. Some return a bare list, the paginated ones return
    canopy-web's Page envelope — which is {"items": [...], "total", "offset", "limit"},
    NOT {"results": [...]}. Guessing "results" alone silently yielded [] on every
    paginated endpoint: `agent syncs` reported "no syncs" for an agent with three,
    so manager-sync recomputed its window from project start every run. Accept both.
    """
    if isinstance(raw, list):
        return raw
    raw = raw or {}
    for key in ("items", "results"):
        if isinstance(raw.get(key), list):
            return raw[key]
    return []


class AgentClient:
    def __init__(self, identity, *, base_url: Optional[str] = None,
                 token: Optional[str] = None, transport: Optional[Transport] = None):
        self.identity = identity if isinstance(identity, AgentIdentity) else AgentIdentity(**identity)
        self._base = base_url
        self._token = token
        self._transport = transport

    @property
    def slug(self) -> str:
        return self.identity.slug

    def _call(self, method: str, path: str, body=None) -> dict:
        return canopy_web.call(method, path, body, base_url=self._base,
                               token=self._token, transport=self._transport)

    def register(self) -> dict:
        return self._call("POST", "/api/agents/", self.identity.model_dump())

    def get_agent(self) -> dict:
        return self._call("GET", f"/api/agents/{self.slug}/")

    def turn_mode(self) -> str:
        """The agent's runtime autonomy posture (manual | auto) — board-side
        STATE on canopy-web, flipped by a human from /agents/<slug>, never by
        the agent or its repo. Raises on transport failure; the turn procedure
        treats a failed read as manual (fail safe), and that fallback belongs to
        the caller so it stays a visible decision, not a swallowed error.

        `gated` was this mode's name before 2026-08-01 and is normalized here, so
        a canopy that has updated ahead of its canopy-web still reads a mode it
        understands instead of an unknown string. Only the safe mode has an
        alias — nothing silently resolves TO `auto`."""
        mode = str(self.get_agent().get("turn_mode") or "manual")
        return "manual" if mode == "gated" else mode

    def post_sync(self, *, period_start, period_end, title, doc_url,
                  summary="", self_grades=None, source="manager-sync") -> dict:
        body = {"period_start": period_start, "period_end": period_end, "title": title,
                "summary": summary, "doc_url": doc_url,
                "self_grades": self_grades or {}, "source": source}
        return self._call("POST", f"/api/agents/{self.slug}/syncs/", body)

    def post_turn(self, *, cli_session_id, title, summary="", task_ext_ids=None,
                  work_product_urls=None, session_slug="", share_token="",
                  started_at=None, ended_at=None, source="turn",
                  emdash_task_id=None) -> dict:
        """Package one turn as a unit of work: the request(s) it advanced
        (`task_ext_ids`), what it did (`summary`), the deliverables produced
        (`work_product_urls`), and — optionally — a transcript link (`session_slug`
        + `share_token`). Idempotent per (agent, cli_session_id) server-side.

        `emdash_task_id` names the emdash session this turn ran in. The server uses
        it to attach this report to the harness turn that DISPATCHED the session,
        so a turn is one row rather than a dispatch record and an unrelated report.
        Defaults to deriving it from the cwd; pass "" to skip, or an explicit value
        when closing out on someone else's behalf. Unmatched is not an error — the
        report is still recorded, just standalone."""
        if emdash_task_id is None:
            emdash_task_id = emdash_task_from_cwd()
        body = {"cli_session_id": cli_session_id, "title": title, "summary": summary,
                "task_ext_ids": list(task_ext_ids or []),
                "work_product_urls": list(work_product_urls or []),
                "session_slug": session_slug, "share_token": share_token,
                "started_at": started_at, "ended_at": ended_at, "source": source,
                "emdash_task_id": emdash_task_id}
        return self._call("POST", f"/api/agents/{self.slug}/turns/", body)

    def put_work_products(self, items: list[dict]) -> dict:
        return self._call("POST", f"/api/agents/{self.slug}/work-products/", {"work_products": items})

    def put_skills(self, items: list[dict]) -> dict:
        return self._call("PUT", f"/api/agents/{self.slug}/skills/", {"skills": items})

    def sync_tasks(self, tasks: list[dict]) -> dict:
        return self._call("POST", f"/api/agents/{self.slug}/tasks/sync", {"tasks": tasks})

    def list_tasks(self) -> "list[dict]":
        return _rows(self._call("GET", f"/api/agents/{self.slug}/tasks/"))

    def list_syncs(self, limit: int | None = None) -> "list[dict]":
        """Past manager syncs, newest period_end first. The manager-sync window is
        the latest sync's period_end → today, so state lives here, not a repo file."""
        path = f"/api/agents/{self.slug}/syncs/"
        if limit:
            path += f"?limit={int(limit)}"
        return _rows(self._call("GET", path))

    def delete_sync(self, sync_id: int) -> dict:
        """Remove ONE sync by id. post_sync upserts per (period, source), so
        re-posting only corrects a sync for the SAME window — a sync filed under
        the wrong period is otherwise unreachable. Returns {} on success (204)."""
        return self._call("DELETE", f"/api/agents/{self.slug}/syncs/{int(sync_id)}/")

    def pending_commands(self) -> "list[BoardCommand]":
        raw = self._call("GET", f"/api/agents/{self.slug}/commands?status=pending")
        return [BoardCommand(**c) for c in (raw or [])]

    def apply_command(self, command_id: int, result_note: str = "") -> dict:
        return self._call("POST", f"/api/agents/{self.slug}/commands/{command_id}/apply",
                          {"result_note": result_note})

    def patch_task(self, task_id: int, **fields) -> dict:
        patch = {k: v for k, v in fields.items() if v is not None}
        return self._call("PATCH", f"/api/agents/{self.slug}/tasks/{task_id}/", patch)

    def record_verdict(self, run_id: str, step_key: str, *, kind: str,
                       score: float | None = None, passed: bool | None = None,
                       criteria: dict | None = None, rationale: str = "") -> dict:
        """Attach a judge/QA verdict to a run step (the run lifecycle's eval write
        path). `kind=qa` is the binary gate; `kind=judge` carries the score the
        run rolls up. POSTs to /api/agents/{slug}/runs/{run_id}/steps/{key}/verdict."""
        body = {"kind": kind, "score": score, "passed": passed,
                "criteria": criteria or {}, "rationale": rationale}
        return self._call(
            "POST", f"/api/agents/{self.slug}/runs/{run_id}/steps/{step_key}/verdict", body)


def _frontmatter(path: str) -> "tuple[str, str] | None":
    text = Path(path).read_text()
    m = re.match(r"^---\n(.*?)\n---", text, re.S)
    if not m:
        return None
    block = m.group(1)
    name = re.search(r"^name:\s*(.+)$", block, re.M)
    desc = re.search(r"^description:\s*(?:>\s*)?\n?((?:.|\n)*?)(?:\n\w[\w-]*:|\Z)", block, re.M)
    name_v = name.group(1).strip() if name else ""
    desc_v = " ".join(l.strip() for l in (desc.group(1).splitlines() if desc else [])).strip()
    return name_v, desc_v


def list_agent_slugs(call: Callable) -> list[str]:
    """All agent slugs from the paginated /api/agents/ envelope."""
    slugs, offset = [], 0
    while True:
        page = call("GET", f"/api/agents/?offset={offset}" if offset else "/api/agents/")
        items = page.get("items") or []
        slugs.extend(a["slug"] for a in items)
        offset += len(items)
        if not items or offset >= (page.get("total") or 0):
            return slugs


def catalog_from_repo(skills_root, url_template: str) -> "list[dict]":
    items = []
    for p in sorted(glob.glob(os.path.join(str(skills_root), "*", "SKILL.md"))):
        fm = _frontmatter(p)
        if not fm or not fm[0]:
            continue
        name, desc = fm
        items.append({"name": name, "description": desc,
                      "url": url_template.format(name=name), "improvement_note": ""})
    return items
