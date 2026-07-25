"""Shared Google-Doc authoring engine for the agent fleet — backs `canopy gdoc`.

The sibling of `canopy email` (agent_email.py). Implements the interim doc-authoring
adapter of docs/architecture/shared-gog-gdrive.md: publish a markdown deliverable as a
*rendered* Google Doc **authored as the agent itself** (echo@ / eva@ / hal@ / ada@),
filed into a shared Drive folder, shared, and permission-verified — one place, fixed
once, propagated to the whole fleet.

Why this exists (the friction Hal hit, 2026-07-21): §3's email adapter shipped, but the
doc-authoring capability was parked behind §5's bigger `gws-mcp` extraction. In the gap,
echo / eva / hal each hand-rolled their own gdoc publisher — three different
upload/convert/share implementations of one procedure, drifting on the details that
matter (echo shares by domain via a keychain-authed REST call, eva by `gog drive upload
--convert`, hal by `--convert-to doc`). This collapses them to one engine + per-agent
identity, exactly as email did.

Why gog, not the Drive REST API (echo's route): the shared engine auths the SAME way
`canopy email` does — through gog's own token bucket (`--account` + `--client`), never
the macOS Keychain. echo_gdoc.py minted a bearer token via `security find-generic-password`,
which blocks forever on a GUI prompt in the non-interactive shells turns run in
(dimagi-internal/ace#827). gog v0.12+ covers every operation we need — `drive upload
--convert-to doc` (create), the `docs write` + `docs find-replace --format markdown` pair
(in-place body replace of a NATIVE Doc, preserving its id/link/permissions), `drive share`,
and `drive permissions` (the share-verify step both echo and hal hand-rolled).

Why the replace path edits through the Docs API, not a Drive media overwrite (issue #353):
a `files.update` with media — what `drive upload --replace` does — is **forbidden by the
Drive API on Workspace-native files** (`mimeType=application/vnd.google-apps.document`),
which is exactly the type `--replace` targets. So it always errored, and agents fell back
to publish-fresh + trash-old, churning the doc URL on every revision. The fix routes a
native-Doc replace through the Docs API instead: blank the body to a sentinel (`docs
write`, plain text), then swap that sentinel for the markdown-rendered content (`docs
find-replace --format markdown`, which does the md→Doc conversion — headings, lists,
tables, images). Same id, same link, same permissions.

Why NOT the ace-gdrive MCP: it authenticates as a shared GWS **service account**, so it
cannot author a Doc *as* the agent — the entire point of a fleet deliverable. gog-OAuth
per-mailbox is the identity model the fleet already runs on (§3). The full ~50-atom
Workspace MCP extraction (§5, `drive_*`/`docs_*`/`sheets_*`/`slides_*` batch ops) still
stands and can absorb this later; this adapter deliberately covers only the single
high-frequency operation, in the spirit of §3's "adapter first, small, high leverage."

Identity is resolved exactly as email's is — the agent's Google account (`email`) + gog
client (`gog_client`) from its repo's `config/agent.json`. The account is the same
Workspace identity that sends the agent's mail; here it authors the agent's docs.
"""
from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import click

# Reuse the shared identity resolution rather than re-deriving it — re-deriving is the
# exact drift this adapter exists to kill. The "email identity" (slug + Google account +
# gog client) IS the agent's Workspace identity; it authors docs as readily as it sends
# mail. The identity-bleed warning in _identity_from_opts applies verbatim to publishing
# as another agent.
from orchestrator.agent_email import (
    AgentEmailError,
    EmailIdentity,
    _identity_from_opts,
    _with_identity_options,
    resolve_email_identity,
)

UPLOAD_TIMEOUT = 120  # seconds — a hung gog must not hang the whole turn
GOG_NOT_FOUND = "gog CLI not found on PATH (brew install steipete/tap/gogcli)"


class AgentGdocError(Exception):
    """Raised for identity/config problems or a failed publish."""


