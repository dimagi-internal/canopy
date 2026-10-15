"""Target resolution — a thin author-facing layer over Playwright locators.

The walkthrough recorder needs to turn the spec author's free-form ``target``
string ("Resolved wards", "#cfg-strategy", "testid:plan-picker") into something
the cursor can glide to and the verb can act on. Two questions matter:

  1. **Where is the element?**
  2. **How do we click / fill / read its box?**

Playwright already answers both, well: ``Locator`` is the source of truth,
``get_by_role / get_by_text / get_by_label / get_by_test_id / get_by_placeholder``
are the recommended idioms (they use the browser's accessibility tree and
auto-wait for visibility + actionability), and ``Locator.bounding_box()``
gives us coordinates for the synthetic cursor. This module is the adapter
between our author syntax and those APIs — nothing more.

What changed vs the original 0.2.141 implementation: ``_box_center`` ran a
hand-rolled DOM scan ranked actionable-vs-text and ``_glide_to`` polled it
every 150ms until a deadline. Both were partial reimplementations of what
Playwright's locator engine already does — and they bypassed the
``Locator.click()`` actionability checks (visible, stable, receives events,
not detached) by clicking at coordinates. The result was a less-reliable
clone of a thing Playwright already provides correctly.

Now:

  - Each prefix maps directly to a Playwright ``get_by_*`` call:
        ``css:#x``        → ``page.locator("#x")``
        ``testid:foo``    → ``page.get_by_test_id("foo")``
        ``aria:Foo``      → ``page.get_by_label("Foo", exact=False)``
        ``role:button``   → ``page.get_by_role("button")``
        ``role:button:Sign in`` → ``page.get_by_role("button", name="Sign in", exact=True)``
        ``text:Foo``      → ``page.get_by_text("Foo")``
  - Bare targets use a heuristic: CSS-shaped → ``page.locator(...)``; English
    → a small role-prefers-actionable cascade ending in ``get_by_text``.
  - Auto-wait for visibility uses ``Locator.wait_for(state="visible", ...)``
    — the same call Playwright tests use.
  - ``resolve_target`` returns the ``Locator`` itself; primitives that need
    Playwright API surface (``locator.click()``, ``locator.fill()``,
    ``locator.select_option()``) get it directly. The cursor-coordinate box
    comes from ``locator.bounding_box()``.

Net code goes down (~150 lines deleted, ~80 added), reliability goes up
(actionability checks are back), and the author syntax is unchanged — every
existing spec records the same.
"""

from __future__ import annotations

from typing import NamedTuple

from playwright.sync_api import Locator, Page

# Recognised target prefixes. Order matters for documentation — at parse time
# we look for an exact prefix match.
_PREFIXES = ("css", "text", "testid", "aria", "role")
_PREFIX_SEPARATOR = ":"

# Roles that map to user-actionable elements — used by the bare-target text
# heuristic when an author writes "Save" expecting the Save button to win
# over a `<span>Save</span>` heading on the same page. Order = preference,
# but in practice each role probe is cheap (Playwright either has a match
# immediately or doesn't).
_ACTIONABLE_ROLES = (
    "button", "link", "option", "menuitem", "tab", "switch",
    "checkbox", "radio", "combobox", "treeitem",
    # ``columnheader`` covers sortable table headers (the compare-page
    # "Longest worker travel" / "Coverage" clicks); ``cell`` covers
    # grid-style row clicks ("Burji", "Madobi" in a resolved-wards table).
    "columnheader", "cell",
)


class ResolvedTarget(NamedTuple):
    """A Playwright ``Locator`` + the viewport-centre cursor coordinate.

    ``locator``: the Playwright handle every primitive uses to act
        (``click()``, ``fill()``, ``select_option()``, ``scroll_into_view_if_needed()``,
        etc.). NEVER ``None`` — if we couldn't resolve, the whole
        ``ResolvedTarget`` is ``None``.
    ``box``: ``{x, y}`` viewport centre of the locator's bounding box — what
        the synthetic cursor glides to before the primitive acts.
    ``kind``: ``"css"`` | ``"testid"`` | ``"aria"`` | ``"role"`` | ``"text"``.
        Useful for telemetry; the report layer wants to know whether a click
        landed via the selector engine or visible-text ranking.
    """

    locator: Locator
    box: dict
    kind: str


def parse_target(target: str) -> tuple[str, str]:
    """Split an optional ``kind:`` prefix off ``target``.

    Returns ``(kind, value)`` where ``kind`` is one of the prefixes in
    :data:`_PREFIXES`, or ``"auto"`` if no prefix was given. A bare target
    is returned as ``("auto", target)`` so callers can route to the heuristic.
    """
    if not target:
        return "auto", ""
    for p in _PREFIXES:
        head = p + _PREFIX_SEPARATOR
        if target.startswith(head):
            return p, target[len(head):]
    return "auto", target


