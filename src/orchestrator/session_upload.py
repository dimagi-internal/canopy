"""Upload a Claude session transcript to canopy-web's ``/api/sessions/upload``.

The packageable core of what ``scripts/share-session/upload.py`` does standalone:
reduce a raw ``.jsonl`` to conversation-only via ``turn_synthesis`` (dropping
tool_use / tool_result / sidechain on the machine), then POST it multipart to
canopy-web, returning ``{slug, share_token, …}``.

This lives in the ``orchestrator`` package (unlike the share-session script, which
sits under a hyphenated dir and can't be imported) so ``canopy agent turn`` can
package a turn's transcript. Transport is injectable for unit tests (no network).
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Optional

from orchestrator import canopy_web, turn_synthesis
from orchestrator.canopy_web import CanopyError, Transport
from orchestrator.provenance import provenance_headers

UPLOAD_PATH = "/api/sessions/upload"


def _project_dir_for(cwd: Path) -> Path:
    """Claude Code's transcript dir for a working directory (cwd '/' → '-')."""
    return Path.home() / ".claude" / "projects" / str(cwd).replace("/", "-")


def discover_transcript(cwd: Path) -> Path:
    """The newest ``.jsonl`` under Claude's project dir for ``cwd`` (the live session)."""
    proj = _project_dir_for(cwd)
    if not proj.is_dir():
        raise FileNotFoundError(
            f"no Claude session dir for {cwd} (looked in {proj}); pass --transcript"
        )
    candidates = sorted(proj.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"no .jsonl transcripts in {proj}")
    return candidates[0]


def _multipart(fields: dict, filename: str, file_bytes: bytes) -> "tuple[bytes, str]":
    boundary = "----canopysess" + base64.urlsafe_b64encode(os.urandom(9)).decode("ascii")
    crlf = b"\r\n"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts += [
            f"--{boundary}".encode(),
            f'Content-Disposition: form-data; name="{name}"'.encode(),
            b"",
            str(value).encode("utf-8"),
        ]
    parts += [
        f"--{boundary}".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"'.encode(),
        b"Content-Type: application/x-ndjson",
        b"",
    ]
    body = crlf.join(parts) + crlf + file_bytes + crlf + f"--{boundary}--".encode() + crlf
    return body, f"multipart/form-data; boundary={boundary}"


def upload_transcript(
    path,
    *,
    title: str,
    project: Optional[str] = None,
    visibility: str = "link",
    full: bool = False,
    base_url: Optional[str] = None,
    token: Optional[str] = None,
    transport: Optional[Transport] = None,
) -> dict:
    """Reduce (unless ``full``) and upload one transcript; return the server body.

    The returned dict carries ``slug`` + ``share_token`` (the ``/share/<token>``
    link) plus ``cli_session_id`` (the Claude session id — the turn's dedup key),
    added here so the caller need not re-parse the transcript.
    """
    path = Path(path)
    if full:
        file_bytes = path.read_bytes()
        cli_session_id = path.stem
    else:
        cli_session_id, turns = turn_synthesis.synthesize(path)
        file_bytes, _n = turn_synthesis.to_share_jsonl(cli_session_id, turns)

    fields = {"title": title, "visibility": visibility}
    if project:
        fields["project_slug"] = project
    started_at, ended_at = turn_synthesis.timespan(path)
    if started_at:
        fields["started_at"] = started_at
    if ended_at:
        fields["ended_at"] = ended_at
    active = turn_synthesis.active_seconds(path)
    if active:
        fields["active_seconds"] = str(active)

    body, ctype = _multipart(fields, path.name, file_bytes)
    base = canopy_web.resolve_base_url(base_url)
    tok = canopy_web.resolve_token(token)
    tr = transport or canopy_web.urllib_transport
    headers = {**provenance_headers(), "Authorization": f"Bearer {tok}", "Content-Type": ctype}
    status, text = tr("POST", base + UPLOAD_PATH, headers, body)
    if not (200 <= status < 300):
        raise CanopyError(f"POST {UPLOAD_PATH} -> {status}: {text[:400]}")
    result = json.loads(text) if text.strip() else {}
    result.setdefault("cli_session_id", cli_session_id)
    return result
