"""Deep-linked findings review for demo-driven-development.

DDD has ONE behavior (no `review_mode` — that field is deprecated/ignored): the
loop auto-applies every finding it can act on itself (`fix_kind: mechanical`) and
surfaces ONLY what it genuinely can't decide (`fix_kind: options`/`redesign`, plus
the `concept_change`/`external_release` gates). This module IS that surfacing —
the deep-linked review the loop posts when it hits something undecidable, so the
human decides from evidence (a thumbnail + deck/video deep-links) instead of a
hand-written chat table they'd have to scrub the deck and video to verify.

This module formalizes that flow as a **first-class RUN-CHILD review** (gate
``product_findings``). It is NOT a narrative version: the posted request_json
carries **no ``narrative_slug``** — canopy-web pins ``narrative_slug=None,
version=0`` for this gate, filing the review under the run rather than
polluting the narrative timeline.

The request_json (the canopy ↔ canopy-web contract) carries:
``run_id``, ``gate``, ``feature``, ``iteration``, ``video {url}``,
``deck_url``, ``summary {concept_score, user_score, verdict}``, and
``clusters[]`` where each cluster has ``id``, ``title``, ``severity``,
``fix_kind``, ``route``, ``scenes``, ``suggested_fix``, ``count``, and
``evidence[]``. Each evidence item carries an **inline thumbnail**
(``thumb`` — a ~480px-wide JPEG of that scene's screenshot, base64 data-URI),
the deck anchor (``deck_anchor`` = ``#scene-<N>``), and the integer video
offset (``video_t`` = that scene's ``start_seconds``). The user opens one
link, reads each cluster next to its inline evidence, and decides
implement / skip / defer per cluster.

Where the data comes from in the run dir:
  - ``design_findings.json``  — the concept judge's PRODUCT findings (clusters source).
  - ``run-report.json``       — per-scene ``start_seconds`` (→ ``video_t``).
  - ``snapshots_iter<N>/scene_<N>.png`` (or the flat ``snapshots/scene_<N>.png``
    fallback) — the per-scene screenshot downscaled into ``evidence[].thumb``.
  - ``verdict-concept.yaml`` / ``verdict-user.yaml`` — the ``summary`` scores.

Public API (pure functions — no network):
    cluster_findings(findings, user_verdict=None, scene_resolver=None) -> list[dict]
    build_findings_review_request(run_id, feature, iteration, spec, findings,
                                  user_verdict, deck_url, clip_url,
                                  scene_timestamps, summary=None,
                                  thumb_resolver=None) -> ReviewRequest
    parse_selection(response_json) -> dict
    resolve_review_mode(spec_or_path) -> str

CLI (post touches network via review.post_review_request):
    python -m scripts.ddd.findings_review post <run_id> [--spec PATH] [--deck-url URL] [--clip-url URL]
    python -m scripts.ddd.findings_review apply <response_json_file>
    python -m scripts.ddd.findings_review mode <spec_path>
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

# NOTE: ``scripts.ddd.narrative`` is imported ONLY for slug-derivation /
# review-URL helpers used by the CLI presentation layer. The POSTed payload for
# gate=product_findings must NOT carry a narrative_slug (it's a run-child), so we
# deliberately do not call ``_narrative_slug_from_run_id`` into the request.
from scripts.ddd.narrative import (
    _internal_review_url,
    _title_slug,
    _tokenized_review_url,
)
from scripts.ddd.schemas.models import Decision, Gate, NarrationItem, ReviewRequest, UnifiedSpec

GATE = Gate.PRODUCT_FINDINGS

# Decision vocabulary — per cluster, and the one overall decision.
CLUSTER_OPTIONS = ["implement", "skip", "defer"]
OVERALL_DECISION_ID = "findings-verdict"
OVERALL_OPTIONS = ["proceed with selected", "discuss"]

# Merge orderings: a cluster takes the WORST of its members.
_FIX_KIND_ORDER = {"mechanical": 0, "options": 1, "redesign": 2}
_SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}

_SCENE_INT_RE = re.compile(r"(\d+)")


# ---------------------------------------------------------------------------
# Scene resolution — finding["scene"] is "<scene title or index>" per the
# ddd-concept-eval contract, so it may be an int, "3", "Scene 3", or a title.
# ---------------------------------------------------------------------------


def make_scene_resolver(spec: UnifiedSpec | dict | None):
    """Return a ``str|int -> int|None`` resolver mapping a finding's ``scene``
    field to its 1-based spec index.

    Resolution order: int / digit string → that index; ``"Scene N"`` style →
    N; otherwise slug-match against the spec's scene titles. ``None`` when the
    value can't be resolved (the cluster then gets no per-scene anchors).
    """
    title_to_index: dict[str, int] = {}
    scenes = []
    if isinstance(spec, UnifiedSpec):
        scenes = [s.title for s in spec.scenes]
    elif isinstance(spec, dict):
        scenes = [s.get("title", "") for s in (spec.get("scenes") or []) if isinstance(s, dict)]
    for i, title in enumerate(scenes, start=1):
        title_to_index[_title_slug(title)] = i

    def resolve(value) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        s = str(value).strip()
        if not s:
            return None
        if s.isdigit():
            return int(s)
        slug = _title_slug(s)
        if slug in title_to_index:
            return title_to_index[slug]
        m = _SCENE_INT_RE.search(s)
        if m and s.lower().startswith("scene"):
            return int(m.group(1))
        return None

    return resolve


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


def _one_line_title(detail: str, *, max_len: int = 110) -> str:
    """First sentence of *detail*, truncated — the cluster's scannable title."""
    text = " ".join((detail or "").split())
    if not text:
        return "(untitled finding)"
    first = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0]
    if len(first) > max_len:
        first = first[: max_len - 1].rstrip() + "…"
    return first


