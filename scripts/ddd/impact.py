"""Impact-aware scene fingerprints — re-judge only what COULD have changed a verdict.

The problem, measured (canopy#780)
----------------------------------
Over about 40 judged passes on four ACE Spark runs (2026-10-06), scoped judging
(:mod:`scripts.ddd.judge_scope`) reused zero scenes. Every fix batch edited a
shared report template, so every scene sat on a changed page, and the v2
fingerprint read any change anywhere on that page as a change to the scene:
the frame was compared byte for byte, and the frame is not what a scene is
about. A replay of the archived frames of runs ``-002``/``-004``/``-005`` adds
two facts: 10 of the 56 scene re-judges on scoped passes had a BYTE-IDENTICAL
frame (so page text or spec moved, not pixels), and 11 more were large layout
changes (>20% of the frame) that should re-judge anyway. The other 35 were
localized pixel changes — the population this module scopes to the scene's
subject. The whole page text stays a component: the judges read it and cite
lines below the fold.

The components (v3)
-------------------
Each is compared on its own, and ``changed`` names the ones that moved:

``context`` / ``spec`` / ``trace``
    Unchanged from v2: why-brief + rubric, the scene's spec entry, its action
    trace (spec-form targets).
``page_text``
    Captured page text minus the render stamp, reseeded ids in ``${var}`` form,
    and VOLATILE stamps (clock times, ISO timestamps, "N minutes ago") — text
    that changes on every render without any edit.
``region_dom``
    The DOM text and state attributes of the elements the scene is about: its
    action targets, its ``wait_for`` targets, and the elements its narration
    names (``scene_<N>_regions.json``, written by the recorder).
``region_image``
    Those elements' crops out of the frame, compared pixel by pixel with an
    anti-aliasing tolerance (a pixel counts only past :data:`PIXEL_DELTA` on some
    channel; a crop changes at :data:`MIN_CHANGED_PIXELS` such pixels). A crop
    follows its element, so content that merely MOVED reads as unchanged here —
    which is what the layout guard is for.
``layout``
    The guard: the whole after-frame (and before-frame) as a grid of
    :data:`LAYOUT_CELL`-pixel cell means. A cell changes past
    :data:`LAYOUT_CELL_DELTA`; the frame changes when more than
    :data:`LAYOUT_FRACTION` of its cells did, or its size changed. A large
    layout change re-judges a scene even when its own region looks the same.

Reseeded ids and volatile stamps ON SCREEN (canopy#785)
--------------------------------------------------------
Text comparisons already put reseeded ids back in ``${var}`` form, but pixels
cannot be un-substituted: a region showing "Tender 341" one take and "Tender 342"
the next moved its crop every render, so a state-mutating narrative (supply:
``setup: rerun: per_render``) could never reuse a scene whose subject shows an
id. Each crop's signature therefore carries the hash of its element's RAW text
and of its NORMALISED text (volatile stamps masked, id-named ``${var}`` values
put back). A crop that moved while its raw text changed and its normalised text
did not is EXPLAINED by the reseed and does not count — the element says the
same thing with a different id. Two guards keep this from hiding a real edit:
``region_dom`` (text and state attributes) must be unchanged, and a scene the
last fix batch edited never gets the explanation (``explain=False``), so a
style fix landing on an id-bearing element still re-judges.

With no ``scene_<N>_regions.json`` (an older render, or a capture failure) the
region is the WHOLE frame: ``region_dom`` is empty and ``region_image`` compares
the full frame with the same pixel tolerance. A frame that does not decode
(a stub in a test) falls back to an exact byte hash. A false reuse is worse
than a re-judge, so every fallback errs toward re-judging.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
from pathlib import Path
from typing import Any

#: Per-pixel tolerance: a channel must move by more than this to count. Font
#: anti-aliasing and chart re-rasterisation move channels by a few units; a real
#: text, colour or icon change moves them by tens. Measured on the Spark run
#: frames: re-render noise peaked at 12 (8px cell means), a one-word bold change
#: produced 1,730 pixels past 40.
PIXEL_DELTA = 40
#: A crop changes when at least this many pixels moved past PIXEL_DELTA. Isolated
#: anti-aliasing flips stay below it; the smallest real change measured (a chart
#: axis label clipped vs shown) moved 423.
MIN_CHANGED_PIXELS = 16
#: Layout guard grid and thresholds.
LAYOUT_CELL = 16
LAYOUT_CELL_DELTA = 16.0
LAYOUT_FRACTION = 0.20
#: Pad around each region box, in frame pixels, so a border or badge drawn just
#: outside the element's box still belongs to it.
REGION_PAD = 6
#: A match covering more than this share of the frame is the page, not a subject
#: (``css:*:has-text("…")`` resolves to ``<html>`` first): its crop would make
#: every edit anywhere a change to the scene. The layout guard covers the page.
PAGE_SIZED = 0.5
_PAGE_TAGS = frozenset({"html", "body"})

COMPONENTS = ("context", "layout", "page_text", "region_dom", "region_image", "spec", "trace")

_VOLATILE = [
    re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?\b"),
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s?(?:[AaPp]\.?[Mm]\.?)?(?=\W|$)"),
    re.compile(
        r"\b(?:\d+|an?|a few)\s+(?:seconds?|secs?|minutes?|mins?|hours?|hrs?)\s+ago\b",
        re.I,
    ),
    re.compile(r"\bjust now\b", re.I),
]


def scrub_volatile(text: str) -> str:
    """``text`` with per-render stamps (times, timestamps, "3 minutes ago") masked."""
    out = text
    for pat in _VOLATILE:
        out = pat.sub("<t>", out)
    return out


def _scrub_deep(obj: Any) -> Any:
    if isinstance(obj, str):
        return scrub_volatile(obj)
    if isinstance(obj, list):
        return [_scrub_deep(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _scrub_deep(v) for k, v in obj.items()}
    return obj


def _sha(obj: Any) -> str:
    raw = obj if isinstance(obj, bytes) else json.dumps(
        obj, sort_keys=True, default=str, separators=(",", ":")
    ).encode()
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


def _load_rgb(path: Path | None):
    """The frame as an ``(h, w, 3)`` int16 array, or ``None`` if it won't decode."""
    if path is None or not path.exists():
        return None
    try:
        import numpy as np
        from PIL import Image

        with Image.open(path) as im:
            return np.asarray(im.convert("RGB"), dtype=np.int16)
    except Exception:  # noqa: BLE001 — a stub or truncated frame: exact-hash fallback
        return None