@dataclass
class GdocIdentity:
    slug: str        # agent slug, e.g. "hal"
    account: str     # Google account / mailbox, e.g. hal@dimagi-ai.com — authors the doc
    client: str      # gog client name (credentials-<client>.json), usually == slug
    root_folder: str = ""   # the agent's <Agent> Drive folder — see resolve_gdoc_identity
    share_default: str = "domain"  # default share posture (agent.json `gdrive_share_default`)
    repo: Path | None = None


# The agent's <Agent> Drive folder id. Resolved from the per-agent 1Password vault
# (`op://Agent-<Slug>/gdrive-root-folder/credential`) via the agent's config/secrets.yaml (or
# .env.tpl) and materialized into ~/.<agent>/.env by `canopy provision`. A Drive folder id is
# environment-specific — it differs per Workspace/tenant — so it is referenced, never
# committed. See agent-core/agent-runtime.md.
GDRIVE_ROOT_ENV = "GDRIVE_ROOT_FOLDER"


def resolve_gdoc_identity(repo_dir: Path) -> GdocIdentity:
    """Resolve the agent's doc-authoring identity from its repo + the resolved env.

    Account + client come from the same fields email uses. The agent's Drive root is
    read from the environment (`GDRIVE_ROOT_FOLDER`) — `canopy provision` resolves it from
    the agent's own 1Password vault into ~/.<agent>/.env, so the id never lives in git and
    can differ per environment. `config/agent.json`'s `gdrive_root_folder` remains a
    deprecated fallback for un-provisioned boxes. The other OPTIONAL
    carve-out is `gdrive_share_default` (domain | anyone | none).
    """
    try:
        base = resolve_email_identity(Path(repo_dir))
    except AgentEmailError as e:
        raise AgentGdocError(str(e)) from e
    extra = {}
    aj = Path(repo_dir) / "config" / "agent.json"
    if aj.is_file():
        extra = json.loads(aj.read_text())
    share_default = (extra.get("gdrive_share_default") or "domain").strip()
    root = (os.environ.get(GDRIVE_ROOT_ENV) or extra.get("gdrive_root_folder") or "").strip()
    return GdocIdentity(
        slug=base.slug, account=base.account, client=base.client,
        root_folder=root,
        share_default=share_default if share_default in ("domain", "anyone", "none") else "domain",
        repo=Path(repo_dir),
    )


def _gdoc_identity_from_opts(repo, agent, account, client) -> GdocIdentity:
    """Mirror email's identity resolution, then layer on the gdoc carve-outs.

    Reuses _identity_from_opts so the --repo/--agent/--account/--client semantics and the
    identity-bleed warning stay identical to `canopy email`; the share default comes from
    agent.json only when a repo is resolvable (explicit --account has none). The Drive
    root comes from the provisioned env either way, so a bare --account still files
    correctly on a provisioned box."""
    base: EmailIdentity = _identity_from_opts(repo, agent, account, client)
    ident = GdocIdentity(slug=base.slug, account=base.account, client=base.client,
                         repo=base.repo,
                         root_folder=(os.environ.get(GDRIVE_ROOT_ENV) or "").strip())
    if base.repo:
        try:
            resolved = resolve_gdoc_identity(base.repo)
            ident.root_folder = resolved.root_folder
            ident.share_default = resolved.share_default
        except AgentGdocError:
            pass
    return ident


# --------------------------------------------------------------------------------------
# markdown -> html  (ported verbatim from echo's proven bin/echo_gdoc.py — good rendering,
# deterministic, and independent of Google's own markdown importer quality)
# --------------------------------------------------------------------------------------

def _inline(t: str) -> str:
    # protect code spans
    codes: list[str] = []

    def stash(m):
        codes.append(m.group(1))
        return f"\x00{len(codes)-1}\x00"

    t = re.sub(r"`([^`]+)`", stash, t)
    t = html.escape(t, quote=False)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"__([^_]+)__", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", t)
    t = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"<em>\1</em>", t)
    for i, c in enumerate(codes):
        t = t.replace(f"\x00{i}\x00", f"<code>{html.escape(c, quote=False)}</code>")
    return t


