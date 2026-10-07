"""Shared client for canopy-web's agent workspace (/api/agents). Operator-plane
only (identity, syncs, skills, projects, tasks and the actions people take on
them) — NO run lifecycle.

Tasks are addressed by `ext_id` (T3) — canopy-web exposes no other task id. A
person's approve / decline / reply / dispatch / done on a task is an ACTION; the
agent drains its pending actions (`pending_actions`) and marks each one applied
(`mark_action_applied`)."""
from __future__ import annotations

import glob
import os
import re
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlencode
from pydantic import BaseModel, ConfigDict

from orchestrator import canopy_web
from orchestrator.canopy_web import CanopyError, Transport  # re-export

__all__ = ["AgentIdentity", "TaskAction", "AgentClient", "task_idempotency_key", "catalog_from_repo", "CanopyError",
          "list_agent_slugs", "emdash_task_from_cwd"]


# A harness-dispatched emdash session is named `c-<subject>-<disc>` by the runner
# (canopy_runner.session_naming, which OWNS the format), and emdash gives its
# worktree that name plus an optional 5-character de-dupe suffix:
# `.../ace/emdash/c-issue-triage-4a4e-7ohfp`.
#
# THIS IS A MIRROR of `session_naming.CANOPY_PREFIX` / `DISC_LEN`. The CLI and the
# runner share no dependency (the runner ships to an EC2 box as a package with no
# orchestrator in sight), so the format is restated here and nowhere else in this
# repo. A false negative silently detaches an agent's close-out from its turn, so
# if the runner's format moves, this moves with it.
#
# Two shapes, both live:
#
#   CURRENT — `c-` says canopy launched it, which is a far stronger signal than any
#   inference from shape. The discriminator is EXACTLY 4 characters and emdash's
#   suffix is 5, which is what tells the two apart now that there is no timestamp
#   to anchor on; `session_naming._disc` pads a short key precisely to hold that.
#
#   LEGACY — `<agent>-<subject>-<disc>-<MMDD>-<HHMM>`. Sessions carrying it are
#   still live and reuse resolves them by name, so it stays recognised. Greedy `.*`
#   anchors on the RIGHTMOST timestamp pair, so a subject containing four digits of
#   its own does not truncate the name.
#
# A hand-made session ("audit-76bl3", "labs-9i3mk") matches neither, which is the
# correct answer for it — there is no dispatch row to join to.
_EMDASH_TASK = re.compile(
    r"^(?:emdash-)?(?:"
    r"(?P<task>cx?-.*-[a-z0-9]{4})(?:-[a-z0-9]{5})?"         # current (cx- = a caller's session)
    r"|(?P<legacy>.*-\d{4}-\d{4})(?:-[a-z0-9]+)?"           # pre-2026-09 names
    r")$"
)


def emdash_task_from_cwd(cwd: "Optional[Path]" = None) -> str:
    """The emdash session this process is running in, or "" if it can't be told.

    Returns "" rather than guessing: a WRONG task id would attach a close-out to
    another turn's row, which is worse than leaving it unattached.

    THE `emdash-` PREFIX IS NOT PART OF THE TASK ID, and dropping it is what makes the
    close-out attach at all. The runner names a dispatched worktree
    `emdash-<task>-<suffix>` but reports the session to canopy-web as `<task>`
    (execute.py's `client.finish(..., emdash_task_id=task)`), so the two halves of a
    turn derived their join key from different strings. `_claim_dispatch_row` matches
    on `emdash_task_id` exactly, so an agent closing out from a dispatched worktree
    never found its own dispatch row and orphaned a fresh one instead.

    Measured 2026-09-05 across the fleet — every close-out filed from a dispatched
    worktree carried the prefix and attached to nothing: `emdash-echo-api-611f-0901-1248`,
    `emdash-ace-api-2146-0904-1204`, `emdash-hal-api-0255-0904-1210`. The docstring above
    was already right about the danger; the regex just captured a string that could never
    match. This is the third place the same prefix has bitten (ada's close-sessions
    current-task fail-safe was the second), which is why it is stripped here, at the one
    function that derives the id, rather than compensated for at each reader.
    """
    name = (cwd or Path.cwd()).name
    match = _EMDASH_TASK.match(name)
    if not match:
        return ""
    return match.group("task") or match.group("legacy") or ""


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
    # It is worth setting. Joining a workspace is explicit, but the default
    # (`dimagi`) lists @dimagi.com in its self-join domains and a self-joiner
    # lands as EDITOR — and DELETE /api/agents/{slug} is editor-tier
    # (`agent.work`). Only an agent's keys (credentials, vault, interface) need
    # its owner or an admin. So an agent left on the default can be reshaped or
    # deleted by any employee who chooses to join it; a division or own
    # workspace limits that to people someone actually let in.
    workspace: str = ""


