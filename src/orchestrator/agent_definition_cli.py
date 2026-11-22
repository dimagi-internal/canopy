"""`canopy agent definition …` — read and change a STORED agent's definition in canopy-web.

    canopy agent definition show [--revisions] [--revision N]
    canopy agent definition files [--revision N|live|draft]
    canopy agent definition cat <path> [--revision N|live|draft]
    canopy agent definition diff [--revision N] [--against M]
    canopy agent definition edit --set <path> <file> … --delete <path> … -m <msg> [--base N]
    canopy agent definition publish <n>
    canopy agent definition discard <n>
    canopy agent definition rollback <n>
    canopy agent definition convert --from <dir> [-m <msg>] [--drop-refused] [--with-history] [--dry-run]
    canopy agent definition clone [--slug S] [DIR] [--no-helper]
    canopy agent definition export --to-github OWNER/NAME [--ref main] [--push-myself]

A stored agent's definition is a git repository hosted by canopy-web, with revisions
(docs/architecture/stored-agents.md). An agent, or a person with agent-edit, proposes a DRAFT
(`edit`); a human admin publishes it. Every write is checked against the file policy here
first (`agent_definition.check_tree`) so the refusal names the file before anything is
uploaded; canopy-web is the authority. The repo is read with ordinary git (`clone`); nobody
pushes to it — canopy-web is its only writer.
"""
from __future__ import annotations

import base64
import json
import re
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


#: canopy-web's request cap (Django DATA_UPLOAD_MAX_MEMORY_SIZE, 2.5 MB) less slack: an upload
#: body above this is refused here rather than by a 413.
MAX_CONVERT_BODY = 2_400_000


def history_bundle(repo: Path) -> bytes:
    """`git bundle create - main` of ``repo``: its main branch with all its history."""
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q",
                        "refs/heads/main"], capture_output=True, timeout=60)
    if r.returncode != 0:
        raise click.ClickException(f"{repo} has no `main` branch to carry the history of — "
                                   "convert files-only (without --with-history)")
    r = subprocess.run(["git", "-C", str(repo), "bundle", "create", "-", "main"],
                       capture_output=True, timeout=300)
    if r.returncode != 0 or not r.stdout:
        err = (r.stderr or b"").decode("utf-8", "replace").strip()
        raise click.ClickException(f"git bundle create: {err.splitlines()[-1] if err else r.returncode}")
    return r.stdout


def repo_upload(repo: Path, slug: str, *, drop_refused: bool, with_history: bool,
                dry_run: bool = False) -> tuple[dict, dict]:
    """A local repo as an upload: ``(summary, body)``, body carrying ``files`` and, with
    ``with_history``, ``bundle_b64``. Shared by `convert --from` and `create-agent --stored
    --from`. Refuses before upload on a policy problem, on a refused tracked file unless
    ``drop_refused`` (then it is left out), and above :data:`MAX_CONVERT_BODY`. ``dry_run``
    returns the summary without refusing, and an empty body."""
    plan = plan_convert(repo, slug)
    check = {f["path"]: {"content": f["content"]} for f in plan["upload"]}
    problems = check_tree(check, slug)
    summary = {"slug": slug, "upload": [f["path"] for f in plan["upload"]],
               "generated_by_canopy_web": plan["generated"],
               "refused": [f"{p}: {why}" for p, why in plan["refused"]],
               "problems": problems}
    bundle = history_bundle(repo) if with_history else b""
    if with_history:
        summary["history_bundle_bytes"] = len(bundle)
    if dry_run:
        return summary, {}
    if problems:
        raise click.ClickException("refused before upload:\n  " + "\n  ".join(problems))
    if plan["refused"] and not drop_refused:
        raise click.ClickException(
            "these tracked files cannot be in a stored agent (re-run with --drop-refused to "
            "leave them out):\n  " + "\n  ".join(summary["refused"]))
    files = [{"path": f["path"], "content_b64": base64.b64encode(f["content"]).decode("ascii"),
              "executable": False} for f in plan["upload"]]
    body = {"files": files}
    if with_history:
        body["bundle_b64"] = base64.b64encode(bundle).decode("ascii")
    size = len(json.dumps(body))
    if size > MAX_CONVERT_BODY:
        why = ("the history makes it " if with_history else "")
        raise click.ClickException(
            f"the upload is {size / 1e6:.1f} MB; canopy-web takes about "
            f"{MAX_CONVERT_BODY / 1e6:.1f} MB — {why}too large"
            + (": upload files-only (drop --with-history)" if with_history else ""))
    return summary, body