def _worst(values: list[str], order: dict[str, int], default: str) -> str:
    best = default
    best_rank = order.get(default, 0)
    for v in values:
        rank = order.get(v, -1)
        if rank > best_rank:
            best, best_rank = v, rank
    return best


def cluster_findings(
    findings: list[dict],
    *,
    user_verdict: dict | None = None,
    scene_resolver=None,
) -> list[dict]:
    """Cluster PRODUCT findings for the review surface.

    Parameters
    ----------
    findings:
        ``design_findings.json`` entries (the ddd-concept-eval contract:
        ``scene``, ``dimension``, ``severity``, ``route``, ``detail``,
        ``fix_recommendation``, optional ``fix_kind``, optional explicit
        ``cluster`` key).
    user_verdict:
        The parsed ``verdict-user.yaml`` dict.  Each dimension entry carrying
        a ``fix_kind`` (i.e. it scored ≤3 and emitted a finding) becomes its
        own cluster with id ``user-<dimension>``.  These findings are
        artifact-wide (the user-artifact judge scores the whole run), so they
        carry no scene anchor unless the justification names one.
    scene_resolver:
        ``str|int -> int|None`` mapping a finding's ``scene`` field to its
        1-based spec index (see :func:`make_scene_resolver`).  ``None`` →
        digit-only resolution.

    Returns
    -------
    list[dict]
        One dict per cluster: ``cluster_id``, ``title``, ``detail``,
        ``scenes`` (sorted 1-based indices), ``scene_labels`` (raw values
        that didn't resolve), ``dimension``, ``severity``, ``route``,
        ``fix_kind``, ``suggested_fix``, ``count``.  Only ``route ==
        "PRODUCT"`` findings are included — CONCEPT/RESEARCH/DEFER have
        their own routing and never reach this gate.

    Grouping key: an explicit ``cluster`` key on the finding when present,
    else ``(scene, dimension)`` — repeated judge output about the same scene
    + dimension reads as one decision, not N near-duplicate rows.
    """
    resolve = scene_resolver or make_scene_resolver(None)

    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for f in findings or []:
        if not isinstance(f, dict):
            continue
        if (f.get("route") or "").upper() != "PRODUCT":
            continue
        explicit = (f.get("cluster") or "").strip()
        if explicit:
            key = f"cluster:{_title_slug(explicit)}"
        else:
            idx = resolve(f.get("scene"))
            dim = f.get("dimension") or "general"
            scene_part = f"scene-{idx}" if idx is not None else _title_slug(str(f.get("scene") or "no-scene"))
            key = f"{scene_part}-{_title_slug(dim)}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(f)

    clusters: list[dict] = []
    for key in order:
        members = groups[key]
        cluster_id = key.removeprefix("cluster:")
        scenes: set[int] = set()
        scene_labels: list[str] = []
        details: list[str] = []
        fixes: list[str] = []
        for f in members:
            idx = resolve(f.get("scene"))
            if idx is not None:
                scenes.add(idx)
            elif f.get("scene"):
                label = str(f["scene"])
                if label not in scene_labels:
                    scene_labels.append(label)
            d = (f.get("detail") or "").strip()
            if d and d not in details:
                details.append(d)
            fx = (f.get("fix_recommendation") or "").strip()
            if fx and fx not in fixes:
                fixes.append(fx)
        detail = "\n".join(details)
        # The judge owns severity. Take the worst of the severities its findings
        # actually carried; leave it None when NONE did, so the request builder
        # knows to fall back to derive_severity rather than inventing "low".
        present_sev = [m["severity"] for m in members if (m.get("severity") or "").strip()]
        severity = _worst(present_sev, _SEVERITY_ORDER, "low") if present_sev else None
        clusters.append(
            {
                "cluster_id": cluster_id,
                "title": _one_line_title(details[0] if details else ""),
                "detail": detail,
                "scenes": sorted(scenes),
                "scene_labels": scene_labels,
                "dimension": members[0].get("dimension") or "general",
                "severity": severity,
                "route": "PRODUCT",
                "fix_kind": _worst(
                    [m.get("fix_kind", "options") for m in members], _FIX_KIND_ORDER, "mechanical"
                ),
                "suggested_fix": "\n".join(fixes),
                "count": len(members),
            }
        )

    # User-artifact judge findings: one cluster per dimension carrying a fix.
    dims = (user_verdict or {}).get("dimensions") or {}
    for dim_id, entry in dims.items():
        if not isinstance(entry, dict) or not entry.get("fix_kind"):
            continue
        score = entry.get("score")
        try:
            severity = "high" if float(score) <= 2 else "medium"
        except (TypeError, ValueError):
            severity = "medium"
        detail = (entry.get("justification") or "").strip()
        clusters.append(
            {
                "cluster_id": f"user-{_title_slug(str(dim_id))}",
                "title": _one_line_title(detail) if detail else f"User-artifact: {dim_id}",
                "detail": detail,
                "scenes": [],
                "scene_labels": [],
                "dimension": str(dim_id),
                "severity": severity,
                "route": "PRODUCT",
                "fix_kind": entry.get("fix_kind", "options"),
                "suggested_fix": (entry.get("fix_recommendation") or "").strip(),
                "count": 1,
            }
        )

    return clusters


