"""Interactive recording primitives for DDD walkthrough videos.

The DDD video recorder used to only ``page.goto(url)`` + scroll-pan each scene —
so a rendered demo showed *pages*, never the product being *used*. A demo where
nothing is clicked scores ~1/5 on "demonstrates using the features" no matter how
good the pages look.

This module gives the recorder a synthetic cursor and a small interaction
vocabulary so a scene can declare what the persona *does* (click, fill, open a
menu, dwell, scroll-to) and the recording shows it happening, cursor and all.

Three halves:

1. **Cursor overlay** — :data:`CURSOR_OVERLAY_JS` injects an SVG cursor that
   follows ``mousemove`` and draws a ripple on ``mousedown`` (headless Chromium
   draws no OS cursor). Inject it via ``context.add_init_script(CURSOR_OVERLAY_JS)``
   so it survives navigations.

2. **Primitives** — :func:`click_text`, :func:`click_menu_item`, :func:`fill_field`,
   :func:`select_option`, :func:`scroll_to`, :func:`scroll_page`, :func:`wait_for`,
   :func:`hover`. Each glides the cursor onto its target before acting and
   returns an :class:`ActionResult` so failures stop hiding. All target
   resolution flows through :mod:`._lib.targets` — one resolver to rule them
   all, instead of a different selector ladder per primitive. All timing flows
   through :class:`RecorderConfig` — one place to tune pace, instead of magic
   numbers scattered across primitives.

3. **Dispatcher** — :func:`execute_action` turns one declarative ``Action`` (from
   a scene's ``actions`` list) into the right primitive call. Unknown verbs are
   reported (``error_kind="unknown_kind"``) but never fatal.

Pure helpers over a Playwright ``Page`` — no labs/canopy coupling, so any
walkthrough can import them.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import Page

from .config import RecorderConfig
from .results import ActionAssertError, ActionResult
from .targets import measure_box, resolve_target, wait_for_target

# ACTION_KINDS lives in the neutral narrative substrate (single source of
# truth — keeps the Pydantic Literal and the dispatcher vocabulary in sync). A
# renderer must not depend on a methodology, so this imports from
# scripts.narrative, not scripts.ddd. Imported lazily to avoid a hard
# dependency on pydantic when the recorder is used standalone.
try:
    from scripts.narrative.models import ACTION_KINDS  # noqa: F401
except Exception:  # pragma: no cover — recorder may run without the narrative package on PYTHONPATH
    ACTION_KINDS = (
        "goto", "click", "click_menu", "fill", "select", "type", "press",
        "hover", "scroll_to", "scroll", "wait_for", "hold", "draw", "map_click", "map_zoom", "capture",
        "snapshot",
    )

CURSOR_OVERLAY_JS = (Path(__file__).resolve().parent / "cursor_overlay.js").read_text()


# --------------------------------------------------------------------------- #
# cursor motion
# --------------------------------------------------------------------------- #


def slow_move(page: Page, x: float, y: float, *, steps: int = 36) -> None:
    """Mouse move with enough steps that the cursor overlay animates the glide.

    Deliberately slow — a cursor that teleports reads as a jump-cut; a cursor that
    visibly travels to its target reads as a person operating the page.
    """
    page.mouse.move(x, y, steps=steps)


def _glide_to(page: Page, target: str, *, config: RecorderConfig, dwell_ms: int | None = None):
    """Resolve ``target``, glide the cursor to its centre, dwell, return the ResolvedTarget.

    Returns ``None`` if the target didn't resolve in :attr:`config.glide_timeout_ms`.
    The caller decides what that means (a failed click logs + returns False;
    a failed hover is a no-op).
    """
    rt = resolve_target(page, target, timeout_ms=config.glide_timeout_ms)
    if rt is None:
        return None
    slow_move(page, rt.box["x"], rt.box["y"], steps=config.cursor_steps)
    page.wait_for_timeout(dwell_ms if dwell_ms is not None else config.glide_dwell_ms)
    return rt


# --------------------------------------------------------------------------- #
# interaction primitives
# --------------------------------------------------------------------------- #


def click_text(page: Page, target: str, *, config: RecorderConfig | None = None) -> bool:
    """Glide the cursor onto ``target`` and click via ``Locator.click``.

    The cursor glide is visual: ``slow_move`` chunks the move so the overlay
    animates, the pre-click dwell gives the viewer time to register WHERE
    the click is about to land, and the overlay's click feedback (press-pulse
    + ring + lingering dot) fires from the real ``mousedown``.

    The click itself goes through ``Locator.click()`` — actionability checks
    intact (visible, stable, receives events, enabled, not detached). This
    matters: a video where the click silently misses an obscured element
    used to look identical to a successful click; with ``Locator.click()``
    the timeout fires loudly and the failure shows up in the run report.
    """
    cfg = config or RecorderConfig()
    rt = _glide_to(page, target, config=cfg, dwell_ms=cfg.click_dwell_ms)
    if rt is None:
        print(f"  ! click target not found: {target!r}")
        return False
    # Re-measure right before the click in case a settle moved the element
    # mid-glide; the cursor lands on its current centre, not where it was.
    box = measure_box(rt.locator) or rt.box
    slow_move(page, box["x"], box["y"], steps=cfg.cursor_steps_short)
    page.wait_for_timeout(cfg.pre_click_dwell_ms)
    try:
        rt.locator.click(timeout=cfg.interaction_timeout_ms)
    except Exception as e:
        print(f"  ! click failed (actionability): {target!r}: {e}")
        return False
    page.wait_for_timeout(cfg.post_click_settle_ms)
    return True


def click_menu_item(page: Page, item_text: str, *, config: RecorderConfig | None = None) -> bool:
    """Click an item inside an open dropdown / popover.

    Same resolver as :func:`click_text`, shorter post-click settle (menus
    react faster than top-level buttons). The verb is kept distinct so
    spec authors signal "this click closes a menu" — useful for graders.
    """
    cfg = config or RecorderConfig()
    rt = _glide_to(page, item_text, config=cfg)
    if rt is None:
        print(f"  ! menu item not found: {item_text!r}")
        return False
    try:
        rt.locator.click(timeout=cfg.menu_timeout_ms)
    except Exception as e:
        print(f"  ! menu click failed: {item_text!r}: {e}")
        return False
    page.wait_for_timeout(cfg.menu_click_settle_ms)
    return True


def fill_field(page: Page, target: str, value: str, *, config: RecorderConfig | None = None) -> bool:
    """Glide to an input, click to focus, clear, then type character-by-character.

    Uses ``Locator.click()`` + ``Locator.fill("")`` + ``Locator.type(...)`` —
    typing fires real ``input`` events (reactive form widgets that gate
    buttons on debounced input depend on this; a raw ``element.value =``
    setter wouldn't trigger them).
    """
    cfg = config or RecorderConfig()
    rt = _glide_to(page, target, config=cfg, dwell_ms=cfg.pre_fill_dwell_ms)
    if rt is None:
        print(f"  ! fill target not found: {target!r}")
        return False
    try:
        rt.locator.click(timeout=cfg.interaction_timeout_ms)
        input_type = (rt.locator.get_attribute("type") or "").strip().lower()
        if input_type in ("date", "datetime-local", "time", "month", "week"):
            # A native date/time input is a SEGMENTED control: per-character
            # typing lands digit-by-digit in whatever segment has focus and
            # the browser reinterprets the stream — a typed "2026-08-18"
            # rendered as 02/02/60818 on camera, the server 400ed the year
            # 60818, and the modal that should have closed swallowed the rest
            # of the scene. Locator.fill sets the value atomically and still
            # fires input/change. Nothing is lost visually: segmented typing
            # never read as typing anyway.
            rt.locator.fill(value)
        else:
            rt.locator.fill("")
            rt.locator.type(value, delay=cfg.typing_delay_ms)
    except Exception as e:
        print(f"  ! fill failed: {target!r}: {e}")
        return False
    page.wait_for_timeout(cfg.post_fill_settle_ms)
    return True


def select_option(page: Page, target: str, value: str, *, config: RecorderConfig | None = None) -> bool:
    """Pick an option from a native ``<select>``.

    Native HTML selects can't be reliably opened by clicking — across
    platforms, the dropdown rendering is OS-controlled. ``Locator.select_option``
    is the canonical way: it fires the right ``change`` events without
    needing the visual open. The cursor still glides onto the select so the
    viewer sees which control is changing; the closed widget flips to the
    new value.

    ``value`` is interpreted as the option's ``value`` attribute first, then
    a digit-only string as the 0-based ``index``, then the visible text
    ``label``. Returns False if none match.
    """
    cfg = config or RecorderConfig()
    rt = _glide_to(page, target, config=cfg, dwell_ms=cfg.pre_select_dwell_ms)
    if rt is None:
        print(f"  ! select target not found: {target!r}")
        return False
    # Render a synthetic dropdown so the VIEWER sees the options + which one is
    # picked. Native OS select popups can't be screen-recorded, so without this
    # the closed widget just silently flips value (the user can't tell a choice
    # was made). The cursor then glides down onto the chosen option.
    reveal_open = _reveal_select(page, rt, value, cfg)
    val = str(value)
    attempts: list[dict] = [{"value": val}]
    if val.lstrip("-").isdigit():
        attempts.append({"index": int(val)})
    attempts.append({"label": val})
    ok = False
    # ``.first`` keeps a multi-match target (e.g. one selector matching several
    # rows' <select>s) from throwing strict-mode — commit to the first match,
    # the same one the cursor glided to.
    sel_loc = rt.locator.first
    for attempt in attempts:
        try:
            sel_loc.select_option(**attempt, timeout=cfg.interaction_timeout_ms)
            ok = True
            break
        except Exception:
            continue
    if reveal_open:
        _close_select(page)
    if ok:
        page.wait_for_timeout(cfg.post_select_settle_ms)
        return True
    print(f"  ! select_option failed: {target!r} value={value!r}")
    return False


# Synthetic dropdown: build a styled options list over a native <select>, glide
# the cursor onto the chosen option, hold, then let the caller commit + close.
_SELECT_REVEAL_JS = r"""
(sel, value) => {
  try {
    if (!sel || sel.tagName !== 'SELECT') return null;
    const r = sel.getBoundingClientRect();
    const opts = Array.from(sel.options).map((o) => ({ value: o.value, text: (o.textContent || '').trim() }));
    if (!opts.length) return null;
    let ci = opts.findIndex((o) => o.value === String(value));
    if (ci < 0 && /^-?\d+$/.test(String(value))) ci = parseInt(value, 10);
    if (ci < 0) ci = opts.findIndex((o) => o.text === String(value));
    if (ci < 0) ci = sel.selectedIndex < 0 ? 0 : sel.selectedIndex;
    const W = Math.max(r.width, 180);
    const wrap = document.createElement('div');
    wrap.setAttribute('data-wt-select', '1');
    wrap.style.cssText = 'position:fixed;left:' + Math.round(r.left) + 'px;top:' + Math.round(r.bottom + 4)
      + 'px;width:' + Math.round(W) + 'px;z-index:2147483646;background:#fff;border:1px solid #d1d5db;'
      + 'border-radius:8px;box-shadow:0 12px 28px rgba(0,0,0,.20);overflow:hidden;'
      + "font:13px -apple-system,'Segoe UI',Roboto,sans-serif;color:#111827";
    opts.forEach((o, i) => {
      const on = i === ci;
      const row = document.createElement('div');
      row.style.cssText = 'padding:8px 12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;'
        + (i ? 'border-top:1px solid #f3f4f6;' : '') + (on ? 'background:#eff6ff;color:#1d4ed8;font-weight:600;' : '');
      row.textContent = (on ? '✓ ' : '   ') + o.text;
      wrap.appendChild(row);
    });
    document.body.appendChild(wrap);
    const rows = wrap.children;
    let mid = { x: Math.round(r.left + W / 2), y: Math.round(r.bottom + 18) };
    if (rows[ci]) {
      const rr = rows[ci].getBoundingClientRect();
      mid = { x: Math.round(rr.left + Math.min(rr.width / 2, 90)), y: Math.round(rr.top + rr.height / 2) };
    }
    return mid;
  } catch (e) { return null; }
}
"""


def _reveal_select(page: Page, rt, value, cfg: RecorderConfig) -> bool:
    """Open a synthetic dropdown over the native ``<select>`` and glide the
    cursor onto the chosen option. Returns True if the overlay was shown (so
    the caller knows to close it after committing). Best-effort — never blocks
    the actual selection."""
    if not getattr(cfg, "select_reveal", True):
        return False
    try:
        mid = rt.locator.first.evaluate(_SELECT_REVEAL_JS, str(value))
    except Exception:
        return False
    if not mid:
        return False
    try:
        slow_move(page, float(mid["x"]), float(mid["y"]), steps=cfg.cursor_steps)
        page.wait_for_timeout(int(getattr(cfg, "select_reveal_dwell_ms", 700)))
    except Exception:
        pass
    return True


def _close_select(page: Page) -> None:
    try:
        page.evaluate("() => document.querySelectorAll('[data-wt-select]').forEach((e) => e.remove())")
    except Exception:
        pass


def hover(page: Page, target: str, *, seconds: float | None = None, config: RecorderConfig | None = None) -> bool:
    """Glide the cursor onto ``target`` and rest. No click.

    Uses ``Locator.hover()`` to fire real ``mouseenter`` / ``mouseover``
    events — tooltips and hover-revealed controls depend on those. The
    cursor glide is purely visual; the hover semantics come from Playwright.
    """
    cfg = config or RecorderConfig()
    dwell_ms = int(seconds * 1000) if seconds is not None else cfg.glide_dwell_ms
    rt = _glide_to(page, target, config=cfg, dwell_ms=dwell_ms)
    if rt is None:
        return False
    try:
        rt.locator.hover(timeout=cfg.interaction_timeout_ms)
    except Exception:
        # Tooltip-only hovers are not always actionable in Playwright's strict
        # sense; the cursor already landed on the spot, so don't fail loud.
        pass
    return True


def scroll_to(page: Page, target: str, *, config: RecorderConfig | None = None) -> bool:
    """Smooth-scroll the element matching ``target`` into view, with the cursor.

    Resolves via the unified locator engine (same syntax as every other
    primitive). For the actual scroll: ``Locator.scroll_into_view_if_needed``
    guarantees the element ends up visible (handles nested scroll containers,
    fixed headers, sticky elements — all the cases a window-level
    ``scrollTo`` misses). We chase it with a brief in-page smooth-scroll
    nudge so the motion is visible in the recording — the locator's
    instant scroll alone reads as a teleport.

    The cursor glides onto the target BEFORE the scroll, mirroring the
    click_text / fill_field pattern (resolve → glide → act). When the
    target is already in view (the "no-op scroll" case — common because
    spec authors ``scroll_to`` defensively), the page may not move at all
    but the cursor still visibly arrives on what's about to be clicked,
    so the viewer never sees a frozen ``scroll_settle_ms`` of nothing.
    After the smooth-scroll, we re-measure the locator (its viewport
    position changed) and glide the cursor again with ``cursor_steps_short``
    so it follows the element to its new spot — the same re-measure pattern
    ``click_text`` uses to land on a settled element.
    """
    cfg = config or RecorderConfig()
    rt = resolve_target(page, target, timeout_ms=cfg.glide_timeout_ms)
    if rt is None:
        return False
    # Pre-scroll glide — cursor lands on the target at its current viewport
    # position. Same shape every other primitive uses; keeps "boring" frames
    # from accumulating when ``scroll_to`` is a no-op.
    slow_move(page, rt.box["x"], rt.box["y"], steps=cfg.cursor_steps)
    try:
        rt.locator.scroll_into_view_if_needed(timeout=cfg.glide_timeout_ms)
    except Exception:
        # If actionability strictness blocks the scroll, the smooth-scroll
        # nudge below still tries; coordinate-based scroll is harmless.
        pass
    # Re-measure AFTER scroll_into_view_if_needed. That call may have scrolled
    # the page, changing the element's viewport-relative y. The smooth nudge
    # below combines this y with the *live* window.scrollY, so both must be read
    # at the same scroll position — pairing the pre-scroll rt.box.y with the
    # post-scroll window.scrollY computed a wrong target and left the element
    # off-screen on tall pages (e.g. a back-check far below the fold).
    scrolled_box = measure_box(rt.locator) or rt.box
    page.evaluate(
        """([x, y]) => window.scrollTo({top: y + window.scrollY - window.innerHeight / 2, behavior: 'smooth'})""",
        [scrolled_box["x"], scrolled_box["y"]],
    )
    # The smooth-scroll moved the element under our cursor. Re-measure +
    # short glide so the cursor follows it to its new viewport position —
    # this is the analogue of click_text's re-measure-then-click, applied
    # to "cursor must end on the scrolled-to element".
    new_box = measure_box(rt.locator)
    if new_box is not None:
        slow_move(page, new_box["x"], new_box["y"], steps=cfg.cursor_steps_short)
    page.wait_for_timeout(cfg.scroll_settle_ms)
    return True


def draw_polygon(
    page: Page,
    target: str,
    points: list,
    *,
    tool: str | None = None,
    config: RecorderConfig | None = None,
) -> bool:
    """Draw a polygon on a map/canvas by clicking a sequence of fractional points.

    The other primitives resolve a DOM element and click its centre. Map drawing
    (Mapbox GL Draw, or any canvas tool) needs clicks at *coordinates on the canvas*,
    which no labelled-element verb can express — this is the gap ``draw`` fills.

    ``target`` resolves to the map/canvas element; ``points`` is a list of
    ``[fx, fy]`` fractions (0-1) within that element's bounding box. The synthetic
    cursor glides to each vertex and clicks (real Playwright pointer events the
    drawing tool receives, unlike JS-synthetic events), then double-clicks the last
    vertex to close the polygon (Mapbox finishes a polygon on a double-click). The
    drawing tool must already be active — click its toolbar button in a prior
    ``click`` action.

    Returns False if the element doesn't resolve, has no box, or ``points`` is empty.
    """
    cfg = config or RecorderConfig()
    # Activate the drawing tool first if asked. A normal Locator.click on a small map
    # control (e.g. Mapbox's polygon button) fails Playwright's actionability checks;
    # a coordinate mouse-click on its centre activates it reliably.
    if tool:
        trt = resolve_target(page, tool, timeout_ms=cfg.glide_timeout_ms)
        if trt is not None:
            tbox = trt.locator.bounding_box()
            if tbox:
                # Glide the visible cursor onto the tool so the video shows the reach...
                slow_move(page, tbox["x"] + tbox["width"] / 2, tbox["y"] + tbox["height"] / 2, steps=cfg.cursor_steps)
                page.wait_for_timeout(cfg.glide_dwell_ms)
            # ...but TOGGLE via the element's own click() handler. Mapbox-GL-Draw tool
            # buttons don't enter draw mode on a synthetic mouse/Locator click (the
            # mode stays simple_select); el.click() fires the handler that does.
            try:
                trt.locator.evaluate("el => el.click()")
            except Exception:
                pass
            page.wait_for_timeout(cfg.glide_dwell_ms)
    rt = resolve_target(page, target, timeout_ms=cfg.glide_timeout_ms)
    if rt is None:
        return False
    box = rt.locator.bounding_box()
    if not box or not points:
        return False
    coords = [
        (box["x"] + float(fx) * box["width"], box["y"] + float(fy) * box["height"])
        for fx, fy in points
    ]
    for x, y in coords:
        slow_move(page, x, y, steps=cfg.cursor_steps)
        page.wait_for_timeout(cfg.glide_dwell_ms)
        page.mouse.click(x, y)
    # Close the polygon — Mapbox GL Draw finishes on a double-click at the last vertex.
    page.mouse.dblclick(*coords[-1])
    page.wait_for_timeout(cfg.glide_dwell_ms)
    return True


# --------------------------------------------------------------------------- #
# map feature click (named Mapbox polygon → real on-canvas click)
# --------------------------------------------------------------------------- #

# Resolve the Mapbox map in the page. The microplans editor exposes it at
# ``window.__review.map``; a generic fallback walks ``window`` for any object that
# quacks like a Mapbox GL map (has ``queryRenderedFeatures`` + ``project``). Kept
# as a JS string so ``map_click`` and any future map verb share ONE resolver.
_FIND_MAP_JS = """() => {
  const looksLikeMap = (m) => m
    && typeof m.queryRenderedFeatures === 'function'
    && typeof m.project === 'function'
    && typeof m.getCanvas === 'function';
  try { if (window.__review && looksLikeMap(window.__review.map)) return window.__review.map; } catch (e) {}
  try { if (looksLikeMap(window.map)) return window.map; } catch (e) {}
  for (const k of Object.keys(window)) {
    try { const v = window[k]; if (looksLikeMap(v)) return v; } catch (e) {}
  }
  return null;
}"""


def _ring_centroid(ring: list) -> tuple[float, float]:
    """Area-weighted centroid of one polygon ring (``[[lng,lat], ...]``).

    The standard shoelace centroid. Degenerate (zero-area) rings fall back to the
    arithmetic mean of the vertices so a thin/collinear ring still yields a point.
    Pure — unit-tested directly (no browser).
    """
    pts = [(float(x), float(y)) for x, y in ring]
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if not pts:
        raise ValueError("empty ring")
    n = len(pts)
    a = cx = cy = 0.0
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    a *= 0.5
    if abs(a) < 1e-12:  # degenerate ring → arithmetic mean
        return (sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n)
    return (cx / (6 * a), cy / (6 * a))


def _point_in_ring(pt: tuple[float, float], ring: list) -> bool:
    """Ray-casting point-in-polygon test for one ring. Pure — unit-tested."""
    x, y = pt
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = float(ring[i][0]), float(ring[i][1])
        xj, yj = float(ring[j][0]), float(ring[j][1])
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-30) + xi):
            inside = not inside
        j = i
    return inside


def polygon_interior_point(rings: list) -> tuple[float, float]:
    """Return a [lng, lat] point guaranteed to fall INSIDE the polygon's outer ring.

    ``rings`` is GeoJSON Polygon coordinates (``[outer, hole1, ...]``). The
    area-weighted centroid is used when it lands inside the outer ring (and outside
    every hole); a CONCAVE polygon whose centroid falls outside gets a representative
    interior point instead — we scan a small grid over the ring's bbox and pick the
    in-ring sample closest to the centroid. This is the bit a ``map_click`` must get
    right: a click on a point OUTSIDE the polygon hits the wrong ward (or nothing),
    so the helper is isolated and unit-tested without a browser.
    """
    if not rings or not rings[0]:
        raise ValueError("polygon has no outer ring")
    outer = [(float(x), float(y)) for x, y in rings[0]]
    holes = [[(float(x), float(y)) for x, y in r] for r in rings[1:]]

    def good(p: tuple[float, float]) -> bool:
        return _point_in_ring(p, outer) and not any(_point_in_ring(p, h) for h in holes)

    c = _ring_centroid(rings[0])
    if good(c):
        return c
    # Concave / hole case: grid-scan the bbox for the in-ring sample nearest the centroid.
    xs = [p[0] for p in outer]
    ys = [p[1] for p in outer]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    best: tuple[float, float] | None = None
    best_d = float("inf")
    steps = 24
    for i in range(1, steps):
        for j in range(1, steps):
            px = minx + (maxx - minx) * i / steps
            py = miny + (maxy - miny) * j / steps
            if good((px, py)):
                d = (px - c[0]) ** 2 + (py - c[1]) ** 2
                if d < best_d:
                    best_d, best = d, (px, py)
    if best is not None:
        return best
    # Last resort: midpoint of the longest diagonal of the first two vertices.
    return outer[0]


def map_click(
    page: Page,
    target: str,
    *,
    layer: str | None = None,
    source: str | None = None,
    config: RecorderConfig | None = None,
) -> bool:
    """Click a NAMED Mapbox feature on the main map by its ``name`` property.

    The other verbs resolve a labelled DOM element; a ward polygon is a feature
    *inside* the map canvas with no DOM node of its own. ``map_click`` bridges that:
    it finds the Mapbox map in the page (``window.__review.map`` for the microplans
    editor, else any map-shaped global), looks up the feature whose ``name`` matches
    ``target`` — preferring rendered features on the boundary FILL layer
    (``mp-admin-fill``), falling back to ``querySourceFeatures`` on the source
    (``mp-admin``) when the feature is loaded but not currently painted — computes a
    point guaranteed to lie INSIDE the polygon (centroid, or a representative interior
    point for a concave ward), and ``map.project()``s it to screen pixels. The
    synthetic cursor then glides to those pixels and dispatches a REAL mouse click, so
    the app's own ``map.on('click', FILL, …)`` handler fires and the ward is added —
    exactly as if a person clicked it.

    ``layer`` / ``source`` override the defaults for non-microplans maps. Returns
    ``True`` when a click was dispatched at an in-polygon pixel, ``False`` when the
    map or the named feature couldn't be resolved (so a ``must_succeed`` map_click
    aborts the render cleanly rather than silently clicking empty canvas).
    """
    cfg = config or RecorderConfig()
    name = (target or "").strip()
    if not name:
        return False
    fill_layer = layer or "mp-admin-fill"
    src = source or "mp-admin"

    # Pull the named feature's polygon coordinates out of the live map. We do the
    # geometry lookup in the page (the map owns the features) but compute the
    # interior point in Python so the tricky concave-polygon logic is unit-testable.
    geom = page.evaluate(
        """({name, fillLayer, src, findMap}) => {
            const map = (new Function('return (' + findMap + ')'))()();
            if (!map) return {error: 'no-map'};
            const lname = String(name).trim().toLowerCase();
            const nameOf = (f) => String((f.properties && f.properties.name) || '').trim().toLowerCase();
            let feats = [];
            try { feats = map.queryRenderedFeatures({layers: [fillLayer]}) || []; } catch (e) { feats = []; }
            let hit = feats.find((f) => nameOf(f) === lname);
            if (!hit) {
                // Loaded-but-not-painted fallback: query the SOURCE directly.
                try {
                    const sf = map.querySourceFeatures(src) || [];
                    hit = sf.find((f) => nameOf(f) === lname);
                } catch (e) {}
            }
            if (!hit || !hit.geometry) return {error: 'no-feature'};
            const g = hit.geometry;
            // Normalise to a single polygon's ring list (Polygon or first part of MultiPolygon).
            let rings = null;
            if (g.type === 'Polygon') rings = g.coordinates;
            else if (g.type === 'MultiPolygon') {
                // Largest part by outer-ring vertex count — the ward's main body.
                let best = null, bestN = -1;
                for (const part of g.coordinates) {
                    const n = (part && part[0] && part[0].length) || 0;
                    if (n > bestN) { bestN = n; best = part; }
                }
                rings = best;
            }
            if (!rings) return {error: 'not-polygon', gtype: g.type};
            return {rings};
        }""",
        {"name": name, "fillLayer": fill_layer, "src": src, "findMap": _FIND_MAP_JS},
    )
    if not isinstance(geom, dict) or geom.get("error") or not geom.get("rings"):
        why = (geom or {}).get("error") if isinstance(geom, dict) else "no-result"
        print(f"  ! map_click could not resolve feature {name!r} ({why})")
        return False

    try:
        lng, lat = polygon_interior_point(geom["rings"])
    except Exception as e:  # noqa: BLE001
        print(f"  ! map_click interior-point failed for {name!r}: {e}")
        return False

    # Project the interior lng/lat to PAGE pixels (project() gives canvas-relative
    # coords; add the canvas's bounding-rect origin to get viewport pixels the
    # Playwright mouse uses). Returned so a failure here is visible too.
    px = page.evaluate(
        """({lng, lat, findMap}) => {
            const map = (new Function('return (' + findMap + ')'))()();
            if (!map) return null;
            const p = map.project([lng, lat]);
            const rect = map.getCanvas().getBoundingClientRect();
            return {x: rect.left + p.x, y: rect.top + p.y};
        }""",
        {"lng": lng, "lat": lat, "findMap": _FIND_MAP_JS},
    )
    if not isinstance(px, dict) or "x" not in px or "y" not in px:
        print(f"  ! map_click could not project {name!r} to screen pixels")
        return False

    x, y = float(px["x"]), float(px["y"])
    # Mirror the draw coordinate-click path: glide the visible cursor to the pixel,
    # dwell so the viewer registers WHERE the click lands, then a real mouse click
    # the app's `map.on('click', FILL)` handler receives.
    slow_move(page, x, y, steps=cfg.cursor_steps)
    page.wait_for_timeout(cfg.click_dwell_ms)
    page.mouse.click(x, y)
    page.wait_for_timeout(cfg.glide_dwell_ms)
    print(f"  · map_click {name!r} → in-polygon pixel ({x:.0f}, {y:.0f})")
    return True


def map_zoom(
    page: Page,
    zoom: float | str,
    *,
    duration_ms: int = 2000,
    config: RecorderConfig | None = None,
) -> bool:
    """Fly the main Mapbox map to a ``zoom`` level — a cinematic push-in / pull-out.

    The camera-move sibling of ``map_click``: finds the map via the SAME shared
    resolver and calls ``map.flyTo({zoom, duration})``, so a demo can push in to
    reveal the individual building footprints (the households drawn from Overture)
    and pull back to the whole ward. ``zoom`` is the target Mapbox zoom level
    (≈16.5 to see rooftops, ≈13 for the ward); ``duration_ms`` is the animation
    length and the verb waits it out before returning. ``False`` when the map
    can't be resolved or has no ``flyTo``."""
    cfg = config or RecorderConfig()  # noqa: F841 — parity with map_click signature
    try:
        z = float(zoom)
    except (TypeError, ValueError):
        print(f"  ! map_zoom: invalid zoom {zoom!r}")
        return False
    ok = page.evaluate(
        """({z, dur, findMap}) => {
            const map = (new Function('return (' + findMap + ')'))()();
            if (!map || typeof map.flyTo !== 'function') return false;
            map.flyTo({ zoom: z, duration: dur, essential: true });
            return true;
        }""",
        {"z": z, "dur": int(duration_ms), "findMap": _FIND_MAP_JS},
    )
    if not ok:
        print("  ! map_zoom: map not found or has no flyTo")
        return False
    page.wait_for_timeout(int(duration_ms) + 300)
    print(f"  · map_zoom → {z}")
    return True


