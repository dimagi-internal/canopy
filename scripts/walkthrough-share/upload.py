#!/usr/bin/env python3
"""Upload a walkthrough (.html or .mp4) to a canopy-web instance.

Used by /canopy:walkthrough-share. Stdlib only for the core upload path —
``--spec`` derivation imports pyyaml lazily (only when that flag is passed).

Flow:
  1. Resolve config (api URL, PAT).
  2. For HTML: inline relative image/CSS refs as base64 data URIs.
  3. Assemble companion links (--narrative-url / --companion-url / --link /
     --spec) into the typed `links` field the /w/<id> viewer renders.
  4. POST /api/walkthroughs/ multipart with Authorization: Bearer <PAT>.
  5. Print the view URL and (optionally) the share URL.

Auth: canopy-web's PersonalToken (PAT) at ~/.claude/canopy/workbench-token.
Mint with /canopy:canopy-web-pat-mint. Replaces the dead e2e-login flow.

Exits non-zero on any failure. Designed to be friendly to a SKILL.md
caller that just shells out and reads stdout.
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

# This uploader runs under a bare ``python3``; add the canopy ``src/`` to the path
# so the canonical PAT/base-url core (orchestrator.canopy_web) is importable.
# (repo/scripts/walkthrough-share → repo/src.)
_REPO_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))
try:
    from orchestrator import canopy_web  # noqa: E402
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

# Recognized file kinds and their server-side content types.
KIND_BY_EXT = {".html": "html", ".htm": "html", ".mp4": "video"}
CONTENT_TYPE_BY_KIND = {"html": "text/html", "video": "video/mp4"}


def fail(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def _describe_error(body: dict) -> str:
    """Render a Ninja problem+json error body into a readable one-liner.

    Falls back to {"error": ...} (legacy DRF) and raw-dict for defense.
    """
    if not isinstance(body, dict):
        return str(body)
    detail = body.get("detail")
    title = body.get("title")
    if detail and title:
        return f"{title}: {detail}"
    return detail or title or body.get("error") or str(body)


def resolve_pat() -> str:
    """Read the PAT via the canonical canopy_web precedence (env → token file)."""
    try:
        return canopy_web.resolve_token(None)
    except RuntimeError as exc:
        fail(str(exc))
        raise SystemExit  # unreachable, helps the type checker


def detect_kind(path: Path) -> str:
    kind = KIND_BY_EXT.get(path.suffix.lower())
    if kind is None:
        fail(
            f"unsupported extension {path.suffix!r} — only .html, .htm, and "
            f".mp4 are accepted",
        )
    return kind  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# HTML asset inlining
# ---------------------------------------------------------------------------

_ATTR_RE = re.compile(
    r"""(\b(?:src|href|poster)\s*=\s*)(['"])([^'"]+?)(\2)""",
    re.IGNORECASE,
)
_CSS_URL_RE = re.compile(r"""url\(\s*(['"]?)([^)'"]+?)\1\s*\)""", re.IGNORECASE)


def _looks_remote(ref: str) -> bool:
    if not ref:
        return True
    if ref.startswith(("#", "data:", "mailto:", "javascript:")):
        return True
    if "://" in ref:
        return True
    if ref.startswith("//"):
        return True
    return False


def _inline_one(base_dir: Path, ref: str) -> str | None:
    """Resolve a relative ref to a data URI, or return None to leave it alone."""
    if _looks_remote(ref):
        return None
    clean = ref.split("?", 1)[0].split("#", 1)[0]
    target = (base_dir / clean).resolve()
    try:
        # Defense: don't escape the base dir.
        target.relative_to(base_dir.resolve())
    except ValueError:
        return None
    if not target.is_file():
        return None
    mime, _ = mimetypes.guess_type(str(target))
    if not mime:
        mime = "application/octet-stream"
    data = base64.b64encode(target.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def inline_html(path: Path) -> bytes:
    """Return the HTML with relative src/href/poster + url(...) refs inlined."""
    src = path.read_text(encoding="utf-8")
    base_dir = path.parent

    def attr_sub(m: re.Match) -> str:
        prefix, quote, ref, _ = m.group(1), m.group(2), m.group(3), m.group(4)
        data_uri = _inline_one(base_dir, ref)
        if data_uri is None:
            return m.group(0)
        return f"{prefix}{quote}{data_uri}{quote}"

    def css_sub(m: re.Match) -> str:
        quote, ref = m.group(1), m.group(2)
        data_uri = _inline_one(base_dir, ref)
        if data_uri is None:
            return m.group(0)
        return f"url({quote}{data_uri}{quote})"

    out = _ATTR_RE.sub(attr_sub, src)
    out = _CSS_URL_RE.sub(css_sub, out)
    return out.encode("utf-8")


# ---------------------------------------------------------------------------
# Multipart POST with Bearer auth
# ---------------------------------------------------------------------------


def _build_multipart(
    fields: dict[str, str],
    file_field: str,
    filename: str,
    content_type: str,
    file_bytes: bytes,
) -> tuple[bytes, str]:
    boundary = "----canopyshare" + base64.urlsafe_b64encode(os.urandom(9)).decode("ascii")
    crlf = b"\r\n"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f"--{boundary}".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"'.encode())
        parts.append(b"")
        parts.append(str(value).encode("utf-8"))
    parts.append(f"--{boundary}".encode())
    parts.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"'.encode()
    )
    parts.append(f"Content-Type: {content_type}".encode())
    parts.append(b"")
    body = crlf.join(parts) + crlf + file_bytes + crlf + f"--{boundary}--".encode() + crlf
    return body, f"multipart/form-data; boundary={boundary}"


def upload_multipart(
    url: str,
    pat: str,
    fields: dict[str, str],
    file_field: str,
    filename: str,
    content_type: str,
    file_bytes: bytes,
    timeout: int = 120,
) -> tuple[int, dict]:
    body, ct = _build_multipart(fields, file_field, filename, content_type, file_bytes)
    headers = {
        "Content-Type": ct,
        "Authorization": f"Bearer {pat}",
    }
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {"error": e.reason}
        return e.code, payload
    raw = resp.read()
    payload = json.loads(raw.decode("utf-8")) if raw else {}
    return resp.status, payload


# ---------------------------------------------------------------------------
# Companion links (rendered on the /w/<id> viewer page)
# ---------------------------------------------------------------------------

# Default labels by companion kind, keyed on the kind of THIS artifact:
# uploading a video, the companion is the still-frame deck; uploading a deck,
# the companion is the video.
_COMPANION_LABEL = {"video": "Still-frame walkthrough", "html": "Watch the video"}


def _parse_link_arg(raw: str) -> dict:
    """Parse a ``--link "Label::https://url"`` value into a reference link."""
    label, sep, url = raw.partition("::")
    if not sep or not label.strip() or not url.strip():
        fail(f"--link must be 'Label::https://url' (got: {raw!r})")
    return {"label": label.strip(), "url": url.strip(), "kind": "reference"}


def links_from_spec(spec_path: Path) -> list[dict]:
    """Derive reference links from a walkthrough spec's scene URLs.

    Each scene with a ``url`` becomes one "Explore in the app" link
    (label = scene title). Relative URLs are absolutized against
    ``base_url``; duplicates are dropped so a page shown in several scenes
    only links once. ``continue`` scenes (no url) are skipped.

    Imports pyyaml lazily — only callers that pass ``--spec`` pay for it.
    """
    try:
        import yaml
    except ImportError:
        fail("--spec needs pyyaml (pip install pyyaml) — or pass --link instead")
    spec = yaml.safe_load(spec_path.read_text())  # type: ignore[name-defined]
    base = (spec.get("base_url") or "").rstrip("/")
    out: list[dict] = []
    seen: set[str] = set()
    for i, scene in enumerate(spec.get("scenes") or [], 1):
        u = (scene.get("url") or "").strip()
        if not u:
            continue
        full = u if u.startswith("http") else base + u
        if full in seen:
            continue
        seen.add(full)
        out.append(
            {"label": scene.get("title") or f"Scene {i}", "url": full, "kind": "reference"}
        )
    return out


def assemble_links(args, kind: str) -> list[dict]:
    """Build the typed `links` list from the CLI flags, in display order.

    Order: narrative first, then companion, then reference links (explicit
    --link before --spec-derived). Reference URLs are de-duplicated across
    both sources.
    """
    links: list[dict] = []
    if args.narrative_url:
        links.append(
            {"label": args.narrative_label, "url": args.narrative_url, "kind": "narrative"}
        )
    if args.companion_url:
        label = args.companion_label or _COMPANION_LABEL.get(kind, "Companion walkthrough")
        links.append({"label": label, "url": args.companion_url, "kind": "companion"})

    refs: list[dict] = [_parse_link_arg(r) for r in (args.link or [])]
    if args.spec:
        spec_path = Path(args.spec).expanduser().resolve()
        if not spec_path.is_file():
            fail(f"--spec file not found: {spec_path}")
        refs.extend(links_from_spec(spec_path))

    seen: set[str] = set()
    for r in refs:
        if r["url"] in seen:
            continue
        seen.add(r["url"])
        links.append(r)
    return links


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="canopy:walkthrough-share",
        description="Upload a walkthrough to canopy-web (auth via PAT).",
    )
    p.add_argument("path", help="Path to a .html, .htm, or .mp4 file")
    p.add_argument("--title", help="Walkthrough title (defaults to filename stem)")
    p.add_argument("--description", default="", help="Optional description")
    p.add_argument("--project", dest="project_slug", help="Optional project slug")
    p.add_argument("--run-id", dest="run_id", help="DDD run_id this artifact belongs to")
    p.add_argument("--feature", help="Narrative slug (defaults from run_id server-side)")
    p.add_argument(
        "--workspace",
        help="Workspace slug to upload INTO. Required when you belong to more "
        "than one: reads are workspace-scoped, so a video uploaded to the wrong "
        "workspace silently never binds to its narrative.",
    )
    p.add_argument(
        "--role",
        choices=["hero_video", "deck", "docs", "clip"],
        help="Artifact role within the DDD run",
    )
    p.add_argument(
        "--narrative-review-id",
        dest="narrative_review_id",
        help="ReviewRequest id of the narrative version this run rendered",
    )
    p.add_argument(
        "--public",
        action="store_true",
        help="Set visibility=link and print the share URL",
    )
    p.add_argument(
        "--api-url", default=os.environ.get("CANOPY_WEB_API_URL", DEFAULT_API),
        help="canopy-web base URL (default: %(default)s)",
    )
    # Companion links shown on the /w/<id> viewer page.
    p.add_argument(
        "--narrative-url",
        help="Link back to the design narrative / spec that generated this "
        "walkthrough (rendered in the 'This walkthrough' panel).",
    )
    p.add_argument(
        "--narrative-label", default="Back to the narrative",
        help="Label for --narrative-url (default: %(default)s)",
    )
    p.add_argument(
        "--companion-url",
        help="Link to the sibling artifact — the still-frame deck for a video, "
        "or the video for a deck.",
    )
    p.add_argument(
        "--companion-label",
        help="Label for --companion-url (default: by kind — "
        "'Still-frame walkthrough' for a video, 'Watch the video' for a deck).",
    )
    p.add_argument(
        "--link", action="append", metavar="LABEL::URL",
        help="A 'Explore in the app' reference link, 'Label::https://url'. "
        "Repeatable.",
    )
    p.add_argument(
        "--spec",
        help="Walkthrough spec YAML — derive 'Explore in the app' reference "
        "links from each scene's url (label = scene title, deduped).",
    )
    args = p.parse_args(argv)

    src = Path(args.path).expanduser().resolve()
    if not src.is_file():
        fail(f"file not found: {src}")
    kind = detect_kind(src)
    content_type = CONTENT_TYPE_BY_KIND[kind]

    pat = resolve_pat()
    api = args.api_url.rstrip("/")
    title = (args.title or src.stem).strip()
    visibility = "link" if args.public else "private"

    if kind == "html":
        print(f"inlining HTML assets from {src.parent}…", file=sys.stderr)
        payload_bytes = inline_html(src)
    else:
        payload_bytes = src.read_bytes()
    upload_name = "slideshow.html" if kind == "html" else "video.mp4"

    size_mb = len(payload_bytes) / (1024 * 1024)
    print(f"uploading {size_mb:.1f} MB to {api}…", file=sys.stderr)

    fields = {
        "title": title,
        "kind": kind,
        "description": args.description,
        "visibility": visibility,
    }
    if args.project_slug:
        fields["project_slug"] = args.project_slug
    # DDD-run grouping (optional). The server fills the narrative slug from
    # `run_id` when omitted, and derives `role` from `kind` when blank.
    if args.run_id:
        fields["run_id"] = args.run_id
    if args.feature:
        # The FORM FIELD is `narrative_slug` (see apps/walkthroughs/api.py
        # upload_walkthrough). We sent `feature` — which Django Ninja drops
        # silently, because an unknown form field is not an error. So a video
        # uploaded with `--feature <slug>` landed with narrative_slug=None and
        # never bound to its narrative: the storyboard and the DDD narrative
        # page both kept resolving to the PREVIOUS video, with no failure
        # anywhere to notice. `--feature` stays the CLI spelling; only the wire
        # name changes.
        fields["narrative_slug"] = args.feature
    if args.role:
        fields["role"] = args.role
    if args.narrative_review_id:
        fields["narrative_review_id"] = args.narrative_review_id

    links = assemble_links(args, kind)
    if links:
        fields["links"] = json.dumps(links)
        print(f"attaching {len(links)} companion link(s)", file=sys.stderr)

    # Workspace-scoped path when asked for. The server reads the workspace from
    # the URL (`request.workspace_slug`) and otherwise falls back to the
    # caller's default — which, for a user in more than one workspace, is NOT
    # the workspace the narrative lives in. Every read path scopes by workspace
    # (`apps/runs/aggregate._scope`), so a video that lands in the wrong one is
    # invisible to the narrative and the storyboard, with no error at upload
    # time and no way to tell from the artifact itself.
    endpoint = (
        f"{api}/api/w/{args.workspace}/walkthroughs/"
        if args.workspace
        else f"{api}/api/walkthroughs/"
    )
    status, body = upload_multipart(
        endpoint,
        pat=pat,
        fields=fields,
        file_field="file",
        filename=upload_name,
        content_type=content_type,
        file_bytes=payload_bytes,
    )
    if status != 201:
        fail(f"upload failed (HTTP {status}): {_describe_error(body)}")

    # canopy-web migrated DRF → Django Ninja in May 2026 — responses are bare
    # typed payloads (no {success, data, timing_ms} envelope). Read fields
    # directly off `body`.
    wid = body.get("id")
    if not wid:
        fail(f"unexpected response: {body}")

    # The viewer lives at /walkthrough/<id> on the same host as the API base
    # (/w/ was reclaimed as the workspace tenant prefix in mid-2026).
    print(f"View: {api}/walkthrough/{wid}")
    # Public walkthroughs are token-gated: the API returns the owner-only
    # share_url (…/walkthrough/<id>?t=<token>) and never the raw token.
    share_url = body.get("share_url")
    if visibility == "link" and share_url:
        print(f"Share: {share_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
