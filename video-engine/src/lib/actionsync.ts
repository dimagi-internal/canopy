/**
 * Action↔word voiceover sync — piecewise footage time-warp (pure).
 *
 * A walkthrough beat plays one footage range under one VO clip, anchored only at
 * the beat's start frame. A scene that narrates a form speaks its field names in
 * a compact sentence while the footage demonstrates each field one-by-one, so the
 * spoken field name races ahead of the cursor ("the VO is moving faster than the
 * UI"). See ``docs/action-word-sync.md``.
 *
 * The recorder stamps each field action with its footage timestamp; snippets map
 * those into ON-SCREEN seconds and tag each with candidate narration words
 * (`action_marks`). Here we resolve each mark's word to its VO time and build a
 * warp plan: between two word-anchors the footage plays at the constant rate that
 * lands the next field exactly when the VO speaks it.
 *
 * Two stages:
 *   1. {@link buildActionWarp} — anchors → pieces in the ON-SCREEN footage domain.
 *   2. {@link composeWithSegments} — split pieces at de-dwell segment boundaries
 *      and map on-screen → master-clip seconds, so a constant-rate piece never
 *      straddles a jump-cut. (Teach scenes are single-segment → a pass-through.)
 *
 * Graceful: <1 usable anchor ⇒ `[]` ⇒ the caller keeps today's linear playback.
 */

/** Min/max footage playback rate. A field-fill demo never speeds past 2.5×
 * (still readable) nor slows below 0.7× (slower reads as lag, not teaching). */
export const RATE_MIN = 0.7;
export const RATE_MAX = 2.5;

export interface ActionMark {
  /** On-screen footage seconds where the field comes on camera. */
  on_seconds: number;
  /** Candidate narration words, most specific first; first to resolve wins. */
  words: string[];
  target?: string | null;
  kind?: string | null;
}

/** A constant-rate piece in the ON-SCREEN footage domain. */
export interface WarpPiece {
  /** Output (beat) time where this piece begins. */
  outStartSec: number;
  /** Output duration of this piece. */
  outDurSec: number;
  /** On-screen footage seconds where playback of this piece starts. */
  srcStartSec: number;
  /** playbackRate applied (= on-screen srcΔ / outΔ), clamped to [RATE_MIN,MAX]. */
  rate: number;
}

/** A render piece in the MASTER-clip domain (what the composition plays). */
export interface RenderPiece {
  outStartSec: number;
  outDurSec: number;
  /** Master-clip seconds to start the <Video> from. */
  assetStartSec: number;
  rate: number;
}

export interface BuildWarpArgs {
  marks: ActionMark[];
  /** Resolve a narration word to its start time in the beat VO (or null). */
  resolveWord: (word: string) => number | null;
  /** Summed on-screen footage duration of the beat (sum of segment durations). */
  footageOnscreenSec: number;
  /** Beat VO duration (seconds). Words resolve within [0, voSec]. */
  voSec: number;
  /** Beat on-screen OUTPUT duration (durationInFrames / fps) — VO-aligned. */
  beatSec: number;
  /** Override the playback-rate clamp (defaults RATE_MIN / RATE_MAX). A
   * `style: recorded` cut passes a tighter band so the footage stays near real
   * time — see style.ts. */
  rateMin?: number;
  rateMax?: number;
}

const clamp = (n: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, n));
const r3 = (n: number) => Math.round(n * 1000) / 1000;

/**
 * Resolve marks → monotonic (src,out) anchors. Each mark binds to the first of
 * its candidate words that resolves against the VO. Anchors are sorted by source
 * time and filtered to strictly-increasing output time (a later field whose word
 * is spoken earlier — an inversion — is dropped, keeping the earlier binding).
 */
export function resolveAnchors(
  marks: ActionMark[],
  resolveWord: (word: string) => number | null,
  voSec: number,
): { src: number; out: number }[] {
  const raw: { src: number; out: number }[] = [];
  for (const m of marks) {
    let out: number | null = null;
    for (const w of m.words ?? []) {
      const t = resolveWord(w);
      if (t != null && t >= 0 && t <= voSec + 0.001) {
        out = t;
        break;
      }
    }
    if (out != null) raw.push({ src: m.on_seconds, out });
  }
  raw.sort((a, b) => a.src - b.src || a.out - b.out);
  // Enforce strictly-increasing OUT as src increases; drop inversions. Also
  // collapse duplicate src (e.g. scroll_to + fill on the same field resolve the
  // same word) — the earliest src for a given out already won via the sort.
  const out: { src: number; out: number }[] = [];
  for (const a of raw) {
    const last = out[out.length - 1];
    if (last && (a.out <= last.out + 1e-6 || a.src <= last.src + 1e-6)) continue;
    out.push(a);
  }
  return out;
}

