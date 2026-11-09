"""Unit tests for target parsing + Playwright dispatch (no real browser needed).

The pure-function tests (parse_target, looks_like_selector) stay
browser-independent. The dispatch tests use a FakePage that records which
``get_by_*`` (or ``locator``) method was called — that's enough to pin the
"prefix → right Playwright API" routing without spinning up Chromium.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.walkthrough._lib.targets import (  # noqa: E402
    _locator_for_prefix,
    looks_like_selector,
    parse_target,
)


# ---- parse_target ---------------------------------------------------------


def test_parse_target_empty():
    assert parse_target("") == ("auto", "")


def test_parse_target_no_prefix_is_auto():
    assert parse_target("Resolved wards") == ("auto", "Resolved wards")
    assert parse_target("#cfg-strategy") == ("auto", "#cfg-strategy")


def test_parse_target_css_prefix():
    assert parse_target("css:#cfg-strategy") == ("css", "#cfg-strategy")


def test_parse_target_testid_prefix():
    assert parse_target("testid:plan-picker") == ("testid", "plan-picker")


def test_parse_target_aria_prefix():
    assert parse_target("aria:Resolved wards") == ("aria", "Resolved wards")


def test_parse_target_role_prefix():
    assert parse_target("role:option") == ("role", "option")


def test_parse_target_text_prefix_forces_text_path():
    """``text:#hashtag`` looks for the literal text ``#hashtag`` — the
    prefix is consumed, the rest is verbatim. Authors use this when label
    contains a leading ``#`` but the heuristic would route it as CSS."""
    assert parse_target("text:#hashtag") == ("text", "#hashtag")


def test_parse_target_unknown_prefix_is_left_alone():
    """``blah:foo`` is NOT a recognised prefix — the whole string stays as
    the auto-target. Typoing a prefix can't silently change meaning."""
    assert parse_target("blah:foo") == ("auto", "blah:foo")


# ---- looks_like_selector --------------------------------------------------


def test_looks_like_selector_empty_is_false():
    assert looks_like_selector("") is False
    assert looks_like_selector("   ") is False


def test_looks_like_selector_leading_punct_is_true():
    assert looks_like_selector("#cfg-strategy") is True
    assert looks_like_selector(".btn-primary") is True
    assert looks_like_selector("[data-testid=foo]") is True
    assert looks_like_selector(":focus") is True


def test_looks_like_selector_bare_leading_combinator_is_false():
    """``+ Bulk paste list`` (combinator + space + label) is text, not CSS.

    The microplans-10-wards spec uses this exact string as a button label.
    The old heuristic mis-classified it and silently broke the whole
    scene-2 cascade — every subsequent action failed because the page
    didn't navigate to the bulk-create form.
    """
    assert looks_like_selector("+ Bulk paste list") is False
    assert looks_like_selector("> Click for details") is False
    assert looks_like_selector("~ Approximately 10 items") is False


def test_looks_like_selector_plain_text_is_false():
    """The exact strings that caused a wait_for hang."""
    assert looks_like_selector("Resolved wards") is False
    assert looks_like_selector("Creating 10 plans") is False
    assert looks_like_selector("Plan metric definitions") is False


def test_looks_like_selector_compound_selector_is_true():
    """Real microplans-10-wards spec compound selectors."""
    assert looks_like_selector("tr.is-unresolved select") is True
    assert looks_like_selector("#resolved-tbody tr.is-unresolved select") is True
    assert looks_like_selector("#plan-picker > label:nth-of-type(1) input.psel") is True


def test_looks_like_selector_word_with_dot_is_true():
    """tag.class shapes resolve as selectors."""
    assert looks_like_selector("input.psel") is True


# ---- _locator_for_prefix dispatch ----------------------------------------


class _FakePage:
    """Records which Playwright locator-API the dispatcher called.

    Just enough surface to pin the prefix → API routing; we don't exercise
    the locator itself (that's a real-browser concern).
    """

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []

    def _record(self, _method, *args, **kwargs):
        # ``_method`` is leading-underscore so it doesn't collide with the
        # ``name=`` kwarg Playwright's ``get_by_role(role, name=...)`` uses.
        self.calls.append((_method, args, kwargs))
        return f"<locator from {_method}>"

    def locator(self, *a, **kw): return self._record("locator", *a, **kw)
    def get_by_test_id(self, *a, **kw): return self._record("get_by_test_id", *a, **kw)
    def get_by_label(self, *a, **kw): return self._record("get_by_label", *a, **kw)
    def get_by_role(self, *a, **kw): return self._record("get_by_role", *a, **kw)
    def get_by_text(self, *a, **kw): return self._record("get_by_text", *a, **kw)


def test_dispatch_css_calls_locator():
    p = _FakePage()
    _locator_for_prefix(p, "css", "#x")
    assert p.calls == [("locator", ("#x",), {})]


def test_dispatch_testid_calls_get_by_test_id():
    p = _FakePage()
    _locator_for_prefix(p, "testid", "plan-picker")
    assert p.calls == [("get_by_test_id", ("plan-picker",), {})]


def test_dispatch_aria_calls_get_by_label():
    """``aria:`` uses ``get_by_label`` — picks up ``aria-label``,
    ``aria-labelledby``, ``<label for>``, and ``<label>`` wrapping via
    Playwright's accessible-name semantics."""
    p = _FakePage()
    _locator_for_prefix(p, "aria", "Resolved wards")
    assert p.calls == [("get_by_label", ("Resolved wards",), {"exact": False})]


def test_dispatch_role_simple_calls_get_by_role():
    p = _FakePage()
    _locator_for_prefix(p, "role", "button")
    assert p.calls == [("get_by_role", ("button",), {})]


def test_dispatch_role_with_name_uses_exact_name():
    """``role:button:Sign in`` → ``get_by_role("button", name="Sign in", exact=True)``."""
    p = _FakePage()
    _locator_for_prefix(p, "role", "button:Sign in")
    assert p.calls == [("get_by_role", ("button",), {"name": "Sign in", "exact": True})]


def test_dispatch_text_calls_get_by_text():
    p = _FakePage()
    _locator_for_prefix(p, "text", "Resolved wards")
    assert p.calls == [("get_by_text", ("Resolved wards",), {})]


def test_dispatch_unknown_kind_returns_none():
    """Unknown kinds (shouldn't happen post-parse_target) → no call, None return.

    Defensive — keeps the auto-fallback safe if someone bypasses parse_target.
    """
    p = _FakePage()
    result = _locator_for_prefix(p, "totally_unknown", "x")
    assert result is None
    assert p.calls == []
