"""Recorded-walkthrough style: N short standalone cuts, no gloss.

The default narrated video (``style: explainer``) is ONE long arc: a title
card, every scene back to back, the brand end card, and a music bed under it
all. ``style: recorded`` is a different product — "a very well produced
version of *I just recorded myself walking through this for someone*" (Jon
Jackson, 2 Oct 2026; tightened by Sagar Atre's brief, 7 Oct 2026):

* no music, no title or end cards, no zooms / callouts / lower-thirds /
  captions added afterwards;
* each cut opens on a live screen, and its first spoken line is
  "This is a quick overview of how we <do X>.";
* each cut stands alone — 30 s target, 40 s hard ceiling, ~65–80 spoken words,
  first person, plain English;
* the cursor moves where a person's would, and every sentence describes
  something visible on screen.

A spec opts in with ``style: recorded`` plus a ``cuts:`` list (see
``scripts.narrative.models.Cut``). This module owns the rules every consumer
shares, all PURE except :func:`probe_duration`:

* :func:`resolve_cuts` — cuts → ordered scene indexes (raises on a broken list).
* :func:`lint_recorded_spec` — pre-render checks: structural errors, the opener
  line, the 65–80 word band and the estimated length.
* :func:`gate_cut_durations` — post-render timing gate on the real mp4 lengths:
  warn above 30 s, fail above 40 s.

CLI::

    python -m scripts.ddd.recorded lint <spec>
    python -m scripts.ddd.recorded gate --cut <id>=<output.mp4> [...] [--out verdict-recorded.json]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

STYLE_EXPLAINER = "explainer"
STYLE_RECORDED = "recorded"
STYLES = (STYLE_EXPLAINER, STYLE_RECORDED)

#: Every cut's first spoken sentence starts with exactly this.
OPENER = "This is a quick overview of how we"

#: Cut length: aim for TARGET, never exceed CEILING (seconds of rendered video).
TARGET_SECONDS = 30.0
CEILING_SECONDS = 40.0

#: Spoken-word band for one cut (a warning band, not a hard rule).
WORDS_MIN = 65
WORDS_MAX = 80

#: The ElevenLabs voice speaks ~2.6 words/second (snippets.py uses the same rate).
WORDS_PER_SECOND = 2.6

#: Footage playback-rate clamp for the action↔word warp in recorded cuts. The
#: explainer allows 0.7–2.5x, and at 2.5x the cursor darts like nobody's hand;
#: a recorded cut stays within a range a viewer reads as real time. Mirrored in
#: video-engine/src/lib/style.ts (RECORDED_RATE_MIN / RECORDED_RATE_MAX).
RECORDED_RATE_MIN = 0.85
RECORDED_RATE_MAX = 1.35


class RecordedSpecError(ValueError):
    """A ``style: recorded`` spec whose cuts cannot be resolved."""


# --------------------------------------------------------------------------- #
# spec helpers (accept a raw dict OR a pydantic UnifiedSpec)
# --------------------------------------------------------------------------- #


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def spec_style(spec: Any) -> str:
    """The spec's video style; absent/empty → ``explainer``."""
    return str(_get(spec, "style") or STYLE_EXPLAINER)


def is_recorded(spec: Any) -> bool:
    return spec_style(spec) == STYLE_RECORDED


def _scene_text(narrative: Any) -> str:
    if isinstance(narrative, list):
        return " ".join(str(x).strip() for x in narrative if str(x).strip())
    return str(narrative or "").strip()


def word_count(text: str) -> int:
    """Spoken words, counted the way snippets.py counts them."""
    return len(re.findall(r"[A-Za-z0-9']+", text or ""))


def first_sentence(text: str) -> str:
    """The first sentence of ``text`` (up to and including the first . ! or ?)."""
    t = " ".join((text or "").split())
    m = re.search(r"[.!?](\s|$)", t)
    return t[: m.end()].strip() if m else t