def md_to_html(md: str) -> str:
    out: list[str] = []
    i = 0
    lines = md.split("\n")

    def flush_p(buf: list[str]):
        if buf:
            out.append("<p>" + _inline(" ".join(buf)) + "</p>")
            buf.clear()

    para: list[str] = []
    _blk = r"^\s*([-*+]|\d+\.)\s+|^\s*(#{1,6}\s|>)|^(-{3,}|\*{3,}|_{3,})\s*$"
    while i < len(lines):
        ln = lines[i]
        if re.match(r"^```", ln):
            flush_p(para)
            i += 1
            code: list[str] = []
            while i < len(lines) and not re.match(r"^```", lines[i]):
                code.append(lines[i])
                i += 1
            out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
            i += 1
            continue
        if not ln.strip():
            flush_p(para)
            i += 1
            continue
        h = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if h:
            flush_p(para)
            lvl = len(h.group(1))
            out.append(f"<h{lvl}>{_inline(h.group(2).strip())}</h{lvl}>")
            i += 1
            continue
        if re.match(r"^(-{3,}|\*{3,}|_{3,})\s*$", ln):
            flush_p(para)
            out.append("<hr/>")
            i += 1
            continue
        if re.match(r"^>\s?", ln):
            flush_p(para)
            quote: list[str] = []
            while i < len(lines) and re.match(r"^>\s?", lines[i]):
                quote.append(re.sub(r"^>\s?", "", lines[i]))
                i += 1
            out.append("<blockquote>")
            qbuf: list[str] = []
            for q in quote + [""]:
                if q.strip():
                    qbuf.append(q)
                elif qbuf:
                    out.append("<p>" + _inline(" ".join(qbuf)) + "</p>")
                    qbuf = []
            out.append("</blockquote>")
            continue
        if re.match(r"^\s*[-*+]\s+", ln):
            flush_p(para)
            items: list[str] = []
            while i < len(lines) and re.match(r"^\s*[-*+]\s+", lines[i]):
                cur = re.sub(r"^\s*[-*+]\s+", "", lines[i])
                i += 1
                while i < len(lines) and lines[i].strip() and not re.match(_blk, lines[i]):
                    cur += " " + lines[i].strip()
                    i += 1
                items.append(cur)
            out.append("<ul>" + "".join(f"<li>{_inline(x)}</li>" for x in items) + "</ul>")
            continue
        if re.match(r"^\s*\d+\.\s+", ln):
            flush_p(para)
            items = []
            while i < len(lines) and re.match(r"^\s*\d+\.\s+", lines[i]):
                cur = re.sub(r"^\s*\d+\.\s+", "", lines[i])
                i += 1
                while i < len(lines) and lines[i].strip() and not re.match(_blk, lines[i]):
                    cur += " " + lines[i].strip()
                    i += 1
                items.append(cur)
            out.append("<ol>" + "".join(f"<li>{_inline(x)}</li>" for x in items) + "</ol>")
            continue
        para.append(ln)
        i += 1
    flush_p(para)
    return "<html><body>" + "\n".join(out) + "</body></html>"


# --------------------------------------------------------------------------------------
# gog command construction (pure — unit-testable without a subprocess)
# --------------------------------------------------------------------------------------

# Sentinel that briefly holds the whole doc body between the plain-text blank (docs write)
# and the markdown re-insert (docs find-replace). Distinctive enough never to collide with
# real content; it exists in the doc only for the microsecond between the two gog calls.
REPLACE_SENTINEL = "__CANOPY_GDOC_BODY_SENTINEL__"


def build_upload_command(identity: GdocIdentity, *, html_path: str, name: str | None,
                         parent: str | None) -> list[str]:
    """`gog drive upload` for a fresh create — convert HTML→Doc into --parent.

    Create-only. An in-place --replace of an existing native Doc does NOT go through
    `drive upload` (a Drive media overwrite is forbidden on Workspace-native Docs, issue
    #353) — see build_replace_commands for the Docs-API path."""
    cmd = ["gog", "drive", "upload", html_path,
           "--account", identity.account, "--client", identity.client, "--json",
           "--convert-to", "doc"]
    if name:
        cmd += ["--name", name]
    if parent:
        cmd += ["--parent", parent]
    return cmd


