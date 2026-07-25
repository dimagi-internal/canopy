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
          "list_agent_slugs"]


class AgentIdentity(BaseModel):
    slug: str
    name: str = ""
    email: str = ""
    description: str = ""
    persona: str = ""
    avatar_url: str = ""


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

    def post_sync(self, *, period_start, period_end, title, doc_url,
                  summary="", self_grades=None, source="manager-sync") -> dict:
        body = {"period_start": period_start, "period_end": period_end, "title": title,
                "summary": summary, "doc_url": doc_url,
                "self_grades": self_grades or {}, "source": source}
        return self._call("POST", f"/api/agents/{self.slug}/syncs/", body)

    def post_turn(self, *, cli_session_id, title, summary="", task_ext_ids=None,
                  work_product_urls=None, session_slug="", share_token="",
                  started_at=None, ended_at=None, source="turn") -> dict:
        """Package one turn as a unit of work: the request(s) it advanced
        (`task_ext_ids`), what it did (`summary`), the deliverables produced
        (`work_product_urls`), and — optionally — a transcript link (`session_slug`
        + `share_token`). Idempotent per (agent, cli_session_id) server-side."""
        body = {"cli_session_id": cli_session_id, "title": title, "summary": summary,
                "task_ext_ids": list(task_ext_ids or []),
                "work_product_urls": list(work_product_urls or []),
                "session_slug": session_slug, "share_token": share_token,
                "started_at": started_at, "ended_at": ended_at, "source": source}
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
