"""The geometry + colour lens — canopy#525.

Every case here is one of the three defects that scored PASSING on every
text-based lens across four iterations of ACE run
``bednet-check-2-visit/20260825-1310``, or one of the false positives the
hand-written probe that eventually found them emitted on the same page. The
measured numbers are the ones actually recorded on that run, so these tests
double as the record of what the lens is for.
"""
from __future__ import annotations

from scripts.ddd.data_fidelity import data_fidelity
from scripts.ddd.narrated_numbers import page_values
from scripts.ddd.visual_geometry import (
    BLIND_SPOT_UTILITIES,
    contrast_ratio,
    is_blind_spot,
    required_contrast,
    visual_geometry,
)

WHITE = [255, 255, 255]
NEAR_BLACK = [37, 37, 37]  # oklch(0.145 0 0), the unstyled Tailwind baseline
# The grey that reproduces the run's measured 2.46:1 label contrast against white
# (recorded as 2.47 / 2.60 on the four segment bars).
CONTRAST_2_47 = [165, 165, 165]


def _capture(elements, *, defined=(), readable=True, scene_index=1):
    return {
        "scene_index": scene_index,
        "defined_class_selectors": list(defined),
        "stylesheets_readable": readable,
        "unreadable_stylesheets": 0 if readable else 3,
        "elements": elements,
    }