def _normalize(text: str) -> str:
    t = (text or "").replace("’", "'").replace("‘", "'")
    return " ".join(t.split()).lower()


def opener_ok(text: str) -> bool:
    """True when ``text`` opens with "This is a quick overview of how we <X>."

    Case-insensitive and whitespace-tolerant; requires something after "we" and
    a sentence end, so a bare "This is a quick overview of how we." fails.
    """
    first = _normalize(first_sentence(text))
    stem = _normalize(OPENER)
    if not first.startswith(stem + " "):
        return False
    rest = first[len(stem) :].strip()
    return bool(re.match(r"^[a-z0-9'].*[.!?]$", rest)) and word_count(rest) >= 1


# --------------------------------------------------------------------------- #
# cuts
# --------------------------------------------------------------------------- #


def resolve_cuts(spec: Any) -> list[dict[str, Any]]:
    """Resolve the spec's ``cuts`` to scene positions.

    Returns one dict per cut, in declared order::

        {"id", "title", "scene_ids": [...], "scene_indexes": [1-based...]}

    ``scene_indexes`` are 1-based to match the recorder's run report
    (``scenes[].scene_index``). Raises :class:`RecordedSpecError` listing every
    problem (no cuts, duplicate cut id, empty cut, unknown scene id, a scene in
    two cuts) — a cut list that half-resolves would render the wrong video.
    """
    scenes = list(_get(spec, "scenes") or [])
    index_by_id: dict[str, int] = {}
    for i, sc in enumerate(scenes, start=1):
        sid = str(_get(sc, "id") or "").strip()
        if sid:
            index_by_id.setdefault(sid, i)

    raw_cuts = list(_get(spec, "cuts") or [])
    problems: list[str] = []
    if not raw_cuts:
        problems.append("style: recorded needs a non-empty `cuts:` list")
    out: list[dict[str, Any]] = []
    seen_cut: set[str] = set()
    owner: dict[str, str] = {}
    for n, c in enumerate(raw_cuts, start=1):
        cid = str(_get(c, "id") or "").strip()
        if not cid:
            problems.append(f"cut #{n} has no id")
            cid = f"cut-{n}"
        if cid in seen_cut:
            problems.append(f"cut id '{cid}' is used twice")
        seen_cut.add(cid)
        ids = [str(x).strip() for x in (_get(c, "scenes") or []) if str(x).strip()]
        if not ids:
            problems.append(f"cut '{cid}' lists no scenes")
        idxs: list[int] = []
        for sid in ids:
            if sid not in index_by_id:
                problems.append(f"cut '{cid}': unknown scene id '{sid}'")
                continue
            if sid in owner and owner[sid] != cid:
                problems.append(
                    f"scene '{sid}' is in cut '{owner[sid]}' and cut '{cid}' — "
                    "each cut stands alone, so a scene belongs to one cut"
                )
            owner.setdefault(sid, cid)
            idxs.append(index_by_id[sid])
        out.append(
            {
                "id": cid,
                "title": str(_get(c, "title") or "").strip() or cid,
                "scene_ids": ids,
                "scene_indexes": idxs,
            }
        )
    if problems:
        raise RecordedSpecError("; ".join(problems))
    return out


def cut_narration(spec: Any, cut: dict[str, Any]) -> str:
    """The cut's full spoken text: its scenes' narratives, in cut order."""
    scenes = list(_get(spec, "scenes") or [])
    parts = []
    for idx in cut["scene_indexes"]:
        parts.append(_scene_text(_get(scenes[idx - 1], "narrative")))
    return " ".join(p for p in parts if p)


def estimate_seconds(words: int) -> float:
    return round(words / WORDS_PER_SECOND, 1)