def scroll_page(page: Page, to: str = "bottom", *, max_duration_ms: int = 4000) -> None:
    """Eased scroll to ``"top"``, ``"bottom"``, or a pixel offset."""
    if to == "top":
        page.evaluate("() => window.scrollTo({top: 0, behavior: 'smooth'})")
        page.wait_for_timeout(600)
        return
    if to.isdigit():
        page.evaluate("(y) => window.scrollTo({top: y, behavior: 'smooth'})", int(to))
        page.wait_for_timeout(600)
        return
    page.evaluate(
        """(maxDur) => new Promise(res => {
            const dist = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
            if (dist <= 50) return res();
            const start = performance.now();
            const dur = Math.min(maxDur, dist * 1.3);
            function step(t) {
                const r = Math.min(1, (t - start) / dur);
                const eased = r < 0.5 ? 4*r*r*r : 1 - Math.pow(-2*r + 2, 3)/2;
                window.scrollTo(0, dist * eased);
                if (r < 1) requestAnimationFrame(step); else res();
            }
            requestAnimationFrame(step);
        })""",
        max_duration_ms,
    )


def wait_for(
    page: Page,
    target: str,
    *,
    seconds: float | None = None,
    config: RecorderConfig | None = None,
) -> bool:
    """Wait for ``target`` (text or selector) to appear, or pause for N ms if ``target`` is all digits.

    Plain-text targets skip the selector engine entirely — see
    :func:`._lib.targets.wait_for_target` for the rationale.

    ``seconds`` is a per-call timeout override (the spec's
    ``{kind: wait_for, target: X, seconds: 120}``). It's converted to
    milliseconds and forwarded as ``timeout_ms`` to
    :func:`._lib.targets.wait_for_target`. ``None`` falls back to
    ``RecorderConfig.wait_for_timeout_ms`` — back-compat with every call
    site that doesn't pass it. Negative values are floored at 0 (the
    underlying Playwright ``wait_for`` would raise on negatives; we
    rather treat "negative timeout" as "don't wait").
    """
    cfg = config or RecorderConfig()
    if seconds is not None:
        timeout_ms = max(0, int(float(seconds) * 1000))
    else:
        timeout_ms = cfg.wait_for_timeout_ms
    return wait_for_target(page, target, timeout_ms=timeout_ms)


