/**
 * Every frame of a beat must be covered by exactly one warp piece.
 *
 * Uncovered frames fall through to the AbsoluteFill behind the pieces, which
 * is painted theme.colors.foreground (#0A0620). 3.5s of exactly that solid
 * colour shipped mid-scene in microplans-study-groups — sampled off the
 * rendered mp4 as rgb(10,6,32) — because only the LAST piece was stretched.
 */
import { describe, expect, it } from "vitest";
import { warpSequenceWindows } from "./Walkthrough";

const FPS = 30;

/** frames [0, total) covered exactly once */
function coverage(wins: { from: number; seqLen: number }[], total: number) {
  const hits = new Array(total).fill(0);
  for (const w of wins)
    for (let f = w.from; f < Math.min(w.from + w.seqLen, total); f++) hits[f]++;
  return {
    uncovered: hits.filter((h) => h === 0).length,
    doubled: hits.filter((h) => h > 1).length,
  };
}

describe("warpSequenceWindows", () => {
  it("leaves no uncovered frame when a gap sits between two pieces", () => {
    // piece 0 ends at 2s, piece 1 starts at 5s — a 3s hole
    const warp = [
      { outStartSec: 0, outDurSec: 2 },
      { outStartSec: 5, outDurSec: 2 },
    ];
    const total = 10 * FPS;
    const { uncovered, doubled } = coverage(warpSequenceWindows(warp, FPS, total), total);
    expect(uncovered).toBe(0);
    expect(doubled).toBe(0);
  });

  it("covers the head when the first piece starts after zero", () => {
    const warp = [{ outStartSec: 1.5, outDurSec: 2 }];
    const total = 5 * FPS;
    expect(coverage(warpSequenceWindows(warp, FPS, total), total).uncovered).toBe(0);
  });

  it("still runs the last piece to the end of the beat", () => {
    const warp = [{ outStartSec: 0, outDurSec: 1 }];
    const total = 6 * FPS;
    const [w] = warpSequenceWindows(warp, FPS, total);
    expect(w.seqLen).toBe(total);
    expect(w.own).toBe(FPS); // beyond `own` the caller freezes
  });

  it("is a no-op for contiguous pieces", () => {
    const warp = [
      { outStartSec: 0, outDurSec: 2 },
      { outStartSec: 2, outDurSec: 3 },
    ];
    const wins = warpSequenceWindows(warp, FPS, 5 * FPS);
    expect(wins[0]).toMatchObject({ from: 0, own: 2 * FPS, seqLen: 2 * FPS });
    expect(wins[1].from).toBe(2 * FPS);
  });

  it("marks extended pieces so the caller knows to freeze", () => {
    const warp = [
      { outStartSec: 0, outDurSec: 1 },
      { outStartSec: 4, outDurSec: 1 },
    ];
    const wins = warpSequenceWindows(warp, FPS, 6 * FPS);
    expect(wins[0].seqLen).toBeGreaterThan(wins[0].own); // holds across the gap
  });
});