def looks_like_selector(s: str) -> bool:
    """Heuristic: is ``s`` a CSS selector or visible text?

    Returns True for strings whose first non-space char is a structural
    selector character followed by an identifier-shaped continuation
    (``#cfg-strategy``, ``.btn``, ``[data-foo=bar]``, ``:focus``), or that
    combine an identifier with selector punctuation (``input.psel``,
    ``tr.is-unresolved select``).

    Returns False for English text — including text that *starts* with a
    CSS combinator like ``+ Bulk paste list``. ``+`` and ``~`` are valid
    only INSIDE a selector (sibling combinators); as a leading char of a
    bare target with a space after, they're a button label, not a query.
    The old heuristic mis-classified those and routed authors' bare-text
    targets through ``page.locator(...)`` which then threw on the invalid
    selector.

    Even with the tighter heuristic the resolver falls through to the
    text-engine path on a CSS miss (see :func:`resolve_target`), so a
    mis-classification is recoverable — but the tightening avoids the
    extra round-trip when text was clearly the intent.
    """
    s = (s or "").strip()
    if not s:
        return False
    # Leading structural chars are only selector-y when followed immediately
    # by an identifier-shaped char (``#a``, ``.btn``, ``[x]``, ``:hover``).
    # ``+ Bulk`` (combinator + space + label) is text.
    if s[0] in "#.[:" and len(s) > 1 and (s[1].isalnum() or s[1] in "-_*["):
        return True
    # ``>`` / ``~`` / ``+`` as leading chars are ALWAYS combinators — they
    # need a left-hand side to be valid CSS. Bare-leading is text.
    if s[0].isalpha() and any(c in s for c in ">[.#:"):
        return True
    return False