# ---------------------------------------------------------------------------
# Severity derivation
# ---------------------------------------------------------------------------


def derive_severity(*, route: str, fix_kind: str, score: float | None) -> str:
    """Map a finding's (route, fix_kind, judge score) → high | medium | low.

    The contract leaves severity to the poster, so we pick a sane, explainable
    mapping:

      * A PRODUCT/CONCEPT finding on a badly-scored iteration (overall judge
        score ≤ 2 on the 1–5 scale) is **high** — the artifact is failing and
        this finding is part of why.
      * A ``redesign`` fix is **high** regardless of score (it's never trivial).
      * An ``options`` fix is **medium** (needs a human pick but isn't a fire).
      * A ``mechanical`` fix on a non-failing iteration is **low** (clean,
        unambiguous, safe to apply).

    ``score`` is the gating judge overall_score for the iteration (lower of the
    two judges); ``None`` when unknown → treated as not-failing.
    """
    rt = (route or "").upper()
    fk = (fix_kind or "").lower()
    failing = score is not None and float(score) <= 2
    if fk == "redesign":
        return "high"
    if failing and rt in ("PRODUCT", "CONCEPT"):
        return "high"
    if fk == "options":
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Thumbnails — inline downscaled JPEG data-URIs for evidence
# ---------------------------------------------------------------------------

