"""Post-render dead-air QA detector (Layer 2).

"Dead air" = a span where the video is FROZEN (no on-screen motion) AND there is
no voiceover. Layer 1 (the render-time beat cap in the video engine's
``render.ts`` → ``src/lib/deadair.ts``) PREVENTS dead air by shrinking any beat
whose hold outlasts both its footage motion and its VO. This module is the
independent QA check that runs AFTER a render and reports any dead air that
slipped through.

Mechanism: intersect ffmpeg ``freezedetect`` spans (frozen video) with
``silencedetect`` spans (no VO), keep overlaps ≥ ``MIN_OVERLAP_SECONDS``, and
flag any that exceed the ``DEAD_THRESHOLD_SECONDS`` product threshold.

Audio nuance: the music bed plays as a quiet ~-50 dB bed under the whole video,
so ``silencedetect`` at -40 dB correctly flags a no-VO span as "silent" even
though the bed is technically audible — that is intended. We want frozen + no-VO
(dead air the viewer feels), not frozen + literal-digital-zero (which the looped
bed makes impossible anyway). The bed's continuity is also why Layer 1 caps via
re-render rather than a raw cut: a shorter total just re-laps the looped bed.
A ``style: recorded`` cut has NO bed (``scripts.ddd.recorded``), so there a
no-VO span is digital silence — the same thresholds flag it the same way.

The parsing + intersection are pure and unit-tested; the ffmpeg probe and the
human-readable report are thin wrappers around them.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

# Spans whose frozen+silent overlap STRICTLY exceeds this are real dead air.
# Product call: "leave anything under 3s" (sub-3s settles are intentional).
DEAD_THRESHOLD_SECONDS = 3.0
# A freeze∩silence overlap must reach this to register at all (filters noise).
MIN_OVERLAP_SECONDS = 1.0
# freezedetect noise floor: a frame within this dB of the previous = "frozen".
# Matches the render-time motion probe so the two layers agree.
FREEZE_NOISE_DB = -55
FREEZE_MIN_SECONDS = 0.7
# silencedetect noise floor — above the ~-50 dB music bed, so a no-VO span
# reads as "silent" even with the bed playing under it (see module docstring).
SILENCE_NOISE_DB = -40
SILENCE_MIN_SECONDS = 0.7

Span = tuple[float, float]


def parse_freeze_spans(log: str, total_seconds: float | None = None) -> list[Span]:
    """Parse ``freezedetect`` (start, end) spans from an ffmpeg stderr log.

    A ``freeze_start`` with no matching ``freeze_end`` (the clip ends frozen)
    closes at ``total_seconds`` when provided, else is dropped.
    """
    # freezedetect with metadata=print emits each value twice (the detect
    # filter and the metadata filter both log it). Collapse consecutive dupes.
    starts = _dedupe_consecutive(
        [float(m) for m in re.findall(r"freeze_start[:=]\s*([0-9.]+)", log)]
    )
    ends = _dedupe_consecutive(
        [float(m) for m in re.findall(r"freeze_end[:=]\s*([0-9.]+)", log)]
    )
    spans: list[Span] = []
    for i, s in enumerate(starts):
        if i < len(ends):
            spans.append((round(s, 3), round(ends[i], 3)))
        elif total_seconds is not None:
            spans.append((round(s, 3), round(total_seconds, 3)))
    return spans


def _dedupe_consecutive(xs: list[float]) -> list[float]:
    """Drop consecutive duplicate timestamps (the doubled metadata prints)."""
    out: list[float] = []
    for x in xs:
        if not out or out[-1] != x:
            out.append(x)
    return out


def parse_silence_spans(log: str, total_seconds: float | None = None) -> list[Span]:
    """Parse ``silencedetect`` (start, end) spans from an ffmpeg stderr log."""
    starts = [float(m) for m in re.findall(r"silence_start:\s*([0-9.]+)", log)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*([0-9.]+)", log)]
    spans: list[Span] = []
    for i, s in enumerate(starts):
        if i < len(ends):
            spans.append((round(s, 3), round(ends[i], 3)))
        elif total_seconds is not None:
            spans.append((round(s, 3), round(total_seconds, 3)))
    return spans


def intersect_spans(
    freeze: list[Span], silence: list[Span], *, min_overlap: float = MIN_OVERLAP_SECONDS
) -> list[Span]:
    """Intersect freeze spans with silence spans; keep overlaps ≥ ``min_overlap``.

    Each returned span is the actual frozen+silent intersection (clipped to the
    overlap), not the union — so the reported seconds are the dead air the
    viewer experiences.
    """
    out: list[Span] = []
    for fs, fe in freeze:
        for ss, se in silence:
            lo = max(fs, ss)
            hi = min(fe, se)
            if hi - lo >= min_overlap:
                out.append((round(lo, 3), round(hi, 3)))
    out.sort()
    return out


def build_report(spans: list[Span], *, threshold: float = DEAD_THRESHOLD_SECONDS) -> dict[str, Any]:
    """Summarize dead-air spans: count, total seconds, and which exceed the
    product threshold (those are the ones Layer 1 should have prevented)."""
    over = [
        {"start": s, "end": e, "seconds": round(e - s, 3)}
        for s, e in spans
        if (e - s) > threshold
    ]
    return {
        "span_count": len(spans),
        "total_seconds": round(sum(e - s for s, e in spans), 3),
        "spans": [{"start": s, "end": e, "seconds": round(e - s, 3)} for s, e in spans],
        "over_threshold": over,
        "has_dead_air": len(over) > 0,
        "threshold_seconds": threshold,
    }


def _probe_total_seconds(mp4: str) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", mp4],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        v = float(out)
        return v if v > 0 else None
    except Exception:  # noqa: BLE001 — best-effort
        return None


def _drop_ignored(spans: list[Span], ignore: list[Span]) -> list[Span]:
    """Drop any span that is fully contained in an ignored range.

    Used to exclude DESIGNED static cards (intro title, outro card + its music
    fade-out) from the dead-air report: those are intentional held frames, not
    frozen recorded footage, so Layer 1 (which only caps footage beats) leaves
    them — and the QA report should not flag them either.
    """
    kept: list[Span] = []
    for s, e in spans:
        if any(s >= lo - 0.05 and e <= hi + 0.05 for lo, hi in ignore):
            continue
        kept.append((s, e))
    return kept


def detect_dead_air(mp4: str, ignore_ranges: list[Span] | None = None) -> dict[str, Any]:
    """Run freeze+silence detection over a rendered mp4 and return a report.

    ``ignore_ranges`` (final-video seconds) are excised from the result — used
    to drop designed static cards (intro/outro) from the QA report. Returns the
    :func:`build_report` shape. On ffmpeg/probe failure returns a report with
    ``error`` set and no spans (advisory — never blocks a render).
    """
    if not Path(mp4).is_file():
        return {**build_report([]), "error": f"file not found: {mp4}"}
    total = _probe_total_seconds(mp4)
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", mp4,
             "-vf", f"freezedetect=n={FREEZE_NOISE_DB}dB:d={FREEZE_MIN_SECONDS},metadata=print",
             "-af", f"silencedetect=noise={SILENCE_NOISE_DB}dB:d={SILENCE_MIN_SECONDS}",
             "-map", "0:v", "-map", "0:a", "-f", "null", "-"],
            capture_output=True, text=True, timeout=300,
        )
    except Exception as e:  # noqa: BLE001 — advisory
        return {**build_report([]), "error": str(e)}
    log = proc.stderr or ""
    freeze = parse_freeze_spans(log, total_seconds=total)
    silence = parse_silence_spans(log, total_seconds=total)
    spans = intersect_spans(freeze, silence)
    if ignore_ranges:
        spans = _drop_ignored(spans, ignore_ranges)
    return build_report(spans)


def format_report(report: dict[str, Any]) -> str:
    """Human-readable block for the local renderer to print after timing."""
    lines = ["\n==> Dead-air detector (freeze ∩ silence)"]
    if report.get("error"):
        lines.append(f"    skipped ({report['error']})")
        return "\n".join(lines)
    n = report["span_count"]
    if n == 0:
        lines.append("    none detected — no frozen+silent spans ≥ "
                     f"{MIN_OVERLAP_SECONDS}s. ✓")
        return "\n".join(lines)
    lines.append(f"    {n} frozen+silent span(s), {report['total_seconds']}s total:")
    for s in report["spans"]:
        flag = "  ⚠ DEAD AIR" if s["seconds"] > report["threshold_seconds"] else ""
        lines.append(f"      {s['start']:7.1f}s → {s['end']:7.1f}s  ({s['seconds']:.1f}s){flag}")
    if report["has_dead_air"]:
        lines.append(
            f"    ⚠ {len(report['over_threshold'])} span(s) exceed "
            f"{report['threshold_seconds']}s — Layer 1 should have capped these. "
            "Re-render, or pass --trim-dead-air-fallback."
        )
    else:
        lines.append(f"    all spans under the {report['threshold_seconds']}s threshold. ✓")
    return "\n".join(lines)
