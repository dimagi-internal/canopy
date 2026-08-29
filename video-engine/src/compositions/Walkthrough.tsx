import { AbsoluteFill, Freeze, Sequence, Video, staticFile, useVideoConfig } from "remotion";
import { theme } from "../theme";
import { Lower3rd } from "../components/Lower3rd";
import type { ProgramSpec, WalkthroughBeat } from "../lib/spec";
import type { RenderPiece } from "../lib/actionsync";

/**
 * One walkthrough section of the connect-ddd-walkthrough arc. Plays a RANGE
 * of one master clip full-bleed (objectFit: cover, `startFrom` honoring
 * the beat's start_seconds in the source), overlays a single
 * lower-third, and lets the top-level CaptionBar + per-beat VO ride on
 * top.
 *
 * The beat's on-screen DURATION is the Sequence length given to it by
 * Root.tsx (b.durationFrames) — i.e. the beat's `seconds` from the
 * spec's `beats:` list, which the audio-aligner STRETCHES to fit the
 * narration. `start_seconds` is the IN-point into the master clip and
 * `duration_seconds` is how long the selected RANGE of footage runs.
 *
 * HOLD-LAST-FRAME: when the beat's on-screen length (durationInFrames)
 * runs LONGER than the selected range (duration_seconds), the clip would
 * otherwise keep playing past its range into the NEXT section's footage
 * (drift). We wrap the <Video> in <Freeze> so it plays only its range
 * [start_seconds, start_seconds + duration_seconds] and then freezes on
 * the last frame of that range for the remaining beat time — the section
 * stays on its own footage while the narration finishes.
 *
 * When duration_seconds is absent there's no defined range, so we fall
 * back to playing the whole beat (the original behavior).
 */
/**
 * Decide whether to hold the last frame and, if so, at which range frame.
 *
 * Returns the range length in frames when the beat's on-screen length
 * (`beatFrames`) runs LONGER than the selected range (`durationSeconds`)
 * — i.e. when footage would otherwise drift into the next section.
 * Returns null (play the whole beat unchanged) when `durationSeconds` is
 * absent or the range is at least as long as the beat.
 */
export function freezeRangeFrames(
  durationSeconds: number | undefined,
  fps: number,
  beatFrames: number
): number | null {
  if (durationSeconds == null) return null;
  const rangeFrames = Math.max(1, Math.round(durationSeconds * fps));
  return rangeFrames < beatFrames ? rangeFrames : null;
}

/**
 * The sub-ranges of the master clip a beat plays, in order. Prefers the
 * de-dwelled ``segments`` list (motion spans with dead-air gaps collapsed →
 * jump-cuts); falls back to the single ``start_seconds``/``duration_seconds``
 * range for older / non-de-dwelled specs. ``duration_seconds`` may be
 * undefined in the single-range fallback (= play the whole beat).
 */
export function beatSegments(
  wt: WalkthroughBeat
): { start_seconds: number; duration_seconds?: number }[] {
  if (wt.segments && wt.segments.length > 0) return wt.segments;
  return [{ start_seconds: wt.start_seconds ?? 0, duration_seconds: wt.duration_seconds }];
}

/**
 * The Sequence window for each warp piece: where it starts and how long it
 * stays on screen. PURE, so the covering property is testable without a
 * renderer.
 *
 * Every frame of the beat must belong to exactly one piece. Each piece used to
 * occupy only [outStartSec, +outDurSec) with just the LAST one stretched, so a
 * gap between pieces — or a first piece starting after 0 — was covered by
 * nothing and fell through to the AbsoluteFill behind them, which is painted
 * theme.colors.foreground (#0A0620). 3.5s of exactly that solid colour shipped
 * mid-scene in microplans-study-groups; to a viewer the player just breaks
 * while the narration keeps talking.
 *
 * `own` is the piece's real footage length — beyond it the caller freezes the
 * last frame. With contiguous pieces the windows are unchanged.
 */
export function warpSequenceWindows(
  warp: { outStartSec: number; outDurSec: number }[],
  fps: number,
  durationInFrames: number
): { from: number; seqLen: number; own: number }[] {
  return warp.map((p, i) => {
    const isLast = i === warp.length - 1;
    const rawFrom = Math.round(p.outStartSec * fps);
    const from = i === 0 ? 0 : rawFrom;
    const own = Math.max(1, Math.round(p.outDurSec * fps) + (rawFrom - from));
    const nextFrom = isLast ? durationInFrames : Math.round(warp[i + 1].outStartSec * fps);
    return { from, own, seqLen: Math.max(own, nextFrom - from) };
  });
}