#: Default thumbnail width (px) and JPEG quality per the contract (~480px / q≈70).
THUMB_WIDTH = 480
THUMB_QUALITY = 70


def thumbnail_data_uri(
    png_path: Path, *, width: int = THUMB_WIDTH, quality: int = THUMB_QUALITY
) -> str | None:
    """Downscale *png_path* to a ``data:image/jpeg;base64,…`` URI, or None.

    Loads the PNG with Pillow, scales it to ``width`` px wide (preserving
    aspect ratio; never upscales), flattens any alpha onto white (JPEG has no
    alpha), encodes JPEG at ``quality``, and base64-wraps it as an inline
    data-URI suitable for ``evidence[].thumb``.

    Returns ``None`` (rather than raising) when the file is missing or
    unreadable — a run captured before per-scene snapshots existed degrades to
    "no inline thumb", exactly as untimed scenes degrade to "no video link".
    """
    if png_path is None or not Path(png_path).exists():
        return None
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise RuntimeError(
            "Pillow is required for findings-review thumbnails; add 'pillow' to "
            "the canopy pyproject deps (uv add pillow)."
        ) from exc
    try:
        with Image.open(png_path) as im:
            im = im.convert("RGBA")
            if im.width > width:
                height = max(1, round(im.height * width / im.width))
                im = im.resize((width, height), Image.LANCZOS)
            # Flatten onto white — JPEG can't carry the alpha channel.
            background = Image.new("RGB", im.size, (255, 255, 255))
            background.paste(im, mask=im.split()[-1])
            buf = io.BytesIO()
            background.save(buf, format="JPEG", quality=quality, optimize=True)
    except Exception:
        return None
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def make_thumb_resolver(run_dir: Path | None, iteration: int):
    """Return a ``scene:int -> (thumb|None)`` resolver bound to a run dir.

    Looks for the scene PNG at the per-iteration archive path the contract
    names — ``<run_dir>/snapshots_iter<N>/scene_<scene>.png`` — and falls back
    to the flat capture dir the recorder actually writes today,
    ``<run_dir>/snapshots/scene_<scene>.png`` (overwritten each iteration). The
    first that exists is downscaled via :func:`thumbnail_data_uri`.
    """

    def resolve(scene: int) -> str | None:
        if run_dir is None:
            return None
        candidates = [
            Path(run_dir) / f"snapshots_iter{iteration}" / f"scene_{scene}.png",
            Path(run_dir) / "snapshots" / f"scene_{scene}.png",
        ]
        for path in candidates:
            if path.exists():
                return thumbnail_data_uri(path)
        return None

    return resolve


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


def _format_ts(seconds: float) -> str:
    """``83.4 -> "1:23"`` — mm:ss label for a video timestamp."""
    total = int(seconds)
    return f"{total // 60}:{total % 60:02d}"


def build_evidence(
    cluster: dict,
    *,
    scene_timestamps: dict[int, float] | None,
    thumb_resolver=None,
) -> list[dict]:
    """Build the contract ``evidence[]`` list for one cluster.

    One entry per scene the cluster touches, in the contract shape::

        {"scene": 9, "thumb": "data:image/jpeg;base64,…",
         "deck_anchor": "#scene-9", "video_t": 84}

    - ``thumb``: the downscaled JPEG data-URI of ``scene_<N>.png`` via
      *thumb_resolver* (``None`` when the snapshot is missing — the surface
      then shows just the deck/video deep-links for that scene).
    - ``deck_anchor``: always ``#scene-<N>`` (combined with the request's
      ``deck_url`` on the surface).
    - ``video_t``: integer seconds = that scene's ``start_seconds`` from the
      run report; omitted when the recorder skipped/never-timed that scene.

    A cluster with no resolvable scenes (e.g. an artifact-wide user-judge
    finding) returns ``[]`` — the surface falls back to the top-level video +
    deck links.
    """
    ts = scene_timestamps or {}
    out: list[dict] = []
    for n in cluster.get("scenes") or []:
        item: dict = {"scene": n, "deck_anchor": f"#scene-{n}"}
        thumb = thumb_resolver(n) if thumb_resolver else None
        if thumb:
            item["thumb"] = thumb
        if n in ts:
            item["video_t"] = int(ts[n])
        out.append(item)
    return out