def _el(**kw):
    base = {
        "path": "div.panel",
        "tag": "div",
        "classes": [],
        "w": 200.0,
        "h": 20.0,
        "display": "block",
        "visibility": "visible",
        "opacity": "1",
        "aria_hidden": False,
        "color_is_inherited": False,
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# Defect 1 — the 12-week chart at zero height (`h-28` purged)
# ---------------------------------------------------------------------------


def test_twelve_weekly_bars_at_zero_height_are_flagged():
    """All 12 bars computed to 0px. An entire chart panel was invisible and
    every text lens read the page as correct."""
    bars = [
        _el(path=f"div.chart>div.bar-{i}", classes=["h-28", "bg-indigo-500"], w=18.0, h=0.0)
        for i in range(12)
    ]
    result = visual_geometry(_capture(bars, defined=["bg-indigo-500"]))

    zero = [f for f in result["findings"] if f["kind"] == "zero-height"]
    assert len(zero) == 12, result["findings"]
    assert result["verdict"] == "warn"

    # And the second signal: h-28 is on the element, no rule defines it, and
    # the box is 0px. That is the mechanism, named.
    inert = [f for f in result["findings"] if f["kind"] == "inert-utility"]
    assert len(inert) == 12
    assert "h-28" in inert[0]["detail"]
    assert "no enumerated CSS rule defines it" in inert[0]["detail"]


def test_a_zero_height_element_the_narration_names_is_a_hard_fail():
    """'a zero-height chart is a hard fail, not a score' — the issue's words.
    The narration is talking about a thing the frame does not show."""
    element = _el(
        path="div.kpi",
        classes=["h-28"],
        w=180.0,
        h=0.0,
        text="Week 12: 41 visits",
    )
    result = visual_geometry(
        _capture([element]),
        narration="By week 12 the team is logging 41 visits a week.",
    )
    zero = [f for f in result["findings"] if f["kind"] == "zero-height"]
    assert zero and zero[0]["narrated"] is True
    assert result["verdict"] == "fail"


def test_bars_that_render_are_silent():
    """Post-fix all 12 rendered proportionally, 78-112px. The lens must go
    quiet, or it is noise rather than a check."""
    bars = [
        _el(path=f"div.bar-{i}", classes=["h-28"], w=18.0, h=h)
        for i, h in enumerate([78, 84, 91, 96, 101, 104, 107, 109, 110, 111, 112, 112])
    ]
    result = visual_geometry(_capture(bars, defined=["h-28"]))
    assert result["findings"] == []
    assert result["verdict"] == "pass"


# ---------------------------------------------------------------------------
# Defect 2 — segment bar labels at contrast 2.47
# ---------------------------------------------------------------------------


def test_segment_labels_at_contrast_two_point_four_seven_are_flagged():
    """Measured 2.47 / 2.60 — effectively white-on-white, below AA by ~2x.
    A text assertion for these PASSES, because the text is in the DOM."""
    labels = [
        _el(
            path="div.segment>span.label",
            classes=["text-white"],
            text="Segment A · 41%",
            color_rgb=CONTRAST_2_47,
            bg_rgb=WHITE,
            font_px=13.0,
            font_weight=400.0,
        )
    ]
    result = visual_geometry(_capture(labels, defined=["text-white"]))
    low = [f for f in result["findings"] if f["kind"] == "low-contrast"]
    assert len(low) == 1
    # Report the measured ratio, not a boolean, so 2.47 reads as "half of what
    # it needs" rather than just "failed".
    assert 2.0 < low[0]["measured"]["contrast"] < 3.0
    assert low[0]["measured"]["required"] == 4.5


def test_post_fix_contrast_of_seven_is_silent():
    """Post-fix: 7.13 / 7.58."""
    labels = [
        _el(
            classes=["text-slate-700"],
            text="Segment A · 41%",
            color_rgb=[51, 65, 85],
            bg_rgb=WHITE,
            font_px=13.0,
            font_weight=400.0,
        )
    ]
    result = visual_geometry(_capture(labels, defined=["text-slate-700"]))
    assert result["findings"] == []


def test_large_text_uses_the_three_to_one_floor():
    """WCAG AA is 3:1 for >=24px, or >=18.66px bold. A heading at 3.5:1 is
    compliant; the same colour on body text is not."""
    heading = _el(
        classes=["text-3xl"],
        text="Programme measurement",
        color_rgb=[128, 128, 128],
        bg_rgb=WHITE,
        font_px=30.0,
        font_weight=400.0,
    )
    body = dict(heading, path="p.body", font_px=14.0, text="Programme measurement detail")
    assert visual_geometry(_capture([heading], defined=["text-3xl"]))["findings"] == []
    assert [f["kind"] for f in visual_geometry(_capture([body]))["findings"]] == ["low-contrast"]
    assert required_contrast(30.0, 400) == 3.0
    assert required_contrast(19.0, 700) == 3.0
    assert required_contrast(19.0, 400) == 4.5


def test_contrast_ratio_matches_the_wcag_reference_points():
    assert round(contrast_ratio([0, 0, 0], WHITE), 2) == 21.0
    assert round(contrast_ratio(WHITE, WHITE), 2) == 1.0
    assert round(contrast_ratio([119, 119, 119], WHITE), 2) == 4.48  # the classic near-miss


def test_text_over_a_background_image_is_not_guessed_at():
    """Contrast against an image is not a number this lens can produce.
    Saying nothing beats saying something wrong."""
    element = _el(
        text="Overlaid caption",
        color_rgb=[240, 240, 240],
        bg_rgb=WHITE,
        bg_has_image=True,
        font_px=14.0,
        font_weight=400.0,
    )
    assert visual_geometry(_capture([element]))["findings"] == []


# ---------------------------------------------------------------------------
# Defect 3 — a pay-affecting `text-rose-700` rendering near-black
# ---------------------------------------------------------------------------


def test_purged_text_colour_falling_back_to_inherited_is_flagged():
    """`text-rose-700` styles 'consent 89.7% - below the 90% floor', the only
    pay-affecting figure on the page. Purged, it fell to the inherited
    oklch(0.145 0 0). A computed-style probe returns a plausible colour here,
    which is exactly why this survived visual inspection — so the check is
    'no enumerated rule defines it' AND 'the colour is the inherited one'."""
    element = _el(
        path="p.consent>span",
        classes=["text-rose-700", "font-medium"],
        text="consent 89.7% - below the 90% floor",
        color_rgb=NEAR_BLACK,
        bg_rgb=WHITE,
        font_px=14.0,
        font_weight=500.0,
        color_is_inherited=True,
    )
    result = visual_geometry(_capture([element], defined=["font-medium"]))
    inert = [f for f in result["findings"] if f["kind"] == "inert-utility"]
    assert len(inert) == 1
    assert inert[0]["classes"] == ["text-rose-700"]
    assert "inherited value" in inert[0]["detail"]


def test_a_colour_utility_that_took_effect_is_silent():
    """Same element, `text-rose-700` present in the bundle: the colour differs
    from its parent's and a rule defines it. Nothing to report."""
    element = _el(
        classes=["text-rose-700"],
        text="consent 89.7% - below the 90% floor",
        color_rgb=[190, 18, 60],
        bg_rgb=WHITE,
        font_px=14.0,
        font_weight=500.0,
        color_is_inherited=False,
    )
    assert visual_geometry(_capture([element], defined=["text-rose-700"]))["findings"] == []


# ---------------------------------------------------------------------------
# The false positives the original probe emitted — method note 2
# ---------------------------------------------------------------------------


def test_the_four_documented_blind_spots_stay_silent():
    """`space-y-1`, `space-y-6`, `list-disc` and `mx-auto` read zero
    rendering-diff on the same page while genuinely present. A naive
    'no diff => broken' rule emitted four false positives on one file. A lens
    that cries wolf gets ignored, which costs more than the check is worth."""
    elements = [
        _el(path="div.stack", classes=["space-y-1"], h=0.0),
        _el(path="div.stack2", classes=["space-y-6"], h=0.0),
        _el(path="ul.notes", classes=["list-disc"], h=0.0),
        _el(path="div.wrap", classes=["mx-auto"], h=0.0),
    ]
    result = visual_geometry(_capture(elements, defined=[]))
    assert result["findings"] == [], result["findings"]
    assert result["verdict"] == "pass"


def test_the_carve_out_is_load_bearing_on_a_class_that_would_otherwise_fire():
    """`space-y-*`, `list-disc` and `mx-auto` are silent for TWO independent
    reasons here: the carve-out, and the fact that none of them is a height or
    colour utility, so the symptom half of `inert-utility` can never match. That
    is the enumerated-rules design doing what the issue predicted it would.

    `divide-y-*` and `sr-only` are the two that genuinely need the list, and
    they are the ones asserted here — otherwise the carve-out could be deleted
    with every test still green, which is the same vacuity trap this lens
    exists to catch."""
    # divide-y-4 matches the colour-utility pattern, so without the carve-out
    # the symptom (an inherited colour) makes it a finding.
    divider = _el(
        path="ul.rows",
        classes=["divide-y-4"],
        text="row",
        color_rgb=NEAR_BLACK,
        bg_rgb=WHITE,
        font_px=14.0,
        font_weight=400.0,
        color_is_inherited=True,
    )
    assert visual_geometry(_capture([divider], defined=[]))["findings"] == []

    # sr-only text is deliberately removed from the visual layout — zero height
    # is correct, and without the carve-out every one is a zero-height finding.
    screen_reader = _el(
        path="span.sr-only",
        classes=["sr-only"],
        text="Sorted ascending",
        w=1.0,
        h=0.0,
        color_rgb=NEAR_BLACK,
        bg_rgb=WHITE,
        font_px=14.0,
        font_weight=400.0,
    )
    assert visual_geometry(_capture([screen_reader], defined=["sr-only"]))["findings"] == []


def test_every_blind_spot_carries_its_reason():
    """The carve-out list is only defensible if each entry says why."""
    for name, reason in BLIND_SPOT_UTILITIES.items():
        assert len(reason.split()) >= 5, f"{name} has no real justification"
    assert is_blind_spot("space-y-6") is not None   # prefix match
    assert is_blind_spot("list-disc") is not None   # exact match
    assert is_blind_spot("h-28") is None            # the real defect is not carved out


def test_deliberately_hidden_elements_are_not_defects():
    """A closed Alpine modal, an x-cloak panel, display:none — every such
    element on a real page is zero-height and none of them is a defect. Without
    this carve-out the lens reports dozens of findings per scene."""
    hidden = [
        _el(path="div.modal", classes=["h-28"], h=0.0, display="none"),
        _el(path="div.panel", classes=["h-28"], h=0.0, visibility="hidden"),
        _el(path="div.fade", classes=["h-28"], h=0.0, opacity="0"),
        _el(path="div.a11y", classes=["h-28"], h=0.0, aria_hidden=True),
    ]
    assert visual_geometry(_capture(hidden))["findings"] == []


def test_an_empty_layout_wrapper_is_not_a_defect():
    """Zero on both axes, no text, no size assertion — ordinary layout."""
    assert visual_geometry(_capture([_el(path="div.spacer", classes=["flex"], w=0.0, h=0.0)]))["findings"] == []


def test_unreadable_stylesheets_report_undecidable_not_a_flood():
    """Leaflet and Mapbox CSS come from a CDN; the browser refuses to read
    their rules. If the enumerated set is empty for THAT reason, every class on
    the page looks undefined. Say so once, rather than emit hundreds of
    findings that are all wrong."""
    elements = [
        _el(path=f"div.leaflet-{i}", classes=[f"leaflet-pane-{i}", "h-28"], h=0.0)
        for i in range(50)
    ]
    result = visual_geometry(_capture(elements, defined=[], readable=False))
    inert = [f for f in result["findings"] if f["kind"] == "inert-utility"]
    undecidable = [f for f in result["findings"] if f["kind"] == "undecidable-utilities"]
    assert inert == []
    assert len(undecidable) == 1


def test_js_hook_classes_are_not_reported_as_missing_utilities():
    """`js-*`, `x-*`, `is-active` and friends are behaviour markers, never
    styling. Reporting them is the same cry-wolf failure from the other side."""
    elements = [
        _el(path="div.hook", classes=["js-toggle", "x-cloak", "is-active", "h-28"], h=0.0),
    ]
    result = visual_geometry(_capture(elements, defined=[]))
    reported = {c for f in result["findings"] if f["kind"] == "inert-utility" for c in f["classes"]}
    assert reported == {"h-28"}


# ---------------------------------------------------------------------------
# The gap this lens exists to close
# ---------------------------------------------------------------------------


def test_the_text_lenses_are_structurally_blind_to_all_three_defects():
    """The load-bearing claim of canopy#525: `data_fidelity` scored 9/9 and
    `narrated_numbers` 9/9 on the very page carrying these defects. Assert
    that directly, so it is a measured fact rather than a story in a docstring
    — and so a future change that makes a text lens catch one of these is
    noticed here rather than assumed."""
    # The page text is IDENTICAL whether the bar is 112px or 0px tall, and
    # whether the label is legible or white-on-white. That is the whole point.
    page_text = (
        "Weekly visits\n"
        "Week 1\t31\nWeek 2\t44\nWeek 3\t52\nWeek 4\t47\n"
        "Week 5\t61\nWeek 6\t58\nWeek 7\t66\nWeek 8\t70\n"
        "consent 89.7% - below the 90% floor\n"
    )
    assert data_fidelity(page_text)["verdict"] == "pass"
    assert 89.7 in page_values(page_text)

    broken = _capture(
        [
            _el(path="div.bar", classes=["h-28"], w=18.0, h=0.0),
            _el(
                path="span.label",
                classes=["text-white"],
                text="Segment A - 41%",
                color_rgb=CONTRAST_2_47,
                bg_rgb=WHITE,
                font_px=13.0,
                font_weight=400.0,
            ),
            _el(
                path="span.consent",
                classes=["text-rose-700"],
                text="consent 89.7% - below the 90% floor",
                color_rgb=NEAR_BLACK,
                bg_rgb=WHITE,
                font_px=14.0,
                font_weight=500.0,
                color_is_inherited=True,
            ),
        ],
        defined=["text-white"],
    )
    kinds = {f["kind"] for f in visual_geometry(broken)["findings"]}
    assert kinds == {"zero-height", "low-contrast", "inert-utility"}
