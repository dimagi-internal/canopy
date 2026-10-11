"""A task's Drive folder is a property of the task (T72, canopy-web#1403).

A product area is ONE project board, and a big piece of work inside it is a task with
subtasks — not a subproject (Jonathan, 2026-10-09). The files follow the same shape:

    <agent root>/Projects/<project>/<task ext_id> <task title>/
    <agent root>/Projects/<project>/<parent folder>/<subtask ext_id> <subtask title>/

`ensure_task_folder` is the one operation, the task-level twin of
`project_folder.ensure_project_folder`: a folder the task already links always wins;
otherwise its container (the parent task's folder, else the project's) is resolved first,
the task's folder is found or created inside it, and the id is PATCHed onto the task so the
next session reads it straight off the record.

A folder is FOUND by its ext_id prefix ("T71 …"), not by its full name: a title gets
reworded on the board long after the folder was made, and matching the exact name would
then make a second folder beside the first.
"""
from __future__ import annotations

import re
import subprocess
import sys

from orchestrator.agent_gdoc import (
    FOLDER_MIME,
    AgentGdocError,
    GdocIdentity,
    _run_gog,
    build_list_command,
    build_mkdir_command,
    parse_list_result,
    parse_mkdir_result,
)
from orchestrator.project_audit import folder_id_from, folder_url

#: Drive accepts long names, but a folder name is read in a narrow column.
_MAX_NAME = 120


def task_folder_name(task: dict) -> str:
    """`T71 Sophie's RUTF tender` — the ext_id, a space, the title on one line."""
    ext_id = str(task.get("ext_id") or "").strip()
    title = re.sub(r"\s+", " ", str(task.get("title") or "")).strip()
    name = f"{ext_id} {title}".strip()
    return name if len(name) <= _MAX_NAME else name[:_MAX_NAME - 1].rstrip() + "…"


def find_task_folder(ident: GdocIdentity, container: str, ext_id: str,
                     runner=subprocess.run) -> str:
    """The folder under `container` that belongs to `ext_id` — named exactly `ext_id`, or
    starting with `ext_id` and a space (case-insensitive) — or "" when there is none.
    Two candidates is ambiguous: refuse rather than file into the wrong one."""
    r = _run_gog(build_list_command(ident, container), runner)
    if r.returncode != 0:
        raise AgentGdocError(f"gog drive ls failed listing {container} as {ident.account}: "
                             f"{(r.stderr or r.stdout or '').strip()[:300]}")
    key = ext_id.strip().casefold()
    hits = [f for f in parse_list_result(r.stdout)
            if f.get("mimeType") == FOLDER_MIME
            and (str(f.get("name") or "").casefold() == key
                 or str(f.get("name") or "").casefold().startswith(key + " "))]
    if len(hits) > 1:
        names = ", ".join(repr(f.get("name")) for f in hits)
        raise AgentGdocError(f"{len(hits)} folders under {container} belong to {ext_id} ({names}) "
                             f"— link the right one onto the task (patch_task {ext_id} "
                             f"drive_folder_id=<id>) and publish again")
    return str(hits[0].get("id") or "") if hits else ""


def _container(client, ident: GdocIdentity, task: dict, *, runner, trace, dry_run: bool,
               depth: int) -> str:
    """The folder `task`'s own folder goes in: its parent task's, else its project's."""
    from orchestrator.project_folder import ensure_project_folder

    parent_ref = str(task.get("parent_ext_id") or "").strip()
    if parent_ref:
        parent = client.get_task(parent_ref)
        return ensure_task_folder(client, ident, parent, runner=runner, trace=trace,
                                  dry_run=dry_run, _depth=depth + 1)["folder_id"]
    project_ref = str(task.get("project_ext_id") or "").strip()
    if not project_ref:
        raise AgentGdocError(
            f"task {task.get('ext_id')} is in no project, and a task's folder lives inside its "
            f"project's (Projects/<project>/<task>). File it into one first "
            f"(patch_task project=P<N>), or publish with --project / --parent instead")
    project = client.get_project(project_ref)
    if dry_run:
        return folder_id_from(project) or f"(would find-or-create Projects/{project.get('name')})"
    got = ensure_project_folder(client, ident, project, runner=runner, trace=trace, strict=False)
    return got["folder_id"]