# ``base`` captures the host AND any deployment sub-path (e.g. ``/canopy`` on
# labs.connect.dimagi.com/canopy) so the derived ``/w/<id>/content`` URL keeps
# the prefix — otherwise the clip stream 404s and the review drops clip_url.
_W_PAGE_RE = re.compile(
    r"^(?P<base>https?://[^/]+(?:/[^/]+)*?)/w/(?P<wid>[0-9a-fA-F-]{36})"
)


def _clip_content_url(clip_url: str | None) -> str | None:
    """Derive the streamable ``/w/<id>/content`` URL from a ``/w/<id>`` page URL.

    The review surface embeds ``request_json.video.url`` in a player; the
    ``/w/<id>`` page URL is an HTML app route, not media — the bytes live at
    ``/w/<id>/content``.  Preserves any existing query (e.g. a share token).
    Returns ``None`` when *clip_url* isn't a recognisable ``/w/`` URL.
    """
    if not clip_url:
        return None
    m = _W_PAGE_RE.match(clip_url)
    if not m:
        return None
    query = ""
    if "?" in clip_url:
        query = "?" + clip_url.split("?", 1)[1].split("#", 1)[0]
    return f"{m.group('base')}/w/{m.group('wid')}/content{query}"


# ---------------------------------------------------------------------------
# Request building
# ---------------------------------------------------------------------------


def _summary_verdict(concept: float | None, user: float | None) -> str:
    """Headline verdict from the two judge overall_scores (1–5 scale).

    Weakest-link, mirroring the judges' own ``overall_rule: lowest``: FAIL if
    either is ≤2, WARN if either is ≤3, else PASS. Unknown scores don't drag
    the verdict down (a missing judge isn't a failing judge)."""
    scores = [s for s in (concept, user) if s is not None]
    if not scores:
        return "UNKNOWN"
    low = min(scores)
    if low <= 2:
        return "FAIL"
    if low <= 3:
        return "WARN"
    return "PASS"