def lint_recorded_spec(spec: Any) -> list[dict[str, str]]:
    """Pre-render checks for a ``style: recorded`` spec.

    Returns a list of ``{"level": "error"|"warn", "cut": id-or-"", "message"}``.
    Errors: the cut list does not resolve; a cut's first line is not the
    opener. Warnings: a cut's word count is outside 65–80; its estimated length
    (words / 2.6) is over the 30 s target. An estimated length over the 40 s
    ceiling is an ERROR — the render can only come out longer than the VO.
    A non-recorded spec returns ``[]``.
    """
    if not is_recorded(spec):
        return []
    issues: list[dict[str, str]] = []
    try:
        cuts = resolve_cuts(spec)
    except RecordedSpecError as e:
        return [{"level": "error", "cut": "", "message": str(e)}]

    for cut in cuts:
        text = cut_narration(spec, cut)
        cid = cut["id"]
        if not text:
            issues.append({"level": "error", "cut": cid, "message": "cut has no narration"})
            continue
        if not opener_ok(text):
            issues.append(
                {
                    "level": "error",
                    "cut": cid,
                    "message": (
                        f"first line must be '{OPENER} <do X>.' — got "
                        f"'{first_sentence(text)[:90]}'"
                    ),
                }
            )
        words = word_count(text)
        est = estimate_seconds(words)
        if not (WORDS_MIN <= words <= WORDS_MAX):
            issues.append(
                {
                    "level": "warn",
                    "cut": cid,
                    "message": f"{words} spoken words — aim for {WORDS_MIN}–{WORDS_MAX}",
                }
            )
        if est > CEILING_SECONDS:
            issues.append(
                {
                    "level": "error",
                    "cut": cid,
                    "message": (
                        f"~{est}s of voiceover is over the {CEILING_SECONDS:.0f}s ceiling "
                        "before any footage plays — cut words"
                    ),
                }
            )
        elif est > TARGET_SECONDS:
            issues.append(
                {
                    "level": "warn",
                    "cut": cid,
                    "message": f"~{est}s of voiceover — over the {TARGET_SECONDS:.0f}s target",
                }
            )
    return issues


# --------------------------------------------------------------------------- #
# post-render timing gate
# --------------------------------------------------------------------------- #


def classify_duration(seconds: float) -> str:
    """``pass`` ≤ 30 s, ``warn`` ≤ 40 s, ``fail`` above 40 s."""
    if seconds > CEILING_SECONDS:
        return "fail"
    if seconds > TARGET_SECONDS:
        return "warn"
    return "pass"


_RANK = {"pass": 0, "warn": 1, "fail": 2}


def gate_cut_durations(
    durations: dict[str, float | None], words: dict[str, int] | None = None
) -> dict[str, Any]:
    """The per-cut timing verdict for a rendered set of cuts.

    ``durations`` maps cut id → rendered seconds (``None`` = could not probe,
    which FAILS — an unmeasured cut is not a passing cut). ``words`` optionally
    maps cut id → spoken word count, adding a warn-only finding outside 65–80.
    The overall verdict is the worst cut's.
    """
    cuts: list[dict[str, Any]] = []
    findings: list[str] = []
    overall = "pass"
    for cid, secs in durations.items():
        if secs is None:
            verdict = "fail"
            findings.append(f"{cid}: rendered length could not be measured")
        else:
            verdict = classify_duration(float(secs))
            if verdict == "fail":
                findings.append(
                    f"{cid}: {secs:.1f}s is over the {CEILING_SECONDS:.0f}s ceiling"
                )
            elif verdict == "warn":
                findings.append(f"{cid}: {secs:.1f}s is over the {TARGET_SECONDS:.0f}s target")
        row: dict[str, Any] = {
            "cut": cid,
            "seconds": None if secs is None else round(float(secs), 2),
            "verdict": verdict,
        }
        if words and cid in words:
            w = int(words[cid])
            row["words"] = w
            if not (WORDS_MIN <= w <= WORDS_MAX):
                findings.append(f"{cid}: {w} spoken words (aim for {WORDS_MIN}–{WORDS_MAX})")
                if verdict == "pass":
                    verdict = row["verdict"] = "warn"
        cuts.append(row)
        if _RANK[verdict] > _RANK[overall]:
            overall = verdict
    return {
        "kind": "recorded_timing",
        "target_seconds": TARGET_SECONDS,
        "ceiling_seconds": CEILING_SECONDS,
        "words_band": [WORDS_MIN, WORDS_MAX],
        "verdict": overall if cuts else "fail",
        "cuts": cuts,
        "findings": findings if cuts else ["no cuts to gate"],
    }


