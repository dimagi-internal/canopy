#!/usr/bin/env python3
"""Upload the current Claude Code session transcript to canopy-web.

Used by /canopy:share-session. Stdlib only — bare ``python3`` runs it.

Flow:
  1. Resolve the transcript .jsonl. Default: the newest file under
     ``~/.claude/projects/<cwd-slash-encoded>/`` (same dir Claude Code writes
     the live session to). Override with a positional path.
  2. Resolve config (api URL, PAT at ~/.claude/canopy/workbench-token).
  3. POST it multipart to /api/sessions/upload with Authorization: Bearer.
     The server parses, best-effort-scrubs secrets, and (link-by-default)
     mints a share token.
  4. Print the /share/<token> URL and the redaction count.

Auth: canopy-web's PersonalToken (PAT). Mint with /canopy:canopy-web-pat-mint.

Exits non-zero on any failure with a one-line ``error: …`` on stderr.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# The canonical turn-synthesis reducer lives in the package (src/orchestrator).
# This uploader runs under a bare ``python3``, so add ``src/`` to the path and
# import it — one source of truth shared with harvest. (repo/scripts/share-session
# → repo/src.)
_REPO_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))
try:
    from orchestrator import canopy_web, turn_synthesis  # noqa: E402
except ImportError as exc:  # pragma: no cover - deployment path sanity
    print(
        f"error: cannot import orchestrator from {_REPO_SRC} ({exc}). "
        f"Run /canopy:update to sync the canopy checkout.",
        file=sys.stderr,
    )
    sys.exit(1)

# Canonical PAT/base-url conventions live in canopy_web; alias for back-compat.
DEFAULT_API = canopy_web.DEFAULT_API
TOKEN_FILE = canopy_web.TOKEN_FILE


def fail(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def reduce_transcript(path: Path) -> tuple[bytes, int]:
    """Reduce a raw Claude .jsonl to a clean conversation, client-side.

    Thin wrapper over ``turn_synthesis``: keep only what the human typed and the
    FINAL assistant text of each turn, dropping tool_use / tool_result /
    sidechain / harness-noise entirely — they are never uploaded. Re-emits a
    minimal Claude-format .jsonl (init + user/assistant text lines) the server
    parses back into exactly these turns.

    The full noisy transcript (and any sensitive tool output it carries) never
    leaves the machine.
    """
    session_id, turns = turn_synthesis.synthesize(path)
    return turn_synthesis.to_share_jsonl(session_id, turns)


def _describe_error(body: dict) -> str:
    if not isinstance(body, dict):
        return str(body)
    detail = body.get("detail")
    title = body.get("title")
    if detail and title:
        return f"{title}: {detail}"
    return detail or title or body.get("error") or str(body)


def resolve_pat() -> str:
    # Delegates the precedence to canopy_web; maps its RuntimeError onto this
    # script's fail() (stderr + exit) for a consistent standalone-CLI UX.
    try:
        return canopy_web.resolve_token(None)
    except RuntimeError as exc:
        fail(str(exc))
        raise SystemExit  # unreachable


def _project_dir_for(cwd: Path) -> Path:
    """Claude Code's transcript dir for a working directory.

    Claude encodes the absolute cwd by replacing each '/' with '-', e.g.
    /Users/x/proj → -Users-x-proj, under ~/.claude/projects/.
    """
    encoded = str(cwd).replace("/", "-")
    return Path.home() / ".claude" / "projects" / encoded


def discover_transcript(cwd: Path) -> Path:
    proj = _project_dir_for(cwd)
    if not proj.is_dir():
        fail(
            f"no Claude session log dir for {cwd} (looked in {proj}). "
            f"Pass the transcript path explicitly, or run this from the "
            f"project where the session ran.",
        )
    candidates = sorted(
        proj.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not candidates:
        fail(f"no .jsonl transcripts found in {proj}")
    return candidates[0]


def _build_multipart(
    fields: dict[str, str], filename: str, file_bytes: bytes
) -> tuple[bytes, str]:
    boundary = "----canopysess" + base64.urlsafe_b64encode(os.urandom(9)).decode("ascii")
    crlf = b"\r\n"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f"--{boundary}".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"'.encode())
        parts.append(b"")
        parts.append(str(value).encode("utf-8"))
    parts.append(f"--{boundary}".encode())
    parts.append(
        f'Content-Disposition: form-data; name="file"; filename="{filename}"'.encode()
    )
    parts.append(b"Content-Type: application/x-ndjson")
    parts.append(b"")
    body = crlf.join(parts) + crlf + file_bytes + crlf + f"--{boundary}--".encode() + crlf
    return body, f"multipart/form-data; boundary={boundary}"


def upload(
    url: str, pat: str, fields: dict[str, str], filename: str, file_bytes: bytes
) -> tuple[int, dict]:
    body, ct = _build_multipart(fields, filename, file_bytes)
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": ct, "Authorization": f"Bearer {pat}"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=120)
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {"error": e.reason}
        return e.code, payload
    raw = resp.read()
    return resp.status, (json.loads(raw.decode("utf-8")) if raw else {})


def post_json(url: str, pat: str, payload: dict) -> tuple[int, dict]:
    """POST a JSON body with Bearer auth (used to create an arc)."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {pat}"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=120)
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8") or "{}")
        except json.JSONDecodeError:
            body = {"error": e.reason}
        return e.code, body
    raw = resp.read()
    return resp.status, (json.loads(raw.decode("utf-8")) if raw else {})


