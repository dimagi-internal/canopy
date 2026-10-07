r"""Publish a .pptx as a native Google Slides deck authored as the agent — backs `canopy gslides`.

The presentation sibling of `canopy gdoc` / `canopy gsheet` (agent_gdoc.py), with the same
filing contract (agent-core/deliverables.md): authored as the agent's own Workspace identity
via gog, filed under `<agent root>/<area>/<project>`, shared, and permission-verified.

Why this exists (2026-10-07): a deck built as a Claude Slides artifact has a built-in "send
to Google Drive" export, but it rides the claude.ai Google Drive connector — and a Workspace
admin can switch connectors off for the whole org. The deck's own **.pptx download** does not
depend on any connector, so the dependable route is: download the .pptx, then let the agent
upload it with `--convert-to slides`. The pptx is the runtime's own (editable) export, so
this engine does no rendering of its own — it only files, converts, shares and verifies.

Why every publish is a NEW file (no in-place replace): Drive forbids a media overwrite
(`files.update` with content — what `gog drive upload --replace` does) on Workspace-native
files, the same wall `canopy gdoc` hit in #353, and the Slides API has no way to copy slides
in from another presentation. So a re-export makes a new deck; `--supersede <old id>` trashes
the previous one AFTER the new one verified, so the folder holds one current copy. The link
changes on every export — callers must report the new url, never assume the old one.

Verification reads the converted deck back (`presentations.get`) and compares its slide
count to the number of slides inside the .pptx. A conversion that silently drops slides is
the failure that would otherwise reach the audience first.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import click

from orchestrator.agent_email import _with_identity_options
from orchestrator.agent_gdoc import (
    GOG_NOT_FOUND,
    AgentGdocError,
    GdocIdentity,
    _gdoc_identity_from_opts,
    _report_reuse,
    build_share_command,
    parse_upload_result,
    resolve_subfolder,
    verify_permissions,
)

# A 50-slide deck full of screenshots is tens of MB; gdoc's 120s budget is for text.
UPLOAD_TIMEOUT = 900
# Drive's import limit for a presentation converted to Google Slides.
MAX_CONVERT_BYTES = 100 * 1024 * 1024
SLIDES_MIME = "application/vnd.google-apps.presentation"
_SLIDE_PART = re.compile(r"^ppt/slides/slide\d+\.xml$")


def pptx_slide_count(path: str) -> int:
    """Count the slides inside a .pptx (one `ppt/slides/slideN.xml` part per slide).

    Raises AgentGdocError when the file is not a readable .pptx — a half-finished browser
    download is a zip with no central directory, and uploading it would fail opaquely."""
    try:
        with zipfile.ZipFile(path) as z:
            return sum(1 for n in z.namelist() if _SLIDE_PART.match(n))
    except (zipfile.BadZipFile, OSError) as e:
        raise AgentGdocError(f"{path} is not a readable .pptx ({e}) — is the download finished?")


def build_upload_command(identity: GdocIdentity, *, pptx_path: str, name: str,
                         parent: str) -> list[str]:
    return ["gog", "drive", "upload", pptx_path, "--convert-to", "slides",
            "--name", name, "--parent", parent,
            "--account", identity.account, "--client", identity.client, "--json"]


def build_get_command(identity: GdocIdentity, presentation_id: str) -> list[str]:
    return ["gog", "api", "call", "slides", "v1", "presentations.get",
            "--params", json.dumps({"presentationId": presentation_id,
                                    "fields": "presentationId,slides(objectId)"}),
            "--account", identity.account, "--client", identity.client, "--json"]


def build_trash_command(identity: GdocIdentity, file_id: str) -> list[str]:
    return ["gog", "drive", "delete", file_id, "--force",
            "--account", identity.account, "--client", identity.client, "--json"]


def parse_slide_count(stdout: str) -> int | None:
    """Slides in a `presentations.get` response, or None if it is not one.

    Tolerates gog wrapping the API body under a key, as it does for other verbs."""
    try:
        raw = json.loads(stdout)
    except (ValueError, TypeError):
        return None
    if isinstance(raw, dict) and "slides" not in raw:
        for v in raw.values():
            if isinstance(v, dict) and ("slides" in v or "presentationId" in v):
                raw = v
                break
    if not isinstance(raw, dict) or "presentationId" not in raw and "slides" not in raw:
        return None
    slides = raw.get("slides") or []
    return len(slides) if isinstance(slides, list) else None


def _run(cmd: list[str], runner, timeout: int = 120) -> subprocess.CompletedProcess:
    try:
        return runner(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise AgentGdocError(GOG_NOT_FOUND)
    except subprocess.TimeoutExpired:
        raise AgentGdocError(f"gog {cmd[1]} {cmd[2]} timed out after {timeout}s")


def _err(r: subprocess.CompletedProcess) -> str:
    return (r.stderr or r.stdout or "").strip()[:400]


def publish_slides(identity: GdocIdentity, *, pptx_path: str, name: str | None,
                   parent: str | None, share: str, share_email: str | None = None,
                   supersede: str | None = None, dry_run: bool = False,
                   runner=subprocess.run) -> dict:
    """Upload PPTX as a Google Slides deck — filed, shared, read back, and verified."""
    p = Path(pptx_path)
    if p.suffix.lower() != ".pptx":
        raise AgentGdocError(f"{pptx_path} is not a .pptx — Drive converts only PowerPoint "
                             "files to Google Slides (a PDF cannot be converted)")
    if not parent:
        raise AgentGdocError(
            "no destination: pass --project \"<Project>\" (or --area / --parent) — a deck "
            "with no destination lands in My Drive root (agent-core/deliverables.md rule 1)")
    size = p.stat().st_size
    if size > MAX_CONVERT_BYTES:
        raise AgentGdocError(f"{p.name} is {size // (1024 * 1024)} MB; Drive converts at most "
                             "100 MB to Google Slides. Export a lighter pptx (fewer or "
                             "smaller images) and retry.")
    expected = pptx_slide_count(pptx_path)
    name = name or p.stem
    upload_cmd = build_upload_command(identity, pptx_path=str(p), name=name, parent=parent)
    if dry_run:
        return {"dry_run": True, "account": identity.account, "client": identity.client,
                "name": name, "parent": parent, "share": share,
                "share_email": share_email or "", "supersede": supersede or "",
                "pptx_slides": expected, "upload_cmd": upload_cmd}

    r = _run(upload_cmd, runner, timeout=UPLOAD_TIMEOUT)
    if r.returncode != 0:
        raise AgentGdocError(f"gog drive upload failed (exit {r.returncode}) as "
                             f"{identity.account}: {_err(r)}")
    up = parse_upload_result(r.stdout)
    deck_id = up["id"]
    if not deck_id:
        raise AgentGdocError(f"gog drive upload returned no file id: {up['raw']!r}")
    raw_file = up["raw"].get("file", {}) if isinstance(up["raw"], dict) else {}
    if raw_file.get("mimeType") and raw_file["mimeType"] != SLIDES_MIME:
        raise AgentGdocError(f"uploaded {deck_id} but Drive did not convert it "
                             f"(mimeType {raw_file['mimeType']}) — it is a raw .pptx, not Slides")
    result = {"id": deck_id,
              "url": f"https://docs.google.com/presentation/d/{deck_id}/edit",
              "name": name, "pptx_slides": expected}

    g = _run(build_get_command(identity, deck_id), runner)
    got = parse_slide_count(g.stdout) if g.returncode == 0 else None
    result["slides"] = got
    degraded = []
    if got is None:
        degraded.append(f"could not read the deck back ({_err(g) or 'unparseable response'})")
    elif got != expected:
        degraded.append(f"the pptx has {expected} slides but the converted deck has {got}")
    result["degraded"] = degraded

    result["shared"] = share
    result["verified"] = True
    if share != "none":
        s = _run(build_share_command(identity, deck_id, share=share, email=share_email), runner)
        if s.returncode != 0:
            raise AgentGdocError(f"deck created ({result['url']}) but share failed "
                                 f"(exit {s.returncode}): {_err(s)}")
        result["verified"] = verify_permissions(identity, deck_id, share=share,
                                                email=share_email, runner=runner)

    # Only retire the old copy once the new one is known-good: a failed conversion must
    # never leave the requester with no deck at all.
    if supersede and supersede != deck_id:
        if degraded or not result["verified"]:
            result["superseded"] = ""
            result["supersede_skipped"] = "new deck did not verify; old copy kept"
        else:
            t = _run(build_trash_command(identity, supersede), runner)
            result["superseded"] = supersede if t.returncode == 0 else ""
            if t.returncode != 0:
                result["supersede_skipped"] = f"trash failed: {_err(t)}"
    return result


@click.group("gslides")
def gslides_group():
    """Author Google Slides as the agent — the presentation sibling of `canopy gdoc`,
    same filing contract (agent-core/deliverables.md)."""


@gslides_group.command("publish")
@_with_identity_options
@click.option("--pptx", "pptx_file", required=True,
              type=click.Path(exists=True, dir_okay=False),
              help="The .pptx to convert (e.g. a Claude Slides deck's Download › .pptx).")
@click.option("--name", help="Deck title (default: the file name without .pptx).")
@click.option("--parent", help="Destination Drive folder id (bypasses --project/--area resolution).")
@click.option("--project", help="File into <agent root>/Projects/<project> (find-or-create).")
@click.option("--area", help="Top-level area under the agent root: 'Projects' (default) or "
              "'Process State'.")
@click.option("--supersede", help="File id of the previous export to trash once the new deck "
              "verifies (Drive cannot overwrite a native deck in place, so the link changes).")
@click.option("--share", type=click.Choice(["domain", "anyone", "user", "none"]), default=None,
              help="Share posture (default: agent.json `gdrive_share_default`, else domain).")
@click.option("--share-email", help="Recipient for --share user.")
@click.option("--dry-run", is_flag=True, help="Print the commands without touching Drive.")
def gslides_publish(repo, agent, account, client, pptx_file, name, parent, project, area,
                    supersede, share, share_email, dry_run):
    """Convert a .pptx into a native Google Slides deck authored as the agent.

    Files it, shares it, reads it back to confirm every slide arrived, and verifies the
    share. Emits JSON with the deck id + url; exit 1 if anything did not verify."""
    try:
        ident = _gdoc_identity_from_opts(repo, agent, account, client)
        trace: list = []
        if not parent:
            if not (project or area):
                raise AgentGdocError("no destination: pass --project \"<Project>\" "
                                     "(or --area / --parent)")
            parent = resolve_subfolder(ident, area=(area or "Projects"), project=project,
                                       trace=trace)
        share = share or ident.share_default
        result = publish_slides(ident, pptx_path=pptx_file, name=name, parent=parent,
                                share=share, share_email=share_email, supersede=supersede,
                                dry_run=dry_run)
        _report_reuse(ident, trace, parent, dry_run=dry_run)
    except AgentGdocError as e:
        raise click.ClickException(str(e))
    click.echo(json.dumps(result, indent=2))
    if dry_run:
        return
    if result.get("degraded"):
        sys.stderr.write("WARNING: the deck uploaded but did not verify — open it before "
                         "handing out the link:\n"
                         + "".join(f"  - {d}\n" for d in result["degraded"]))
        sys.exit(1)
    if not result.get("verified", True):
        sys.stderr.write("WARNING: could not verify the share landed — open the url and "
                         "check access before handing out the link.\n")
        sys.exit(1)