@definition.command("convert")
@click.option("--from", "from_dir", required=True, type=click.Path(exists=True, file_okay=False),
              help="A local checkout of the agent's repo; its TRACKED files are uploaded.")
@click.option("--slug", default=None, help="Default: .claude-plugin/plugin.json `name` in --from.")
@click.option("-m", "--message", default="converted from a repo", show_default=True)
@click.option("--drop-refused", is_flag=True,
              help="Leave out files the policy refuses (workflows, scripts, .py tests) instead "
                   "of stopping.")
@click.option("--with-history", is_flag=True,
              help="Keep the repo's history: send `git bundle create - main` too. canopy-web "
                   "imports it (refs/heads/imported) and revision 1's parent is its tip.")
@click.option("--dry-run", is_flag=True, help="Print the plan; upload nothing.")
def definition_convert(from_dir, slug, message, drop_refused, with_history, dry_run):
    """Turn a REPO agent into a STORED one: its tracked files become revision 1 (published).

    Files canopy-web writes itself (.claude-plugin/*, .claude/settings.json,
    bin/<slug>-email, .gitignore) are not uploaded; it rewrites them. Afterwards the agent
    has no repo_url, and runners and `canopy agent use` clone it from canopy-web.
    --with-history keeps main's history (within canopy-web's ~1.8 MB request limit)."""
    repo = Path(from_dir).expanduser().resolve()
    if not slug:
        try:
            slug = json.loads((repo / ".claude-plugin" / "plugin.json")
                              .read_text(encoding="utf-8"))["name"]
        except (OSError, ValueError, KeyError) as e:
            raise click.ClickException(f"pass --slug (no plugin.json name in {repo}: {e})")
    summary, body = repo_upload(repo, slug, drop_refused=drop_refused,
                                with_history=with_history, dry_run=dry_run)
    if dry_run:
        click.echo(json.dumps(summary, indent=1))
        return
    body["message"] = message
    try:
        out = canopy_web.call("POST", f"/api/agents/{slug}/definition/convert", body)
    except _ERRORS as e:
        if with_history and "-> 413" in str(e):
            raise click.ClickException(f"{e}\ncanopy-web refused the history as too large: "
                                       "upload files-only (drop --with-history)")
        raise click.ClickException(str(e))
    click.echo(json.dumps({**summary, "result": out}, indent=1))


# ── reading the repo with git ───────────────────────────────────────────────────────────

def _git_url(slug: str, token: str) -> str:
    from orchestrator import agent_materialize as am

    base = canopy_web.resolve_base_url(None)
    info = am.definition_info(slug, base_url=base, token=token)
    if str(info.get("source") or "") == "repo" and not info.get("git_url"):
        raise click.ClickException(f"{slug} is not a stored agent"
                                   + (f": its repo is {info['repo_url']}" if info.get("repo_url")
                                      else ""))
    url = str(info.get("git_url") or "")
    if not url and info.get("workspace"):
        url = am.git_url_for(base, str(info["workspace"]), slug)
    if not url:
        raise click.ClickException(f"canopy-web gave no git url for {slug}")
    return url


def _person_token() -> str:
    try:
        return canopy_web.resolve_token(None)
    except RuntimeError as e:
        raise click.ClickException(f"no canopy-web login: {e}")


def _helper_command() -> str:
    import shutil

    exe = shutil.which("canopy") or "canopy"
    return f"!{json.dumps(exe)} agent definition credential"