def _apply_capture_pattern(raw: str, pattern: str | None) -> tuple[bool, str | None]:
    """Extract the capture value from *raw* (trimmed) per *pattern*.

    - ``pattern is None`` → the whole trimmed string is the value (empty ⇒ fail).
    - ``pattern`` given → must compile, must MATCH (``re.search``), and must
      carry at least one capture group; group 1 (trimmed) is the value. A
      no-group pattern, a non-matching pattern, or an empty group-1 all FAIL.

    Returns ``(ok, value)``. ``ok=False`` ⇒ nothing usable was captured.
    """
    text = (raw or "").strip()
    if pattern is None:
        if not text:
            return False, None
        return True, text
    try:
        rx = re.compile(pattern)
    except re.error as e:
        print(f"  ! capture pattern is not a valid regex: {pattern!r} ({e})")
        return False, None
    if rx.groups < 1:
        print(f"  ! capture pattern has no capture group: {pattern!r} (need group 1)")
        return False, None
    m = rx.search(text)
    if m is None:
        print(f"  ! capture pattern {pattern!r} did not match: {text[:120]!r}")
        return False, None
    value = (m.group(1) or "").strip()
    if not value:
        return False, None
    return True, value


def capture_value(
    page: Page, action: dict[str, Any], *, config: RecorderConfig | None = None
) -> tuple[bool, str | None]:
    """Read a value off the live page per a ``capture`` action.

    ``source: url`` → read ``page.url``; ``pattern`` (REQUIRED) group 1 is the
    value. ``source: element`` → resolve ``target``, read ``attr`` (or the
    element's text when ``attr`` is omitted); ``pattern`` is optional (group 1,
    else the whole trimmed attr/text). The value is always trimmed.

    Returns ``(ok, value)``. ``ok=False`` ⇒ nothing was captured (missing
    URL/attr/text, bad/non-matching pattern, or an unresolved element). The
    caller (``execute_action``) decides whether that aborts the render via
    ``must_succeed`` (which defaults True for capture).
    """
    cfg = config or RecorderConfig()
    source = (action.get("source") or "url").strip()
    pattern = action.get("pattern")

    if source == "url":
        if not pattern:
            print("  ! capture source=url requires a `pattern` (regex with group 1)")
            return False, None
        return _apply_capture_pattern(getattr(page, "url", "") or "", pattern)

    if source == "element":
        target = action.get("target")
        if not target:
            print("  ! capture source=element requires a `target`")
            return False, None
        rt = resolve_target(page, target, timeout_ms=cfg.glide_timeout_ms)
        if rt is None:
            print(f"  ! capture target not found: {target!r}")
            return False, None
        attr = action.get("attr")
        try:
            if attr:
                raw = rt.locator.get_attribute(attr, timeout=cfg.interaction_timeout_ms)
                if raw is None:
                    print(f"  ! capture element has no attribute {attr!r}: {target!r}")
                    return False, None
            else:
                raw = rt.locator.inner_text(timeout=cfg.interaction_timeout_ms)
        except Exception as e:  # noqa: BLE001
            print(f"  ! capture element read failed: {target!r}: {e}")
            return False, None
        return _apply_capture_pattern(raw or "", pattern)

    print(f"  ! capture source must be url | element (got: {source!r})")
    return False, None