def build_replace_commands(identity: GdocIdentity, *, doc_id: str, md_path: str,
                           name: str | None = None) -> list[list[str]]:
    """The ordered gog calls that replace a NATIVE Doc's body in place, keeping its
    id/link/permissions (issue #353).

    A Drive media overwrite (`files.update` with media) is forbidden on Workspace-native
    Docs, so we edit through the Docs API instead:
      1. `docs write` — blank the whole body down to a single sentinel (plain text).
      2. `docs find-replace --format markdown` — swap the sentinel for the markdown-rendered
         content (gog does the md→Doc conversion: headings, lists, tables, inline images).
      3. `drive rename` — only if a new --name was given (docs write/find-replace can't rename).
    Returns a list of argv lists to run in sequence; a non-zero exit on any aborts publish."""
    ident = ["--account", identity.account, "--client", identity.client, "--json"]
    cmds = [
        ["gog", "docs", "write", doc_id, *ident, "--text", REPLACE_SENTINEL],
        ["gog", "docs", "find-replace", doc_id, REPLACE_SENTINEL,
         "--content-file", md_path, "--format", "markdown", *ident],
    ]
    if name:
        cmds.append(["gog", "drive", "rename", doc_id, name, *ident])
    return cmds


FOLDER_MIME = "application/vnd.google-apps.folder"


def build_list_command(identity: GdocIdentity, parent: str) -> list[str]:
    """`gog drive ls` a folder's children (used to find an existing area/project subfolder)."""
    return ["gog", "drive", "ls", "--parent", parent,
            "--account", identity.account, "--client", identity.client, "--json"]


def build_mkdir_command(identity: GdocIdentity, name: str, parent: str) -> list[str]:
    """`gog drive mkdir` a subfolder under `parent` (used to create a missing area/project folder)."""
    return ["gog", "drive", "mkdir", name, "--parent", parent,
            "--account", identity.account, "--client", identity.client, "--json"]


def parse_list_result(stdout: str) -> list[dict]:
    """Normalize `gog drive ls --json` to a list of file dicts ({id, name, mimeType, …}).

    gog wraps children under a `files` key ({"files": [ … ]}); tolerate a bare list too."""
    try:
        raw = json.loads(stdout)
    except (ValueError, TypeError):
        return []
    files = raw.get("files") if isinstance(raw, dict) else raw
    return [f for f in files if isinstance(f, dict)] if isinstance(files, list) else []


def parse_mkdir_result(stdout: str) -> str:
    """Extract the new folder id from `gog drive mkdir --json` ({"folder": {"id", …}})."""
    try:
        raw = json.loads(stdout)
    except (ValueError, TypeError):
        return ""
    obj = raw if isinstance(raw, dict) else {}
    if isinstance(obj.get("folder"), dict):  # unwrap gog's {"folder": {...}} envelope
        obj = obj["folder"]
    return str(obj.get("id") or obj.get("fileId") or obj.get("file_id") or "")


def find_child_folder(identity: GdocIdentity, parent: str, name: str,
                      runner=subprocess.run) -> str:
    """Return the id of the child FOLDER named `name` under `parent`, or "" if absent.

    Name match is exact + folder-typed, so a same-named file never shadows the folder."""
    r = _run_gog(build_list_command(identity, parent), runner)
    if r.returncode != 0:
        raise AgentGdocError(
            f"gog drive ls failed listing {parent} as {identity.account}: "
            f"{(r.stderr or r.stdout or '').strip()[:300]}"
        )
    for f in parse_list_result(r.stdout):
        if f.get("mimeType") == FOLDER_MIME and (f.get("name") or "") == name:
            return str(f.get("id") or "")
    return ""


