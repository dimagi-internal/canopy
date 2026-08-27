/**
 * DDD timing eval — deterministic VO↔UI sync verdict for a narrated walkthrough.
 *
 * The DDD concept/visual judge scores per-scene SCREENSHOTS for concept soundness
 * and never looks at the produced video, so the rendered video's audio-visual
 * timing (does the cursor reach a field when the VO names it; does footage run out
 * under a long narration) is unjudged. This module fills that gap with a PURE,
 * LLM-free verdict computed from the same data render.ts already has: each scene's
 * `action_marks` + its ElevenLabs VO alignment (via the actionsync anchors) + the
 * footage/VO durations.
 *
 * It scores ONE axis — the warp's actual job, which nothing else measures:
 * FIELD SYNC — of the form fields the narration actually NAMES, how many land on
 * their spoken word (became warp anchors) vs drift (dropped as inversions: the
 * narration enumerates fields in a different ORDER than the form lays them out).
 * It deliberately does NOT score held-frame overrun (VO playing over a held
 * frame): a teach hold under the voice is intentional, and the render already
 * prints a footage-vs-VO overrun line — folding it in here just double-counts and
 * punishes legitimate teach holds. The denominator is fields the narration NAMES,
 * so a scene that never names a field (a pure dashboard read) is "n/a", not a fail.
 *
 * Output mirrors the other ddd verdicts (overall_score 0–5 + pass|warn|fail +
 * per-scene rows + findings) so it can sit alongside verdict-concept.yaml.
 * Thresholds are PROVISIONAL (like the concept rubric) — calibrate on real runs.
 */

import { resolveAnchors, type ActionMark } from "./actionsync";

export interface TimingBeatInput {
  beatId: string;
  /** This beat's field action_marks (may be [] for a non-field beat). */
  marks: ActionMark[];
  /** Resolve a narration word to its VO start time (or null). */
  resolveWord: (word: string) => number | null;
  /** Beat VO duration (s). */
  voSec: number;
}

export interface SceneTiming {
  beatId: string;
  fieldMarks: number;
  /** Marks with ≥1 candidate word that resolves in the VO (= NAMED by narration). */
  wordMatchable: number;
  /** Marks that became monotonic warp anchors (land on their word). */
  anchored: number;
  /** wordMatchable − anchored: dropped because narration order ≠ footage order. */
  droppedInversions: number;
  /** Mean field↔word lag the warp removes (s), over anchored marks. */
  meanLagRemovedS: number;
  worstLagRemovedS: number;
}

export type Verdict = "pass" | "warn" | "fail";

export interface TimingVerdict {
  eval: "ddd-timing";
  /** 0–5 = 5 × field-sync coverage. `null` when no field is ever named (n/a). */
  overallScore: number | null;
  verdict: Verdict;
  syncedFields: number; // total anchored across beats
  wordMatchableFields: number; // fields the narration NAMES
  totalFieldMarks: number;
  /** anchored / wordMatchable (0–1). 1 ⇒ every NAMED field lands on its word. */
  coverage: number | null;
  /** anchored / totalFieldMarks (0–1) — the CROSS-RUN comparable rate.
   *
   * `coverage` (and therefore `overallScore`) divides by the fields the
   * narration NAMES, which is not constant between runs: rewrite a scene's
   * narration to name more of what it demonstrates and the denominator grows,
   * so the score can FALL while strictly more fields land on their word.
   * Observed: a rewrite took 10/27 → 11/41, i.e. one more field synced and the
   * score down 1.85 → 1.34. A keep-if-better loop comparing `overallScore`
   * across runs will revert the better video. Compare THIS instead — its
   * denominator is the footage's own field count, which only a re-record
   * changes. */
  syncRate: number | null;
  meanLagRemovedS: number;
  worstLagRemovedS: number;
  scenes: SceneTiming[];
  findings: string[];
}

const r2 = (n: number) => Math.round(n * 100) / 100;
const mean = (xs: number[]) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : 0);

// Provisional thresholds (calibrate on real runs).
export const COVERAGE_PASS = 0.75; // ≥75% of NAMED fields land on their word.
export const COVERAGE_WARN = 0.4;

