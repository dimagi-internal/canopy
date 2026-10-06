"""Per-scene judged REGIONS — what a scene is about, captured at render time.

Why this exists (canopy#780)
----------------------------
DDD's scoped judging re-judges a scene only when an input its judges read
changed. Its fingerprint used to be the whole frame, byte for byte, so any edit
to a page template a scene sits on — a legend restyle, a footer line — re-judged
every scene on that template, even when nothing the scene narrates moved. Over
about 40 judged passes on four ACE Spark runs, it reused zero scenes.

A scene is ABOUT a few elements: the ones its actions click, hover and wait for,
and the ones its narration names ("MEETING REGULARITY reads 66.0%", "Ulemu
Chirwa"). This module writes ``scene_<N>_regions.json`` beside the frame, at the
same steady state:

    {"render_id", "full_page", "dpr", "scroll": [x, y],
     "regions": [{"key", "source", "found", "matches": [
         {"tag", "text", "attrs", "box": [x, y, w, h]}]}]}

``box`` is in FRAME pixels (document coordinates for a full-page capture,
viewport coordinates otherwise, times the device pixel ratio), so the judge
scope can crop the region out of the frame. ``scripts.ddd.impact`` fingerprints
each region's DOM text and its crop; the frame as a whole only acts as a guard.

Best-effort, like the visual capture: a failure here writes no file (the judge
scope then falls back to comparing the whole frame), and never fails a render.
"""
from __future__ import annotations

import re
from typing import Any, Callable

#: Caps that keep one capture cheap on a 16,000px table page.
TERM_LIMIT = 24
MATCHES_PER_TERM = 3
TEXT_CAP = 400
TARGET_TIMEOUT_MS = 400

#: Action kinds whose target is part of what the scene is about. ``goto`` is a
#: navigation, ``hold``/``scroll`` move the camera — none name an element.
_TARGET_KINDS = frozenset(
    {
        "click",
        "click_menu",
        "hover",
        "wait_for",
        "scroll_to",
        "fill",
        "select",
        "type",
        "press",
        "draw",
        "map_click",
        "upload",
        "snapshot",
    }
)

#: Attributes worth fingerprinting: they change what a reader of the element
#: sees or what state it is in. Layout-only attributes are left to the crop.
_ATTRS = (
    "class",
    "role",
    "aria-label",
    "aria-sort",
    "aria-selected",
    "aria-expanded",
    "aria-checked",
    "aria-disabled",
    "title",
    "href",
    "value",
    "data-state",
)

_VAR = re.compile(r"\$\{\w+\}")
_QUOTED = re.compile(r"[\"“”']([^\"“”'\n]{2,60})[\"“”']")
_NUMBER = re.compile(r"(?<![\w.])(?:\d[\d,]*\.\d+%?|\d[\d,]*%|\d{1,3}(?:,\d{3})+|\d{2,})(?![\w])")
_CAPITALISED = re.compile(
    r"\b(?:[A-Z][\w’'\-]+|[A-Z]{2,})(?:\s+(?:[A-Z][\w’'\-]+|[A-Z]{2,}|\d+(?:\.\d+)?%?))+"
)
#: Leading words that make a capitalised run a sentence opener, not a name.
_OPENERS = frozenset({"The", "A", "An", "Its", "Her", "His", "Their", "One", "Each", "Every", "In", "On", "At"})


def _features_text(scene: dict) -> str:
    out = []
    for f in scene.get("features") or []:
        if isinstance(f, dict):
            out.append(str(f.get("verify") or ""))
    return " ".join(out)


def narration_terms(scene: dict, *, limit: int = TERM_LIMIT) -> list[str]:
    """Strings the scene's words name on screen, most specific first.

    Read from ``narrative``, ``show`` and each feature's ``verify`` (the
    narration a judge compares against the frame): quoted strings, numbers that
    carry a unit or precision (``66.0%``, ``1,594``), and capitalised runs of two
    or more words (names, tile labels: ``Ulemu Chirwa``, ``MEETING REGULARITY``),
    and ``${var}`` placeholders (matched by their bound value, keyed in spec form).
    Single bare digits and lone capitalised words are skipped — they match half
    the page and say nothing about where the scene points.
    """
    text = " ".join(
        str(scene.get(k) or "") for k in ("narrative", "show")
    ) + " " + _features_text(scene)
    seen: dict[str, None] = {}

    def add(term: str) -> None:
        t = " ".join(term.split()).strip(" .,;:")
        if len(t) < 2 or t.lower() in {s.lower() for s in seen}:
            return
        seen[t] = None

    for m in _VAR.finditer(text):  # a bound value the narration speaks
        add(m.group(0))
    for m in _QUOTED.finditer(text):
        add(m.group(1))
    for m in _CAPITALISED.finditer(text):
        words = m.group(0).split()
        while words and words[0] in _OPENERS:
            words = words[1:]
        if len(words) >= 2:
            add(" ".join(words))
    for m in _NUMBER.finditer(text):
        add(m.group(0))
    return list(seen)[:limit]