# --------------------------------------------------------------------------- #
# dispatcher
# --------------------------------------------------------------------------- #


def _config_with_action_timeout(cfg: RecorderConfig, action: dict[str, Any]) -> RecorderConfig:
    """Apply a per-action ``timeout_ms`` override onto a config copy.

    Playwright's ``locator.click`` waits for any scheduled navigation inside
    the same timeout, so a ``must_succeed`` click whose POST does slow
    server-side work (publish/submit minting records before the redirect)
    aborts an otherwise healthy render at the global default. The override
    can only LOOSEN the timeouts (``max`` with the preset), never tighten.
    """
    raw = action.get("timeout_ms")
    try:
        timeout_ms = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        timeout_ms = None
    if not timeout_ms or timeout_ms <= 0:
        return cfg
    import dataclasses

    return dataclasses.replace(
        cfg,
        interaction_timeout_ms=max(cfg.interaction_timeout_ms, timeout_ms),
        goto_timeout_ms=max(cfg.goto_timeout_ms, timeout_ms),
    )


def execute_action(
    page: Page,
    action: dict[str, Any],
    *,
    base_url: str = "",
    config: RecorderConfig | None = None,
    variables: dict[str, Any] | None = None,
) -> ActionResult:
    """Execute one declarative ``Action`` (from ``scene.actions``) with the cursor.

    Returns an :class:`ActionResult` describing what happened. A bad action
    yields ``ok=False`` with a tagged ``error_kind`` but does NOT raise — unless
    the action sets ``must_succeed: true``, in which case the failure raises
    :class:`ActionAssertError` for the orchestrator to handle.

    Returning a result (instead of ``None`` as before) lets the orchestrator
    accumulate a :class:`RunReport` so silent failures stop hiding.

    ``variables`` is the LIVE late-binding ``${var}`` map. A ``capture`` action
    reads an id off the page and writes it here, so a LATER scene/action can
    resolve ``${that_id}`` against the same dict (the recorder resolves lazily,
    right before each action). The captured value overrides nothing unless the
    names collide, in which case the captured value wins and a warning is
    printed — the on-camera value is the fresher truth. ``None`` ⇒ a private
    empty map (a direct test call with no later scenes still works).
    """
    cfg = config or RecorderConfig()
    cfg = _config_with_action_timeout(cfg, action)
    if variables is None:
        variables = {}
    kind = (action.get("kind") or "").strip()
    target = action.get("target")
    value = action.get("value")
    seconds = action.get("seconds")
    note = action.get("note")
    # capture defaults must_succeed True (a later ${var} that never bound films
    # a literal placeholder URL); every other kind defaults False.
    must_succeed = bool(action.get("must_succeed", kind == "capture"))
    # `word` is the older spelling; both mean "the narration word that names this
    # field". Kept on the result so the run report carries it (see ActionResult.say).
    say = action.get("say") or action.get("word")
    say = str(say).strip() or None if say else None
    captured_var: str | None = None
    captured_value: str | None = None

    label = f"{kind}({target or value or ''})"
    if note:
        label += f"  — {note}"
    print(f"    · {label}")

    start = time.monotonic()
    ok = True
    error_kind: str | None = None
    error_message: str | None = None

    try:
        if kind == "goto":
            url = (target or value or "").strip()
            if url.startswith("/"):
                url = base_url.rstrip("/") + url
            page.goto(url, wait_until="domcontentloaded", timeout=cfg.goto_timeout_ms)
            page.wait_for_timeout(cfg.goto_settle_ms)
        elif kind == "click":
            ok = click_text(page, target or value or "", config=cfg)
            if not ok:
                error_kind = "target_not_found"
        elif kind == "click_menu":
            ok = click_menu_item(page, target or value or "", config=cfg)
            if not ok:
                error_kind = "target_not_found"
        elif kind == "fill":
            ok = fill_field(page, target or "", value or "", config=cfg)
            if not ok:
                error_kind = "target_not_found"
        elif kind == "select":
            ok = select_option(page, target or "", value or "", config=cfg)
            if not ok:
                error_kind = "target_not_found"
        elif kind == "type":
            page.keyboard.type(value or "", delay=cfg.typing_delay_ms)
        elif kind == "press":
            page.keyboard.press(value or "Enter")
        elif kind == "hover":
            ok = hover(page, target or value or "", seconds=seconds, config=cfg)
            if not ok:
                error_kind = "target_not_found"
        elif kind == "scroll_to":
            ok = scroll_to(page, target or value or "", config=cfg)
            if not ok:
                error_kind = "target_not_found"
        elif kind == "scroll":
            scroll_page(page, value or "bottom")
        elif kind == "wait_for":
            ok = wait_for(
                page, str(target or value or "1000"), seconds=seconds, config=cfg
            )
            if not ok:
                error_kind = "timeout"
        elif kind == "hold":
            hold_ms = int(float(seconds or value or 1.0) * 1000)
            # A flow scene caps explicit holds so a deliberate `hold 4` dropped
            # in a beat later marked `pace: flow` doesn't freeze a connective
            # scene (cfg.hold_action_ceiling_ms is None for teach scenes → no cap).
            ceiling = getattr(cfg, "hold_action_ceiling_ms", None)
            if ceiling is not None:
                hold_ms = min(hold_ms, int(ceiling))
            page.wait_for_timeout(hold_ms)
        elif kind == "draw":
            ok = draw_polygon(
                page, target or "", action.get("points") or [], tool=action.get("tool"), config=cfg
            )
            if not ok:
                error_kind = "target_not_found"
        elif kind == "map_click":
            ok = map_click(
                page, target or value or "",
                layer=action.get("layer"), source=action.get("source"), config=cfg,
            )
            if not ok:
                error_kind = "target_not_found"
        elif kind == "map_zoom":
            ok = map_zoom(
                page, action.get("zoom", target or value or ""),
                duration_ms=int(float(action.get("seconds") or 2.0) * 1000), config=cfg,
            )
            if not ok:
                error_kind = "target_not_found"
        elif kind == "capture":
            captured_var = action.get("var")
            if not captured_var:
                ok = False
                error_kind = "other"
                error_message = "capture action missing `var`"
                print(f"    ! {error_message}")
            else:
                ok, captured_value = capture_value(page, action, config=cfg)
                if ok and captured_value is not None:
                    if captured_var in variables and str(variables[captured_var]) != captured_value:
                        print(
                            f"    ! capture var ${{{captured_var}}} overrides a prior value "
                            f"({variables[captured_var]!r} → {captured_value!r}) — captured wins"
                        )
                    variables[captured_var] = captured_value
                    print(f"    · captured ${{{captured_var}}} = {captured_value!r}")
                else:
                    error_kind = "capture_failed"
                    error_message = f"capture for ${{{captured_var}}} produced no value"
        elif kind == "snapshot":
            # Recorder-state, not a page interaction — the canonical scene frame
            # is written by Recorder.run_scene (which holds the snapshot context).
            # Reached here only for an ad-hoc execute_action call with no scene;
            # treat as a successful no-op so it never reports unknown_kind.
            pass
        else:
            ok = False
            error_kind = "unknown_kind"
            error_message = f"unknown action kind: {kind!r}"
            print(f"    ! {error_message} (skipped)")
    except Exception as e:  # noqa: BLE001
        ok = False
        error_kind = "playwright"
        error_message = str(e)
        print(f"    ! action {label} failed: {e}")

    elapsed_ms = int((time.monotonic() - start) * 1000)
    result = ActionResult(
        kind=kind, ok=ok, target=target, value=value, note=note,
        elapsed_ms=elapsed_ms, error_kind=error_kind, error_message=error_message,
        must_succeed=must_succeed, say=say,
        capture_var=captured_var, capture_value=captured_value,
    )
    if not ok and must_succeed:
        raise ActionAssertError(f"required action failed: {label}: {error_message or error_kind}")
    return result