def build_findings_review_request(
    run_id: str,
    feature: str,
    iteration: int,
    spec: UnifiedSpec | dict | None,
    findings: list[dict],
    user_verdict: dict | None,
    deck_url: str | None,
    clip_url: str | None,
    scene_timestamps: dict[int, float] | None,
    *,
    summary: dict | None = None,
    thumb_resolver=None,
) -> ReviewRequest:
    """Build the ``product_findings`` RUN-CHILD ReviewRequest for one iteration.

    This is NOT a narrative version: the request carries **no
    ``narrative_slug``** (left ""). canopy-web pins ``narrative_slug=None,
    version=0`` for gate=product_findings and files the review under the run.

    Each PRODUCT finding CLUSTER (see :func:`cluster_findings`) becomes one
    contract cluster (``id``, ``title``, ``severity``, ``fix_kind``, ``route``,
    ``scenes``, ``suggested_fix``, ``count``, ``evidence``) and one
    implement/skip/defer Decision. ``evidence[]`` carries an inline downscaled
    JPEG thumb, the deck ``#scene-<N>`` anchor, and the integer ``video_t`` so
    the reviewer never has to scrub for the moment a finding refers to. Plus
    one overall decision: proceed with the selected fixes, or discuss.

    The contract cluster list rides on ``ReviewRequest.findings``; the
    orchestrator reads the resolved ``response_json.decisions`` back via
    :func:`parse_selection`.
    """
    summary = summary or {}
    gating_score = summary.get("concept_score")
    if summary.get("user_score") is not None:
        gating_score = (
            min(gating_score, summary["user_score"])
            if gating_score is not None
            else summary["user_score"]
        )

    resolver = make_scene_resolver(spec)
    raw_clusters = cluster_findings(findings, user_verdict=user_verdict, scene_resolver=resolver)

    clusters: list[dict] = []
    narration: list[NarrationItem] = []
    decisions: list[Decision] = []
    for i, cluster in enumerate(raw_clusters, start=1):
        evidence = build_evidence(
            cluster,
            scene_timestamps=scene_timestamps,
            thumb_resolver=thumb_resolver,
        )
        # The judge is the severity source. Use the severity its findings carried
        # (clustered worst-of); fall back to derive_severity ONLY when no finding
        # in the cluster supplied one.
        severity = cluster.get("severity") or derive_severity(
            route=cluster["route"], fix_kind=cluster["fix_kind"], score=gating_score
        )
        contract_cluster = {
            "id": cluster["cluster_id"],
            "title": cluster["title"],
            "severity": severity,
            "fix_kind": cluster["fix_kind"],
            "route": cluster["route"],
            "scenes": cluster["scenes"],
            "suggested_fix": cluster["suggested_fix"],
            "count": cluster["count"],
            "evidence": evidence,
        }
        clusters.append(contract_cluster)

        # Mirror the cluster into narration as plain text so surfaces that don't
        # know the findings[] field still render a readable review.
        text_parts = [cluster["detail"] or cluster["title"]]
        if cluster["suggested_fix"]:
            text_parts.append(f"Suggested fix: {cluster['suggested_fix']}")
        scene_tag = (
            "scenes " + ", ".join(str(n) for n in cluster["scenes"])
            if cluster["scenes"]
            else "whole artifact"
        )
        narration.append(
            NarrationItem(
                scene=i,
                id=cluster["cluster_id"],
                title=cluster["title"],
                text="\n\n".join(text_parts),
            )
        )
        decisions.append(
            Decision(
                id=cluster["cluster_id"],
                prompt=(
                    f"[{severity} · {cluster['dimension']} · "
                    f"{cluster['fix_kind']} · {scene_tag}] {cluster['title']}"
                ),
                options=list(CLUSTER_OPTIONS),
                recommended="implement",
                **{"class": GATE},
            )
        )

    decisions.append(
        Decision(
            id=OVERALL_DECISION_ID,
            prompt="Proceed with the selected fixes, or discuss first?",
            options=list(OVERALL_OPTIONS),
            recommended="proceed with selected",
            **{"class": GATE},
        )
    )

    video: dict = {}
    content_url = _clip_content_url(clip_url)
    if content_url:
        video = {"url": content_url}

    return ReviewRequest(
        run_id=run_id,
        # RUN-CHILD: deliberately NO narrative_slug — gate=product_findings is a
        # run-child, not a narrative version. Leaving it "" lets canopy-web pin
        # narrative_slug=None, version=0 rather than fighting a slug we send.
        gate=GATE,
        feature=feature,
        iteration=iteration,
        video=video,
        deck_url=(deck_url or ""),
        summary=summary,
        narration=narration,
        decisions=decisions,
        findings=clusters,
        autonomous_audit=[],
    )


# ---------------------------------------------------------------------------
# Response parsing (the `apply` subcommand)
# ---------------------------------------------------------------------------


def parse_selection(response_json: dict) -> dict:
    """Turn a resolved review's ``response_json`` into a machine-readable selection.

    Contract ``response_json`` shape (per-finding decision + comment; nothing is
    pre-selected, only findings the reviewer touched are present)::

        {"decisions": {"<cluster_id>": {"decision": "implement" | "skip" | null,
                                        "comment": "free text"}}}

    ``decision`` is ``null`` when the reviewer left a comment but didn't pick
    implement/skip — treat that as guidance to address, never as an auto-skip.
    Returns::

        {"selections": [{"cluster_id": str, "decision": str|None, "comment": str}, ...],
         "implement": [cluster ids],          # decision == "implement"
         "skip": [cluster ids],               # decision == "skip"
         "commented": [cluster ids],          # any non-empty comment
         "comments": {cluster_id: comment}}   # non-empty comments, keyed by id

    Backward-compatible: a legacy flat value (``decisions[id] == "implement"``)
    is still bucketed correctly.
    """
    response_json = response_json or {}
    decisions: dict = response_json.get("decisions") or {}
    selections: list[dict] = []
    buckets: dict[str, list[str]] = {"implement": [], "skip": []}
    commented: list[str] = []
    comments: dict[str, str] = {}
    for cluster_id, entry in decisions.items():
        if isinstance(entry, dict):
            decision = entry.get("decision")
            comment = (entry.get("comment") or "").strip()
        else:
            # Legacy flat shape: the value IS the decision string.
            decision = entry
            comment = ""
        selections.append({"cluster_id": cluster_id, "decision": decision, "comment": comment})
        if decision in buckets:
            buckets[decision].append(cluster_id)
        if comment:
            commented.append(cluster_id)
            comments[cluster_id] = comment
    return {
        "selections": selections,
        "implement": buckets["implement"],
        "skip": buckets["skip"],
        "commented": commented,
        "comments": comments,
    }


