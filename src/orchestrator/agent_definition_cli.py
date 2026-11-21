"""`canopy agent definition …` — read and change a STORED agent's definition in canopy-web.

    canopy agent definition show [--revisions] [--revision N]
    canopy agent definition files [--revision N|live|draft]
    canopy agent definition cat <path> [--revision N|live|draft]
    canopy agent definition diff [--revision N] [--against M]
    canopy agent definition edit --set <path> <file> … --delete <path> … -m <msg> [--base N]
    canopy agent definition publish <n>
    canopy agent definition discard <n>
    canopy agent definition rollback <n>
    canopy agent definition convert --from <dir> [-m <msg>] [--drop-refused] [--dry-run]

A stored agent's definition is a file tree with revisions (docs/architecture/stored-agents.md).
An agent, or a person with agent-edit, proposes a DRAFT (`edit`); a human admin publishes it.
Every write is checked against the file policy here first (`agent_definition.check_tree`) so
the refusal names the file before anything is uploaded; canopy-web is the authority.
"""
from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

import click

from orchestrator import canopy_web
from orchestrator.agent_definition import check_tree, classify_path


def _call(method: str, path: str, body=None):
    try:
        return canopy_web.call(method, path, body)
    except (canopy_web.CanopyError, RuntimeError, OSError) as e:
        raise click.ClickException(str(e))


_ERRORS = (canopy_web.CanopyError, RuntimeError, OSError)


def _base(slug: str) -> str:
    return f"/api/agents/{slug}/definition"


def _resolve_number(slug: str, revision: str) -> int:
    """`live` / `draft` / a number → the revision number."""
    rev = str(revision or "live").strip().lower()
    if rev.isdigit():
        return int(rev)
    body = _call("GET", _base(slug)) or {}
    block = body.get(rev) if rev in ("live", "draft") else None
    if not isinstance(block, dict) or not block.get("number"):
        raise click.ClickException(f"{slug} has no {rev} revision")
    return int(block["number"])


_slug_opt = click.option("--slug", envvar="CANOPY_AGENT", required=True,
                         help="The stored agent. Default: $CANOPY_AGENT.")


@click.group("definition")
def definition():
    """A STORED agent's definition: its files and revisions in canopy-web."""


@definition.command("show")
@_slug_opt
@click.option("--revisions", is_flag=True, help="List every revision.")
@click.option("--revision", default=None, help="One revision's record (number, live or draft).")
def definition_show(slug, revisions, revision):
    """The definition summary (live, draft, policy), the revision list, or one revision."""
    if revisions:
        body = _call("GET", f"{_base(slug)}/revisions")
    elif revision:
        body = _call("GET", f"{_base(slug)}/revisions/{_resolve_number(slug, revision)}")
    else:
        body = _call("GET", _base(slug))
    click.echo(json.dumps(body, indent=1))


@definition.command("files")
@_slug_opt
@click.option("--revision", default="live", show_default=True)
@click.option("--json", "as_json", is_flag=True)
def definition_files(slug, revision, as_json):
    """The files of a revision (path, size, x for executable)."""
    n = _resolve_number(slug, revision)
    tree = (_call("GET", f"{_base(slug)}/revisions/{n}") or {}).get("tree") or {}
    if as_json:
        click.echo(json.dumps(tree, indent=1))
        return
    for path in sorted(tree):
        e = tree[path] or {}
        click.echo(f"{'x' if e.get('executable') else ' '} {int(e.get('size') or 0):>8}  {path}")


@definition.command("cat")
@_slug_opt
@click.argument("path")
@click.option("--revision", default="live", show_default=True)
def definition_cat(slug, path, revision):
    """Print one file of a revision."""
    n = _resolve_number(slug, revision)
    try:
        text = canopy_web.call_text(
            "GET", f"/api/agents/{slug}/definition/revisions/{n}/files/{path.lstrip('/')}")
    except _ERRORS as e:
        raise click.ClickException(str(e))
    click.echo(text, nl=False)


@definition.command("diff")
@_slug_opt
@click.option("--revision", default="draft", show_default=True)
@click.option("--against", default="live", show_default=True)
def definition_diff(slug, revision, against):
    """Unified diff of a revision against another (default: the draft against live)."""
    n, m = _resolve_number(slug, revision), _resolve_number(slug, against)
    try:
        text = canopy_web.call_text(
            "GET", f"/api/agents/{slug}/definition/revisions/{n}/diff?against={m}")
    except _ERRORS as e:
        raise click.ClickException(str(e))
    try:
        body = json.loads(text)
    except ValueError:
        click.echo(text, nl=False)
        return
    rows = (body.get("files") or body.get("diffs")) if isinstance(body, dict) else body
    if isinstance(rows, list):
        for r in rows:
            click.echo((r.get("diff") or r.get("patch") or "") if isinstance(r, dict) else str(r))
    else:
        click.echo(json.dumps(body, indent=1))


@definition.command("edit")
@_slug_opt
@click.option("--set", "sets", nargs=2, multiple=True, metavar="PATH FILE",
              help="Write PATH from local FILE (repeatable).")
@click.option("--delete", "deletes", multiple=True, metavar="PATH", help="Delete PATH (repeatable).")
@click.option("-m", "--message", required=True)
@click.option("--base", default="live", show_default=True,
              help="The revision the draft builds on; canopy-web refuses a base that is not live.")