class TaskAction(BaseModel):
    """One thing a person did TO a task (approve / decline / reply / dispatch / done).

    While `status` is `pending` it is on the agent's queue: carry it out, then
    `mark_action_applied(id)`."""
    model_config = ConfigDict(extra="allow")
    id: int
    task_ext_id: str = ""
    action: str
    comment: str = ""
    by: str = ""
    status: str = "pending"


def task_idempotency_key(slug: str, ext_id: str) -> str:
    """The key that makes creating task `ext_id` safe to repeat.

    `POST /tasks/` replays a key it has already seen instead of making a second
    task, so a create retried after a timeout — or re-run by a turn that does not
    know the first one landed — returns the task it made the first time. Keyed on
    the agent too: canopy-web refuses a key another agent already used."""
    return f"{slug}:{ext_id}"


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
                  emdash_task_id=None, origin_ref=None) -> dict:
        """Package one turn as a unit of work: the request(s) it advanced
        (`task_ext_ids`), what it did (`summary`), the deliverables produced
        (`work_product_urls`), and — optionally — a transcript link (`session_slug`
        + `share_token`). Idempotent per (agent, cli_session_id) server-side.

        `emdash_task_id` names the emdash session this turn ran in. The server uses
        it to attach this report to the harness turn that DISPATCHED the session,
        so a turn is one row rather than a dispatch record and an unrelated report.
        Defaults to deriving it from the cwd; pass "" to skip, or an explicit value
        when closing out on someone else's behalf. Unmatched is not an error — the
        report is still recorded, just standalone.

        `origin_ref` tags the row (canopy-web MERGES it into the row's own origin_ref) —
        e.g. a huddle's anchor turn. Sent only when given, so every other close-out is
        byte-identical to before it existed."""
        if emdash_task_id is None:
            emdash_task_id = emdash_task_from_cwd()
        body = {"cli_session_id": cli_session_id, "title": title, "summary": summary,
                "task_ext_ids": list(task_ext_ids or []),
                "work_product_urls": list(work_product_urls or []),
                "session_slug": session_slug, "share_token": share_token,
                "started_at": started_at, "ended_at": ended_at, "source": source,
                "emdash_task_id": emdash_task_id}
        if origin_ref:
            body["origin_ref"] = dict(origin_ref)
        return self._call("POST", f"/api/agents/{self.slug}/turns/", body)

    def put_skills(self, items: list[dict]) -> dict:
        return self._call("PUT", f"/api/agents/{self.slug}/skills/", {"skills": items})

    def get_interface(self) -> dict:
        """The declared interface as canopy-web holds it: {interface, source, …}."""
        return self._call("GET", f"/api/agents/{self.slug}/interface")

    def put_interface_source(self, source: str) -> dict:
        """Save the declared interface as YAML. It is LIVE STATE on canopy-web, not
        a file in the agent's repo. Owner-or-admin on the server: it decides what
        callers can make the agent do."""
        return self._call("PUT", f"/api/agents/{self.slug}/interface", {"source": source})

    def create_tasks(self, tasks: list[dict]) -> "list[dict]":
        """Create tasks — the body is a BARE list. Returns the tasks as created.

        CREATE, not upsert: a task that names an `ext_id` gets the idempotency key
        `<slug>:<ext_id>` (unless it brings its own), so repeating the call hands
        back the task the first call made — UNCHANGED. To change a task that
        exists, `patch_task` it. An `ext_id` already on the board under a
        different key is a 409. Omit `ext_id` and the server assigns the next T<N>."""
        body = []
        for task in tasks:
            task = dict(task)
            ext_id = str(task.get("ext_id") or "").strip()
            if ext_id and not task.get("idempotency_key"):
                task["idempotency_key"] = task_idempotency_key(self.slug, ext_id)
            body.append(task)
        return _rows(self._call("POST", f"/api/agents/{self.slug}/tasks/", body))

    def list_tasks(self, **filters) -> "list[dict]":
        """The agent's tasks. Server-side filters, all optional: `project` (P2, or
        `none` for one-offs), `status` (comma-separated), `waiting` (`me`),
        `ask` (`open` | `closed`), `batch` (a batch_key)."""
        query = urlencode({k: v for k, v in filters.items() if v})
        path = f"/api/agents/{self.slug}/tasks/" + (f"?{query}" if query else "")
        return _rows(self._call("GET", path))

    def get_task(self, ref: str) -> dict:
        """One task by its `ext_id` (T3), with every action taken on it."""
        return self._call("GET", f"/api/agents/{self.slug}/tasks/{ref}/")

    def list_projects(self) -> "list[dict]":
        """The agent's projects — the state behind its Drive `Projects/<name>` folders.

        Per agent, like the folders are: two agents on one initiative have a project
        each and share files when they want to."""
        return _rows(self._call("GET", f"/api/agents/{self.slug}/projects/"))

    def get_project(self, ref: str) -> dict:
        """One project by `P<N>` or numeric id: its fields and links, its tasks and
        the recent turns that worked on them."""
        return self._call("GET", f"/api/agents/{self.slug}/projects/{ref}/")

    def create_project(self, **fields) -> dict:
        return self._call("POST", f"/api/agents/{self.slug}/projects/", fields)

    def patch_project(self, ref: str, **fields) -> dict:
        """Patch by `P<N>` ext_id or numeric id. Omitted fields are left alone."""
        patch = {k: v for k, v in fields.items() if v is not None}
        return self._call("PATCH", f"/api/agents/{self.slug}/projects/{ref}/", patch)

    def list_turns(self, *, page_size: int = 200, max_rows: int = 2000) -> "list[dict]":
        """The agent's turn records (`canopy agent turn` reports + harness turns), newest
        first. Each row carries `task_ext_ids` — the only place a turn names the tasks it
        advanced. The route pages ({items, total, offset, limit}) and has no per-task
        filter, so this walks the pages; `max_rows` bounds a runaway history."""
        rows: list[dict] = []
        while len(rows) < max_rows:
            raw = self._call("GET", f"/api/agents/{self.slug}/turns/"
                                    f"?limit={int(page_size)}&offset={len(rows)}")
            page = _rows(raw)
            rows.extend(page)
            total = raw.get("total") if isinstance(raw, dict) else None
            if not page or total is None or len(rows) >= int(total):
                break
        return rows[:max_rows]

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

    def pending_actions(self) -> "list[TaskAction]":
        """The agent's queue: actions people took on its tasks that it has not yet
        carried out, oldest first — the order to carry them out in."""
        raw = self._call("GET", f"/api/agents/{self.slug}/actions/?status=pending")
        return [TaskAction(**a) for a in _rows(raw)]

    def mark_action_applied(self, action_id: int, result_note: str = "") -> dict:
        return self._call("POST", f"/api/agents/{self.slug}/actions/{int(action_id)}/applied",
                          {"result_note": result_note})

    def patch_task(self, ref: str, **fields) -> dict:
        """Patch by `ext_id` (T3). Omitted (None) fields are left alone."""
        patch = {k: v for k, v in fields.items() if v is not None}
        return self._call("PATCH", f"/api/agents/{self.slug}/tasks/{ref}/", patch)

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
    text = Path(path).read_text(encoding="utf-8")
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