def measure_box(loc: Locator) -> dict | None:
    """Return ``{x, y}`` viewport centre of a Playwright ``Locator``, or ``None``.

    Public because :func:`recorder.click_text` re-measures right before the
    click to handle the case where a settle moved the target mid-glide. The
    cursor lands on the element's current centre, not where it was when the
    glide started.
    """
    box = loc.bounding_box()
    if not box:
        return None
    return {"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2}


# Internal alias for resolve_target's own calls — same function, less typing.
_measure = measure_box


def _locator_for_prefix(page: Page, kind: str, value: str) -> Locator | None:
    """Map a parsed ``(kind, value)`` to the right Playwright locator API.

    Returns ``None`` for unknown prefixes (which never happens once
    ``parse_target`` has validated the kind) so callers can fall through
    to the auto-heuristic without a separate signal.
    """
    if kind == "css":
        return page.locator(value)
    if kind == "testid":
        return page.get_by_test_id(value)
    if kind == "aria":
        # ``get_by_label`` uses Playwright's accessible-name semantics —
        # matches ``aria-label``, ``aria-labelledby``, ``<label for>``, and
        # ``<label>`` wrapping. The old ``[aria-label*=...]`` CSS shorthand
        # missed everything except the literal aria-label attribute.
        return page.get_by_label(value, exact=False)
    if kind == "role":
        # ``role:button`` matches first button; ``role:button:Sign in``
        # matches a button with that accessible name (exact). Authors who
        # need substring-match name use the text path.
        if _PREFIX_SEPARATOR in value:
            role, name = value.split(_PREFIX_SEPARATOR, 1)
            return page.get_by_role(role, name=name, exact=True)  # type: ignore[arg-type]
        return page.get_by_role(value)  # type: ignore[arg-type]
    if kind == "text":
        return page.get_by_text(value)
    return None


def _visible_now(locator: Locator) -> Locator | None:
    """``locator.first`` if it is on the page and visible RIGHT NOW, else ``None``.

    Never waits: ``count()`` and ``is_visible()`` both answer from the current
    DOM. Used to prefer an exact text match without paying a timeout when there
    is none (canopy#912).
    """
    try:
        if locator.count() > 0 and locator.first.is_visible():
            return locator.first
    except Exception:  # noqa: BLE001 — a probe; the caller has a fallback
        pass
    return None


def _resolve_via_text(page: Page, value: str, *, timeout_ms: int) -> Locator | None:
    """Find an element by visible text.

    Tries Playwright's text engine first (cheap, browser-native, picks the
    smallest matching element — which is usually the actionable one
    because a wrapping ``<div>`` has a bigger bounding box than its inner
    ``<button>``). Falls back to role + accessible-name probes for the
    rare case where the visible text and the accessible name diverge
    (icon-only buttons that label themselves via ``aria-label``).

    The ordering matters for speed: a 14-action scene that text-resolves
    cleanly costs ~14×fast paths; flipping it to role-probes-first added
    ~80 s of overhead on the microplans-10-wards recording for no
    reliability win on these UIs.
    """
    # Half the budget on the cheap path. ONE blocking wait, on the substring
    # locator — every exact match is also a substring match, so it appears as
    # soon as either would. The exact preference is then a NON-blocking probe
    # (``count()`` + ``is_visible()`` return immediately). The old order waited
    # on the exact locator first, so whenever the visible text was only PART of
    # a larger node ("Rijiyar Kwara" inside "Rijiyar Kwara (Kano)") the exact
    # wait burned its whole half-budget (~3 s at the default) before the
    # substring fallback ran — 3.83 s per hover vs 0.83 s with ``text:``, which
    # pushed every later beat of a ``style: recorded`` cut off its spoken word
    # (canopy#912).
    text_budget = max(500, timeout_ms // 2)
    substring = page.get_by_text(value)
    exact = page.get_by_text(value, exact=True)
    substring_ok = True
    try:
        substring.first.wait_for(state="visible", timeout=text_budget)
    except Exception:
        substring_ok = False
    exact_now = _visible_now(exact)
    if exact_now is not None:
        return exact_now
    if substring_ok:
        return substring.first

    # Role + accessible-name fallback. Each probe is short (we've already
    # spent half the budget); we're covering the icon-only / aria-labeled
    # case, not doing the primary lookup.
    probe_budget = max(150, (timeout_ms - text_budget) // len(_ACTIONABLE_ROLES))
    for role in _ACTIONABLE_ROLES:
        loc = page.get_by_role(role, name=value, exact=True).first  # type: ignore[arg-type]
        try:
            loc.wait_for(state="visible", timeout=probe_budget)
            return loc
        except Exception:
            continue
    return None


def resolve_target(page: Page, target: str, *, timeout_ms: int = 6000) -> ResolvedTarget | None:
    """Resolve a ``target`` string to a Playwright ``Locator`` + cursor coordinate.

    Routing:
      - Explicit ``css:|testid:|aria:|role:|text:`` — the matching
        ``get_by_*`` (or raw ``locator(...)``) is the only path tried.
        Explicit means explicit; no text-fallback if a CSS selector misses.
      - Bare target — heuristic: selector-shaped → ``page.locator(value)``,
        else the role-prefers-actionable cascade in :func:`_resolve_via_text`.

    Returns ``None`` if neither path resolved within ``timeout_ms``. The
    returned ``Locator`` is auto-waited to visible so the caller can call
    ``locator.click()`` / ``locator.fill()`` / etc. straight away.
    """
    if not target:
        return None
    kind, value = parse_target(target)

    if kind != "auto":
        loc = _locator_for_prefix(page, kind, value)
        if loc is None:
            return None
        try:
            loc = loc.first
            loc.wait_for(state="visible", timeout=timeout_ms)
        except Exception:
            return None
        box = _measure(loc)
        if box is None:
            return None
        return ResolvedTarget(locator=loc, box=box, kind=kind)

    # auto — heuristic dispatch with FALL-THROUGH to text on CSS miss.
    #
    # The heuristic is fast but not infallible; a bare-leading combinator
    # (``+ Foo``) used to read as CSS and throw, which silently killed the
    # action. Now we try the selector engine first when the shape says so,
    # and if it doesn't resolve in half the budget, we fall through to the
    # visible-text path. Heuristic is helpful when right and recoverable
    # when wrong.
    if looks_like_selector(value):
        try:
            loc = page.locator(value).first
            loc.wait_for(state="visible", timeout=timeout_ms // 2)
            box = _measure(loc)
            if box is not None:
                return ResolvedTarget(locator=loc, box=box, kind="css")
        except Exception:
            pass  # selector parse error or no match — text below
        text_budget = max(500, timeout_ms // 2)
    else:
        text_budget = timeout_ms

    loc = _resolve_via_text(page, value, timeout_ms=text_budget)
    if loc is None:
        return None
    box = _measure(loc)
    if box is None:
        return None
    return ResolvedTarget(locator=loc, box=box, kind="text")


def wait_for_target(page: Page, target: str, *, timeout_ms: int = 12000) -> bool:
    """Wait for ``target`` to appear, or pause for N ms if all-digits.

    All resolution paths flow through :func:`resolve_target` — that means
    the same prefix syntax + the same role-prefers-actionable cascade as
    everywhere else. No special-case selector branch (was a 12 s hang for
    text targets before 0.2.141; the Playwright text engine handles it
    correctly here).
    """
    if not target:
        return False
    if target.isdigit():
        page.wait_for_timeout(int(target))
        return True
    return resolve_target(page, target, timeout_ms=timeout_ms) is not None