def _png_b64(arr) -> str:
    import numpy as np
    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(np.asarray(arr, dtype=np.uint8)).save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


def _from_b64(raw: str):
    import numpy as np
    from PIL import Image

    with Image.open(io.BytesIO(base64.b64decode(raw))) as im:
        return np.asarray(im.convert("RGB"), dtype=np.int16)


def cell_grid(arr, cell: int = LAYOUT_CELL):
    """Mean colour of each ``cell`` x ``cell`` block (edge remainder dropped)."""
    import numpy as np

    h, w, _ = arr.shape
    gh, gw = max(1, h // cell), max(1, w // cell)
    a = arr[: gh * cell, : gw * cell].astype(np.float32)
    if a.shape[0] < cell or a.shape[1] < cell:
        return a.reshape(1, 1, -1, 3).mean(axis=2).astype(np.uint8)
    return a.reshape(gh, cell, gw, cell, 3).mean(axis=(1, 3)).round().astype(np.uint8)


def layout_change(prior: dict | None, current: dict | None) -> dict:
    """``{changed, fraction, reason}`` between two layout signatures."""
    import numpy as np

    if not prior or not current:
        return {"changed": True, "fraction": None, "reason": "no prior layout signature"}
    if prior.get("size") != current.get("size"):
        return {
            "changed": True,
            "fraction": 1.0,
            "reason": f"frame size {prior.get('size')} -> {current.get('size')}",
        }
    a, b = _from_b64(prior["grid"]), _from_b64(current["grid"])
    if a.shape != b.shape:
        return {"changed": True, "fraction": 1.0, "reason": "layout grid shape changed"}
    diff = np.abs(a - b).max(axis=2) > LAYOUT_CELL_DELTA
    frac = float(diff.mean())
    return {
        "changed": frac > LAYOUT_FRACTION,
        "fraction": round(frac, 4),
        "reason": f"{frac:.0%} of {LAYOUT_CELL}px cells moved (guard at {LAYOUT_FRACTION:.0%})",
    }


def pixel_change(prior_b64: str | None, current_b64: str | None) -> dict:
    """``{changed, changed_px}`` between two crops, tolerant of anti-aliasing."""
    import numpy as np

    if prior_b64 is None or current_b64 is None:
        return {"changed": prior_b64 != current_b64, "changed_px": None}
    a, b = _from_b64(prior_b64), _from_b64(current_b64)
    if a.shape != b.shape:
        return {"changed": True, "changed_px": None, "reason": f"size {a.shape[:2]} -> {b.shape[:2]}"}
    n = int((np.abs(a - b).max(axis=2) > PIXEL_DELTA).sum())
    return {"changed": n >= MIN_CHANGED_PIXELS, "changed_px": n}


def _crop(arr, box: list, pad: int = REGION_PAD):
    h, w, _ = arr.shape
    x, y, bw, bh = (int(v) for v in box)
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(w, x + bw + pad), min(h, y + bh + pad)
    if x1 <= x0 or y1 <= y0:
        return None
    return arr[y0:y1, x0:x1]


# ---------------------------------------------------------------------------
# Per-scene capture -> components + signatures
# ---------------------------------------------------------------------------


def load_regions(path: Path | None) -> dict | None:
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("regions"), list) else None


