"""Video-judge HARNESS — assemble per-scene evidence packets from a rendered
connect-ddd-walkthrough so a multimodal judge can score the VIDEO (not screenshots).

The DDD concept/visual judge scores per-scene screenshots of the LIVE app and
never opens the produced mp4, so audio-visual TIMING + pacing are invisible to it
(see canopy:ddd-timing-eval for the deterministic half). This harness builds the
evidence a multimodal judge needs to score the rendered video:

  * VO-word-mark frames — a frame grabbed at the exact instant the voiceover speaks
    each named field, labelled with the spoken word. Tests "does the screen show
    what's being said?" (semantic VO↔visual coherence — richer than the
    field-token timing eval).
  * pacing frames — evenly sampled across the scene, to judge motion/pacing.

Per scene it writes a labelled montage + a manifest row (narration, concept_claim,
the word→time→field list, durations). A judge (canopy:ddd-video-judge) then reads
the montages + manifest and emits verdict-video.json.

Usage:
  python3 -m scripts.ddd.video_judge <run_dir> <explainer_spec.(json|yaml)> <audio_dir> <out_dir>
  # run_dir holds output.mp4 + beat-timeline.json (written by render.ts)
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _font(size: int) -> ImageFont.FreeTypeFont:
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _load_spec(path: str) -> dict:
    if path.endswith(".json"):
        return json.load(open(path))
    # Composes recipe + narrative lock when that is the on-disk shape. Raw:
    # the judge reads whatever the recorder filmed, schema or no schema.
    from scripts.ddd.spec_io import load_spec_raw

    return load_spec_raw(path)


def word_start(align: dict, stem: str) -> float | None:
    """First start-time of a word (case-insensitive word-boundary prefix)."""
    text = "".join(align["characters"]).lower()
    m = re.search(r"(^|[^a-z])" + re.escape(stem.lower()), text)
    if not m:
        return None
    idx = m.start() + (len(m.group(1)) if m.group(1) else 0)
    st = align["character_start_times_seconds"]
    return st[idx] if 0 <= idx < len(st) else None


def find_alignment(audio_dir: str, narration: str) -> dict | None:
    for f in os.listdir(audio_dir):
        if not f.endswith(".json"):
            continue
        try:
            d = json.load(open(os.path.join(audio_dir, f)))
        except (json.JSONDecodeError, OSError):
            continue
        a = d.get("alignment")
        if not a or not a.get("characters"):
            continue
        text = "".join(a["characters"]).strip()
        if text and narration.startswith(text[:25]):
            return a
    return None


def resolve_anchors(marks: list[dict], align: dict, vo_sec: float) -> list[dict]:
    """marks → monotonic (src, vo, word, target). Mirrors actionsync.resolveAnchors:
    bind each mark to its first candidate word that resolves; sort by src; keep
    strictly-increasing vo (drop inversions)."""
    raw = []
    for m in marks:
        for w in m.get("words", []):
            t = word_start(align, w)
            if t is not None and 0 <= t <= vo_sec + 0.001:
                raw.append({"src": m["on_seconds"], "vo": t, "word": w, "target": m.get("target", "")})
                break
    raw.sort(key=lambda a: (a["src"], a["vo"]))
    out: list[dict] = []
    for a in raw:
        if out and (a["vo"] <= out[-1]["vo"] + 1e-6 or a["src"] <= out[-1]["src"] + 1e-6):
            continue
        out.append(a)
    return out


def extract_frame(video: str, t: float, dest: str, width: int = 720) -> bool:
    subprocess.run(
        ["ffmpeg", "-y", "-ss", f"{t:.3f}", "-i", video, "-frames:v", "1", "-vf", f"scale={width}:-1", dest],
        capture_output=True,
    )
    return os.path.exists(dest)


def _label(img_path: str, caption: str) -> Image.Image:
    im = Image.open(img_path).convert("RGB")
    draw = ImageDraw.Draw(im)
    font = _font(22)
    pad = 6
    box = draw.textbbox((0, 0), caption, font=font)
    h = (box[3] - box[1]) + pad * 2
    draw.rectangle([0, 0, im.width, h], fill=(0, 0, 0))
    draw.text((pad, pad), caption, fill=(255, 220, 0), font=font)
    return im


def montage(labelled: list[Image.Image], cols: int = 3) -> Image.Image:
    if not labelled:
        return Image.new("RGB", (10, 10))
    w = max(i.width for i in labelled)
    h = max(i.height for i in labelled)
    rows = math.ceil(len(labelled) / cols)
    sheet = Image.new("RGB", (cols * w + (cols + 1) * 6, rows * h + (rows + 1) * 6), (40, 40, 48))
    for idx, im in enumerate(labelled):
        r, c = divmod(idx, cols)
        sheet.paste(im, (6 + c * (w + 6), 6 + r * (h + 6)))
    return sheet


def build(run_dir: str, spec_path: str, audio_dir: str, out_dir: str) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    frames_dir = os.path.join(out_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    video = os.path.join(run_dir, "output.mp4")
    timeline = {b["id"]: b for b in json.load(open(os.path.join(run_dir, "beat-timeline.json")))}
    spec = _load_spec(spec_path)
    by_beat = spec["narration"]["by_beat"]
    walkthrough = spec.get("walkthrough", {})

    scenes = []
    for bid, beat in timeline.items():
        if beat.get("kind") != "body_walkthrough":
            continue
        start, dur = beat["startSec"], beat["durationSec"]
        narration = by_beat.get(bid, "")
        align = find_alignment(audio_dir, narration)
        marks = walkthrough.get(bid, {}).get("action_marks", [])
        anchors = resolve_anchors(marks, align, align["character_end_times_seconds"][-1]) if align and marks else []

        labelled: list[Image.Image] = []
        anchor_rows = []
        # VO-word-mark frames (semantic coherence): screen at the instant the word is spoken
        for a in anchors:
            abs_t = start + a["vo"]
            png = os.path.join(frames_dir, f"{bid}_vo_{a['word']}_{abs_t:.1f}.png")
            if extract_frame(video, abs_t, png):
                field = a["target"].split(":")[-1] if a["target"] else ""
                labelled.append(_label(png, f"VO says “{a['word']}” @{abs_t:.1f}s  → expect field: {field}"))
                anchor_rows.append({"word": a["word"], "field": field, "at_s": round(abs_t, 1)})
        # pacing frames: evenly across the scene
        for k in range(3):
            frac = (k + 0.5) / 3
            abs_t = start + frac * dur
            png = os.path.join(frames_dir, f"{bid}_pace_{abs_t:.1f}.png")
            if extract_frame(video, abs_t, png):
                labelled.append(_label(png, f"pacing @{abs_t:.1f}s ({int(frac*100)}% into scene)"))

        sheet_path = os.path.join(out_dir, f"{bid}_montage.png")
        montage(labelled).save(sheet_path)
        scenes.append(
            {
                "beat": bid,
                "title": walkthrough.get(bid, {}).get("lower_third") or bid,
                "window_s": [round(start, 1), round(start + dur, 1)],
                "duration_s": round(dur, 1),
                "narration": narration,
                "concept_claim": "",  # filled from the source walkthrough spec if present
                "vo_word_marks": anchor_rows,
                "montage": os.path.relpath(sheet_path, out_dir),
            }
        )

    manifest = {"video": os.path.relpath(video, out_dir), "scenes": scenes}
    json.dump(manifest, open(os.path.join(out_dir, "manifest.json"), "w"), indent=2)
    return manifest


if __name__ == "__main__":
    if len(sys.argv) < 5:
        print(__doc__)
        sys.exit(2)
    m = build(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])
    print(f"built {len(m['scenes'])} scene packets → {sys.argv[4]}")
    for s in m["scenes"]:
        print(f"  {s['beat']}: {len(s['vo_word_marks'])} VO-marks, window {s['window_s']}s")
