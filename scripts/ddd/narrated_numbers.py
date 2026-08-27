"""Every number the narration speaks must be readable off the screen.

"Kukawa is eleven days out" survived three iterations of a judged loop. The
screen said 1.4 weeks, then 0 days, then 11 — and only the third was the number
being spoken. Each round a judge had to notice it again, and twice it was the
finding that capped the scene.

That check is mechanical. A narration is a short string; the captured page text
is another; a number spoken over a screen that does not contain it is a defect
the moment both artifacts exist. No LLM, no judgment, no variance.

The rule is deliberately forgiving in the two places where being strict would
produce noise:

  * **Spelled-out numbers count, on BOTH sides.** "eleven" and "11" are the
    same claim, and a narration written for the ear spells small numbers out —
    as does prose UI copy ("the three Layer C rules"). The same extractor runs
    over the narration and over the captured page text, so neither side can
    read a number the other cannot.
  * **Approximations count.** A narration saying "about twelve thousand" over a
    screen reading "12,058" is honest rounding, not a mismatch. Anything within
    a small tolerance of a figure on screen passes.

What it will not forgive is a number with no neighbour on screen at all.

    python -m scripts.ddd.narrated_numbers <run_dir> <recipe.yaml> [--json]
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

# Rounding tolerance. "about twelve thousand" over 12,058 is fine; a claim of
# 11 over a screen reading 9 is not.
TOLERANCE = 0.06

WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
SCALES = {"hundred": 100, "thousand": 1_000, "million": 1_000_000}

# Numbers that are almost never a data claim: ordinals in dates, years, and the
# small counts that show up in prose ("two stages", "a second look").
_IGNORE_BELOW = 3

_DIGITS = re.compile(r"\b\d[\d,]*(?:\.\d+)?\b")
_WORD_RUN = re.compile(
    r"\b(?:(?:" + "|".join(WORDS) + r"|" + "|".join(SCALES) + r"|and)[\s-]*)+\b",
    re.IGNORECASE,
)


def _word_value(phrase: str) -> float | None:
    """'twelve thousand' -> 12000. Returns None if the run holds no number."""
    total = 0.0
    running = 0.0
    saw = False
    for token in re.split(r"[\s-]+", phrase.strip().lower()):
        if not token or token == "and":
            continue
        if token in WORDS:
            running += WORDS[token]
            saw = True
        elif token in SCALES:
            scale = SCALES[token]
            if scale >= 1000:
                total += (running or 1) * scale
                running = 0.0
            else:
                running = (running or 1) * scale
            saw = True
        else:
            return None
    return (total + running) if saw else None


def _all_values(text: str) -> list[tuple[str, float]]:
    """(surface form, value) for every number in *text*, digits or words.

    One extractor, used for BOTH sides of the comparison. The two sides used to
    disagree — narration parsed words, the page did not — so a screen reading
    "the three Layer C rules" was invisible to a narration saying "Three".
    """
    out: list[tuple[str, float]] = []
    for match in _DIGITS.finditer(text or ""):
        raw = match.group(0)
        try:
            out.append((raw, float(raw.replace(",", ""))))
        except ValueError:
            continue
    for match in _WORD_RUN.finditer(text or ""):
        phrase = match.group(0).strip()
        value = _word_value(phrase)
        if value is not None:
            out.append((phrase, value))
    return out


def narrated_values(text: str) -> list[tuple[str, float]]:
    """(surface form, value) for every number the narration states."""
    return [(s, v) for s, v in _all_values(text) if v > 0 and v >= _IGNORE_BELOW]


def page_values(page_text: str) -> list[float]:
    """Every number READABLE off the screen — spelled out or in digits.

    Unfiltered on purpose: this is the candidate set a narrated claim is
    matched against, so a screen honestly printing 0 or 1 is a real neighbour.
    """
    return [v for _s, v in _all_values(page_text)]


def _supported(value: float, on_screen: list[float]) -> bool:
    for candidate in on_screen:
        if candidate == value:
            return True
        scale = max(abs(value), abs(candidate), 1.0)
        if abs(candidate - value) / scale <= TOLERANCE:
            return True
    return False


def check(run_dir: str | Path, recipe_path: str | Path) -> dict:
    """Compare each scene's narration against its own captured page text."""
    from scripts.ddd.spec_io import load_spec

    run = Path(run_dir)
    spec = load_spec(str(recipe_path))
    findings: list[dict[str, Any]] = []
    checked = 0

    for index, scene in enumerate(spec.scenes, start=1):
        narration = (getattr(scene, "narrative", "") or "").strip()
        if not narration:
            continue
        capture = run / "snapshots" / f"scene_{index}_page_text.json"
        if not capture.exists():
            capture = run / f"scene_{index}_page_text.json"
        if not capture.exists():
            continue

        raw = json.loads(capture.read_text())
        on_screen = page_values(raw.get("page_text", "") if isinstance(raw, dict) else str(raw))

        for surface, value in narrated_values(narration):
            checked += 1
            if not _supported(value, on_screen):
                findings.append(
                    {
                        "scene": index,
                        "scene_id": getattr(scene, "id", None),
                        "narrated": surface,
                        "value": value,
                        "detail": (
                            f"the narration says '{surface}' but no figure within "
                            f"{int(TOLERANCE * 100)}% of {value:g} appears anywhere in "
                            "this scene's captured page text"
                        ),
                    }
                )

    return {
        "numbers_checked": checked,
        "unsupported": len(findings),
        "findings": findings,
        "verdict": "pass" if not findings else "fail",
    }


def _cli() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    result = check(args[0], args[1])
    if "--json" in sys.argv:
        print(json.dumps(result, indent=1))
    else:
        print(f"narrated-numbers: {result['verdict']} ({result['numbers_checked']} checked)")
        for finding in result["findings"]:
            print(f"  scene {finding['scene']} ({finding['scene_id']}): '{finding['narrated']}'")
            print(f"      {finding['detail']}")
    return 0 if result["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
