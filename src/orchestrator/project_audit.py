"""`canopy agent project-audit` — does the board's project model match the agent's work?

agent-core/task-tracker.md § "Projects — one name, two halves" says a project is a Drive
folder `<agent root>/Projects/<name>/` plus a canopy-web project of the SAME name, with its
tasks filed in, and every turn that advances a task recorded with `canopy agent turn`. That
was prose the agent had to remember, and it decayed: on 2026-10-04 ACE's board had one
registered project with no folder linked, five `Projects/` folders with no project, and a
six-PR, multi-deliverable piece of work living as a loose task with no turn records. Nothing
checked it. This is the check — read-only, and it never fails (an audit that can crash the
close-out is an audit that gets skipped).

Five checks, all pure functions over (tasks, projects, turns, Drive folders) so each is unit
testable without a network:

  a. LOOSE  — an open task with no project that looks like project work. A task "looks like
              a project" when ANY of: >= 2 links (it has produced artifacts), >= 1 turn record
              (it has spanned turns), >= 2 appended note entries (it has a multi-turn log).
  b. NOFOLDER — an active project with no Drive folder linked.
  c. DORMANT — `Projects/<name>/` folders with no project pointing at them. INFORMATIONAL
              ONLY, never a finding: the board holds ACTIVE work and Drive is the durable
              archive, so a folder is EXPECTED to outlive its board entry (owner decision,
              2026-10-04). Reported as a count; the folders are also used to suggest
              re-registering a returning project against its existing folder (same name).
  d. DRIFT  — a project whose linked folder's name differs from the project name, or whose
              linked folder is not under `Projects/` at all.
  e. UNRECORDED — an open task updated in the last N days with no turn record naming it in
              that same window.

Tasks whose Next action carries the `[MANUAL — …]` marker are skipped by (a) and (e): the
human has taken them off the agent's queue (task-tracker.md vocabulary).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

OPEN_STATUSES = ("suggested", "in_progress")
MANUAL_MARKER = "[MANUAL"
DEFAULT_RECENT_DAYS = 2
DRIVE_LIST_MAX = 1000

_FOLDER_ID_RE = re.compile(r"(?:/folders/|[?&]id=)([A-Za-z0-9_-]{10,})")


def folder_id_from(project: dict) -> str:
    """A project's linked Drive folder id — the explicit id, else parsed from the url."""
    fid = str(project.get("drive_folder_id") or "").strip()
    if fid:
        return fid
    m = _FOLDER_ID_RE.search(str(project.get("drive_folder_url") or ""))
    return m.group(1) if m else ""