def _find_or_create_folder(identity: GdocIdentity, parent: str, name: str, runner) -> str:
    fid = find_child_folder(identity, parent, name, runner)
    if fid:
        return fid
    r = _run_gog(build_mkdir_command(identity, name, parent), runner)
    if r.returncode != 0:
        raise AgentGdocError(
            f"gog drive mkdir {name!r} under {parent} failed as {identity.account}: "
            f"{(r.stderr or r.stdout or '').strip()[:300]}"
        )
    fid = parse_mkdir_result(r.stdout)
    if not fid:
        raise AgentGdocError(f"gog drive mkdir {name!r} returned no folder id: {r.stdout!r}")
    return fid


def resolve_subfolder(identity: GdocIdentity, *, area: str = "Projects",
                      project: str | None = None, runner=subprocess.run) -> str:
    """Find-or-create `<agent root>/<area>[/<project>]` and return its folder id.

    Implements the fleet filing layout (agent-core/deliverables.md): every agent's
    Drive root (`GDRIVE_ROOT_FOLDER`) is its `<Agent>` folder under the shared root; deliverables land
    in `Projects/<project>` and durable trackers in `Process State`. Reuse-then-create by
    exact name keeps the same folder stable across turns, so the next turn re-files there
    instead of spawning a duplicate. Requires a resolved Drive root; pass --parent to bypass."""
    if not identity.root_folder:
        raise AgentGdocError(
            f"no Drive root resolved (${GDRIVE_ROOT_ENV}) — it comes from "
            "op://Agent-<Slug>/gdrive-root-folder; run `canopy provision` and re-source "
            "~/.<agent>/.env, or pass --parent explicitly")
    area_id = _find_or_create_folder(identity, identity.root_folder, (area or "Projects").strip(), runner)
    if not project or not project.strip():
        return area_id
    return _find_or_create_folder(identity, area_id, project.strip(), runner)


def build_share_command(identity: GdocIdentity, file_id: str, *, share: str,
                        email: str | None) -> list[str]:
    """`gog drive share` for domain (dimagi.com reader), anyone-with-link reader, or a
    specific user. `none` never calls this."""
    cmd = ["gog", "drive", "share", file_id,
           "--account", identity.account, "--client", identity.client, "--json"]
    if share == "domain":
        cmd += ["--to", "domain", "--domain", "dimagi.com", "--role", "reader"]
    elif share == "anyone":
        cmd += ["--to", "anyone", "--role", "reader"]
    elif share == "user":
        if not email:
            raise AgentGdocError("--share user requires --share-email <addr>")
        cmd += ["--to", "user", "--email", email, "--role", "reader"]
    else:
        raise AgentGdocError(f"unknown share posture {share!r} (domain|anyone|user|none)")
    return cmd


def parse_upload_result(stdout: str) -> dict:
    """Normalize gog's --json upload output to {id, url, raw}.

    gog wraps the created file under a `file` envelope: {"file": {"id", "webViewLink", …}}
    (confirmed live, gog v0.12) — unwrap it. Liberal on the inner key names too, so a gog
    version bump doesn't silently drop the id/link callers record into their state."""
    try:
        raw = json.loads(stdout)
    except (ValueError, TypeError):
        return {"id": "", "url": "", "raw": (stdout or "").strip()}
    obj = raw if isinstance(raw, dict) else {}
    if isinstance(obj.get("file"), dict):  # unwrap gog's {"file": {...}} envelope
        obj = obj["file"]
    file_id = obj.get("id") or obj.get("fileId") or obj.get("file_id") or ""
    url = (obj.get("webViewLink") or obj.get("webviewlink") or obj.get("link")
           or obj.get("url") or "")
    if not url and file_id:
        url = f"https://docs.google.com/document/d/{file_id}/edit"
    return {"id": str(file_id), "url": url, "raw": raw}