def definition_edit(slug, sets, deletes, message, base):
    """Create or replace the single DRAFT. A human admin publishes it."""
    if not sets and not deletes:
        raise click.ClickException("nothing to change: pass --set PATH FILE or --delete PATH")
    changes, check = [], {}
    for path, local in sets:
        try:
            content = Path(local).expanduser().read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise click.ClickException(f"{local}: {e}")
        changes.append({"path": path, "content": content})
        check[path] = {"content": content}
    for path in deletes:
        changes.append({"path": path, "content": None})
        check[path] = {"content": None}
    problems = check_tree(check, slug)
    if problems:
        raise click.ClickException("refused before upload:\n  " + "\n  ".join(problems))
    body = {"base": _resolve_number(slug, base), "changes": changes, "message": message}
    click.echo(json.dumps(_call("POST", f"{_base(slug)}/drafts", body), indent=1))


@definition.command("publish")
@_slug_opt
@click.argument("number", type=int)
def definition_publish(slug, number):
    """Publish draft NUMBER (a human with agent-admin; never an agent login)."""
    click.echo(json.dumps(_call("POST", f"{_base(slug)}/revisions/{number}/publish", {}), indent=1))


@definition.command("discard")
@_slug_opt
@click.argument("number", type=int)
def definition_discard(slug, number):
    """Discard draft NUMBER."""
    click.echo(json.dumps(_call("POST", f"{_base(slug)}/revisions/{number}/discard", {}), indent=1))


@definition.command("rollback")
@_slug_opt
@click.argument("number", type=int)
def definition_rollback(slug, number):
    """Publish a NEW revision whose files equal revision NUMBER's (history stays append-only)."""
    click.echo(json.dumps(_call("POST", f"{_base(slug)}/rollback", {"revision": number}), indent=1))


def tracked_files(repo: Path) -> list[dict]:
    """The repo's tracked files: {path, mode, content}. Mode is git's (100644/100755/120000/…)."""
    r = subprocess.run(["git", "-C", str(repo), "ls-files", "-s", "-z"], capture_output=True,
                       timeout=60)
    if r.returncode != 0:
        raise click.ClickException(f"{repo} is not a git repo: "
                                   f"{r.stderr.decode('utf-8', 'replace').strip()}")
    out = []
    for rec in r.stdout.split(b"\0"):
        if not rec:
            continue
        meta, _, path = rec.partition(b"\t")
        mode = meta.split()[0].decode()
        p = path.decode("utf-8", "surrogateescape")
        content = None
        if mode in ("100644", "100755"):
            try:
                content = (repo / p).read_bytes()
            except OSError:
                content = None
        out.append({"path": p, "mode": mode, "content": content})
    return out


def plan_convert(repo: Path, slug: str) -> dict:
    """{upload: [...], generated: [...], refused: [(path, why)]} for `convert --from`."""
    upload, generated, refused = [], [], []
    for f in tracked_files(repo):
        if f["mode"] not in ("100644", "100755") or f["content"] is None:
            refused.append((f["path"], "not a regular file (a link or submodule)"))
            continue
        kind = classify_path(f["path"], slug, f["mode"] == "100755")
        if kind == "generated":
            generated.append(f["path"])
        elif kind == "editable":
            upload.append(f)
        else:
            refused.append((f["path"], kind.removeprefix("refused: ")))
    return {"upload": upload, "generated": generated, "refused": refused}


@definition.command("convert")
@click.option("--from", "from_dir", required=True, type=click.Path(exists=True, file_okay=False),
              help="A local checkout of the agent's repo; its TRACKED files are uploaded.")
@click.option("--slug", default=None, help="Default: .claude-plugin/plugin.json `name` in --from.")
@click.option("-m", "--message", default="converted from a repo", show_default=True)
@click.option("--drop-refused", is_flag=True,
              help="Leave out files the policy refuses (workflows, scripts, .py tests) instead "
                   "of stopping.")
@click.option("--dry-run", is_flag=True, help="Print the plan; upload nothing.")
def definition_convert(from_dir, slug, message, drop_refused, dry_run):
    """Turn a REPO agent into a STORED one: its tracked files become revision 1 (published).

    Files canopy-web writes itself (.claude-plugin/*, .claude/settings.json,
    bin/<slug>-email, .gitignore) are not uploaded; it rewrites them. Afterwards the agent
    has no repo_url, and runners and `canopy agent use` materialize it from canopy-web."""
    repo = Path(from_dir).expanduser().resolve()
    if not slug:
        try:
            slug = json.loads((repo / ".claude-plugin" / "plugin.json")
                              .read_text(encoding="utf-8"))["name"]
        except (OSError, ValueError, KeyError) as e:
            raise click.ClickException(f"pass --slug (no plugin.json name in {repo}: {e})")
    plan = plan_convert(repo, slug)
    check = {f["path"]: {"content": f["content"]} for f in plan["upload"]}
    problems = check_tree(check, slug)
    summary = {"slug": slug, "upload": [f["path"] for f in plan["upload"]],
               "generated_by_canopy_web": plan["generated"],
               "refused": [f"{p}: {why}" for p, why in plan["refused"]],
               "problems": problems}
    if dry_run:
        click.echo(json.dumps(summary, indent=1))
        return
    if problems:
        raise click.ClickException("refused before upload:\n  " + "\n  ".join(problems))
    if plan["refused"] and not drop_refused:
        raise click.ClickException(
            "these tracked files cannot be in a stored agent (re-run with --drop-refused to "
            "leave them out):\n  " + "\n  ".join(summary["refused"]))
    files = [{"path": f["path"], "content_b64": base64.b64encode(f["content"]).decode("ascii"),
              "executable": False} for f in plan["upload"]]
    out = _call("POST", f"/api/agents/{slug}/definition/convert",
                {"files": files, "message": message})
    click.echo(json.dumps({**summary, "result": out}, indent=1))