/**
 * Build the warp plan (ON-SCREEN domain). Returns `[]` when fewer than one
 * interior anchor resolves (nothing to align → caller keeps linear playback).
 */
export function buildActionWarp(args: BuildWarpArgs): WarpPiece[] {
  const { marks, resolveWord, footageOnscreenSec, voSec, beatSec } = args;
  const rateLo = args.rateMin ?? RATE_MIN;
  const rateHi = args.rateMax ?? RATE_MAX;
  if (footageOnscreenSec <= 0 || beatSec <= 0) return [];
  const anchors = resolveAnchors(marks, resolveWord, voSec);
  if (anchors.length === 0) return [];

  // Frame the plan with endpoints (0,0) and (footageOnscreen, beatSec): footage
  // starts at the beat's start and is consumed by the beat's end. Interior
  // anchors pull field-arrivals onto their spoken words; the tail distributes
  // any remaining footage across the time after the last word.
  const pts: { src: number; out: number }[] = [{ src: 0, out: 0 }];
  for (const a of anchors) {
    // Keep anchors strictly inside the footage/out window.
    if (a.src > 0.05 && a.src < footageOnscreenSec - 0.05 && a.out > 0.05 && a.out < beatSec - 0.05) {
      pts.push(a);
    }
  }
  pts.push({ src: footageOnscreenSec, out: beatSec });

  const pieces: WarpPiece[] = [];
  for (let i = 0; i < pts.length - 1; i++) {
    const outDur = pts[i + 1].out - pts[i].out;
    const srcDur = pts[i + 1].src - pts[i].src;
    if (outDur <= 1e-6 || srcDur <= 1e-6) continue;
    const rate = clamp(srcDur / outDur, rateLo, rateHi);
    pieces.push({
      outStartSec: r3(pts[i].out),
      outDurSec: r3(outDur),
      srcStartSec: r3(pts[i].src),
      rate: r3(rate),
    });
  }
  return pieces;
}

/**
 * Split each ON-SCREEN warp piece at de-dwell segment boundaries and map to
 * MASTER-clip seconds, so a constant-rate piece never straddles a jump-cut.
 * `segments` are the beat's kept master sub-ranges (played back-to-back); their
 * summed duration is the on-screen length the warp was built against. PURE.
 */
export function composeWithSegments(
  pieces: WarpPiece[],
  segments: { start_seconds: number; duration_seconds: number }[],
): RenderPiece[] {
  if (pieces.length === 0) return [];
  // On-screen → master lookup: cumulative segment starts.
  const segs = segments.map((s) => ({ ...s }));
  const out: RenderPiece[] = [];
  for (const p of pieces) {
    // Walk this piece's on-screen source span, splitting at segment edges.
    let srcPos = p.srcStartSec;
    let outPos = p.outStartSec;
    const srcEnd = p.srcStartSec + p.outDurSec * p.rate;
    let onscreen = 0;
    for (const seg of segs) {
      const segOnStart = onscreen;
      const segOnEnd = onscreen + seg.duration_seconds;
      onscreen = segOnEnd;
      // overlap of [srcPos, srcEnd] with this segment's on-screen span
      const lo = Math.max(srcPos, segOnStart);
      const hi = Math.min(srcEnd, segOnEnd);
      if (hi <= lo + 1e-6) continue;
      const srcSub = hi - lo;
      const outSub = srcSub / p.rate;
      out.push({
        outStartSec: r3(outPos),
        outDurSec: r3(outSub),
        assetStartSec: r3(seg.start_seconds + (lo - segOnStart)),
        rate: p.rate,
      });
      outPos += outSub;
      srcPos = hi;
      if (srcPos >= srcEnd - 1e-6) break;
    }
  }
  return out;
}

/** Convenience: build + compose in one call. Returns `[]` to fall back. */
export function planActionWarp(
  args: BuildWarpArgs & { segments: { start_seconds: number; duration_seconds: number }[] },
): RenderPiece[] {
  const pieces = buildActionWarp(args);
  return composeWithSegments(pieces, args.segments);
}