def _upload_one(
    api: str,
    pat: str,
    src: Path,
    *,
    title: str,
    project: str | None,
    visibility: str,
    full: bool,
) -> dict:
    """Upload one transcript and return the server's JSON body. Reduces to a
    turn-synthesis client-side unless ``full``."""
    if full:
        file_bytes = src.read_bytes()
    else:
        file_bytes, n_turns = reduce_transcript(src)
        print(
            f"  {src.name}: reduced to {n_turns} turn(s)",
            file=sys.stderr,
        )
    fields = {"title": title, "visibility": visibility}
    if project:
        fields["project_slug"] = project
    # Session timing (when / how long) — read from the RAW transcript before it's
    # reduced; the reduced upload drops per-event timestamps.
    started_at, ended_at = turn_synthesis.timespan(src)
    if started_at:
        fields["started_at"] = started_at
    if ended_at:
        fields["ended_at"] = ended_at
    active = turn_synthesis.active_seconds(src)
    if active:
        fields["active_seconds"] = str(active)
    status, body = upload(
        f"{api}/api/sessions/upload", pat, fields, src.name, file_bytes
    )
    if status != 201:
        fail(f"upload failed for {src.name} (HTTP {status}): {_describe_error(body)}")
    return body


def _title_for(src: Path) -> str:
    """A readable section heading for an arc member: its first human prompt,
    else the file stem."""
    try:
        _sid, turns = turn_synthesis.synthesize(src)
        if turns and turns[0].prompt:
            return turns[0].prompt.replace("\n", " ")[:80]
    except Exception:
        pass
    return src.stem


def _owner_clause(owner_email: str) -> str:
    """Who can actually open a private upload.

    Falls back when the server predates ``owner_email`` on the upload response
    (dimagi-internal/canopy-web) — vaguer, but still true, which the old
    "dimagi login required" was not.
    """
    return f"Only {owner_email} can open it." if owner_email else (
        "Only the account that uploaded it can open it."
    )


def format_session_result(
    api: str, *, visibility: str, slug: str, token: str | None, owner_email: str = ""
) -> list[str]:
    """The stdout block for one uploaded session.

    Two rules here, both paid for by dimagi-internal/canopy#484 — a share an
    agent made for a human that was a black screen nobody could open:

    1. **One URL per line, with nothing trailing it.** The private line used to
       read ``View (dimagi login required): <api>/sessions  (slug: ABC123)`` —
       a URL and a bare identifier on the same line, which reads as one thing
       and gets concatenated into ``<api>/sessions/ABC123``. That is not a
       route (``/sessions`` and ``/share/<token>`` are the only two), so it
       serves the empty SPA shell: a blank page that looks like a broken share
       rather than a URL that was never real.
    2. **Never promise access the artifact doesn't grant.** "dimagi login
       required" claimed any logged-in dimagi user could view it. Private
       sessions are owner-only, and agents upload under their OWN accounts (the
       uploader resolves its PAT from the repo it runs in) — so the reader is
       usually not the person who asked for the share. Name the owner instead.
    """
    if visibility == "link" and token:
        return [f"Share: {api}/share/{token}"]
    return [
        f"Uploaded PRIVATE — nothing is shared. {_owner_clause(owner_email)}",
        "Open it from that account's session list:",
        f"  {api}/sessions",
        f"Session slug (an id, not a URL): {slug}",
        "To give someone a link, re-run without --private.",
    ]


def format_arc_result(
    api: str, *, visibility: str, slug: str, token: str | None, owner_email: str = ""
) -> list[str]:
    """The stdout block for a created arc.

    Same two rules as ``format_session_result``, plus one fact that makes a
    private arc worse than a private session: canopy-web has **no arcs UI at
    all** (verified 2026-08-14 — ``arcs`` appears only in the generated API
    types; ``SessionsPage`` lists sessions, and ``/share/<token>`` is the only
    arc surface there is). So a private arc isn't merely unlisted, it has no
    page whatsoever — and the member sessions it was stitched from are private
    too. Saying so is the difference between "your link is somewhere" and
    "re-run this".
    """
    if visibility == "link" and token:
        return [f"Arc: {api}/share/{token}"]
    return [
        f"Arc created PRIVATE — nothing is shared. {_owner_clause(owner_email)}",
        "A private arc has NO page: /share/<token> is the only arc surface, and a",
        "private arc has no token. Its member sessions are private too.",
        f"Arc slug (an id, not a URL): {slug}",
        "To get a readable link, re-run without --private.",
    ]


