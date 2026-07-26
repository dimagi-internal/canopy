"""DDD run upload step.

Uploads a converged run's artifacts to canopy-web so they package together
under the run — the hero video, a self-contained documentation HTML page (hero
video + capabilities + why + how) built from the run's unified_spec and
why_brief, and (via the earlier narrative-review gate) the narrative — all
grouped on ``run_id`` and navigable at ``/ddd/<narrative_slug>/<run_id>``.

Public API
----------
build_docs_page(spec, why_brief, video_url) -> str
    Return a self-contained HTML string for the docs page.

publish_artifact(content, *, kind, title, base_url, token, _post) -> str
    Upload one HTML or video artifact to canopy-web and return its hosted URL.

upload_run(run_id, *, video_path, base_url, token, _upload, _gate, auto_approve_for_test) -> str
    Orchestrate: load state + spec + why_brief → upload video → build docs →
    external_release gate → upload HTML → save phase=uploaded → return the run
    **package** URL (``/ddd/<narrative_slug>/<run_id>``), NOT a loose artifact URL.

Notes
-----
- ``kind`` ∈ ``"html"`` | ``"video"``.
- HTML uploads go to ``/api/walkthroughs/`` (same endpoint as walkthrough-share).
- The gate is wired as ``_gate(review_request, base_url, token) -> str`` where the
  return value is the chosen option string (e.g. ``"publish"`` or ``"hold"``).
- No real network calls, no sleeping in production code — inject ``_post`` /
  ``_gate`` / ``_upload`` in tests.
"""
from __future__ import annotations

import base64
import html
import json
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

import yaml

from scripts.ddd.schemas.models import Decision, Gate, ReviewRequest, RunState, UnifiedSpec, WhyBrief
from scripts.ddd.spec_io import load_spec
from scripts.ddd.runstate import load as load_state
from scripts.ddd.runstate import save as save_state
from scripts.ddd.runstate import _resolve_ddd_dir
from scripts.ddd.auth import (
    DEFAULT_API,
    TOKEN_FILE,
    resolve_base_url as _resolve_base_url,
    resolve_token as _resolve_token,
    resolve_ddd_workspace as _resolve_ws,
    scoped_api_path as _scoped_api,
    scoped_app_path as _scoped_app,
)
from scripts.ddd.review import _review_id_from_url

# Escape hatch (emergencies only): set DDD_ALLOW_NO_NARRATIVE=1 to publish a run
# that has no narrative version on canopy-web. Mirrors the SKIP_TESTS pattern in
# deploy.sh — the guard exists precisely so this is a conscious override.
_ALLOW_NO_NARRATIVE_ENV = "DDD_ALLOW_NO_NARRATIVE"


class NarrativeMissingError(RuntimeError):
    """Raised when ``upload_run`` is asked to publish a run with no narrative.

    A run is publishable only if a story-bearing ``concept_change`` review (the
    ``ddd-narrative-review`` gate) exists for its narrative — otherwise the
    published package would render as "no narrative" in canopy-web. The fix is
    to run ``/canopy:ddd-narrative-review <run_id>`` first (which stamps
    ``run_state.narrative_review_id``), then re-upload.
    """


class DeckMissingError(RuntimeError):
    """Raised when ``upload_run`` cannot find a usable render manifest.

    The render engine emits ``walkthrough-run-data.json`` (the canonical
    manifest built by ``scripts.walkthrough.manifest.build_manifest``) into the
    run dir. The deck (``role=deck`` slideshow) is built directly from that
    manifest's scene slides — there is no spec-rebuild fallback. If the manifest
    is absent or carries zero scene slides, the run was not rendered (or was
    rendered by a stale path that didn't emit the manifest); the fix is to
    re-render with ``record_video.py --manifest`` before uploading. Failing loud
    here is deliberate: a silently-empty "Walkthrough slides" section is the bug
    this refactor removes.
    """


def run_package_url(narrative_slug: str, run_id: str, base_url: str | None = None) -> str:
    """Return the canopy-web **run package** URL for a DDD run.

    canopy-web routes the navigable package (video + deck + narrative + links)
    at ``/ddd/<narrative>/<runId>``, where the narrative slug is the ``narrative_slug``
    the plugin sends (a slug in real use). This is the link a human should get
    — NOT a loose ``/walkthrough/<artifact-id>`` single-artifact URL. Path segments are
    URL-quoted defensively in case a narrative_slug carries unsafe characters.
    """
    api = _resolve_base_url(base_url)
    feat = urllib.parse.quote(narrative_slug or run_id, safe="")
    rid = urllib.parse.quote(run_id, safe="")
    return f"{api}{_scoped_app(f'/ddd/{feat}/{rid}', _resolve_ws(None))}"


def narrative_landing_url(narrative_slug: str, base_url: str | None = None) -> str:
    """The canopy-web narrative landing page (versions list) for a slug."""
    api = _resolve_base_url(base_url)
    path = f"/ddd/{urllib.parse.quote(narrative_slug, safe='')}"
    return f"{api}{_scoped_app(path, _resolve_ws(None))}"


