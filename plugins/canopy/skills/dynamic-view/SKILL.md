---
name: dynamic-view
description: |
  Make or change a dynamic view on canopy-web — an AI-written screen (JSX, drawn in a
  sandbox) that lives on a workspace or a project page and shows canopy's own data:
  tasks, projects, milestones, agents, members, sessions, analytics. Use when asked to
  "make a view / dashboard / board / page of X", "add a view to project Y", "show me
  open work by agent on a page", "change / fix / roll back that view", or when the same
  canopy read keeps being re-assembled by hand in chat. Not for data outside canopy-web
  (labs, Drive, GitHub): a view can only read canopy's catalog.
---

# Dynamic view

A dynamic view is a saved screen on canopy-web: JSX defining `function View(props)` plus
the canopy reads it needs, drawn in a sandboxed frame at `/w/<ws>/views/<id>` and, for a
project view, in the Views section of the project's page. Everything goes through the
`canopy-web` MCP server's `*_dynamic_view*` tools. Design:
canopy-web `docs/architecture/dynamic-views.md`.

**This skill is the judgment; `get_dynamic_view_guide` is the contract.** The guide returns
the props, the component kit, the full data catalog and a working example — read it every
time rather than writing from memory, because the catalog grows and a source it does not
list is refused on save.

## Step 1 — place it: workspace or project

- **Project view** (`project=<handle>`) when it is about one project's work — its board,
  milestones, updates. It appears on that project's page, and the project's lead and the
  agents working on it may edit it even if they are only viewers of the workspace.
- **Workspace view** (no `project`) when it spans the workspace. Only workspace editors
  may make or change one.
- Name the `workspace` explicitly. A write lands in exactly the named workspace, and the
  view's reads are always scoped to it — a view cannot read another workspace.

Who may do what (canopy-web enforces it; a 403 names the rule):

| Action | Needs |
|---|---|
| read, list, history | membership of the workspace |
| create, update, rollback, delete | editor role in the workspace, **or** edit rights on the view's project |
| move between workspace and a project | those rights at **both** ends |

If you get a 403, report it with the rule — do not route around it by putting the view
somewhere else you happen to have rights.

## Step 2 — write it

1. `get_dynamic_view_guide(workspace=…)`. Pick sources from `catalog` only; pass only the
   `params` it lists. On a project view, `path.handle` may be omitted (it is the view's own).
2. Unsure what a model returns? Read the same route with the matching canopy-web MCP tool
   first (`list_tasks`, `list_workspace_projects`, …) — lists are mostly plain arrays, a few
   are pages (`{"items": [...]}`), and guessing the shape is the commonest broken view.
3. Handle `{error}` and empty for every source. Dense tables over cards of cards; lead with
   the number or list that matters; canopy's semantic colours only.
4. `create_dynamic_view(workspace, project?, title, description, code, sources, note)`.
   A refusal (422) names exactly what is wrong — fix that and resend.

**Read only what the view needs.** Everyone who can open it sees what it shows, and so can
its author. The catalog leaves out secrets, but a view that pulls every session title in a
workspace to show a count still shows those titles to its viewers.

## Step 3 — check it draws

A 201 means the code defines `View` and the sources are valid catalog reads. It does
**not** mean the view renders: a wrong data shape, a runtime throw, or an empty source all
save fine. Open the returned `url` as someone who can see it and confirm it draws real rows
(the frame's error boundary shows the throw if it does not). Never hand over a view's link
you have not loaded.

## Changing one

- `get_dynamic_view(view_id)` for the current code and sources, then
  `update_dynamic_view(view_id, …only the fields that change…, note="why")`. Every change is
  a new version; leave a `note` so the history reads.
- Broke it? `list_dynamic_view_revisions(view_id)` and `rollback_dynamic_view(view_id,
  version)` — a rollback is itself a new version, so nothing is lost.
- **Edit in place; do not delete and re-create.** Deleting drops the history and changes the
  id, so every link to the old view breaks. Delete only a view that should no longer exist.
- Find one with `list_dynamic_views(workspace, project?)`.