def ensure_task_folder(client, ident: GdocIdentity, task: dict, *, runner=subprocess.run,
                       trace: list | None = None, dry_run: bool = False, strict: bool = True,
                       _depth: int = 0) -> dict:
    """Make sure `task` links its Drive folder; return where it is.

    Returns {ext_id, name, folder_id, folder_url, created, linked, link_error}, the same
    shape as `ensure_project_folder`. With `strict=False` a failed write-back is reported in
    `link_error` rather than raised: a publish already has its folder."""
    if _depth > 5:  # canopy-web caps the tree at three levels; a loop here is corrupt data
        raise AgentGdocError(f"task {task.get('ext_id')}'s parent chain does not end")
    ext_id = str(task.get("ext_id") or "").strip()
    name = task_folder_name(task)
    out = {"ext_id": ext_id, "name": name, "folder_id": "", "folder_url": "",
           "created": False, "linked": False, "link_error": ""}
    fid = folder_id_from(task)
    if fid:
        out.update(folder_id=fid, folder_url=str(task.get("drive_folder_url") or "") or folder_url(fid))
        if str(task.get("drive_folder_id") or "").strip():
            return out
    else:
        container = _container(client, ident, task, runner=runner, trace=trace,
                               dry_run=dry_run, depth=_depth)
        if dry_run:
            out["folder_url"] = f"(would find-or-create {name!r} in {container} and link it to {ext_id})"
            out["folder_id"] = out["folder_url"]
            return out
        fid = find_task_folder(ident, container, ext_id, runner)
        if fid:
            if trace is not None:
                trace.append({"name": name, "id": fid, "created": False})
        else:
            r = _run_gog(build_mkdir_command(ident, name, container), runner)
            if r.returncode != 0:
                raise AgentGdocError(f"gog drive mkdir {name!r} under {container} failed as "
                                     f"{ident.account}: {(r.stderr or r.stdout or '').strip()[:300]}")
            fid = parse_mkdir_result(r.stdout)
            if not fid:
                raise AgentGdocError(f"gog drive mkdir {name!r} returned no folder id: {r.stdout!r}")
            out["created"] = True
            if trace is not None:
                trace.append({"name": name, "id": fid, "created": True})
        out.update(folder_id=fid, folder_url=folder_url(fid))
    if dry_run:
        return out
    try:
        client.patch_task(ext_id, drive_folder_id=out["folder_id"], drive_folder_url=out["folder_url"])
        out["linked"] = True
    except Exception as e:  # noqa: BLE001
        if strict:
            raise
        out["link_error"] = str(e)[:200]
    return out


def resolve_task_destination(ident: GdocIdentity, task_ref: str, *, client=None,
                             runner=subprocess.run, trace: list | None = None,
                             warn=sys.stderr.write, dry_run: bool = False) -> str:
    """The folder id a `--task` publish files into: the task's linked folder, found or
    created (and linked) under its project's folder when it has none. Unlike `--project`
    there is no by-name fallback — a task ref canopy-web does not know is a typo, and
    filing a deliverable under a guess is the failure this layout exists to end."""
    from orchestrator.agent_client import AgentClient

    client = client or AgentClient({"slug": ident.slug})
    try:
        task = client.get_task(task_ref)
    except Exception as e:  # noqa: BLE001
        raise AgentGdocError(f"could not read task {task_ref!r} on {ident.slug}'s board "
                             f"({str(e)[:160]})") from e
    got = ensure_task_folder(client, ident, task, runner=runner, trace=trace,
                             dry_run=dry_run, strict=False)
    if got["link_error"]:
        warn(f"NOTE: filed into {got['folder_url']} but could not link it to {got['ext_id']} "
             f"({got['link_error']}). Link it: patch_task {got['ext_id']} "
             f"drive_folder_id={got['folder_id']}\n")
    return got["folder_id"]