def region_dom_payload(regions: dict | None, variables: dict[str, str] | None = None) -> Any:
    """What the scene's elements SAY and what state they are in — no geometry."""
    if regions is None:
        return None
    rows = []
    for r in regions.get("regions") or []:
        if not isinstance(r, dict):
            continue
        rows.append(
            {
                "key": r.get("key"),
                "found": bool(r.get("found")),
                "matches": [
                    {"tag": m.get("tag"), "text": m.get("text"), "attrs": m.get("attrs") or {}}
                    for m in r.get("matches") or []
                    if isinstance(m, dict) and m.get("tag") not in _PAGE_TAGS
                ],
            }
        )
    rows.sort(key=lambda r: str(r["key"]))
    payload: Any = _scrub_deep(rows)
    if variables:
        from scripts.ddd.stable_ids import id_vars, unsubstitute_deep

        payload = unsubstitute_deep(payload, id_vars(variables))
    return payload


def _cropped_matches(frame, regions: dict) -> dict[str, list[tuple[Any, dict]]]:
    """``{region key: [(crop array, match), ...]}`` for the matches that get a crop."""
    out: dict[str, list[tuple[Any, dict]]] = {}
    area = float(frame.shape[0] * frame.shape[1]) or 1.0
    for r in regions.get("regions") or []:
        if not isinstance(r, dict):
            continue
        kept = []
        for m in r.get("matches") or []:
            box = m.get("box") if isinstance(m, dict) else None
            if not isinstance(box, list) or len(box) != 4 or m.get("tag") in _PAGE_TAGS:
                continue
            if float(box[2]) * float(box[3]) > PAGE_SIZED * area:
                continue
            c = _crop(frame, box)
            if c is not None:
                kept.append((c, m))
        out[str(r.get("key"))] = kept
    return out


def region_crops(frame, regions: dict | None) -> dict[str, list[str]] | None:
    """``{region key: [crop png b64, ...]}``; the whole frame when no regions."""
    if frame is None:
        return None
    if regions is None:
        return {"<frame>": [_png_b64(frame)]}
    return {k: [_png_b64(c) for c, _ in v] for k, v in _cropped_matches(frame, regions).items()}


def region_texts(
    frame, regions: dict | None, variables: dict[str, str] | None = None
) -> dict[str, list[dict[str, str]]] | None:
    """``{region key: [{raw, norm}, ...]}`` aligned with :func:`region_crops`.

    ``raw`` hashes the element's text as captured; ``norm`` hashes it with
    volatile stamps masked and id-named ``${var}`` values put back. Equal
    ``norm`` with different ``raw`` = the same words with a reseeded id or a new
    clock time — which is what explains a crop that moved.
    """
    if frame is None or regions is None:
        return None
    ids: dict[str, str] = {}
    if variables:
        from scripts.ddd.stable_ids import id_vars

        ids = id_vars(variables)
    out: dict[str, list[dict[str, str]]] = {}
    for key, kept in _cropped_matches(frame, regions).items():
        rows = []
        for _, m in kept:
            raw = str(m.get("text") or "")
            norm = scrub_volatile(raw)
            if ids:
                from scripts.ddd.stable_ids import unsubstitute

                norm = unsubstitute(norm, ids)
            rows.append({"raw": _sha(raw), "norm": _sha(norm)})
        out[key] = rows
    return out


def layout_signature(frame) -> dict | None:
    if frame is None:
        return None
    h, w, _ = frame.shape
    return {"size": [int(w), int(h)], "grid": _png_b64(cell_grid(frame))}