def _run_git(*args: str, auth=None, cwd=None, timeout: int = 600) -> subprocess.CompletedProcess:
    from orchestrator.agent_materialize import auth_env

    env = auth_env(*auth) if auth else None
    try:
        r = subprocess.run(["git", *args], capture_output=True, text=True, env=env, cwd=cwd,
                           timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise click.ClickException(f"git {args[0]}: {e}")
    return r


def _git_fail(r: subprocess.CompletedProcess, what: str) -> click.ClickException:
    lines = [ln for ln in (r.stderr or r.stdout or "").splitlines() if ln.strip()]
    return click.ClickException(f"{what}: {lines[-1] if lines else r.returncode}")


@definition.command("clone")
@click.option("--slug", envvar="CANOPY_AGENT", required=True,
              help="The stored agent. Default: $CANOPY_AGENT.")
@click.argument("directory", required=False, type=click.Path(file_okay=False))
@click.option("--no-helper", is_flag=True,
              help="Do not set the clone's credential helper (then pass it per command).")
def definition_clone(slug, directory, no_helper):
    """A working copy of a STORED agent's repo (main, the draft, the r<N> tags).

    Read-only: canopy-web refuses every push — propose a change with
    `canopy agent definition edit`. Your token is sent with the clone and never written to
    the repo; later pulls ask `canopy agent definition credential` for it each time."""
    token = _person_token()
    url = _git_url(slug, token)
    dest = Path(directory or slug).expanduser()
    if dest.exists() and any(dest.iterdir()):
        raise click.ClickException(f"{dest} exists and is not empty")
    r = _run_git("clone", "--quiet", "--", url, str(dest), auth=(url, token))
    if r.returncode != 0:
        raise _git_fail(r, f"git clone {url}")
    from orchestrator.agent_materialize import _host_of

    helper_key = f"credential.{_host_of(url).rstrip('/')}.helper"
    if not no_helper:
        _run_git("-C", str(dest), "config", helper_key, _helper_command())
    click.echo(f"Cloned {slug} into {dest} (origin {url}).")
    if no_helper:
        click.echo(f"To pull later: git -C {dest} -c {helper_key}='{_helper_command()}' pull")
    else:
        click.echo(f"To pull later: git -C {dest} pull — git asks `canopy agent definition "
                   "credential` for your canopy token each time; nothing is stored.")
    click.echo("The repo is read-only: change it with `canopy agent definition edit`.")


@definition.command("credential", hidden=True)
@click.argument("operation")
def definition_credential(operation):
    """A git credential helper for canopy's git repos: answers `get` with the canopy-web
    login as the password (the server takes Basic with the token as password). The token
    is read when git asks and never stored; `store`/`erase` do nothing."""
    import sys

    if operation != "get":
        return
    fields = dict(line.split("=", 1) for line in sys.stdin.read().splitlines() if "=" in line)
    from urllib.parse import urlsplit

    base = urlsplit(canopy_web.resolve_base_url(None))
    if fields.get("protocol") != base.scheme or fields.get("host") != base.netloc:
        return
    try:
        token = canopy_web.resolve_token(None)
    except RuntimeError:
        return
    click.echo(f"username=canopy\npassword={token}")


_GITHUB_REPO = r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$"


def _github_token() -> str:
    """The person's own GitHub token, for canopy-web's one push: GH_TOKEN, GITHUB_TOKEN, else
    `gh auth token`. Sent only in the export request body; never printed or stored."""
    import os
    import subprocess

    for var in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(var, "").strip():
            return os.environ[var].strip()
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


@definition.command("export")
@_slug_opt
@click.option("--to-github", "repo", required=True, metavar="OWNER/NAME",
              help="An EMPTY GitHub repo (create it first).")
@click.option("--ref", default="main", show_default=True, help="The branch to write there.")
@click.option("--push-myself", is_flag=True,
              help="Push with YOUR git credentials from here, then tell canopy-web (detach); "
                   "otherwise canopy-web pushes once with your GitHub token (GH_TOKEN or gh).")
def definition_export(slug, repo, ref, push_myself):
    """Move a STORED agent to GitHub with its whole history (main + the r<N> tags).

    Afterwards it is a repo agent (source=repo, repo_url=the GitHub repo); the canopy repo
    stays as a read-only archive. A person who administers the agent only."""
    import re
    import tempfile

    repo = repo.strip().removeprefix("https://github.com/").removesuffix(".git").strip("/")
    if not re.match(_GITHUB_REPO, repo):
        raise click.ClickException(f"--to-github takes OWNER/NAME, not {repo!r}")
    if not push_myself:
        token = _github_token()
        if not token:
            raise click.ClickException(
                "canopy-web pushes with YOUR GitHub token (it holds no readable copy of the "
                "agent's): set GH_TOKEN, or sign in with `gh auth login`; or use --push-myself")
        click.echo(json.dumps(_call("POST", f"{_base(slug)}/export",
                                    {"repo": repo, "ref": ref, "github_token": token}), indent=1))
        return
    gh = f"https://github.com/{repo}.git"
    r = _run_git("ls-remote", gh)
    if r.returncode != 0:
        raise _git_fail(r, f"cannot read {gh} with your git credentials (create the empty "
                           "repo first)")
    if r.stdout.strip():
        raise click.ClickException(f"{gh} is not empty — export needs an empty repo")
    token = _person_token()
    url = _git_url(slug, token)
    with tempfile.TemporaryDirectory(prefix=f"canopy-export-{slug}-") as tmp:
        mirror = str(Path(tmp) / "repo.git")
        r = _run_git("clone", "--quiet", "--bare", "--", url, mirror, auth=(url, token))
        if r.returncode != 0:
            raise _git_fail(r, f"git clone {url}")
        # Your own credentials: no canopy header on this command.
        r = _run_git("-C", mirror, "push", gh, f"refs/heads/main:refs/heads/{ref}",
                     "refs/tags/*:refs/tags/*")
        if r.returncode != 0:
            raise _git_fail(r, f"git push {gh}")
    click.echo(json.dumps(_call("POST", f"{_base(slug)}/detach",
                                {"repo_url": f"https://github.com/{repo}"}), indent=1))


# ── create-agent --stored --from ────────────────────────────────────────────────────────

_STATUS = re.compile(r"-> (\d{3}): (.*)\Z", re.S)

#: The request fields `--from` adds; an older canopy-web refuses them as extra inputs.
FROM_FIELDS = ("files", "bundle_b64", "drop_refused", "message")

TOO_OLD = ("this canopy-web is too old for `create-agent --stored --from` (it does not take "
           "files): create the agent without --from and add its files with "
           "`canopy agent definition edit`, or retry once canopy-web is updated")


def _error_body(e: Exception) -> tuple[int, str, dict]:
    """``(status, raw body text, parsed JSON body or {})`` of a canopy_web error; status 0 when
    it carries none. canopy_web cuts the body at 400 characters, so the JSON may not parse."""
    m = _STATUS.search(str(e))
    if not m:
        return 0, str(e), {}
    raw = m.group(2)
    try:
        body = json.loads(raw)
    except ValueError:
        body = {}
    return int(m.group(1)), raw, body if isinstance(body, dict) else {}


def is_too_old_refusal(status: int, raw: str) -> bool:
    """A 422 validation error naming one of :data:`FROM_FIELDS` as an extra input."""
    return status == 422 and ("extra_forbidden" in raw or "Extra inputs" in raw) and any(
        f'"{f}"' in raw for f in FROM_FIELDS)


def stored_create_error(e: Exception, slug: str, *, with_history: bool) -> click.ClickException:
    """A `POST /api/agents/stored` failure, said in terms of what to do next."""
    status, raw, body = _error_body(e)
    err = body.get("error")
    if is_too_old_refusal(status, raw):
        return click.ClickException(TOO_OLD)
    if status == 409 or err == "exists":
        return click.ClickException(f"an agent called {slug} already exists: pick another slug")
    if status == 413 or err == "too_large":
        return click.ClickException(
            "canopy-web refused the upload as too large (about 1.8 MB of files and history)"
            + (": drop --with-history" if with_history else ""))
    if err == "policy" or (status == 422 and '"policy"' in raw):
        lines = [f"{p.get('path')}: {p.get('message') or p.get('code')}"
                 for p in (body.get("problems") or []) if isinstance(p, dict)]
        return click.ClickException(
            "canopy-web refused these files (re-run with --drop-refused to leave them out):\n  "
            + "\n  ".join(lines or [raw]))
    if err == "secret" or (status == 422 and '"secret"' in raw):
        return click.ClickException(
            "canopy-web found a secret in the repo's history: drop --with-history to upload "
            f"the files only, or rewrite the history ({body.get('detail') or raw})")
    if status == 403:
        return click.ClickException(
            f"canopy-web refused to create {slug} ({body.get('detail') or body.get('title') or raw}): "
            "creating an agent needs the editor role in the workspace, signed in as a person")
    return click.ClickException(str(e))