def upload_narrative_video(
    narrative_slug: str,
    video_path: str,
    *,
    base_url: str | None = None,
    token: str | None = None,
    role: str = "hero_video",
    title: str | None = None,
    _detail=None,
    _upload=None,
) -> dict:
    """Upload a rendered mp4 and pin it to the narrative's CURRENT version.

    Resolves the current narrative version on canopy-web and stamps the uploaded
    artifact with its review id (``narrative_review_id``), so the video attaches
    to that exact story version — a later narration edit can't leave a stale
    video on a newer version. Returns ``{video_url, version, review_id,
    narrative_url}``.

    Refuses if canopy-web has no narrative version for the slug: a video must
    hang off a real version, so the narrative has to be posted first.
    """
    from scripts.ddd import review as rv

    detail = (_detail or rv.get_narrative)(narrative_slug, base_url=base_url, token=token)
    cur = (detail or {}).get("current_version") or {}
    review_id = cur.get("review_id")
    version = cur.get("version")
    if not review_id:
        raise SystemExit(
            f"canopy-web has no narrative version for {narrative_slug!r} — post the "
            "narrative (run the narrative-review gate) before attaching a video."
        )
    data = Path(video_path).read_bytes()
    video_url = (_upload or publish_artifact)(
        data,
        kind="video",
        title=title or f"{narrative_slug} v{version}",
        narrative_slug=narrative_slug,
        role=role,
        narrative_review_id=str(review_id),
        base_url=base_url,
        token=token,
    )
    return {
        "video_url": video_url,
        "version": version,
        "review_id": str(review_id),
        "narrative_url": narrative_landing_url(narrative_slug, base_url),
    }


def _resolve_narrative_review_id(run_state: RunState) -> str | None:
    """The narrative version this run rendered, however it was stamped.

    Prefers the explicit ``narrative_review_id`` field (written by the
    ``narrative post`` command since 0.2.172); falls back to parsing the legacy
    ``narrative_review_url`` so runs stamped by older plugin versions still link.
    """
    explicit = (getattr(run_state, "narrative_review_id", None) or "").strip()
    if explicit:
        return explicit
    return _review_id_from_url(getattr(run_state, "narrative_review_url", None))


def _default_narrative_check(
    narrative_slug: str,
    run_id: str,
    *,
    base_url: str | None = None,
    token: str | None = None,
) -> bool:
    """Real narrative-existence probe used by ``upload_run`` in production.

    Returns True iff canopy-web has a narrative version for ``narrative_slug``. Kept as
    a thin wrapper (local import) so ``upload.py`` has no import-time dependency
    on the review client and tests can inject a fake without the network.
    """
    from scripts.ddd import review as rv

    return rv.narrative_version_exists(narrative_slug, base_url=base_url, token=token)


# ---------------------------------------------------------------------------
# CSS for the docs page — dark-mode shadcn-style tokens, no external deps
# ---------------------------------------------------------------------------

_DOCS_CSS = """
:root {
  --background: #0a0a0a;
  --foreground: #fafafa;
  --card: #111111;
  --card-foreground: #fafafa;
  --muted: #171717;
  --muted-foreground: #a1a1aa;
  --border: #262626;
  --primary: #fafafa;
  --accent: #262626;
  --accent-foreground: #fafafa;
  --success: #10b981;
  --info: #60a5fa;
  --warning: #f59e0b;
  --radius: 10px;
}

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

html { background: var(--background); scroll-behavior: smooth; }

body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  font-size: 16px;
  line-height: 1.6;
  color: var(--foreground);
  background: var(--background);
  -webkit-font-smoothing: antialiased;
}

a { color: var(--info); text-decoration: none; }
a:hover { text-decoration: underline; }

/* Layout */
.page-wrap {
  max-width: 860px;
  margin: 0 auto;
  padding: 0 1.5rem 6rem;
}

/* Hero */
.hero {
  padding: 3rem 0 2rem;
  border-bottom: 1px solid var(--border);
  margin-bottom: 2.5rem;
}

.hero-label {
  font-size: 0.75rem;
  font-weight: 600;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--muted-foreground);
  margin-bottom: 0.5rem;
}

.hero h1 {
  font-size: 2.5rem;
  font-weight: 700;
  letter-spacing: -0.03em;
  color: var(--foreground);
  line-height: 1.15;
  margin-bottom: 1rem;
}

.hero-lede {
  font-size: 1.05rem;
  line-height: 1.6;
  color: var(--muted-foreground);
  margin-top: 0.5rem;
  max-width: 70ch;
}

.hero-video-wrap {
  position: relative;
  margin-top: 2rem;
  border-radius: var(--radius);
  overflow: hidden;
  border: 1px solid var(--border);
  background: #000;
}

/* Visible "this is a playable video" affordance over the poster frame. */
.hero-video-wrap::after {
  content: "\\25B6";
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-46%, -50%);
  width: 68px;
  height: 68px;
  border-radius: 50%;
  background: rgba(0, 0, 0, 0.55);
  color: rgba(255, 255, 255, 0.95);
  font-size: 1.9rem;
  display: flex;
  align-items: center;
  justify-content: center;
  pointer-events: none;
}

.hero-caption {
  margin-top: 0.6rem;
  font-size: 0.85rem;
  color: var(--muted-foreground);
  text-align: center;
}

.hero-video-wrap video,
.hero-video-wrap iframe {
  display: block;
  width: 100%;
  aspect-ratio: 16 / 9;
  border: none;
}

/* Section titles */
h2 {
  font-size: 1.375rem;
  font-weight: 600;
  letter-spacing: -0.02em;
  color: var(--foreground);
  margin-bottom: 1rem;
  padding-bottom: 0.5rem;
  border-bottom: 1px solid var(--border);
}

section {
  margin-bottom: 2.5rem;
}

/* Capability list */
.capability-list {
  list-style: none;
  padding: 0;
}

.capability-list li {
  display: flex;
  align-items: flex-start;
  gap: 0.75rem;
  padding: 0.75rem 0;
  border-bottom: 1px solid var(--border);
  color: var(--foreground);
  font-size: 0.975rem;
  line-height: 1.5;
}

.capability-list li:last-child { border-bottom: none; }

.cap-icon {
  flex-shrink: 0;
  width: 1.5rem;
  height: 1.5rem;
  border-radius: 50%;
  background: var(--success);
  display: flex;
  align-items: center;
  justify-content: center;
  margin-top: 0.1rem;
  font-size: 0.7rem;
  color: #fff;
  font-weight: 700;
}

/* Why section */
.why-problem {
  background: var(--muted);
  border-left: 3px solid var(--info);
  border-radius: 0 var(--radius) var(--radius) 0;
  padding: 1rem 1.25rem;
  margin-bottom: 1.5rem;
  color: var(--muted-foreground);
  font-size: 0.975rem;
  line-height: 1.6;
}

.spine-item {
  padding: 1rem 0;
  border-bottom: 1px solid var(--border);
}

.spine-item:last-child { border-bottom: none; }

.spine-claim {
  font-weight: 600;
  color: var(--foreground);
  margin-bottom: 0.35rem;
  font-size: 0.975rem;
}

.spine-rationale {
  color: var(--muted-foreground);
  font-size: 0.9rem;
  line-height: 1.55;
}

/* How to use */
.how-list {
  list-style: none;
  padding: 0;
  counter-reset: step-counter;
}

.how-list li {
  display: flex;
  align-items: flex-start;
  gap: 1rem;
  padding: 0.85rem 0;
  border-bottom: 1px solid var(--border);
  counter-increment: step-counter;
}

.how-list li:last-child { border-bottom: none; }

.step-num {
  flex-shrink: 0;
  width: 1.75rem;
  height: 1.75rem;
  border-radius: 50%;
  background: var(--accent);
  border: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 0.75rem;
  font-weight: 700;
  color: var(--muted-foreground);
  margin-top: 0.05rem;
}

.step-body {
  color: var(--foreground);
  font-size: 0.975rem;
  line-height: 1.55;
}

/* Footer */
.page-footer {
  margin-top: 3rem;
  padding-top: 1.5rem;
  border-top: 1px solid var(--border);
  font-size: 0.8rem;
  color: var(--muted-foreground);
  text-align: center;
}
"""