def _run_gog(cmd: list[str], runner) -> subprocess.CompletedProcess:
    try:
        return runner(cmd, capture_output=True, text=True, timeout=UPLOAD_TIMEOUT)
    except FileNotFoundError:
        raise AgentGdocError(GOG_NOT_FOUND)
    except subprocess.TimeoutExpired:
        raise AgentGdocError(
            f"gog {cmd[1]} {cmd[2]} timed out after {UPLOAD_TIMEOUT}s as {cmd[cmd.index('--account')+1]}"
        )


def verify_permissions(identity: GdocIdentity, file_id: str, *, share: str,
                       email: str | None, runner=subprocess.run) -> bool:
    """Read back the file's permissions and confirm the intended grant is actually there.

    The share-verify step echo and hal both hand-rolled: a create-then-share flow can
    silently leave a doc unshared (share call swallowed, wrong drive), and the agent then
    hands out a link nobody can open. `none` is trivially verified true."""
    if share == "none":
        return True
    r = _run_gog(["gog", "drive", "permissions", file_id, "--account", identity.account,
                  "--client", identity.client, "--json"], runner)
    if r.returncode != 0:
        return False
    try:
        perms = json.loads(r.stdout)
    except (ValueError, TypeError):
        return False
    # gog may wrap the list under a key or return it bare — normalize to a list of dicts.
    if isinstance(perms, dict):
        perms = perms.get("permissions") or perms.get("results") or list(perms.values())
    if not isinstance(perms, list):
        return False

    def _t(p):
        return (p.get("type") or "").lower()

    for p in perms:
        if not isinstance(p, dict):
            continue
        if share == "domain" and _t(p) == "domain" and (p.get("domain") or "").lower() == "dimagi.com":
            return True
        if share == "anyone" and _t(p) == "anyone":
            return True
        if share == "user" and _t(p) == "user" and email and \
                (p.get("emailAddress") or p.get("email") or "").lower() == email.lower():
            return True
    return False


def publish(identity: GdocIdentity, *, name: str | None, parent: str | None, md_path: str,
            share: str, share_email: str | None = None, replace: str | None = None,
            dry_run: bool = False, runner=subprocess.run) -> dict:
    """Publish a markdown file as a rendered Google Doc authored as the agent.

    Create: convert HTML→Doc into `parent`, share, and verify the share landed.
    Replace: update an existing native Doc's body in place via the Docs API — same
    id/link/permissions (issue #353). dry_run reports the commands without touching Drive."""
    if not replace and not (name and parent):
        raise AgentGdocError("--name and --parent are required for a new doc "
                             f"(set --parent, or provision ${GDRIVE_ROOT_ENV}); "
                             "use --replace <fileId> to update an existing doc")

    # ---- Replace: in-place Docs-API edit (native-Doc safe, keeps id/link/permissions) ----
    if replace:
        replace_cmds = build_replace_commands(identity, doc_id=replace, md_path=md_path,
                                              name=name)
        url = f"https://docs.google.com/document/d/{replace}/edit"
        if dry_run:
            return {"dry_run": True, "account": identity.account, "client": identity.client,
                    "replace": replace, "name": name, "url": url, "share": "preserved",
                    "replace_cmds": replace_cmds}
        for c in replace_cmds:
            r = _run_gog(c, runner)
            if r.returncode != 0:
                raise AgentGdocError(
                    f"gog {c[1]} {c[2]} failed (exit {r.returncode}) as {identity.account}: "
                    f"{(r.stderr or r.stdout or '').strip()[:400]}"
                )
        # Replace preserves the doc's existing sharing — never re-share (posture would drift).
        return {"id": replace, "url": url, "raw": "", "replaced": True,
                "shared": "preserved", "verified": True}

    # ---- Create: convert HTML→Doc into `parent`, share, verify ----
    body_html = md_to_html(Path(md_path).read_text())
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as tf:
        tf.write(body_html)
        html_path = tf.name
    try:
        upload_cmd = build_upload_command(identity, html_path=html_path, name=name,
                                          parent=parent)
        share_cmd = (build_share_command(identity, "<file_id>", share=share, email=share_email)
                     if share != "none" else None)
        if dry_run:
            return {"dry_run": True, "account": identity.account, "client": identity.client,
                    "name": name, "parent": parent, "replace": "",
                    "share": share, "share_email": share_email or "",
                    "upload_cmd": upload_cmd, "share_cmd": share_cmd, "html": body_html}

        r = _run_gog(upload_cmd, runner)
        if r.returncode != 0:
            raise AgentGdocError(
                f"gog drive upload failed (exit {r.returncode}) as {identity.account}: "
                f"{(r.stderr or r.stdout or '').strip()[:400]}"
            )
        result = parse_upload_result(r.stdout)
        file_id = result["id"]
        if not file_id:
            raise AgentGdocError(f"gog drive upload returned no file id: {result['raw']!r}")

        result["shared"] = share if share != "none" else "none"
        result["verified"] = True
        if share != "none":
            s = _run_gog(build_share_command(identity, file_id, share=share, email=share_email), runner)
            if s.returncode != 0:
                raise AgentGdocError(
                    f"doc created ({result['url']}) but share failed (exit {s.returncode}): "
                    f"{(s.stderr or s.stdout or '').strip()[:300]}"
                )
            result["verified"] = verify_permissions(identity, file_id, share=share,
                                                     email=share_email, runner=runner)
        return result
    finally:
        os.unlink(html_path)