# ---------------------------------------------------------------------------
# Review mode resolution
# ---------------------------------------------------------------------------


def resolve_review_mode(spec_or_path) -> str:
    """Return the effective review mode for a spec: ``autonomous`` | ``human``.

    Accepts a spec dict, a ``UnifiedSpec``, or a path to the spec YAML.  The
    mode lives on the spec's optional ``review_mode`` key; absent, unreadable,
    or unrecognised values default to ``autonomous`` (the documented default —
    human review is always an explicit opt-in).
    """
    raw = None
    if isinstance(spec_or_path, UnifiedSpec):
        return spec_or_path.review_mode
    if isinstance(spec_or_path, dict):
        raw = spec_or_path
    else:
        p = Path(spec_or_path)
        if not p.exists():
            return "autonomous"
        try:
            raw = yaml.safe_load(p.read_text())
        except Exception:
            return "autonomous"
    if not isinstance(raw, dict):
        return "autonomous"
    mode = raw.get("review_mode")
    return mode if mode in ("autonomous", "human") else "autonomous"


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------


def _latest(d: dict) -> str | None:
    """Value at the largest int key of an ``iteration -> url`` map."""
    if not d:
        return None
    try:
        key = max(d, key=lambda k: int(k))
    except (TypeError, ValueError):
        return None
    return d[key]


def _git_toplevel() -> Path | None:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], stderr=subprocess.DEVNULL, text=True
        ).strip()
        return Path(out) if out else None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _default_spec_path(narrative_slug: str) -> Path | None:
    """``<git-toplevel>/docs/walkthroughs/<slug>.yaml`` when it exists."""
    top = _git_toplevel()
    if top is None:
        return None
    candidate = top / "docs" / "walkthroughs" / f"{narrative_slug}.yaml"
    return candidate if candidate.exists() else None


