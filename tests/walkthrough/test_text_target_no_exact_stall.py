"""A bare text target that is only PART of a larger node must resolve fast (canopy#912).

``_resolve_via_text`` used to ``wait_for`` the exact ``get_by_text`` match before
the substring one. When the visible text sat inside a larger node there was no
exact match, so that wait ran out its whole half-budget (~3 s at the default
6 s timeout) before the substring path ran: 3.83 s per hover against 0.83 s with
an explicit ``text:`` prefix. These tests drive the resolver against a fake page
with a fake clock — a blocking ``wait_for`` on a locator with no visible match
advances the clock by its full timeout, exactly as Playwright would.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.walkthrough._lib.targets import resolve_target  # noqa: E402


class Clock:
    def __init__(self) -> None:
        self.ms = 0


class FakeLocator:
    def __init__(self, clock: Clock, name: str, *, visible: bool, box=None) -> None:
        self.clock = clock
        self.name = name
        self.visible = visible
        self.box = box or {"x": 10, "y": 20, "width": 100, "height": 20}

    @property
    def first(self) -> "FakeLocator":
        return self

    def wait_for(self, *, state: str, timeout: int) -> None:
        if not self.visible:
            self.clock.ms += timeout  # Playwright blocks for the whole timeout
            raise TimeoutError(f"{self.name}: not visible after {timeout}ms")
        self.clock.ms += 50  # already on screen: resolves almost immediately

    def count(self) -> int:
        return 1 if self.visible else 0

    def is_visible(self) -> bool:
        return self.visible

    def bounding_box(self):
        return self.box


class FakePage:
    """Visible text "Rijiyar Kwara" exists only inside a larger node."""

    def __init__(self, *, exact_visible: bool, substring_visible: bool = True) -> None:
        self.clock = Clock()
        self.exact = FakeLocator(self.clock, "exact", visible=exact_visible)
        self.substring = FakeLocator(self.clock, "substring", visible=substring_visible)

    def get_by_text(self, value: str, exact: bool = False) -> FakeLocator:
        return self.exact if exact else self.substring

    def get_by_role(self, role, name=None, exact=None) -> FakeLocator:
        return FakeLocator(self.clock, f"role:{role}", visible=False)

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self.clock, f"css:{selector}", visible=False)


def test_partial_text_target_resolves_without_waiting_out_the_exact_probe():
    page = FakePage(exact_visible=False)
    resolved = resolve_target(page, "Rijiyar Kwara", timeout_ms=6000)
    assert resolved is not None
    assert resolved.locator is page.substring
    assert resolved.kind == "text"
    # Before the fix this was 3050 ms: 3000 burned on the exact wait.
    assert page.clock.ms < 500, f"bare text target stalled {page.clock.ms} ms"


def test_partial_text_target_costs_the_same_as_the_text_prefix():
    bare = FakePage(exact_visible=False)
    prefixed = FakePage(exact_visible=False)
    assert resolve_target(bare, "Rijiyar Kwara", timeout_ms=6000) is not None
    assert resolve_target(prefixed, "text:Rijiyar Kwara", timeout_ms=6000) is not None
    assert bare.clock.ms == prefixed.clock.ms


def test_exact_match_is_still_preferred_when_present():
    page = FakePage(exact_visible=True)
    resolved = resolve_target(page, "Save", timeout_ms=6000)
    assert resolved is not None
    assert resolved.locator is page.exact
    assert page.clock.ms < 500


def test_absent_text_still_returns_none():
    page = FakePage(exact_visible=False, substring_visible=False)
    assert resolve_target(page, "Nowhere on the page", timeout_ms=6000) is None