# ---------------------------------------------------------------------------
# build_docs_page
# ---------------------------------------------------------------------------


# A canopy-web walkthrough URL: the viewer page ``/walkthrough/<id>`` (or the
# legacy pre-tenancy ``/w/<id>`` form baked into old artifacts) or its byte
# stream ``…/content`` (optionally with a ``?t=`` share token, kept intact).
_CANOPY_W_RE = re.compile(r"/(?:w|walkthrough)/[^/?#]+(?:/content)?/?$")


def _to_content_url(url: str) -> str:
    """Rewrite a canopy-web viewer URL to its public byte-stream URL.

    ``https://host/walkthrough/<id>?t=tok`` ->
    ``https://host/walkthrough/<id>/content?t=tok`` (same for legacy ``/w/``
    URLs, whose /content path 302-redirects server-side). The viewer page is
    auth-gated for anonymous callers and sends ``X-Frame-Options: DENY``, so
    framing it renders blank; the ``/content`` endpoint serves the bytes
    token-gated and same-origin frameable. Idempotent if already a
    ``/content`` URL.
    """
    base, sep, query = url.partition("?")
    base = base.rstrip("/")
    if base.endswith("/content"):
        return url
    return f"{base}/content{sep}{query}"


def build_docs_page(spec: UnifiedSpec, why_brief: WhyBrief, video_url: str, poster_url: str = "") -> str:
    """Return a self-contained HTML documentation page.

    Structure
    ---------
    - Hero: title + hero video (``video_url``).
    - "What you can do": each scene's ``concept_claim``.
    - "Why it works this way": ``why_brief.problem`` + each ``spine`` item.
    - "How to use it": each scene's ``show``, in scene order.

    All user-supplied text is HTML-escaped — no XSS via concept_claim, show,
    rationale, or problem strings.

    Parameters
    ----------
    spec:
        The run's ``UnifiedSpec``.
    why_brief:
        The run's ``WhyBrief``.
    video_url:
        URL (HTTP/HTTPS or ``data:`` URI) of the hero video to embed.

    Returns
    -------
    str
        A complete, self-contained HTML document.
    """
    # Humanize the slug for display ("demo-driven-development" -> "Demo Driven Development");
    # keep the raw slug only for the <title>/internal use.
    _raw_name = spec.name or why_brief.narrative_slug
    feature_name = html.escape(_raw_name.replace("-", " ").replace("_", " ").strip().title())
    esc_poster = html.escape(poster_url, quote=True) if poster_url else ""
    # One-line lede: a docs page needs a crisp, plain-language hook (what it is + who
    # it's for), NOT the build-audience narrative. Prefer spec.tagline; fall back to the
    # narrative's first sentence only when no tagline is authored.
    tagline = (getattr(spec, "tagline", "") or "").strip()
    if tagline:
        narrative_lede = html.escape(tagline)
    else:
        _narr = (spec.narrative or "").strip()
        _first = re.split(r"(?<=\.)\s+", _narr, maxsplit=1)[0] if _narr else ""
        narrative_lede = html.escape(_first)

    # Embed the hero as the actual video bytes, not the canopy-web viewer page.
    # A canopy-web /walkthrough/<id> URL (viewer or content; legacy /w/<id>
    # too) is rewritten to the public …/content stream and played inline with
    # <video>; .mp4 / data: URIs likewise. Only a genuinely external embed
    # (no walkthrough path, e.g. a Loom share) falls back to an <iframe>.
    _path = video_url.split("?", 1)[0]
    is_canopy_artifact = (
        video_url.startswith("http://") or video_url.startswith("https://")
    ) and bool(_CANOPY_W_RE.search(_path))
    use_video = (
        is_canopy_artifact
        or video_url.startswith("data:")
        or _path.lower().endswith(".mp4")
    )

    hero_src = _to_content_url(video_url) if is_canopy_artifact else video_url
    esc_hero_src = html.escape(hero_src, quote=True)

    if use_video:
        poster_attr = f' poster="{esc_poster}"' if esc_poster else ""
        video_html = (
            f'<video controls preload="auto"{poster_attr} src="{esc_hero_src}">'
            f'<a href="{esc_hero_src}">Watch the demo video</a>'
            f"</video>"
        )
    else:
        video_html = (
            f'<iframe src="{esc_hero_src}" allowfullscreen title="Feature demo"></iframe>'
        )

    # --- What you can do ---
    # Prefer user-facing capabilities (reader benefits); fall back to the build-audience
    # concept_claims only when no plain capabilities are authored.
    cap_source = getattr(spec, "capabilities", []) or [s.concept_claim for s in spec.scenes]
    cap_items = ""
    for text in cap_source:
        cap_items += (
            f'<li><span class="cap-icon">&#10003;</span>'
            f'<span>{html.escape(text)}</span></li>\n'
        )

    capabilities_section = f"""<section id="capabilities">
<h2>What you can do</h2>
<ul class="capability-list">
{cap_items}</ul>
</section>"""

    # --- Get started (user-facing adoption steps) ---
    getting_started = getattr(spec, "getting_started", []) or []
    if getting_started:
        gs_items = "".join(
            f'<li><span class="step-num">{i}</span>'
            f'<span class="step-body">{html.escape(step)}</span></li>\n'
            for i, step in enumerate(getting_started, 1)
        )
        getting_started_section = f"""<section id="get-started">
<h2>Get started</h2>
<ol class="how-list">
{gs_items}</ol>
</section>"""
    else:
        getting_started_section = ""

    # --- Why it works this way ---
    # Prefer a short, plain why_summary for the user docs page; fall back to the
    # build-audience problem + spine only when no user-facing summary is authored.
    why_summary = (getattr(spec, "why_summary", "") or "").strip()
    if why_summary:
        why_section = f"""<section id="why">
<h2>Why it works this way</h2>
<div class="why-problem">{html.escape(why_summary)}</div>
</section>"""
    else:
        problem_esc = html.escape(why_brief.problem)
        spine_html = ""
        for item in why_brief.spine:
            claim_esc = html.escape(item.claim)
            rationale_esc = html.escape(item.rationale)
            spine_html += (
                f'<div class="spine-item">'
                f'<p class="spine-claim">{claim_esc}</p>'
                f'<p class="spine-rationale">{rationale_esc}</p>'
                f"</div>\n"
            )
        why_section = f"""<section id="why">
<h2>Why it works this way</h2>
<div class="why-problem">{problem_esc}</div>
{spine_html}</section>"""

    # --- How to use it ---
    how_items = ""
    for i, scene in enumerate(spec.scenes, 1):
        show_esc = html.escape(scene.show)
        how_items += (
            f'<li><span class="step-num">{i}</span>'
            f'<span class="step-body">{show_esc}</span></li>\n'
        )

    how_section = f"""<section id="how">
<h2>What the demo walks through</h2>
<ol class="how-list">
{how_items}</ol>
</section>"""

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{feature_name}</title>
<style>{_DOCS_CSS}</style>
</head>
<body>
<div class="page-wrap">