def _load_yaml(path: Path) -> dict | None:
    try:
        data = yaml.safe_load(path.read_text())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _load_json(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _cmd_post(args: argparse.Namespace) -> None:
    """Post the product-findings review for *run_id*, stamp run_state, print JSON.

    Inputs are resolved from the run dir (CWD's git toplevel via
    ``runstate._resolve_ddd_dir``): ``design_findings.json``,
    ``verdict-user.yaml``, ``run-report.json`` (scene timestamps), and the
    run_state's ``iteration_decks`` / ``iteration_clips`` for the hosted
    artifact URLs.  Prints ``{posted: false, reason}`` (exit 0) when there are
    no PRODUCT findings — that's a clean outcome, not an error.
    """
    from scripts.ddd import review as rv  # network-touching — local import
    from scripts.ddd import runstate as rs
    from scripts.walkthrough._lib.results import scene_timestamps as read_scene_timestamps

    run_id = args.run_id
    try:
        state = rs.load(run_id)
    except FileNotFoundError:
        print(f"ERROR: run_state for {run_id!r} not found — wrong run_id or wrong CWD?", file=sys.stderr)
        sys.exit(1)

    run_dir = rs.run_dir_for(run_id)
    findings = _load_json(run_dir / "design_findings.json") or []
    user_verdict = _load_yaml(run_dir / "verdict-user.yaml")
    concept_verdict = _load_yaml(run_dir / "verdict-concept.yaml") or {}
    report = _load_json(run_dir / "run-report.json") or {}
    timestamps = read_scene_timestamps(report) if isinstance(report, dict) else {}

    iteration = state.iteration
    deck_url = (args.deck_url or "").strip() or state.iteration_decks.get(iteration) or _latest(
        state.iteration_decks
    )
    clip_url = (args.clip_url or "").strip() or state.iteration_clips.get(iteration) or _latest(
        state.iteration_clips
    )

    spec: dict | None = None
    spec_path = Path(args.spec) if args.spec else _default_spec_path(state.narrative_slug)
    if spec_path is not None:
        spec = _load_yaml(spec_path)
        if spec is None:
            print(f"WARNING: could not read spec at {spec_path} — scene titles won't resolve.", file=sys.stderr)

    # Summary headline from the two judge verdicts (overall_score, weakest-link
    # verdict). The feature is the run's narrative/feature slug.
    concept_score = (concept_verdict or {}).get("overall_score")
    user_score = (user_verdict or {}).get("overall_score")
    summary = {
        "concept_score": concept_score,
        "user_score": user_score,
        "verdict": _summary_verdict(concept_score, user_score),
    }
    feature = state.narrative_slug

    # Per-scene thumbnails: snapshots_iter<N>/scene_<N>.png (contract) with a
    # fallback to the flat snapshots/ dir the recorder writes today.
    thumb_resolver = make_thumb_resolver(run_dir, iteration)

    request = build_findings_review_request(
        run_id,
        feature,
        iteration,
        spec,
        findings,
        user_verdict,
        deck_url,
        clip_url,
        timestamps,
        summary=summary,
        thumb_resolver=thumb_resolver,
    )

    if not request.findings:
        print(
            json.dumps(
                {"posted": False, "reason": "no PRODUCT findings to review", "run_id": run_id}
            )
        )
        return

    result = rv.post_review_request(request)

    # Stamp run_state so the orchestrator (and a resumed session) can find the
    # pending review without re-posting. Mirrors narrative post's stamping.
    review_id = (result.get("id") or "").strip()
    if review_id:
        state.findings_review_id = review_id
    share = _tokenized_review_url(result)
    if share:
        state.findings_review_url = share
    rs.save(state)

    base = rv._resolve_base_url(None)
    out = dict(result)
    out["posted"] = True
    out["clusters"] = len(request.findings)
    internal = _internal_review_url(result, base)
    if internal:
        out["internal_url"] = internal
    if share:
        out["share_url"] = share if share.startswith("http") else f"{base.rstrip('/')}{share}"
    if internal:
        print(f"internal (owner, left rail): {internal}", file=sys.stderr)
    if out.get("share_url"):
        print(f"external (share, no rail):   {out['share_url']}", file=sys.stderr)
    print(json.dumps(out))


def _cmd_apply(args: argparse.Namespace) -> None:
    """Parse a resolved response JSON file and print the selection."""
    response_path = Path(args.response_json_file)
    if not response_path.exists():
        print(f"ERROR: response JSON file not found: {response_path}", file=sys.stderr)
        sys.exit(1)
    response_json = json.loads(response_path.read_text())
    print(json.dumps(parse_selection(response_json)))


def _cmd_mode(args: argparse.Namespace) -> None:
    """Print the effective review mode for a spec path: autonomous | human."""
    print(resolve_review_mode(args.spec_path))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m scripts.ddd.findings_review")
    sub = parser.add_subparsers(dest="command", required=True)

    p_post = sub.add_parser("post", help="post the product-findings review for a judged run")
    p_post.add_argument("run_id")
    p_post.add_argument("--spec", help="unified spec YAML (default: <git-toplevel>/docs/walkthroughs/<narrative_slug>.yaml)")
    p_post.add_argument("--deck-url", help="hosted deck URL (default: run_state.iteration_decks[iteration])")
    p_post.add_argument("--clip-url", help="hosted clip URL (default: run_state.iteration_clips[iteration])")
    p_post.set_defaults(func=_cmd_post)

    p_apply = sub.add_parser("apply", help="parse a resolved response_json into a machine-readable selection")
    p_apply.add_argument("response_json_file")
    p_apply.set_defaults(func=_cmd_apply)

    p_mode = sub.add_parser("mode", help="print the spec's effective review mode (autonomous | human)")
    p_mode.add_argument("spec_path")
    p_mode.set_defaults(func=_cmd_mode)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