def folder_url(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


def _status(task: dict) -> str:
    return str(task.get("status") or "").strip().lower().replace(" ", "_").replace("-", "_")


def _is_open(task: dict) -> bool:
    return _status(task) in OPEN_STATUSES


def _is_manual(task: dict) -> bool:
    return str(task.get("next_action") or "").lstrip().startswith(MANUAL_MARKER)


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def note_entries(notes) -> int:
    """Appended note entries — `agent set --append-notes` joins entries with a blank line."""
    text = str(notes or "").strip()
    return len([b for b in re.split(r"\n\s*\n", text) if b.strip()]) if text else 0


def turns_by_task(turns: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for turn in turns or []:
        for ext in turn.get("task_ext_ids") or []:
            out.setdefault(str(ext), []).append(turn)
    return out


def _suggest_name(task: dict) -> str:
    """A project name from a task title: the part before an em-dash / colon, trimmed."""
    title = str(task.get("title") or "").strip()
    head = re.split(r"\s+[—–-]\s+|:\s", title, maxsplit=1)[0].strip()
    return (head or title)[:200]


def _matching_folder(task: dict, folders: list[dict]) -> dict | None:
    """A Projects/ folder this task plausibly belongs to: one of its links points INTO the
    folder (by id), or the folder's name appears in the task title."""
    urls = " ".join(str(l.get("url") or "") for l in task.get("links") or [])
    title = str(task.get("title") or "").casefold()
    for f in folders:
        if f.get("id") and f["id"] in urls:
            return f
    for f in folders:
        name = str(f.get("name") or "").strip().casefold()
        if name and len(name) >= 3 and name in title:
            return f
    return None


def check_loose_tasks(tasks, turns, folders=None, projects=None) -> list[dict]:
    """(a) open, unfiled tasks that look like project work."""
    by_task = turns_by_task(turns)
    out = []
    for t in tasks or []:
        if not _is_open(t) or _is_manual(t) or t.get("project_ext_id"):
            continue
        ext = str(t.get("ext_id") or t.get("id"))
        signals = []
        n_links = len(t.get("links") or [])
        n_turns = len(by_task.get(ext, []))
        n_notes = note_entries(t.get("notes"))
        if n_links >= 2:
            signals.append(f"{n_links} links")
        if n_turns >= 1:
            signals.append(f"{n_turns} turn record{'s' if n_turns != 1 else ''}")
        if n_notes >= 2:
            signals.append(f"{n_notes} note entries")
        if not signals:
            continue
        match = _matching_folder(t, folders or [])
        project = _project_for_folder(match, projects) if match else None
        out.append({"ext_id": ext, "title": t.get("title") or "", "signals": signals,
                    "matching_folder": (match or {}).get("name") or "",
                    "matching_folder_url": folder_url(match["id"]) if match else "",
                    "existing_project": (project or {}).get("ext_id") or "",
                    "suggested_project": ((project or {}).get("name")
                                          or (match or {}).get("name") or _suggest_name(t))})
    return out


def _project_for_folder(folder: dict, projects) -> dict | None:
    """The ACTIVE project already standing for `folder` (linked by id, or same name)."""
    for p in projects or []:
        if (p.get("status") or "active") != "active":
            continue
        if folder_id_from(p) == folder.get("id") or (
                str(p.get("name") or "").strip().casefold()
                == str(folder.get("name") or "").strip().casefold()):
            return p
    return None


def check_projects_without_folder(projects, folders=None) -> list[dict]:
    """(b) active projects with no Drive folder linked."""
    by_name = {str(f.get("name") or "").strip().casefold(): f for f in folders or []}
    out = []
    for p in projects or []:
        if (p.get("status") or "active") != "active" or folder_id_from(p):
            continue
        match = by_name.get(str(p.get("name") or "").strip().casefold())
        out.append({"ext_id": p.get("ext_id") or "", "name": p.get("name") or "",
                    "existing_folder_url": folder_url(match["id"]) if match else ""})
    return out


def dormant_folders(projects, folders) -> list[dict]:
    """(c, informational) Projects/<name>/ folders no project points at by id or name.

    Not a defect — a folder is the archive and outlives its board entry."""
    linked = {folder_id_from(p) for p in projects or []} - {""}
    names = {str(p.get("name") or "").strip().casefold() for p in projects or []}
    return [{"id": f.get("id") or "", "name": f.get("name") or "",
             "url": folder_url(f.get("id") or "")}
            for f in folders or []
            if f.get("id") not in linked
            and str(f.get("name") or "").strip().casefold() not in names]


def check_name_drift(projects, folders) -> list[dict]:
    """(d) a linked folder whose name differs from the project's, or that is not under
    Projects/ at all (so `gdoc publish --project "<name>"` would file somewhere else)."""
    by_id = {f.get("id"): f for f in folders or []}
    out = []
    for p in projects or []:
        fid = folder_id_from(p)
        if not fid:
            continue
        f = by_id.get(fid)
        if f is None:
            out.append({"ext_id": p.get("ext_id") or "", "name": p.get("name") or "",
                        "folder_name": "", "reason": "linked folder is not under Projects/"})
        elif str(f.get("name") or "") != str(p.get("name") or ""):
            out.append({"ext_id": p.get("ext_id") or "", "name": p.get("name") or "",
                        "folder_name": f.get("name") or "",
                        "reason": "folder name differs from project name"})
    return out


def check_unrecorded_tasks(tasks, turns, *, now: datetime, days: int = DEFAULT_RECENT_DAYS):
    """(e) open tasks touched in the last `days` days with no turn record naming them in
    that window."""
    since = now - timedelta(days=days)
    recent_turn_tasks = set()
    for turn in turns or []:
        ts = _parse_ts(turn.get("created_at") or turn.get("reported_at"))
        if ts and ts >= since:
            recent_turn_tasks.update(str(e) for e in turn.get("task_ext_ids") or [])
    out = []
    for t in tasks or []:
        if not _is_open(t) or _is_manual(t):
            continue
        updated = _parse_ts(t.get("updated_at"))
        ext = str(t.get("ext_id") or t.get("id"))
        if updated and updated >= since and ext not in recent_turn_tasks:
            out.append({"ext_id": ext, "title": t.get("title") or "",
                        "updated_at": t.get("updated_at") or ""})
    return out


def list_project_folders(repo, slug, runner=None) -> "tuple[list[dict] | None, str]":
    """The FOLDERS under `<agent root>/Projects/`, read-only.

    The agent root is resolved exactly as `canopy gdoc publish --project` resolves it
    (`_gdoc_identity_from_opts`: $GDRIVE_ROOT_FOLDER, else the repo's agent.json). Unlike
    publish this never creates anything — a missing `Projects/` folder is reported, not made.
    Returns (None, why) on ANY failure: the audit must never fail on Drive."""
    import subprocess

    from orchestrator.agent_gdoc import (FOLDER_MIME, _gdoc_identity_from_opts, _run_gog,
                                         build_list_command, parse_list_result)
    runner = runner or subprocess.run
    try:
        ident = _gdoc_identity_from_opts(repo, slug, None, None)
    except Exception as e:  # noqa: BLE001 — any resolution failure → skip, never fail
        return None, f"agent identity unresolved: {str(e)[:160]}"
    if not ident.root_folder:
        return None, "no Drive root ($GDRIVE_ROOT_FOLDER unset — source the agent's .env)"
    def child_folders(parent):
        # `gog drive ls` returns 20 children by default — far fewer than a busy Projects/.
        r = _run_gog(build_list_command(ident, parent) + ["--max", str(DRIVE_LIST_MAX)], runner)
        if r.returncode != 0:
            raise RuntimeError(f"gog drive ls failed: {(r.stderr or r.stdout or '').strip()[:160]}")
        return [{"id": str(f.get("id") or ""), "name": str(f.get("name") or "")}
                for f in parse_list_result(r.stdout) if f.get("mimeType") == FOLDER_MIME]

    try:
        projects_id = next((f["id"] for f in child_folders(ident.root_folder)
                            if f["name"] == "Projects"), "")
        if not projects_id:
            return [], "no Projects/ folder under the agent root"
        return child_folders(projects_id), ""
    except Exception as e:  # noqa: BLE001
        return None, f"Drive unreachable: {str(e)[:160]}"


def audit(*, slug: str, tasks, projects, turns, folders, drive_note: str = "",
          now: datetime | None = None, recent_days: int = DEFAULT_RECENT_DAYS) -> dict:
    """Run all five checks. `folders=None` means Drive was not readable: (c) and (d) are
    skipped and `drive_note` says why; (a)/(b) then run without folder matching."""
    now = now or datetime.now(timezone.utc)
    drive_ok = folders is not None
    result = {
        "slug": slug,
        "open_tasks": sum(1 for t in tasks or [] if _is_open(t)),
        "projects": len(projects or []),
        "drive": {"ok": drive_ok, "folders": len(folders or []), "note": drive_note},
        "recent_days": recent_days,
        "loose_tasks": check_loose_tasks(tasks, turns, folders or [], projects),
        "projects_without_folder": check_projects_without_folder(projects, folders or []),
        "dormant_folders": dormant_folders(projects, folders) if drive_ok else None,
        "name_drift": check_name_drift(projects, folders) if drive_ok else None,
        "unrecorded_tasks": check_unrecorded_tasks(tasks, turns, now=now, days=recent_days),
    }
    result["closeout"] = closeout_line(result)
    return result


_LABELS = (
    ("loose_tasks", "loose task", "loose tasks"),
    ("projects_without_folder", "project without folder", "projects without folder"),
    ("name_drift", "name drift", "name drifts"),
    ("unrecorded_tasks", "unrecorded task", "unrecorded tasks"),
)


def closeout_line(result: dict) -> str:
    """The REQUIRED turn close-out line (turn.md Step 4): `projects: clean` or a count list."""
    parts = []
    for key, one, many in _LABELS:
        items = result.get(key)
        if items:
            parts.append(f"{len(items)} {one if len(items) == 1 else many}")
    if not result.get("drive", {}).get("ok"):
        parts.append("Drive unchecked")
    if not parts:
        return "projects: clean"
    if parts == ["Drive unchecked"]:
        return "projects: clean (Drive unchecked)"
    return "projects: " + ", ".join(parts)


def render(result: dict) -> str:
    """Short human summary, one suggested command per finding, close-out line last."""
    s = result["slug"]
    drive = result["drive"]
    lines = [f"project-audit {s} — {result['open_tasks']} open tasks, "
             f"{result['projects']} projects, "
             + (f"Drive: {drive['folders']} folders under Projects/"
                if drive["ok"] else f"Drive: unchecked ({drive['note']})")]

    def section(title, items, fmt):
        if items is None:
            lines.append(f"\n{title}: skipped (Drive unchecked)")
            return
        if not items:
            return
        lines.append(f"\n{title} ({len(items)}):")
        for it in items:
            lines.extend("  " + ln for ln in fmt(it))

    section("(a) Open tasks with no project that look like project work "
            "(file each, or say why it is a one-off; pick a real project name)",
            result["loose_tasks"],
            lambda it: [f"{it['ext_id']} {it['title'][:80]}  [{', '.join(it['signals'])}]",
                        (f"  → canopy agent set --slug {s} --task-id {it['ext_id']} "
                         f"--project {it['existing_project']}"
                         if it["existing_project"] else
                         f"  → re-register against its existing folder: canopy agent project-add "
                         f"--slug {s} --name \"{it['matching_folder']}\" --outcome \"…\" "
                         f"--drive-folder-url {it['matching_folder_url']}; then canopy agent set "
                         f"--slug {s} --task-id {it['ext_id']} --project \"{it['matching_folder']}\""
                         if it["matching_folder"] else
                         f"  → canopy agent project-add --slug {s} --name "
                         f"\"{it['suggested_project']}\" --outcome \"…\" --drive-folder-url <url>; "
                         f"then canopy agent set --slug {s} --task-id {it['ext_id']} "
                         f"--project \"{it['suggested_project']}\"")])
    section("(b) Active projects with no Drive folder linked",
            result["projects_without_folder"],
            lambda it: [f"{it['ext_id']} {it['name']}",
                        f"  → canopy agent project-set --slug {s} --project {it['ext_id']} "
                        f"--drive-folder-url "
                        + (it["existing_folder_url"] + "   (Projects/ folder of that name exists)"
                           if it["existing_folder_url"] else
                           "<url>   (create it: canopy gdoc publish --project \""
                           + it["name"] + "\" …)")])
    if result["dormant_folders"] is not None:
        lines.append(f"\n(c) Dormant Projects/ folders — no active board entry; expected, "
                     f"not a finding: {len(result['dormant_folders'])}")
    section("(d) Project name differs from its folder",
            result["name_drift"],
            lambda it: [f"{it['ext_id']} {it['name']!r} vs folder {it['folder_name']!r} "
                        f"— {it['reason']}",
                        f"  → rename one so they match (canopy agent project-set --slug {s} "
                        f"--project {it['ext_id']} --name \"<folder name>\")"
                        if it["folder_name"] else
                        "  → point it at its Projects/<name> folder, or move the folder there"])
    section(f"(e) Open tasks touched in the last {result['recent_days']}d with no turn record",
            result["unrecorded_tasks"],
            lambda it: [f"{it['ext_id']} {it['title'][:80]}  (updated {it['updated_at'][:16]})",
                        f"  → canopy agent turn --slug {s} --task {it['ext_id']} "
                        f"--session-id <id> --title \"…\" [--work-product-url <url>]"])
    lines.append("")
    lines.append(result["closeout"])
    return "\n".join(lines)
