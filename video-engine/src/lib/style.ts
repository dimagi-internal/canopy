/**
 * Video STYLE → render policy (pure).
 *
 * `explainer` (the default, and every spec written before `style` existed) is
 * the produced cut: a music bed under the whole video, captions when asked
 * for, and an action↔word footage warp free to run 0.7–2.5× so each field
 * lands on its spoken word.
 *
 * `recorded` is "I just recorded myself walking through this for someone"
 * (canopy's ddd-recorded-walkthrough skill): NO music bed, NO burned-in
 * captions, and the footage warp clamped to a band a viewer reads as real
 * time — at 2.5× the cursor darts in a way no hand does. Title/end cards and
 * lower-thirds are kept out structurally by the spec schema (spec.ts), so
 * this policy only covers what render.ts decides at mux/plan time.
 *
 * Mirrored in canopy `scripts/ddd/recorded.py` (RECORDED_RATE_MIN/MAX) — keep
 * the two in step.
 */
import { RATE_MAX, RATE_MIN } from "./actionsync";

export const VIDEO_STYLES = ["explainer", "recorded"] as const;
export type VideoStyle = (typeof VIDEO_STYLES)[number];

/** Footage playback-rate band for a recorded cut's action↔word warp. */
export const RECORDED_RATE_MIN = 0.85;
export const RECORDED_RATE_MAX = 1.35;

export interface RenderPolicy {
  style: VideoStyle;
  /** Mix the global_style.yaml music bed under the cut. */
  musicBed: boolean;
  /** Burn the CaptionBar in (still subject to the --no-captions CLI flag). */
  captions: boolean;
  /** Clamp for the action↔word footage warp. */
  rateMin: number;
  rateMax: number;
}

export function videoStyle(spec: { style?: string | null } | null | undefined): VideoStyle {
  return spec?.style === "recorded" ? "recorded" : "explainer";
}

export function renderPolicy(spec: { style?: string | null } | null | undefined): RenderPolicy {
  if (videoStyle(spec) === "recorded") {
    return {
      style: "recorded",
      musicBed: false,
      captions: false,
      rateMin: RECORDED_RATE_MIN,
      rateMax: RECORDED_RATE_MAX,
    };
  }
  return { style: "explainer", musicBed: true, captions: true, rateMin: RATE_MIN, rateMax: RATE_MAX };
}
