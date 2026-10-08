"""A project's Drive folder is a property of the project.

agent-core/task-tracker.md says a project has two halves with one name: the canopy-web project
(P<N>) and the Drive folder `<agent root>/Projects/<name>/`. Nothing joined them. `project-add`
recorded a folder only if one was passed, and `canopy gdoc publish --project` found-or-created
`Projects/<name>` BY NAME and never wrote the id back. So the record a session can see
(`get_project`) carried an empty `drive_folder_id` and no way to fill it. On 2026-10-08 a
connect-labs session told to "write to Hal's project folder" found P1 with no folder and P4
with an empty one, asked the human which was meant, and gave up. Six of eighteen active fleet
projects had no folder at all.

`ensure_project_folder` is the one operation: find-or-create the folder as the agent and PATCH
it onto the project. It is idempotent, and a folder the project already links always wins. The
CLI surfaces are `canopy agent project-folder`, `project-add` (by default), and
`canopy gdoc|gsheet publish --project`, which now resolve through the project record.
"""
from __future__ import annotations

import subprocess
import sys

from orchestrator.agent_gdoc import GdocIdentity, resolve_subfolder
from orchestrator.project_audit import folder_id_from, folder_url


def find_project(projects: list[dict], ref: str) -> dict | None:
    """The project `ref` names (its P<N> ext_id, or its exact name, case-insensitive), or None.

    A name shared by two projects matches neither: guessing between them would file a
    deliverable into the wrong one."""
    raw = str(ref or "").strip().casefold()
    if not raw:
        return None
    for p in projects:
        if str(p.get("ext_id") or "").strip().casefold() == raw:
            return p
    named = [p for p in projects if str(p.get("name") or "").strip().casefold() == raw]
    return named[0] if len(named) == 1 else None


def ensure_project_folder(client, ident: GdocIdentity, project: dict, *,
                          runner=subprocess.run, trace: list | None = None,
                          dry_run: bool = False, strict: bool = True) -> dict:
    """Make sure `project` links its Drive folder; return where it is.

    An already-linked folder wins, even when its name differs from the project's: someone
    pointed the project at it on purpose. A link held only as a url gets its id filled in.
    Otherwise `<agent root>/Projects/<project name>` is found or created as the agent and
    PATCHed onto the project, so the next session reads it straight off the record.

    Returns {ext_id, name, folder_id, folder_url, created, linked, link_error}: `created`
    means a Drive folder was made, `linked` means the project record was written. With
    `strict=False` a failed write-back is reported in `link_error` instead of raised — a
    publish has already got its folder and must not fail on the bookkeeping."""
    ext_id = str(project.get("ext_id") or "")
    name = str(project.get("name") or "").strip()
    out = {"ext_id": ext_id, "name": name, "folder_id": "", "folder_url": "",
           "created": False, "linked": False, "link_error": ""}
    fid = folder_id_from(project)
    if fid:
        out.update(folder_id=fid,
                   folder_url=str(project.get("drive_folder_url") or "") or folder_url(fid))
        if str(project.get("drive_folder_id") or "").strip():
            return out
    elif dry_run:
        out["folder_url"] = f"(would find-or-create Projects/{name} and link it to {ext_id})"
        return out
    else:
        if not name:
            raise ValueError(f"project {ext_id or '?'} has no name to name its folder after")
        steps: list = []
        fid = resolve_subfolder(ident, project=name, runner=runner, trace=steps)
        if trace is not None:
            trace.extend(steps)
        out.update(folder_id=fid, folder_url=folder_url(fid),
                   created=any(s.get("created") for s in steps))
    if dry_run:
        return out
    try:
        client.patch_project(ext_id, drive_folder_id=fid, drive_folder_url=out["folder_url"])
        out["linked"] = True
    except Exception as e:  # noqa: BLE001
        if strict:
            raise
        out["link_error"] = str(e)[:200]
    return out


def resolve_project_destination(ident: GdocIdentity, project_ref: str, *, client=None,
                                runner=subprocess.run, trace: list | None = None,
                                warn=sys.stderr.write) -> str:
    """The folder id a `--project` publish files into, resolved THROUGH the project record.

    A ref that names a project on the agent's board (P<N> or its name) uses the folder it
    links, linking one first if it has none. A ref naming no project, or a board that cannot
    be read, falls back to finding `Projects/<ref>` by name (the old behaviour) with a note
    that the folder is linked to nothing. A failed write-back never fails the publish."""
    from orchestrator.agent_client import AgentClient

    client = client or AgentClient({"slug": ident.slug})
    try:
        project = find_project(client.list_projects(), project_ref)
        why = f"no project {project_ref!r} on {ident.slug}'s board"
    except Exception as e:  # noqa: BLE001 — the board is advisory here; Drive is the write
        project, why = None, f"could not read {ident.slug}'s projects ({str(e)[:160]})"
    if project is None:
        warn(f"NOTE: {why} — filing into Projects/{project_ref} by name, linked to no "
             f"project. Register it: canopy agent project-add --slug {ident.slug} "
             f"--name \"{project_ref}\"\n")
        return resolve_subfolder(ident, project=project_ref, runner=runner, trace=trace)
    got = ensure_project_folder(client, ident, project, runner=runner, trace=trace, strict=False)
    if got["link_error"]:
        warn(f"NOTE: filed into {got['folder_url']} but could not link it to "
             f"{got['ext_id']} ({got['link_error']}). Link it: canopy agent project-folder "
             f"--slug {ident.slug} --project {got['ext_id']}\n")
    return got["folder_id"]
