#!/usr/bin/env python3
"""Render Pacing Audit — deterministic, objective pinpointing of where a DDD
video has problems, classified as VIEWING ISSUE vs RECORDING BUG.

The LLM visual-judge scores aesthetics/content of a single frame. It is blind to
TIME: dead air, shown loading, silent demo footage, a wait that never resolved.
Those are deterministic and measurable from the audio+video+run-report — no LLM,
no variance. This audit decomposes the final video's SILENT budget and labels it:

  VIEWING ISSUE
    · dead-air      — silent AND frozen. A frozen, silent frame on screen. The
                      dead-air cap should have trimmed it; if it's here it's a
                      bad-video moment (and a cap blind spot when the frame
                      flickers so freeze-detect won't merge it).
    · silent-motion — silent AND moving. Footage runs with no narration over it:
                      a loading spinner shown on camera, or the demo clicking
                      around while the script has nothing to say (narration too
                      sparse for the on-screen activity). Either way: bad video.

  RECORDING BUG (from run-report, if given)
    · a must_succeed action failed, any action errored, or a wait_for timed out —
      the recording itself did the wrong thing; the take is not gradable as-is.

Thresholds are fixed so two runs are comparable. Output is a ledger + a flagged
region list with timestamps, so a reviewer can jump straight to each problem.
"""
from __future__ import annotations
import json, re, statistics, subprocess, sys

# --- fixed, objective thresholds (a region must exceed these to be flagged) ---
DEAD_AIR_FLAG_S = 1.5      # a frozen+silent stretch this long is a bad-video moment
SILENT_MOTION_FLAG_S = 3.0  # silent moving footage this long = shown-loading / sparse-VO
WAIT_LEADIN_S = 1.2        # the #240 excision keeps this much spinner per wait — expected
SILENCE_NOISE_DB = -40     # below this = "silence" (no narration)
SILENCE_MIN_S = 0.5
FREEZE_NOISE_DB = -55      # within this of prev frame = "no motion" (matches the cap)
FREEZE_MIN_S = 0.5
BLANK_FLAG_S = 0.5         # a solid-colour frame this long is a broken frame, always
BLANK_STDEV = 3.0          # per-channel spread below this = one flat colour
BLANK_SAMPLE_FPS = 4


def _ffmpeg_stderr(args: list[str]) -> str:
    return subprocess.run(["ffmpeg", "-hide_banner", "-nostats", *args, "-f", "null", "-"],
                          capture_output=True, text=True).stderr


def _duration(video: str) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", video], capture_output=True, text=True).stdout
    return float(out.strip())


