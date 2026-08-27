"""The capture JS itself, against a real browser — canopy#525.

``test_visual_geometry.py`` tests the RULES against fixtures. This tests the
half that fixtures cannot reach: whether the JavaScript in
``orchestrator._VISUAL_CAPTURE_JS`` actually measures a live page correctly —
that it enumerates the stylesheet's class selectors, resolves oklch/rgb colours
to sRGB through the canvas, walks ancestors for the effective background, and
reports the geometry Chromium computed.

Two pages, identical markup, differing only in whether ``h-28`` and
``text-rose-700`` exist in the stylesheet — the exact mechanism of the run's
defects (labs' Tailwind build purges against its own Django templates while a
workflow's ``render_code`` lives in the DB, so a utility used only in
``render_code`` never makes the bundle). ``broken.html`` must produce all three
findings; ``fixed.html`` must produce none. Same probe, both trees — that is
the non-vacuity proof for the capture, not just for the rules.

**This test SKIPS where no browser is installed, and CI is such a place**
(``python-tests.yml`` installs the playwright package only, deliberately). The
CI gate for this lens is ``test_visual_geometry.py``, which is pure. Run this
one locally before changing the capture JS — nothing else covers it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "visual"


@pytest.fixture(scope="module")
def capture_page():
    playwright = pytest.importorskip("playwright.sync_api", reason="playwright package not installed")
    try:
        manager = playwright.sync_playwright()
        pw = manager.start()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"playwright could not start: {e}")
    try:
        browser = pw.chromium.launch()
    except Exception as e:  # noqa: BLE001 — CI installs the package, not the browsers
        manager.__exit__(None, None, None)
        pytest.skip(f"no chromium available: {e}")
    page = browser.new_page()

    def _capture(name: str) -> dict:
        from scripts.walkthrough._lib.orchestrator import _VISUAL_CAPTURE_JS

        page.goto((FIXTURES / f"{name}.html").as_uri())
        page.wait_for_timeout(150)
        result = page.evaluate(_VISUAL_CAPTURE_JS)
        result["scene_index"] = 1
        return result

    yield _capture
    browser.close()
    manager.__exit__(None, None, None)


def test_capture_reads_the_stylesheets_it_is_allowed_to_read(capture_page):
    """Method note 1 of the issue: the enumerated class set is the input the
    inert-utility check depends on. If this comes back empty the whole check
    silently downgrades to 'undecidable' and reports nothing."""
    capture = capture_page("broken")
    assert capture["stylesheets_readable"] is True
    assert capture["readable_stylesheets"] == 1
    defined = set(capture["defined_class_selectors"])
    assert "label" in defined and "font-medium" in defined
    # The two purged utilities are on elements but in no rule — the whole point.
    assert "h-28" not in defined
    assert "text-rose-700" not in defined


def test_broken_page_produces_all_three_defects(capture_page):
    from scripts.ddd.visual_geometry import visual_geometry

    result = visual_geometry(capture_page("broken"))
    kinds = [f["kind"] for f in result["findings"]]

    assert kinds.count("zero-height") == 3          # the purged h-28 bars
    assert kinds.count("low-contrast") == 1         # the 2.46:1 label
    inert = [f for f in result["findings"] if f["kind"] == "inert-utility"]
    reported = {c for f in inert for c in f["classes"]}
    assert reported == {"h-28", "text-rose-700"}

    low = next(f for f in result["findings"] if f["kind"] == "low-contrast")
    # Measured by Chromium through the canvas conversion, not asserted by the
    # fixture: this is the run's recorded 2.47, reproduced.
    assert low["measured"]["contrast"] == 2.46


def test_fixed_page_is_silent(capture_page):
    """Same markup, same probe, the two utilities present in the bundle."""
    from scripts.ddd.visual_geometry import visual_geometry

    result = visual_geometry(capture_page("fixed"))
    assert result["findings"] == [], result["findings"]
    assert result["verdict"] == "pass"


def test_live_page_blind_spots_and_hidden_elements_stay_silent(capture_page):
    """`space-y-1`, `list-disc`, `mx-auto` and a display:none modal are all on
    the broken page. Measured by a real browser, none of them may be reported —
    a naive rule emitted exactly these four false positives on the run that
    motivated this."""
    from scripts.ddd.visual_geometry import visual_geometry

    result = visual_geometry(capture_page("broken"))
    touched = {c for f in result["findings"] for c in f.get("classes", [])} | {
        f["selector"] for f in result["findings"]
    }
    for silent in ("space-y-1", "list-disc", "mx-auto"):
        assert not any(silent in str(t) for t in touched), silent
    assert not any("modal" in f["selector"] for f in result["findings"])