export const Walkthrough: React.FC<{ wt: WalkthroughBeat; warp?: RenderPiece[] }> = ({
  wt,
  warp,
}) => {
  const { fps, durationInFrames } = useVideoConfig();
  const src = wt.asset.startsWith("http") ? wt.asset : staticFile(wt.asset);
  const segs = beatSegments(wt);

  const videoFrom = (startSeconds: number, playbackRate?: number) => (
    <Video
      src={src}
      startFrom={Math.round(startSeconds * fps)}
      playbackRate={playbackRate}
      style={{ width: "100%", height: "100%", objectFit: "cover" }}
      onError={() => {
        /* Missing asset — render blank; drop the real file in the cache to fix */
      }}
    />
  );

  // Action↔word warp: play the render-side pieces, each a sub-range of the
  // master clip at the constant playbackRate that lands the field on its spoken
  // word (see actionsync.ts). The last piece holds its final frame for any beat
  // time past the summed pieces (VO outruns footage) — same hold-last-frame
  // contract as the de-dwell path below. No plan ⇒ fall through to that path.
  if (warp && warp.length > 0) {
    let summed = 0;
    const windows = warpSequenceWindows(warp, fps, durationInFrames);
    const nodes = warp.map((p, i) => {
      const { from, own, seqLen } = windows[i];
      const vid = videoFrom(p.assetStartSec, p.rate);
      // Any extended piece holds its last frame, not just the final one.
      const child =
        seqLen > own ? (
          <Freeze frame={own - 1} active={(f) => f >= own}>
            {vid}
          </Freeze>
        ) : (
          vid
        );
      summed += own;
      return (
        <Sequence key={i} from={from} durationInFrames={seqLen}>
          {child}
        </Sequence>
      );
    });
    void summed;
    return (
      <AbsoluteFill style={{ background: theme.colors.foreground }}>
        {nodes}
        {wt.lower_third ? <Lower3rd text={wt.lower_third} /> : null}
      </AbsoluteFill>
    );
  }

  // Single open-ended range (no duration): the original whole-beat playback.
  if (segs.length === 1 && segs[0].duration_seconds == null) {
    return (
      <AbsoluteFill style={{ background: theme.colors.foreground }}>
        {videoFrom(segs[0].start_seconds)}
        {wt.lower_third ? <Lower3rd text={wt.lower_third} /> : null}
      </AbsoluteFill>
    );
  }

  // De-dwelled (or single ranged) playback: lay each segment back-to-back via
  // nested Sequences. The dropped gaps between segments are clean jump-cuts.
  // The LAST segment holds its final frame for any beat time beyond the summed
  // segment length (VO overrun) — same hold-last-frame contract as before.
  const segFrames = segs.map((s) => Math.max(1, Math.round((s.duration_seconds as number) * fps)));
  let offset = 0;
  const nodes = segs.map((s, i) => {
    const sf = segFrames[i];
    const isLast = i === segs.length - 1;
    const seqLen = isLast ? Math.max(sf, durationInFrames - offset) : sf;
    const vid = videoFrom(s.start_seconds);
    // Hold the last frame only on the final segment when the beat outruns it.
    const child =
      isLast && seqLen > sf ? (
        <Freeze frame={sf - 1} active={(f) => f >= sf}>
          {vid}
        </Freeze>
      ) : (
        vid
      );
    const node = (
      <Sequence key={i} from={offset} durationInFrames={seqLen}>
        {child}
      </Sequence>
    );
    offset += sf;
    return node;
  });

  return (
    <AbsoluteFill style={{ background: theme.colors.foreground }}>
      {nodes}
      {wt.lower_third ? <Lower3rd text={wt.lower_third} /> : null}
    </AbsoluteFill>
  );
};

/** Lookup helper — resolve a beat id to its walkthrough entry. */
export function walkthroughForBeat(
  spec: ProgramSpec,
  beatId: string
): WalkthroughBeat | undefined {
  return spec.walkthrough?.[beatId];
}