def probe_duration(mp4: str | Path) -> float | None:
    """Rendered length in seconds via ffprobe; ``None`` on any failure."""
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(mp4),
            ],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        v = float(out)
        return v if v > 0 else None
    except Exception:  # noqa: BLE001 — a missing/garbled file reads as unmeasured
        return None


def format_gate(report: dict[str, Any]) -> str:
    lines = [
        f"Recorded-cut timing gate: {report['verdict'].upper()} "
        f"(target {report['target_seconds']:.0f}s, ceiling {report['ceiling_seconds']:.0f}s)"
    ]
    for row in report["cuts"]:
        secs = "  n/a " if row["seconds"] is None else f"{row['seconds']:5.1f}s"
        extra = f"  {row['words']} words" if "words" in row else ""
        lines.append(f"  {row['verdict']:<4}  {secs}  {row['cut']}{extra}")
    for f in report["findings"]:
        lines.append(f"  · {f}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _load_spec(path: str) -> dict[str, Any]:
    from scripts.ddd.spec_io import load_spec_raw

    return load_spec_raw(Path(path)) or {}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="scripts.ddd.recorded")
    sub = p.add_subparsers(dest="cmd", required=True)

    lint = sub.add_parser("lint", help="pre-render checks for a style: recorded spec")
    lint.add_argument("spec")

    gate = sub.add_parser("gate", help="timing gate over rendered cut mp4s")
    gate.add_argument("--cut", action="append", default=[], metavar="ID=MP4",
                      help="a rendered cut (repeatable)")
    gate.add_argument("--spec", default=None,
                      help="the unified spec — adds the per-cut word-count check")
    gate.add_argument("--out", default=None, help="write the verdict JSON here")

    args = p.parse_args(argv)

    if args.cmd == "lint":
        spec = _load_spec(args.spec)
        if not is_recorded(spec):
            print(f"{args.spec}: style is '{spec_style(spec)}' — nothing to lint")
            return 0
        issues = lint_recorded_spec(spec)
        try:
            for cut in resolve_cuts(spec):
                w = word_count(cut_narration(spec, cut))
                print(f"  {cut['id']}: {len(cut['scene_indexes'])} scene(s), {w} words, "
                      f"~{estimate_seconds(w)}s VO")
        except RecordedSpecError:
            pass
        for i in issues:
            where = f"{i['cut']}: " if i["cut"] else ""
            print(f"  {i['level'].upper():<5} {where}{i['message']}")
        errors = [i for i in issues if i["level"] == "error"]
        print(f"recorded lint: {'FAIL' if errors else 'pass'} "
              f"({len(errors)} error(s), {len(issues) - len(errors)} warning(s))")
        return 1 if errors else 0

    durations: dict[str, float | None] = {}
    for item in args.cut:
        if "=" not in item:
            p.error(f"--cut expects ID=MP4, got {item!r}")
        cid, mp4 = item.split("=", 1)
        durations[cid.strip()] = probe_duration(mp4.strip())
    words = None
    if args.spec:
        spec = _load_spec(args.spec)
        words = {c["id"]: word_count(cut_narration(spec, c)) for c in resolve_cuts(spec)}
    report = gate_cut_durations(durations, words)
    print(format_gate(report))
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2))
    return 1 if report["verdict"] == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