def run_arc(args, api: str, pat: str) -> int:
    """Upload each transcript as a (private) member session, then stitch them
    into one shared arc and print the /share/<token> URL."""
    paths = [Path(p).expanduser().resolve() for p in args.paths]
    for p in paths:
        if not p.is_file():
            fail(f"transcript not found: {p}")
    if not paths:
        fail("--arc needs at least one transcript path")

    project = args.project_slug or Path.cwd().name
    print(f"building arc from {len(paths)} session(s)…", file=sys.stderr)

    items = []
    for src in paths:
        # Members upload private — the arc carries the public link, not each one.
        body = _upload_one(
            api, pat, src,
            title=_title_for(src),
            project=project,
            visibility="private",
            full=args.full,
        )
        items.append({"session_slug": body["slug"], "heading": _title_for(src)})

    arc_title = (args.title or f"Session arc ({len(paths)} sessions)").strip()
    visibility = "private" if args.private else "link"
    payload = {
        "title": arc_title,
        "project_slug": project,
        "visibility": visibility,
        "items": items,
    }
    status, body = post_json(f"{api}/api/sessions/arcs", pat, payload)
    if status != 201:
        fail(f"arc creation failed (HTTP {status}): {_describe_error(body)}")

    item_count = body.get("item_count", len(items))
    for line in format_arc_result(
        api,
        visibility=visibility,
        slug=body.get("slug", ""),
        token=body.get("share_token"),
        owner_email=body.get("owner_email", ""),
    ):
        print(line)
    print(f"{item_count} sessions stitched", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="canopy:share-session",
        description="Upload the current Claude session transcript to canopy-web.",
    )
    p.add_argument(
        "paths",
        nargs="*",
        help="Transcript .jsonl path(s). Default (none): newest for the current "
        "dir's session. With --arc: one or more transcripts to stitch in order.",
    )
    p.add_argument(
        "--arc",
        action="store_true",
        help="Stitch the given transcripts into ONE shared arc page (each "
        "uploaded as a private member; the arc carries the public link).",
    )
    p.add_argument(
        "--title",
        help="Session title — or, with --arc, the arc title "
        "(default: the transcript stem / a generated arc title).",
    )
    p.add_argument("--project", dest="project_slug", help="Project slug for the feed.")
    p.add_argument(
        "--private",
        action="store_true",
        help="Upload private (dimagi-only) instead of link-by-default.",
    )
    p.add_argument(
        "--full",
        action="store_true",
        help="Upload the raw transcript (all tool calls). Default reduces to "
        "the conversation — your prompts + Claude's final reply per turn — "
        "client-side, so tool output never leaves your machine.",
    )
    p.add_argument(
        "--api-url",
        default=os.environ.get("CANOPY_WEB_API_URL", DEFAULT_API),
        help="canopy-web base URL (default: %(default)s)",
    )
    args = p.parse_args(argv)

    pat = resolve_pat()
    api = args.api_url.rstrip("/")

    if args.arc:
        if not args.paths:
            fail("--arc needs at least one transcript path")
        return run_arc(args, api, pat)

    cwd = Path.cwd()
    if args.paths:
        if len(args.paths) > 1:
            fail("multiple transcripts given without --arc; pass --arc to stitch "
                 "them into one shared arc, or upload one at a time")
        src = Path(args.paths[0]).expanduser().resolve()
        if not src.is_file():
            fail(f"transcript not found: {src}")
    else:
        src = discover_transcript(cwd)
        print(f"using transcript: {src}", file=sys.stderr)

    title = (args.title or src.stem).strip()
    project = args.project_slug or cwd.name
    visibility = "private" if args.private else "link"

    if args.full:
        file_bytes = src.read_bytes()
    else:
        file_bytes, n_turns = reduce_transcript(src)
        print(
            f"reduced to {n_turns} conversation turn(s) "
            f"(prompts + final replies; tool calls dropped client-side)",
            file=sys.stderr,
        )
    size_kb = len(file_bytes) / 1024
    print(f"uploading {size_kb:.0f} KB to {api}…", file=sys.stderr)

    fields = {"title": title, "visibility": visibility}
    if project:
        fields["project_slug"] = project
    started_at, ended_at = turn_synthesis.timespan(src)
    if started_at:
        fields["started_at"] = started_at
    if ended_at:
        fields["ended_at"] = ended_at
    active = turn_synthesis.active_seconds(src)
    if active:
        fields["active_seconds"] = str(active)

    status, body = upload(
        f"{api}/api/sessions/upload", pat, fields, src.name, file_bytes
    )
    if status != 201:
        fail(f"upload failed (HTTP {status}): {_describe_error(body)}")

    slug = body.get("slug")
    if not slug:
        fail(f"unexpected response: {body}")

    redactions = body.get("redaction_count", 0)
    msg_count = body.get("message_count", 0)
    token = body.get("share_token")

    if body.get("duplicate"):
        print("(already shared — returning the existing link)", file=sys.stderr)

    for line in format_session_result(
        api,
        visibility=visibility,
        slug=slug,
        token=token,
        owner_email=body.get("owner_email", ""),
    ):
        print(line)
    print(
        f"{msg_count} messages · {redactions} secret"
        f"{'' if redactions == 1 else 's'} redacted (best-effort)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
