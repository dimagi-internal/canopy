"""Catch a rendered element that is invisible while every text lens reads green.

Every other per-iteration lens in this pipeline reads TEXT — the captured page
text, the narration, the extracted numbers. None of them can tell that the
element carrying a claim rendered at zero height, or that its label is white on
white. On the ACE run ``bednet-check-2-visit/20260825-1310`` three separate
invisible-element defects scored ``data_fidelity`` 9/9 and ``narrated_numbers``
9/9 across four iterations, and were only found by a hand-written probe after
the loop had already given up (canopy#525):

  * twelve weekly chart bars at 0px — ``h-28`` was purged from the bundle
  * segment bar labels at contrast 2.47 — effectively white on white
  * a pay-affecting ``text-rose-700`` falling back to near-black

All three are in the iteration-0 frames. All three are deterministic. None of
them needs an LLM, and paying a judge round to *not* find them is the most
expensive possible outcome.

Input is ``scene_<N>_visual.json``, written next to the PNG by the walkthrough
recorder (``scripts/walkthrough/_lib/orchestrator.take_snapshot``) from the page
it already has open. This module is pure — it never touches a browser — so the
rules are unit-testable against fixtures.

    python -m scripts.ddd.visual_geometry <run_dir|snapshots_dir|visual.json> [--json]

## The two method notes that make this lens work rather than cry wolf

**1. For "does this utility exist", check the ENUMERATED CSS RULES, not the
computed style.** ``text-*`` and ``border-*`` default to ``currentColor``, so a
computed-style probe returns a plausible colour for a utility that does not
exist — which is exactly why the rose-700 defect survived visual inspection for
months. The capture walks ``document.styleSheets`` and reports the class names
any readable rule actually defines; ``inert_utility`` reads that set.

**2. Some elements are probe blind spots, not misses.** ``space-y-*`` targets
``:not(:last-child)``, ``list-disc``'s value is the CSS initial, and
``mx-auto`` computes to ``0px`` on a full-width block — all three read as "no
rendering difference" while genuinely present. A naive "no diff ⇒ broken" rule
emitted four false positives on a single page of the run that motivated this.
They are carved out by name in ``BLIND_SPOT_UTILITIES`` below, with the reason,
and ``tests/ddd/test_visual_geometry.py`` asserts each one stays silent.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

# WCAG 2.1 AA. 4.5:1 for body text; 3:1 for "large" text, which the spec defines
# as >=18pt (24px) or >=14pt (18.66px) bold. The measured defect was 2.47 —
# roughly half of what body text needs — so nothing here is borderline.
CONTRAST_AA_NORMAL = 4.5
CONTRAST_AA_LARGE = 3.0
LARGE_TEXT_PX = 24.0
LARGE_TEXT_BOLD_PX = 18.66
BOLD_WEIGHT = 700

# Sub-pixel heights are a rounding artifact of a layout that IS working
# (a 1px rule, a hairline border). Zero is the defect.
ZERO_HEIGHT_PX = 0.5

# Utilities that assert a box has a height. When one of these is on an element
# that renders at 0px, the utility did not take — that is the h-28 case.
_HEIGHT_ASSERTING = re.compile(r"^(?:min-)?h-(?!auto$|full$|screen$|fit$|min$|max$)\S+$|^aspect-\S+$")

# Utilities whose whole job is a colour. When one of these is on an element
# whose computed colour equals what it would have inherited anyway, the utility
# did not take — that is the text-rose-700 case.
_COLOUR_ASSERTING = re.compile(r"^(?:text|bg|border|ring|decoration|divide|from|via|to)-\S+$")

# Method note 2. Present, correct, and producing no observable rendering
# difference on their own — a lens that reports these is a lens that gets
# ignored. Keyed by the exact class or a prefix; the reason is the point.
BLIND_SPOT_UTILITIES: dict[str, str] = {
    "space-y-": "targets :not(:last-child); a one-child stack shows no difference",
    "space-x-": "targets :not(:last-child); a one-child stack shows no difference",
    "divide-y-": "targets :not(:last-child); a one-child stack shows no difference",
    "list-disc": "its value is the CSS initial for <ul>, so it is indistinguishable from unset",
    "list-inside": "affects marker position only; invisible without a marker",
    "mx-auto": "computes to 0px on a full-width block, which is the common case",
    "my-auto": "computes to 0px outside a flex/grid context",
    "sr-only": "deliberately removed from the visual layout — zero size is correct",
}

# Class-name conventions that are not styling at all. Reporting these as a
# missing utility is the same cry-wolf failure from the other direction.
_NON_STYLING_CLASS = re.compile(
    r"^(?:js-|is-|has-|x-|v-|ng-|data-|test-|e2e-|qa-)"
    r"|^(?:active|open|selected|disabled|hidden|show|collapsed)$"
)


# ---------------------------------------------------------------------------
# Colour maths (pure; the capture resolves every colour to an sRGB triple)
# ---------------------------------------------------------------------------


def _channel_luminance(value: int) -> float:
    c = value / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb: Iterable[float]) -> float:
    """WCAG 2.1 relative luminance of an sRGB triple."""
    r, g, b = (_channel_luminance(int(round(v))) for v in list(rgb)[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg: Iterable[float], bg: Iterable[float]) -> float:
    """WCAG 2.1 contrast ratio between two sRGB triples. 1.0 .. 21.0."""
    a, b = relative_luminance(fg), relative_luminance(bg)
    lighter, darker = (a, b) if a >= b else (b, a)
    return (lighter + 0.05) / (darker + 0.05)


def required_contrast(font_px: float, font_weight: float) -> float:
    """The AA floor this text has to clear, given its size and weight."""
    try:
        px = float(font_px or 0)
        weight = float(font_weight or 400)
    except (TypeError, ValueError):
        return CONTRAST_AA_NORMAL
    if px >= LARGE_TEXT_PX or (px >= LARGE_TEXT_BOLD_PX and weight >= BOLD_WEIGHT):
        return CONTRAST_AA_LARGE
    return CONTRAST_AA_NORMAL


# ---------------------------------------------------------------------------
# Element predicates
# ---------------------------------------------------------------------------


def is_blind_spot(class_name: str) -> str | None:
    """Return the documented reason this class is unreportable, or None."""
    if class_name in BLIND_SPOT_UTILITIES:
        return BLIND_SPOT_UTILITIES[class_name]
    for key, reason in BLIND_SPOT_UTILITIES.items():
        if key.endswith("-") and class_name.startswith(key):
            return reason
    return None


def _is_rendered(element: dict) -> bool:
    """Is this element in the visual layout at all?

    An element the page deliberately hides — a closed Alpine modal, an
    ``x-cloak`` panel, ``display:none`` — is not a defect, and every such
    element on a page like this one would otherwise report zero height. This
    carve-out is what keeps the geometry check usable on a real app.
    """
    if element.get("display") == "none":
        return False
    if element.get("visibility") in {"hidden", "collapse"}:
        return False
    try:
        if float(element.get("opacity", 1)) <= 0.01:
            return False
    except (TypeError, ValueError):
        pass
    return not element.get("aria_hidden")


def _text_of(element: dict) -> str:
    return (element.get("text") or "").strip()


def _narration_hits(text: str, narration: str) -> bool:
    """Does the narration reference this element's text?

    Deliberately crude and conservative: a numeric token or a >=4-character word
    shared with the narration. A false 'narrated' only raises a finding's
    severity — it never invents one.
    """
    if not text or not narration:
        return False
    lowered = narration.lower()
    for token in re.findall(r"[\d][\d,.%]*|[A-Za-z]{4,}", text):
        if token.lower() in lowered:
            return True
    return False


# ---------------------------------------------------------------------------
# The three checks
# ---------------------------------------------------------------------------


def check_geometry(capture: dict, narration: str = "") -> list[dict[str, Any]]:
    """Elements that are in the layout, carry something, and render at 0px high."""
    findings: list[dict[str, Any]] = []
    for element in capture.get("elements", []):
        if not _is_rendered(element):
            continue
        height = float(element.get("h") or 0)
        width = float(element.get("w") or 0)
        if height > ZERO_HEIGHT_PX:
            continue
        text = _text_of(element)
        height_classes = [c for c in element.get("classes", []) if _HEIGHT_ASSERTING.match(c)]
        blind = next((is_blind_spot(c) for c in element.get("classes", []) if is_blind_spot(c)), None)
        if blind:
            continue
        if not text and not height_classes:
            # A zero-height wrapper with nothing in it and no height utility is
            # ordinary layout, not a defect.
            continue
        if width <= ZERO_HEIGHT_PX and not height_classes:
            # Zero on BOTH axes with no size assertion is an empty node.
            continue
        narrated = _narration_hits(text, narration)
        findings.append(
            {
                "kind": "zero-height",
                "narrated": narrated,
                "selector": element.get("path", ""),
                "classes": element.get("classes", []),
                "measured": {"w": width, "h": height},
                "detail": (
                    f"{element.get('path', 'element')} renders {width:.0f}x{height:.0f}px"
                    + (f" while carrying {', '.join(height_classes)}" if height_classes else "")
                    + (f" and the text {text[:60]!r}" if text else "")
                    + ". Every text lens still reads it as present."
                ),
            }
        )
    return findings


def check_contrast(capture: dict, narration: str = "") -> list[dict[str, Any]]:
    """Text that does not clear its WCAG AA floor against its own background."""
    findings: list[dict[str, Any]] = []
    for element in capture.get("elements", []):
        text = _text_of(element)
        if not text or not _is_rendered(element):
            continue
        fg = element.get("color_rgb")
        bg = element.get("bg_rgb")
        if not fg or not bg:
            continue
        if element.get("bg_has_image"):
            # Contrast against an image is not a number this lens can produce.
            # Saying nothing beats saying something wrong.
            continue
        floor = required_contrast(element.get("font_px"), element.get("font_weight"))
        ratio = contrast_ratio(fg, bg)
        if ratio >= floor:
            continue
        findings.append(
            {
                "kind": "low-contrast",
                "narrated": _narration_hits(text, narration),
                "selector": element.get("path", ""),
                "classes": element.get("classes", []),
                # Report the measured ratio, not a boolean, so a 2.47 is legible
                # as "half of what it needs" rather than just "failed".
                "measured": {"contrast": round(ratio, 2), "required": floor},
                "detail": (
                    f"{text[:60]!r} measures {ratio:.2f}:1 against its own computed "
                    f"background, below the {floor}:1 AA floor. Text presence and text "
                    "legibility are different properties; every text lens sees only the first."
                ),
            }
        )
    return findings


def check_inert_utilities(capture: dict) -> list[dict[str, Any]]:
    """Styling utilities that no readable CSS rule defines AND that show the symptom.

    Two signals, deliberately. The enumerated-rules half alone would fire on
    every class from a cross-origin stylesheet the browser refuses to read
    (leaflet, mapbox), so a class is only reported when the page ALSO shows the
    degradation the missing utility would cause: a height utility on a 0px box,
    or a colour utility on an element whose colour is exactly what it would have
    inherited anyway.
    """
    defined = set(capture.get("defined_class_selectors") or [])
    if not capture.get("stylesheets_readable"):
        # Nothing was readable — the enumerated set is empty for the wrong
        # reason, so every class would look undefined. Say we could not decide.
        return [
            {
                "kind": "undecidable-utilities",
                "narrated": False,
                "selector": "",
                "classes": [],
                "measured": {"unreadable_stylesheets": capture.get("unreadable_stylesheets", 0)},
                "detail": (
                    "No stylesheet's rules were readable, so which utilities exist "
                    "could not be enumerated. Reporting nothing rather than "
                    "reporting every class as missing."
                ),
            }
        ]

    findings: list[dict[str, Any]] = []
    for element in capture.get("elements", []):
        if not _is_rendered(element):
            continue
        height = float(element.get("h") or 0)
        for class_name in element.get("classes", []):
            if class_name in defined:
                continue
            if is_blind_spot(class_name) or _NON_STYLING_CLASS.search(class_name):
                continue
            symptom = None
            if _HEIGHT_ASSERTING.match(class_name) and height <= ZERO_HEIGHT_PX:
                symptom = f"the box renders {height:.0f}px high"
            elif _COLOUR_ASSERTING.match(class_name) and element.get("color_is_inherited"):
                symptom = "the computed colour is exactly the inherited value"
            if not symptom:
                continue
            findings.append(
                {
                    "kind": "inert-utility",
                    "narrated": False,
                    "selector": element.get("path", ""),
                    "classes": [class_name],
                    "measured": {"h": height},
                    "detail": (
                        f"{class_name!r} is on {element.get('path', 'an element')} but no "
                        f"enumerated CSS rule defines it, and {symptom}. A purged utility "
                        "degrades to the unstyled baseline rather than erroring, so nothing "
                        "in the render, the console, or any text judge observes it."
                    ),
                }
            )
    return findings


# ---------------------------------------------------------------------------
# Lens entry point
# ---------------------------------------------------------------------------


def visual_geometry(capture: dict, narration: str = "") -> dict:
    """Run all three checks over one scene's visual capture."""
    findings = (
        check_geometry(capture, narration)
        + check_contrast(capture, narration)
        + check_inert_utilities(capture)
    )
    # A zero-height element the narration is actively talking about is a hard
    # fail — the frame does not show what is being said about it. Everything
    # else is a warning the loop folds in with the judges' findings.
    hard = [f for f in findings if f["narrated"] and f["kind"] == "zero-height"]
    verdict = "fail" if hard else ("warn" if findings else "pass")
    return {
        "scene_index": capture.get("scene_index"),
        "elements_scanned": len(capture.get("elements", [])),
        "findings": findings,
        "verdict": verdict,
    }