def has_audio(video: str) -> bool:
    """True when the file carries at least one audio stream. PURE-ish (ffprobe)."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=index", "-of", "csv=p=0", video],
        capture_output=True, text=True).stdout
    return bool(out.strip())


def silence_intervals(video: str, dur: float) -> list[tuple[float, float]]:
    # No audio stream at all: silencedetect emits nothing, which the caller
    # would read as "no silence" — i.e. 100% speech, zero dead air, a perfect
    # score for a video with no narration whatsoever. That is not theoretical:
    # `create-survey-solicitation v12` shipped silent and this audit rated it
    # the cleanest of the set. A file with no audio is ENTIRELY silent.
    if not has_audio(video):
        return [(0.0, dur)]
    log = _ffmpeg_stderr(["-i", video, "-af", f"silencedetect=noise={SILENCE_NOISE_DB}dB:d={SILENCE_MIN_S}"])
    starts = [float(m) for m in re.findall(r"silence_start:\s*([0-9.]+)", log)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*([0-9.]+)", log)]
    out = []
    for i, s in enumerate(starts):
        out.append((s, ends[i] if i < len(ends) else dur))
    return _merge(out)


def _merge(xs: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for s, e in sorted(xs):
        if out and s <= out[-1][1] + 1e-3:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def freeze_intervals(video: str, dur: float) -> list[tuple[float, float]]:
    log = _ffmpeg_stderr(["-i", video, "-vf", f"freezedetect=n={FREEZE_NOISE_DB}dB:d={FREEZE_MIN_S},metadata=print"])
    # freezedetect + metadata=print each print the span → starts/ends are DOUBLED.
    # Dedupe consecutive duplicate starts (mirrors deadair.parseFreezeSpansFromLog).
    raw_starts = [float(m) for m in re.findall(r"freeze_start[:=]\s*([0-9.]+)", log)]
    raw_ends = [float(m) for m in re.findall(r"freeze_end[:=]\s*([0-9.]+)", log)]
    starts, ends = [], []
    for i, s in enumerate(raw_starts):
        if starts and abs(starts[-1] - s) < 1e-3:
            continue
        starts.append(s)
    for i, e in enumerate(raw_ends):
        if ends and abs(ends[-1] - e) < 1e-3:
            continue
        ends.append(e)
    out = [(s, ends[i] if i < len(ends) else dur) for i, s in enumerate(starts)]
    return _merge(out)


def blank_intervals(video: str, dur: float) -> list[tuple[float, float]]:
    """Spans where the frame is a single flat colour — nothing rendered at all.

    Orthogonal to everything else here. Dead-air is silent AND frozen; a blank
    frame with narration over it is neither silent nor (necessarily) frozen, so
    the existing detectors cannot see it. `microplans-study-groups` shipped 2.5s
    of solid dark purple at 34.0s and this audit reported "VIEWING ISSUES: none".

    Spread is measured PER CHANNEL. Across interleaved RGB a flat colour still
    has the channel offsets between R, G and B in it, which reads as variance
    and hides exactly the frames we are looking for — the first cut of this
    check scored a known-blank video clean for that reason.
    """
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", video, "-vf",
         f"fps={BLANK_SAMPLE_FPS},scale=64:36", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True).stdout
    n = 64 * 36 * 3
    spans: list[tuple[float, float]] = []
    cur: tuple[float, float] | None = None
    for i in range(len(raw) // n):
        fr = raw[i * n:(i + 1) * n]
        flat = max(statistics.pstdev(fr[c::3]) for c in range(3)) < BLANK_STDEV
        t = i / BLANK_SAMPLE_FPS
        if flat:
            cur = (t, t) if cur is None else (cur[0], t)
        elif cur is not None:
            spans.append(cur)
            cur = None
    if cur is not None:
        spans.append(cur)
    return _merge([(s, e) for s, e in spans if e - s >= BLANK_FLAG_S])


def _intersect(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out = []
    for s1, e1 in a:
        for s2, e2 in b:
            s, e = max(s1, s2), min(e1, e2)
            if e - s > 1e-3:
                out.append((s, e))
    return _merge(out)


def _subtract(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """a minus b (both sorted-ish); returns the parts of a not covered by b."""
    out = []
    for s, e in a:
        cuts = sorted([(max(s, bs), min(e, be)) for bs, be in b if min(e, be) - max(s, bs) > 1e-3])
        pos = s
        for cs, ce in cuts:
            if cs > pos + 1e-3:
                out.append((pos, cs))
            pos = max(pos, ce)
        if e > pos + 1e-3:
            out.append((pos, e))
    return out


def _total(xs):
    return round(sum(e - s for s, e in xs), 1)


def recording_bugs(report_path: str | None) -> list[str]:
    if not report_path:
        return []
    try:
        r = json.load(open(report_path))
    except Exception:
        return []
    bugs = []
    for a in r.get("actions", []):
        if a.get("ok") is False:
            sc = a.get("scene_index")
            tgt = (a.get("target") or a.get("value") or "")[:40]
            ek = a.get("error_kind") or "failed"
            ms = " (must_succeed)" if a.get("must_succeed") else ""
            bugs.append(f"scene {sc}: {a.get('kind')}({tgt}) → {ek}{ms}")
    return bugs


def audit(video: str, report_path: str | None = None, label: str = "") -> dict:
    dur = _duration(video)
    sil = silence_intervals(video, dur)
    frz = freeze_intervals(video, dur)
    dead_air = _intersect(sil, frz)          # silent AND frozen
    silent_motion = _subtract(sil, frz)       # silent AND moving
    speech = round(dur - _total(sil), 1)

    # Outro card is static + silent BY DESIGN; the intro title card is static but
    # NARRATED (a `role: overview` scene's VO is spoken over the held card — see
    # snippets.build_explainer_spec), or speaks the tagline when there is none. A
    # frozen region touching the very start or end is an edge card, not dead-air —
    # a SILENT intro would show up as start-edge dead-air (a missing overview VO).
    is_edge = lambda s, e: s <= 1.0 or e >= dur - 0.6
    flags = []
    for s, e in dead_air:
        if e - s < DEAD_AIR_FLAG_S:
            continue
        if is_edge(s, e):
            flags.append(("EDGE-CARD", s, e, "intro/outro card (static+silent) — deliberate, not dead-air"))
        else:
            flags.append(("DEAD-AIR", s, e, "frozen + silent MID-VIDEO — cap blind spot, reads as a stall"))
    for s, e in silent_motion:
        if e - s >= SILENT_MOTION_FLAG_S and not is_edge(s, e):
            flags.append(("SILENT-MOTION", s, e, "moving footage, no narration — shown loading or sparse VO over activity"))
    for s_, e_ in blank_intervals(video, dur):
        if is_edge(s_, e_):
            continue
        flags.append(("BLANK", s_, e_,
                      "SOLID COLOUR — nothing rendered; a viewer sees the player break"))
    bugs = recording_bugs(report_path)
    flags.sort(key=lambda f: f[1])

    return {
        "label": label or video, "dur": dur, "speech": speech, "silence": _total(sil),
        "dead_air": _total(dead_air), "silent_motion": _total(silent_motion),
        "flags": flags, "bugs": bugs,
    }


def render(a: dict) -> str:
    L = []
    L.append(f"\n══ RENDER PACING AUDIT — {a['label']}  ({a['dur']:.1f}s) ══")
    pct = lambda x: f"{100*x/a['dur']:.0f}%"
    L.append(f"  Speech (VO):        {a['speech']:.1f}s  ({pct(a['speech'])})")
    L.append(f"  Silent:             {a['silence']:.1f}s  ({pct(a['silence'])})")
    L.append(f"    ├─ dead-air (silent+frozen):   {a['dead_air']:.1f}s   {'⚠️' if a['dead_air']>DEAD_AIR_FLAG_S else 'ok'}")
    L.append(f"    └─ silent-motion (silent+move): {a['silent_motion']:.1f}s   {'⚠️' if a['silent_motion']>6 else 'ok'}")
    issues = [f for f in a["flags"] if f[0] != "EDGE-CARD"]
    edges = [f for f in a["flags"] if f[0] == "EDGE-CARD"]
    if issues:
        L.append(f"  VIEWING ISSUES ({len(issues)}) — jump to each:")
        for kind, s, e, why in issues:
            L.append(f"    [{kind}]  {s:.1f}–{e:.1f}s  ({e-s:.1f}s)  — {why}")
    else:
        L.append("  VIEWING ISSUES: none over threshold.")
    if edges:
        for kind, s, e, why in edges:
            L.append(f"    (edge ok)  {s:.1f}–{e:.1f}s  ({e-s:.1f}s)  — {why}")
    if a["bugs"]:
        L.append(f"  RECORDING BUGS ({len(a['bugs'])}):")
        for b in a["bugs"]:
            L.append(f"    [RECORDING BUG]  {b}")
    else:
        L.append("  RECORDING BUGS: none (run-report clean)")
    return "\n".join(L)


if __name__ == "__main__":
    video = sys.argv[1]
    report = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != "-" else None
    label = sys.argv[3] if len(sys.argv) > 3 else ""
    print(render(audit(video, report, label)))