def scene_capture(
    after: Path | None, before: Path | None, regions_path: Path | None,
    variables: dict[str, str] | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """``(components, signatures)`` for the image + region inputs of one scene.

    ``components`` holds ``region_dom``, ``region_image`` and ``layout`` hashes;
    ``signatures`` holds what the tolerant comparisons need (crops, layout grids).
    """
    regions = load_regions(regions_path)
    frame = _load_rgb(after)
    before_frame = _load_rgb(before)
    comps: dict[str, str] = {"region_dom": _sha(region_dom_payload(regions, variables))}
    sigs: dict[str, Any] = {"regions_captured": regions is not None}
    if frame is None:
        # Not decodable: exact bytes, exactly as v2 compared frames.
        raw = b"".join(
            (p.name.encode() + (p.read_bytes() if p.exists() else b"<absent>"))
            for p in (x for x in (after, before) if x is not None)
        )
        comps["region_image"] = _sha(raw)
        comps["layout"] = _sha(raw)
        sigs["exact"] = True
        return comps, sigs
    crops = region_crops(frame, regions)
    sigs["crops"] = crops
    texts = region_texts(frame, regions, variables)
    if texts is not None:
        sigs["texts"] = texts
    sigs["layout"] = {
        "after": layout_signature(frame),
        "before": layout_signature(before_frame) if before_frame is not None else None,
    }
    comps["region_image"] = _sha(crops)
    comps["layout"] = _sha(sigs["layout"])
    return comps, sigs


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

#: Components compared by hash equality. The image components are compared
#: tolerantly from their signatures when their hashes differ.
EXACT = ("context", "page_text", "region_dom", "spec", "trace")


def _explained(sp: dict, sn: dict, key: str, i: int) -> bool:
    """True when crop ``i`` of region ``key`` shows the same words with a reseeded
    id or a new volatile stamp: its raw text changed and its normalised text did not."""
    tp = ((sp.get("texts") or {}).get(key) or [])
    tn = ((sn.get("texts") or {}).get(key) or [])
    if i >= len(tp) or i >= len(tn):
        return False
    a, b = tp[i], tn[i]
    return bool(a.get("raw") != b.get("raw") and a.get("norm") == b.get("norm"))


def compare_scene(
    current: dict[str, str],
    prior: dict[str, str] | None,
    sig_now: dict | None,
    sig_prior: dict | None,
    *,
    explain: bool = True,
) -> tuple[list[str], dict[str, Any]]:
    """``(changed components, detail)`` for one scene against its ledger entry.

    ``explain`` lets a crop change that its element's text explains (a reseeded
    id, a clock stamp — see the module doc) count as unchanged. Pass ``False``
    for a scene the last fix batch edited."""
    if not isinstance(prior, dict):
        return sorted(current), {"reason": "no ledger entry for this scene"}
    changed: list[str] = []
    detail: dict[str, Any] = {}
    for comp in sorted(current):
        if comp not in EXACT and comp not in ("region_image", "layout"):
            if prior.get(comp) != current[comp]:
                changed.append(comp)
            continue
        if prior.get(comp) == current[comp]:
            continue
        if comp in EXACT:
            changed.append(comp)
            continue
        sn, sp = sig_now or {}, sig_prior or {}
        if sn.get("exact") or sp.get("exact") or comp not in ("region_image", "layout"):
            changed.append(comp)
            continue
        if comp == "layout":
            res = {
                k: layout_change((sp.get("layout") or {}).get(k), (sn.get("layout") or {}).get(k))
                for k in ("after", "before")
                if (sp.get("layout") or {}).get(k) or (sn.get("layout") or {}).get(k)
            }
            detail["layout"] = res
            if any(r["changed"] for r in res.values()):
                changed.append("layout")
            continue
        # region_image: every region's crops, tolerant per crop.
        cp, cn = sp.get("crops"), sn.get("crops")
        if not isinstance(cp, dict) or not isinstance(cn, dict) or set(cp) != set(cn):
            detail["region_image"] = {"reason": "region set changed"}
            changed.append("region_image")
            continue
        # The explanation is only as good as the DOM text beside it: when the
        # region's words or state attributes moved, nothing is explained.
        can_explain = explain and prior.get("region_dom") == current.get("region_dom")
        moved: dict[str, Any] = {}
        explained: dict[str, Any] = {}
        for key in sorted(cn):
            a, b = cp[key], cn[key]
            if len(a) != len(b):
                moved[key] = {"reason": f"{len(a)} -> {len(b)} matches"}
                continue
            for i, (x, y) in enumerate(zip(a, b)):
                if x == y:
                    continue
                res = pixel_change(x, y)
                if not res["changed"]:
                    continue
                if can_explain and _explained(sp, sn, key, i):
                    explained[key] = {**res, "explained_by": "reseeded id / volatile stamp in the element text"}
                    continue
                moved[key] = res
                break
        if explained:
            detail["region_image_explained"] = explained
        if moved:
            detail["region_image"] = moved
            changed.append("region_image")
    return changed, detail


__all__ = [
    "COMPONENTS",
    "LAYOUT_FRACTION",
    "MIN_CHANGED_PIXELS",
    "PIXEL_DELTA",
    "compare_scene",
    "layout_change",
    "pixel_change",
    "region_dom_payload",
    "region_texts",
    "scene_capture",
    "scrub_volatile",
]