def _narration_for(scene_index: Any, spec: dict | None) -> str:
    if not spec or scene_index is None:
        return ""
    scenes = spec.get("scenes") or []
    try:
        scene = scenes[int(scene_index) - 1]
    except (ValueError, TypeError, IndexError):
        return ""
    parts = [scene.get("narration") or "", scene.get("voiceover") or "", scene.get("say") or ""]
    return " ".join(p for p in parts if p)


def _capture_paths(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    for directory in (target, target / "snapshots"):
        found = sorted(directory.glob("scene_*_visual.json"))
        if found:
            return found
    return []


def _cli() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__, file=sys.stderr)
        return 2

    paths = _capture_paths(Path(args[0]))
    spec = None
    if len(args) > 1:
        try:
            import yaml

            spec = yaml.safe_load(Path(args[1]).read_text())
        except Exception:  # noqa: BLE001 — narration is an enrichment, not a requirement
            spec = None

    if not paths:
        # An honest "nothing to judge" rather than a green that means nothing.
        result = {
            "findings": [],
            "verdict": "skip",
            "reason": (
                "no scene_<N>_visual.json captures found — this run predates the "
                "geometry/contrast capture, or the recorder could not write it"
            ),
        }
        print(json.dumps(result, indent=1) if "--json" in sys.argv else f"visual-geometry: skip ({result['reason']})")
        return 0

    all_findings: list[dict[str, Any]] = []
    verdicts: list[str] = []
    for path in paths:
        capture = json.loads(path.read_text())
        result = visual_geometry(capture, _narration_for(capture.get("scene_index"), spec))
        verdicts.append(result["verdict"])
        for finding in result["findings"]:
            finding["source"] = path.name
            all_findings.append(finding)

    verdict = "fail" if "fail" in verdicts else ("warn" if all_findings else "pass")
    if "--json" in sys.argv:
        print(json.dumps({"findings": all_findings, "verdict": verdict}, indent=1))
    else:
        print(f"visual-geometry: {verdict} ({len(all_findings)} findings over {len(paths)} scenes)")
        for finding in all_findings:
            flag = " NARRATED" if finding.get("narrated") else ""
            print(f"  [{finding['kind']}{flag}] {finding.get('source', '')} {finding.get('measured', '')}")
            print(f"      {finding['detail']}")
    # Non-zero only on a hard fail, matching the other deterministic lenses:
    # a warn folds in with the judges' findings rather than stopping the run.
    return 1 if verdict == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(_cli())