/** Per-beat field-sync analysis. PURE. */
export function evaluateBeat(b: TimingBeatInput): SceneTiming {
  const matchable = b.marks.filter((m) =>
    (m.words ?? []).some((w) => {
      const t = b.resolveWord(w);
      return t != null && t >= 0 && t <= b.voSec + 0.001;
    }),
  ).length;
  const anchors = resolveAnchors(b.marks, b.resolveWord, b.voSec);
  // lag the warp removes = |footage on-screen src − VO word time| per anchor.
  const lags = anchors.map((a) => Math.abs(a.src - a.out));
  return {
    beatId: b.beatId,
    fieldMarks: b.marks.length,
    wordMatchable: matchable,
    anchored: anchors.length,
    droppedInversions: Math.max(0, matchable - anchors.length),
    meanLagRemovedS: r2(mean(lags)),
    worstLagRemovedS: r2(lags.length ? Math.max(...lags) : 0),
  };
}

/**
 * Whole-walkthrough field-sync verdict. PURE. `beats` are all walkthrough beats
 * (field beats carry marks; others contribute nothing but are harmless to pass).
 * Score = 5 × coverage; `null`/pass when the narration never names a field.
 */
export function evaluateTiming(beats: TimingBeatInput[]): TimingVerdict {
  const scenes = beats.map(evaluateBeat);
  const syncedFields = scenes.reduce((a, s) => a + s.anchored, 0);
  const wordMatchableFields = scenes.reduce((a, s) => a + s.wordMatchable, 0);
  const totalFieldMarks = scenes.reduce((a, s) => a + s.fieldMarks, 0);
  const allLags = scenes.flatMap((s) => (s.anchored > 0 ? [s.meanLagRemovedS] : []));

  // No narrated field anywhere ⇒ field-sync is n/a (e.g. a pure dashboard read).
  if (wordMatchableFields === 0) {
    return {
      eval: "ddd-timing",
      overallScore: null,
      verdict: "pass",
      syncedFields: 0,
      wordMatchableFields: 0,
      totalFieldMarks,
      coverage: null,
      syncRate: null,
      meanLagRemovedS: 0,
      worstLagRemovedS: 0,
      scenes,
      findings: ["no form field is named in the narration — field-sync is n/a for this walkthrough."],
    };
  }

  const coverage = syncedFields / wordMatchableFields;
  const syncRate = totalFieldMarks > 0 ? syncedFields / totalFieldMarks : null;
  const overallScore = r2(5 * coverage);
  const verdict: Verdict = coverage < COVERAGE_WARN ? "fail" : coverage < COVERAGE_PASS ? "warn" : "pass";

  const findings: string[] = [];
  if (syncRate !== null) {
    findings.push(
      `sync rate ${syncedFields}/${totalFieldMarks} marks (${r2(100 * syncRate)}%) — compare THIS across runs, not the score: the score divides by the fields the narration NAMES, which changes whenever you rewrite narration.`,
    );
  }
  for (const s of scenes) {
    if (s.worstLagRemovedS >= 3) {
      findings.push(
        `${s.beatId}: warp pulled a field that was ${s.worstLagRemovedS}s off onto its spoken word (${s.anchored}/${s.wordMatchable} named fields synced).`,
      );
    }
    if (s.droppedInversions > 0) {
      findings.push(
        `${s.beatId}: ${s.droppedInversions} named field(s) NOT synced. Either the narration names them in a different ORDER than the footage performs them, or two marks compete for one spoken word (a field whose id/note shares a token with a neighbour). Give each field a \`say:\` hint — ONE word, appearing ONCE in this beat's narration, in action order.`,
      );
    }
    const unnamed = s.fieldMarks - s.wordMatchable;
    if (unnamed > 0) {
      findings.push(
        `${s.beatId}: ${unnamed} field(s) on camera are never named in the narration, so nothing anchors them (${s.wordMatchable}/${s.fieldMarks} marks word-matchable).`,
      );
    }
  }

  return {
    eval: "ddd-timing",
    overallScore,
    verdict,
    syncedFields,
    wordMatchableFields,
    totalFieldMarks,
    coverage: r2(coverage),
    syncRate: syncRate === null ? null : r2(syncRate),
    meanLagRemovedS: r2(mean(allLags)),
    worstLagRemovedS: r2(scenes.length ? Math.max(0, ...scenes.map((s) => s.worstLagRemovedS)) : 0),
    scenes,
    findings,
  };
}
