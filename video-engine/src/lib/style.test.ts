import { describe, it, expect } from "vitest";
import { parseProgramSpec } from "./spec";
import { renderPolicy, videoStyle, RECORDED_RATE_MIN, RECORDED_RATE_MAX } from "./style";
import { buildActionWarp, RATE_MAX, RATE_MIN } from "./actionsync";

// A minimal recorded cut, the shape canopy's build_recorded_cut_specs emits:
// only body_walkthrough beats, no title / end card, no lower-thirds.
const recordedYaml = (extra = "", beatsExtra = "", lowerThird = '""') => `
style: recorded
slug: chlorine-dispensers-assign
name: Assigning a dispenser
country_focus: Kenya
status: DDD recorded walkthrough
tagline: Assigning a dispenser
program_url: https://labs.connect.dimagi.com/
manifest: { master: "file:assets/programs/chlorine-dispensers-assign/walkthrough.mp4" }
beats:
  - { id: s1, kind: body_walkthrough, seconds: 12 }
  - { id: s2, kind: body_walkthrough, seconds: 14 }
${beatsExtra}
walkthrough:
  s1: { asset: "@master", start_seconds: 0, duration_seconds: 12, lower_third: ${lowerThird} }
  s2: { asset: "@master", start_seconds: 12, duration_seconds: 14 }
narration:
  generator: manual
  prompt_version: v1
  script: "This is a quick overview of how we assign dispensers."
  by_beat:
    s1: "This is a quick overview of how we assign dispensers."
    s2: "I pick one and save."
voice: { provider: elevenlabs, voice_id: x, model: eleven_turbo_v2 }
${extra}
`;

describe("renderPolicy", () => {
  it("defaults to the explainer: music bed, captions, the full warp band", () => {
    for (const spec of [undefined, null, {}, { style: "explainer" }, { style: "glossy" }]) {
      const p = renderPolicy(spec as never);
      expect(p).toEqual({
        style: "explainer",
        musicBed: true,
        captions: true,
        rateMin: RATE_MIN,
        rateMax: RATE_MAX,
      });
    }
  });

  it("recorded: no music bed, no captions, warp held near real time", () => {
    const p = renderPolicy({ style: "recorded" });
    expect(p.style).toBe("recorded");
    expect(p.musicBed).toBe(false);
    expect(p.captions).toBe(false);
    expect(p.rateMin).toBe(RECORDED_RATE_MIN);
    expect(p.rateMax).toBe(RECORDED_RATE_MAX);
    expect(RECORDED_RATE_MAX).toBeLessThan(RATE_MAX);
    expect(RECORDED_RATE_MIN).toBeGreaterThan(RATE_MIN);
    expect(videoStyle({ style: "recorded" })).toBe("recorded");
  });
});

describe("ProgramSpecSchema style: recorded", () => {
  it("accepts a cut of body_walkthrough beats only", () => {
    const spec = parseProgramSpec(recordedYaml());
    expect(spec.style).toBe("recorded");
    expect(spec.beats?.map((b) => b.kind)).toEqual(["body_walkthrough", "body_walkthrough"]);
  });

  it("rejects a title card or end card", () => {
    expect(() =>
      parseProgramSpec(recordedYaml("", "  - { id: outro, kind: outro_card, seconds: 5 }")),
    ).toThrow(/only body_walkthrough beats/);
  });

  it("rejects a lower-third overlay", () => {
    expect(() => parseProgramSpec(recordedYaml("", "", '"Assign a dispenser"'))).toThrow(
      /no post-hoc overlays/,
    );
  });

  it("leaves a spec without style untouched (explainer)", () => {
    const yaml = recordedYaml().replace("style: recorded\n", "");
    const spec = parseProgramSpec(yaml);
    expect(spec.style).toBeUndefined();
    expect(renderPolicy(spec).style).toBe("explainer");
  });
});

describe("buildActionWarp rate override", () => {
  // Footage 20s, beat 10s: the unconstrained rate would be 2.0x.
  const args = {
    marks: [{ on_seconds: 10, words: ["water"] }],
    resolveWord: (w: string) => (w === "water" ? 5 : null),
    footageOnscreenSec: 20,
    voSec: 10,
    beatSec: 10,
  };

  it("explainer band lets the footage run at 2x", () => {
    const pieces = buildActionWarp(args);
    expect(Math.max(...pieces.map((p) => p.rate))).toBeCloseTo(2.0, 3);
  });

  it("recorded band clamps the footage to near real time", () => {
    const pieces = buildActionWarp({ ...args, rateMin: RECORDED_RATE_MIN, rateMax: RECORDED_RATE_MAX });
    for (const p of pieces) {
      expect(p.rate).toBeLessThanOrEqual(RECORDED_RATE_MAX);
      expect(p.rate).toBeGreaterThanOrEqual(RECORDED_RATE_MIN);
    }
  });
});