<div class="hero">
  <p class="hero-label">Feature documentation</p>
  <h1>{feature_name}</h1>
  {f'<p class="hero-lede">{narrative_lede}</p>' if narrative_lede else ''}
  <div class="hero-video-wrap">
    {video_html}
  </div>
  <p class="hero-caption">▶ Watch the 40-second demo</p>
</div>

{capabilities_section}

{getting_started_section}

{why_section}

<footer class="page-footer">Generated by canopy</footer>

</div>
</body>
</html>"""

    return page


# ---------------------------------------------------------------------------
# publish_artifact
# ---------------------------------------------------------------------------

# Content-type by kind
_CT_BY_KIND = {"html": "text/html", "video": "video/mp4"}
_FILENAME_BY_KIND = {"html": "docs.html", "video": "video.mp4"}


def _default_post(
    url: str,
    pat: str,
    fields: dict,
    filename: str,
    content_type: str,
    file_bytes: bytes,
) -> dict:
    """Real HTTP multipart POST to canopy-web using stdlib urllib.

    Returns the parsed JSON response body.
    Raises ``RuntimeError`` on non-201 responses.
    """
    boundary = (
        "----canopyupload"
        + base64.urlsafe_b64encode(os.urandom(9)).decode("ascii")
    )
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
    parts.append(f"Content-Type: {content_type}".encode())
    parts.append(b"")
    body = crlf.join(parts) + crlf + file_bytes + crlf + f"--{boundary}--".encode() + crlf

    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Authorization": f"Bearer {pat}",
    }
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)

    try:
        resp = urllib.request.urlopen(req, timeout=120)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {"error": exc.reason}
        raise RuntimeError(
            f"canopy-web upload failed (HTTP {exc.code}): {payload}"
        ) from exc

    raw = resp.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


def _external_links_from_manifest(manifest: dict) -> list[dict]:
    """Build "external systems" reference links from the render manifest: the
    base app URL plus each scene's already-resolved ``url`` and ``urls_visited``,
    de-duped in scene order.

    These are the live systems the run was actually recorded against — the pages
    and entities we used/created during the demo (including pages the run
    NAVIGATED to mid-scene, e.g. a freshly-created audit). Reading them from the
    manifest (not the spec) means the URLs are fully resolved — no unsubstituted
    ``${...}`` placeholders — and they include the ``urls_visited`` the engine
    captured. Returns ``[{label, url, kind: "reference"}, ...]``.
    """
    base = (manifest.get("base_url") or "").rstrip("/")
    out: list[dict] = []
    seen: set[str] = set()
    if base and base not in seen:
        out.append({"label": "App", "url": base, "kind": "reference"}); seen.add(base)
    for s in manifest.get("slides", []):
        if s.get("type") != "scene":
            continue
        label = s.get("title") or "Scene"
        for u in [s.get("url"), *s.get("urls_visited", [])]:
            if u and "${" not in u and u not in seen:
                out.append({"label": label, "url": u, "kind": "reference"}); seen.add(u)
    return out


def publish_artifact(
    content: str | bytes,
    *,
    kind: str,
    title: str,
    base_url: str | None = None,
    token: str | None = None,
    run_id: str | None = None,
    narrative_slug: str | None = None,
    role: str | None = None,
    narrative_review_id: str | None = None,
    links: list[dict] | None = None,
    _post=None,
) -> str:
    """Upload *content* to canopy-web and return the hosted URL.

    Parameters
    ----------
    content:
        The artifact to upload.  ``str`` → encoded as UTF-8.  ``bytes`` → sent
        as-is.
    kind:
        ``"html"`` or ``"video"``.
    title:
        Human-readable title shown in the canopy-web UI.
    base_url:
        canopy-web API base URL.  Falls back to ``CANOPY_WEB_API_URL`` env var
        then ``DEFAULT_API``.
    token:
        Bearer PAT.  Falls back to ``CANOPY_WEB_PAT`` env var then
        ``~/.claude/canopy/workbench-token``.
    _post:
        Injected HTTP callable for testing.  Signature::

            _post(url, pat, fields, filename, content_type, file_bytes) -> dict

        Defaults to the real multipart POST implementation.

    Returns
    -------
    str
        The hosted view URL — the tokened share link when the server returns
        one (``https://canopy-web.../walkthrough/<id>?t=<token>``), else the
        bare viewer URL.
    """
    if kind not in _CT_BY_KIND:
        raise ValueError(f"kind must be 'html' or 'video', got {kind!r}")

    api = _resolve_base_url(base_url)
    pat = _resolve_token(token)
    file_bytes = content.encode("utf-8") if isinstance(content, str) else content
    filename = _FILENAME_BY_KIND[kind]
    content_type = _CT_BY_KIND[kind]

    fields = {
        "title": title,
        "kind": kind,
        "description": "",
        "visibility": "link",
    }
    # DDD-run grouping so the run's artifacts package together under their run.
    if run_id:
        fields["run_id"] = run_id
    if narrative_slug:
        fields["narrative_slug"] = narrative_slug
    if role:
        fields["role"] = role
    # Link this run's artifacts to the narrative version they rendered.
    if narrative_review_id:
        fields["narrative_review_id"] = narrative_review_id
    # External systems the run used/created — rendered as a section on the run
    # page. Sent as a JSON list of {label, url, kind}.
    if links:
        fields["links"] = json.dumps(links)

    post_fn = _post if _post is not None else _default_post
    body = post_fn(
        f"{api}{_scoped_api('/api/walkthroughs/', _resolve_ws(None))}",
        pat,
        fields,
        filename,
        content_type,
        file_bytes,
    )

    wid = body.get("id")
    if not wid:
        raise RuntimeError(f"canopy-web returned unexpected response: {body}")

    # Public walkthroughs are token-gated: canopy-web returns the owner-only
    # tokened share_url (…/walkthrough/<id>?t=<token>) and never the raw
    # token. Fall back to the bare viewer URL (session-authed access only).
    share_url = body.get("share_url")
    if share_url:
        return share_url
    return f"{api}/walkthrough/{wid}"


# ---------------------------------------------------------------------------
# _default_gate — wraps review.post_review_request + await_resolution
# ---------------------------------------------------------------------------


def _default_gate(review_request: ReviewRequest, base_url: str | None, token: str | None) -> str:
    """Get the human's external_release decision — WITHOUT ever silently hanging.

    A blocking 24h poll here is what froze an autonomous publish once: "publish
    it" ran this gate, which posted the review and then polled canopy-web in
    silence for a UI click that, in a non-interactive run, can never come. The
    fix makes that hang structurally impossible:

    - The review URL is printed loudly the moment it's posted.
    - A NON-INTERACTIVE caller (agent / CI / piped run — ``stdin`` is not a TTY)
      does NOT block. The run is HELD (returns ``"hold"``; nothing published),
      and the caller is told to resolve the review and re-run, or re-run with
      ``--release-approved`` (``_delegated_gate``) to publish now.
    - An INTERACTIVE caller waits, but only a bounded 30 min and with a
      heartbeat — never a silent day.

    The resolved ``response_json`` maps decision id → chosen option, e.g.
    ``{"publish": "publish"}``; we return the ``"publish"`` decision's value.
    """
    from scripts.ddd import review as rv

    result = rv.post_review_request(
        review_request,
        visibility="link",
        base_url=base_url,
        token=token,
    )
    review_id = result["id"]
    review_url = result.get("url") or f"{_resolve_base_url(base_url)}/review/{review_id}/"
    print(
        f"\n[external_release] review posted — resolve it in canopy-web to publish:\n"
        f"    {review_url}\n",
        file=sys.stderr,
        flush=True,
    )

    if not sys.stdin.isatty():
        # Nobody can click a UI in a non-interactive run — never block on it.
        print(
            "[external_release] non-interactive run: NOT waiting on the gate; the "
            "run is held (nothing published).\n"
            "    Resolve the review above and re-run, or re-run with "
            "--release-approved to publish now with your approval.\n",
            file=sys.stderr,
            flush=True,
        )
        return "hold"

    print(
        "[external_release] waiting up to 30 min for approval (Ctrl-C to stop)…",
        file=sys.stderr,
        flush=True,
    )
    _hb = [0.0]

    def _heartbeat(elapsed: float) -> None:
        if elapsed - _hb[0] >= 30:
            _hb[0] = elapsed
            print(
                f"[external_release] …still waiting ({int(elapsed)}s) — {review_url}",
                file=sys.stderr,
                flush=True,
            )

    response_json = rv.await_resolution(
        review_id, base_url=base_url, token=token, on_wait=_heartbeat
    )
    # response_json is the raw resolved response dict; the convention is
    # {decision_id: chosen_option} — find the first decision's value.
    if isinstance(response_json, dict):
        for decision in review_request.decisions:
            if decision.id in response_json:
                return str(response_json[decision.id])
    # Fallback: if the server returned a simple string or unexpected shape, hold
    return "hold"


def _delegated_gate(review_request: ReviewRequest, base_url: str | None, token: str | None) -> str:
    """Post the external_release review AND resolve it immediately with the
    operator's already-given approval — instead of blocking on a canopy-web UI click.

    This is NOT the test bypass (``auto_approve_for_test``, which skips the gate
    entirely). The review is still CREATED and SUBMITTED — status ``resolved``,
    attributed to the token's user, with a ``_delegated`` note recording that the
    approval came from an out-of-band operator instruction — so publishing stays
    accountable and auditable. Each decision resolves to its ``recommended`` option
    (``publish`` for external_release). Use only when a human has explicitly
    authorized the release in-session.
    """
    from scripts.ddd import review as rv

    result = rv.post_review_request(
        review_request, visibility="link", base_url=base_url, token=token
    )
    review_id = result["id"]
    response_json: dict = {
        d.id: (d.recommended or (d.options[0] if d.options else "publish"))
        for d in review_request.decisions
    }
    response_json["_delegated"] = "operator-approved external release in-session (no UI click)"
    rv.resolve_review(review_id, response_json, base_url=base_url, token=token)
    for decision in review_request.decisions:
        if decision.id in response_json:
            return str(response_json[decision.id])
    return "publish"


# ---------------------------------------------------------------------------
# upload_run
# ---------------------------------------------------------------------------


def upload_run(
    run_id: str,
    *,
    video_path: str,
    base_url: str | None = None,
    token: str | None = None,
    _upload: Callable = None,  # type: ignore[assignment]
    _gate: Callable = None,  # type: ignore[assignment]
    _narrative_check: Callable = None,  # type: ignore[assignment]
    auto_approve_for_test: bool = False,
    release: bool = True,
    release_approved: bool = False,
) -> str:
    """Upload a run's artifacts to canopy-web as a navigable package.

    Two modes:

    - **release=True** (default, converged run): run the ``external_release`` gate
      and, on ``publish``, upload the deck, set ``phase = "uploaded"`` (terminal),
      and return the package URL. The public-release path.
    - **release=False** (a STUCK run — ``stop_unclear`` / ``stop_max_iter`` /
      ``stop_concept_change`` / ``stop_partial``): upload the navigable package for
      REVIEW without the external_release gate, and LEAVE ``phase`` unchanged so the
      run stays iterable. A stuck run is exactly when you want a navigable
      ``/ddd/<slug>/<run_id>`` to inspect per-scene and decide what to do next — the
      loop should never leave you without a package just because it didn't converge.
      The narrative + partial-run guards still apply.

    Orchestration
    -------------
    1. Load ``run_state.yaml`` for *run_id*.
    2. Load ``unified_spec.yaml`` and ``why_brief.yaml`` from the run directory.
    3. Upload the hero video (*video_path*) via ``_upload(..., kind="video")``.
    4. Build the docs HTML via ``build_docs_page``.
    5. Run the **external_release gate** — construct a ``ReviewRequest`` and
       call ``_gate(review_request, base_url, token)``.
    6. If the human chose ``"hold"``: return ``""`` without uploading HTML.
       Phase stays unchanged.
    7. If ``"publish"``: upload the HTML via ``_upload(..., kind="html")``,
       set ``run_state.phase = "uploaded"``, save, and return the run
       **package** URL (``/ddd/<narrative_slug>/<run_id>``) — the navigable view that
       groups the video, deck, narrative, and links, NOT the loose docs-page
       artifact URL.

    Parameters
    ----------
    run_id:
        Identifies the run under ``<ddd_dir>/runs/<run_id>/``.
    video_path:
        Path to the hero video ``.mp4`` on disk.
    base_url:
        Forwarded to ``_upload`` and ``_gate``.
    token:
        Forwarded to ``_upload`` and ``_gate``.
    _upload:
        Injected uploader callable for tests.  Defaults to ``publish_artifact``.
        Signature: ``(content, *, kind, title, base_url, token) -> str``.
    _gate:
        Injected gate callable for tests.
        Signature: ``(review_request, base_url, token) -> str`` (returns
        chosen option: ``"publish"`` or ``"hold"``).
        Defaults to ``_default_gate``.
    _narrative_check:
        Injected narrative-existence probe for tests.
        Signature: ``(narrative_slug, run_id, *, base_url, token) -> bool``.
        Defaults to ``_default_narrative_check`` (queries canopy-web).
    auto_approve_for_test:
        If ``True``, skip the gate and proceed directly to publish.  Only
        intended for integration tests that cannot mock ``_gate``.

    Returns
    -------
    str
        The run **package** URL (``/ddd/<narrative_slug>/<run_id>``) on publish, or
        ``""`` if the gate returned ``"hold"``.

    Raises
    ------
    NarrativeMissingError
        If the run has no narrative version (neither a stamped
        ``narrative_review_id`` nor a narrative on canopy-web for its narrative_slug),
        unless ``DDD_ALLOW_NO_NARRATIVE=1`` is set. This is the guard that stops
        a run being published as "no narrative".
    """
    upload_fn = _upload if _upload is not None else publish_artifact
    # release_approved → resolve the gate with an attributed, recorded approval the
    # operator already gave in-session (no UI click); otherwise block on the human.
    if _gate is not None:
        gate_fn = _gate
    elif release_approved:
        gate_fn = _delegated_gate
    else:
        gate_fn = _default_gate
    narrative_check = (
        _narrative_check if _narrative_check is not None else _default_narrative_check
    )

    # 1. Load run state
    run_state = load_state(run_id)

    # Uploaded runs are immutable — there is no "continuing" an uploaded run.
    # If this run already uploaded, return its existing package URL without
    # re-uploading. Re-rendering means a NEW run (resolve_narrative treats
    # uploaded/promoted as terminal and starts fresh), not a re-upload of this
    # one — that's what kept piling duplicate artifacts onto a single run_id.
    if run_state.phase in ("uploaded", "promoted"):
        print(
            f"run {run_id} is already {run_state.phase} — returning its existing "
            f"package URL without re-uploading. Start a new run to render again.",
            file=sys.stderr,
        )
        return run_package_url(run_state.narrative_slug, run_id, base_url)

    # The narrative VERSION this run rendered — the ID the narrative-agreement
    # gate stamped on run_state. Lets canopy-web attach the run to its exact
    # story version.
    narrative_review_id = _resolve_narrative_review_id(run_state)

    # GUARD: refuse to publish a run that has no narrative. Without this, a run
    # whose narrative gate never ran (or ran under a different narrative_slug slug)
    # publishes anyway and renders as "no narrative" in canopy-web. A stamped
    # narrative_review_id is proof the gate ran; if it's absent, re-verify
    # against canopy-web before allowing the publish.
    if not narrative_review_id and not os.environ.get(_ALLOW_NO_NARRATIVE_ENV):
        if not narrative_check(
            run_state.narrative_slug, run_id, base_url=base_url, token=token
        ):
            raise NarrativeMissingError(
                f"Refusing to upload run {run_id!r}: no narrative version exists "
                f"for narrative {run_state.narrative_slug!r} on canopy-web, so the "
                f"published package would render as \"no narrative\".\n"
                f"Run `/canopy:ddd-narrative-review {run_id}` first to post and "
                f"lock the narrative (it stamps run_state.narrative_review_id), "
                f"then re-run the upload.\n"
                f"This commonly happens when the narrative_slug slug was renamed "
                f"mid-flow (the narrative was posted under the old slug). "
                f"Emergency override: set {_ALLOW_NO_NARRATIVE_ENV}=1."
            )

    ddd_dir = _resolve_ddd_dir()
    run_dir = ddd_dir / "runs" / run_id

    # 2. Load spec + why_brief
    spec_path = run_dir / "unified_spec.yaml"
    why_brief_path = run_dir / "why_brief.yaml"

    spec = load_spec(spec_path)

    raw_why = yaml.safe_load(why_brief_path.read_text())
    why_brief = WhyBrief.model_validate(raw_why)

    # 3. Upload the hero video
    video_bytes = Path(video_path).read_bytes()
    video_url = upload_fn(
        video_bytes,
        kind="video",
        title=f"{spec.name} — hero video",
        base_url=base_url,
        token=token,
        run_id=run_id,
        narrative_slug=run_state.narrative_slug,
        role="hero_video",
        narrative_review_id=narrative_review_id,
    )

    # 4. Read the render manifest ONCE — it drives both the external-systems
    #    links (step 4) and the slideshow deck (step 6b). The engine emits
    #    walkthrough-run-data.json (record_video.py --manifest); if it's absent
    #    the run was never rendered (or by a stale path), so fail LOUD rather than
    #    fabricate a deck/links from the spec.
    sidecar_path = run_dir / "walkthrough-run-data.json"
    if not sidecar_path.exists():
        raise DeckMissingError(
            f"no render manifest at {sidecar_path}; render must emit "
            f"walkthrough-run-data.json (record_video --manifest). "
            f"Re-render before upload."
        )
    deck_run_data = json.loads(sidecar_path.read_text())

    # Build docs HTML + the external-systems links (live pages the run used),
    # both derived from the manifest's fully-resolved URLs.
    html_content = build_docs_page(spec, why_brief, video_url)
    external_links = _external_links_from_manifest(deck_run_data)

    # 5. External release gate — ONLY for a public release. A stuck/review upload
    #    (release=False) skips the gate: it's an internal inspection package, not a
    #    public release, and the run stays iterable.
    if release and not auto_approve_for_test:
        review_request = ReviewRequest(
            run_id=run_id,
            gate=Gate.EXTERNAL_RELEASE,
            video={"url": video_url},
            decisions=[
                Decision(
                    id="publish",
                    prompt="Publish this docs page for users?",
                    options=["publish", "hold"],
                    recommended="publish",
                    **{"class": Gate.EXTERNAL_RELEASE},
                )
            ],
            narration=[{"text": f"Docs page ready for {spec.name}. Review above video and approve to publish."}],
        )
        chosen = gate_fn(review_request, base_url, token)
        if chosen != "publish":
            # Human chose to hold — do not publish
            return ""

    # 6. Upload the docs HTML. The returned loose /w/ artifact URL is grouped
    #    server-side under the run via run_id/narrative_slug/role — we don't hand it to
    #    the user directly; the package URL (below) is the navigable entry point.
    upload_fn(
        html_content,
        kind="html",
        title=f"{spec.name} — documentation",
        base_url=base_url,
        token=token,
        run_id=run_id,
        narrative_slug=run_state.narrative_slug,
        role="docs",
        narrative_review_id=narrative_review_id,
        links=external_links,
    )

    # 6b. Upload the slideshow deck (role=deck) — built directly from the render
    #     manifest the engine emitted (read once at the top of upload_run). The
    #     package page's "Walkthrough slides" section reads role=deck; the deck is
    #     the manifest's scene slides rendered by build_presentation_html. There is
    #     NO spec-rebuild fallback: a missing or empty manifest is a render gap, so
    #     we fail LOUD (DeckMissingError) rather than silently skip the section.
    from scripts.walkthrough.generate_presentation import build_presentation_html

    deck_slides = [s for s in deck_run_data.get("slides", []) if s.get("type") == "scene"]
    if not deck_slides:
        raise DeckMissingError(f"manifest {sidecar_path} has zero scene slides")
    upload_fn(
        build_presentation_html(deck_run_data),
        kind="html",
        title=f"{spec.name} — walkthrough slides",
        base_url=base_url,
        token=token,
        run_id=run_id,
        narrative_slug=run_state.narrative_slug,
        role="deck",
        narrative_review_id=narrative_review_id,
    )

    # 7. Update run state. A release marks the run terminal (uploaded); a stuck
    #    review upload leaves phase unchanged so the run can keep iterating toward
    #    convergence — the package is just an inspection view, not a publish.
    if release:
        run_state.phase = "uploaded"
        save_state(run_state)

    # 8. Return the run PACKAGE URL — the navigable /ddd/<narrative_slug>/<run_id> view
    #    that canopy-web assembles from the run's video + deck + narrative +
    #    links, NOT the loose docs artifact URL.
    return run_package_url(run_state.narrative_slug, run_id, base_url)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        prog="scripts.ddd.upload",
        description="Upload a converged DDD run's artifacts to canopy-web as a navigable package.",
    )
    parser.add_argument("run_id", help="Run identifier (e.g. my-narrative_slug-2026-01-01-001)")
    parser.add_argument("--video", required=True, dest="video_path", help="Path to hero video .mp4")
    parser.add_argument("--base-url", default=None, help="canopy-web API base URL")
    parser.add_argument(
        "--stuck",
        action="store_true",
        help=(
            "Upload a STUCK (non-converged) run as a navigable REVIEW package: skip "
            "the external_release gate and leave phase unchanged (run stays iterable). "
            "Use when the loop stopped on stop_unclear / stop_max_iter / "
            "stop_concept_change / stop_partial so there's always a /ddd/<slug>/<run_id> "
            "to inspect per-scene and decide next steps."
        ),
    )
    parser.add_argument(
        "--release-approved",
        action="store_true",
        help=(
            "Resolve the external_release gate with an approval the operator already "
            "gave in-session, instead of blocking on a canopy-web UI click. NOT a "
            "bypass: the review is still created and submitted (status resolved, "
            "attributed to the token's user, with a delegated-approval note), so the "
            "publish stays auditable. Pass ONLY when a human has explicitly authorized "
            "the release."
        ),
    )
    args = parser.parse_args(argv)

    package_url = upload_run(
        args.run_id,
        video_path=args.video_path,
        base_url=args.base_url,
        release=not args.stuck,
        release_approved=args.release_approved,
    )
    if package_url:
        label = "Review package (stuck run)" if args.stuck else "Uploaded"
        print(f"{label}: {package_url}")
    else:
        print("Upload held — the run package was not published.")


if __name__ == "__main__":
    main()