# --------------------------------------------------------------------------------------
# CLI  (`canopy gdoc …`)
# --------------------------------------------------------------------------------------

@click.group("gdoc")
def gdoc_group():
    """Author Google Docs as the agent — shared engine, per-agent identity
    (shared-gog-gdrive.md §5, interim doc adapter)."""


@gdoc_group.command("publish")
@_with_identity_options
@click.option("--md", "md_file", required=True, type=click.Path(exists=True, dir_okay=False),
              help="Markdown file to render into a Google Doc.")
@click.option("--name", help="Doc title (required for a new doc).")
@click.option("--parent", help="Destination Drive folder id (bypasses --project/--area resolution).")
@click.option("--project", help="File into <agent root>/Projects/<project> (find-or-create) — "
              "the fleet norm: one stable subfolder per project/task, re-used across turns.")
@click.option("--area", help="Top-level area under the agent root when resolving a destination: "
              "'Projects' (deliverables, default) or 'Process State' (durable trackers).")
@click.option("--replace", help="File id of an existing Doc to update IN PLACE (keeps the link).")
@click.option("--share", type=click.Choice(["domain", "anyone", "user", "none"]), default=None,
              help="Share posture (default: agent.json `gdrive_share_default`, else domain).")
@click.option("--share-email", help="Recipient for --share user.")
@click.option("--dry-run", is_flag=True, help="Render + print the commands without touching Drive.")
def gdoc_publish(repo, agent, account, client, md_file, name, parent, project, area,
                 replace, share, share_email, dry_run):
    """Publish MD as a rendered Google Doc authored as the agent, filed + shared + verified.

    Destination (create only): explicit --parent wins; else --project/--area resolve a
    per-project subfolder under the agent's resolved Drive root (the fleet filing layout,
    agent-core/deliverables.md); else it falls back to the root folder. Emits JSON with the
    doc id + url + whether the share verified — link the url as a deliverable (never paste
    the doc body inline).
    """
    try:
        ident = _gdoc_identity_from_opts(repo, agent, account, client)
        if not replace and not parent:
            if project or area:
                parent = resolve_subfolder(ident, area=(area or "Projects"), project=project)
            else:
                parent = ident.root_folder or None
        share = share or ident.share_default
        result = publish(ident, name=name, parent=parent, md_path=md_file, share=share,
                         share_email=share_email, replace=replace, dry_run=dry_run)
    except AgentGdocError as e:
        raise click.ClickException(str(e))
    click.echo(json.dumps(result, indent=2))
    if not dry_run and not result.get("verified", True):
        sys.stderr.write("WARNING: could not verify the share landed — open the url and "
                         "check access before handing out the link.\n")
        sys.exit(1)