def target_entries(scene: dict) -> list[dict]:
    """``[{key, source, target}]`` for each element the scene's actions touch.

    ``key`` is the spec-form target (``${var}`` unresolved) so the same element
    keys the same region across renders whose seed minted a different id.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for a in scene.get("actions") or []:
        if not isinstance(a, dict):
            continue
        kind = a.get("kind") or ""
        target = a.get("target")
        if kind not in _TARGET_KINDS or not isinstance(target, str) or not target.strip():
            continue
        if target.strip().isdigit():  # wait_for: <ms>
            continue
        key = f"{'wait_for' if kind == 'wait_for' else 'action'}:{target}"
        if key in seen:
            continue
        seen.add(key)
        out.append({"key": key, "source": "wait_for" if kind == "wait_for" else "action", "target": target})
    return out


_ELEMENT_JS = r"""
(el, args) => {
  const [attrs, cap] = args;
  const r = el.getBoundingClientRect();
  const a = {};
  for (const n of attrs) { const v = el.getAttribute(n); if (v !== null) a[n] = v; }
  const t = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, cap);
  return {tag: el.tagName.toLowerCase(), text: t, attrs: a,
          rect: [r.left, r.top, r.width, r.height]};
}
"""

REGIONS_JS = r"""
(args) => {
  const [terms, maxPer, attrs, cap] = args;
  const out = {dpr: window.devicePixelRatio || 1, scroll: [window.pageXOffset || 0, window.pageYOffset || 0],
               viewport: [window.innerWidth, window.innerHeight], terms: []};
  if (!document.body) return out;
  const lowered = terms.map(t => t.toLowerCase());
  const hits = terms.map(() => []);
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    const raw = node.nodeValue;
    if (!raw || !raw.trim()) continue;
    const low = raw.toLowerCase();
    for (let i = 0; i < lowered.length; i++) {
      if (hits[i].length >= maxPer || !low.includes(lowered[i])) continue;
      const el = node.parentElement;
      if (!el) continue;
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) continue;
      const st = getComputedStyle(el);
      if (st.visibility === 'hidden' || st.display === 'none') continue;
      const a = {};
      for (const n of attrs) { const v = el.getAttribute(n); if (v !== null) a[n] = v; }
      hits[i].push({tag: el.tagName.toLowerCase(),
                    text: (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, cap),
                    attrs: a, rect: [r.left, r.top, r.width, r.height]});
    }
  }
  for (let i = 0; i < terms.length; i++) out.terms.push({term: terms[i], matches: hits[i]});
  return out;
}
"""


def _box(rect: list, *, scroll: list, full_page: bool, dpr: float) -> list[int]:
    x, y, w, h = (float(v or 0) for v in (rect + [0, 0, 0, 0])[:4])
    if full_page:
        x += float(scroll[0] or 0)
        y += float(scroll[1] or 0)
    return [int(round(x * dpr)), int(round(y * dpr)), int(round(w * dpr)), int(round(h * dpr))]


def _match(raw: dict, *, scroll: list, full_page: bool, dpr: float) -> dict:
    return {
        "tag": raw.get("tag"),
        "text": raw.get("text") or "",
        "attrs": raw.get("attrs") or {},
        "box": _box(list(raw.get("rect") or []), scroll=scroll, full_page=full_page, dpr=dpr),
    }


def _locator(page: Any, target: str) -> Any:
    """A Playwright locator for a spec target, or ``None`` (never waits)."""
    from scripts.walkthrough._lib.targets import _locator_for_prefix, looks_like_selector, parse_target

    kind, value = parse_target(target)
    if kind != "auto":
        return _locator_for_prefix(page, kind, value)
    if looks_like_selector(value):
        return page.locator(value)
    return page.get_by_text(value)


def capture(
    page: Any,
    scene: dict,
    *,
    full_page: bool,
    resolve: Callable[[str], str] = lambda s: s,
) -> dict | None:
    """Capture the scene's regions from the page already open; ``None`` on failure.

    ``resolve`` turns a spec-form target or term (``${var}``) into the live value
    the page shows; region keys stay in spec form.
    """
    terms = narration_terms(scene)
    try:
        page_info = page.evaluate(
            REGIONS_JS, [[resolve(t) for t in terms], MATCHES_PER_TERM, list(_ATTRS), TEXT_CAP]
        )
    except Exception:  # noqa: BLE001 — a lens input, never a render failure
        return None
    if not isinstance(page_info, dict):
        return None
    dpr = float(page_info.get("dpr") or 1)
    scroll = list(page_info.get("scroll") or [0, 0])
    regions: list[dict] = []
    for entry in target_entries(scene):
        region = {"key": entry["key"], "source": entry["source"], "found": False, "matches": []}
        try:
            loc = _locator(page, resolve(entry["target"]))
            if loc is not None and loc.count() > 0:
                raw = loc.first.evaluate(_ELEMENT_JS, [list(_ATTRS), TEXT_CAP], timeout=TARGET_TIMEOUT_MS)
                if isinstance(raw, dict):
                    region["found"] = True
                    region["matches"] = [_match(raw, scroll=scroll, full_page=full_page, dpr=dpr)]
        except Exception:  # noqa: BLE001 — an unresolvable target is recorded as not found
            pass
        regions.append(region)
    for spec_term, hit in zip(terms, page_info.get("terms") or []):
        matches = [
            _match(m, scroll=scroll, full_page=full_page, dpr=dpr)
            for m in (hit.get("matches") or [])
            if isinstance(m, dict)
        ]
        regions.append(
            {"key": f"term:{spec_term}", "source": "narration", "found": bool(matches), "matches": matches}
        )
    return {
        "full_page": bool(full_page),
        "dpr": dpr,
        "scroll": scroll,
        "viewport": page_info.get("viewport"),
        "regions": regions,
    }


__all__ = ["REGIONS_JS", "capture", "narration_terms", "target_entries"]
