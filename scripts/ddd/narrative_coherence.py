"""DDD narrative-coherence check.

Catches the failure modes the actionability eval doesn't:
  1. OUTCOME LEAKAGE — a scene asserts specific values that a LATER step
     (or the action this scene itself describes) is supposed to GENERATE.
     The persona can't know those values in advance of running the action.
  2. (Temporal order — reserved for future LLM augmentation.)
  3. (Persona-can't-do-that — reserved for future LLM augmentation.)

For v1 this implements rule (1) only, using a small, high-precision pattern
catalog targeted at the failure modes seen in real specs. Verbs come from a
small "outcome verbs" set; numeric patterns come from a short catalog tuned
for false-positive avoidance (input configs like "100 buildings per work area"
or "opportunity 123" are NOT flagged).

The rule is intentionally narrow: per-scene `show` + `concept_claim` fields
are the disciplined area (each beat = one moment). The narrative paragraph
is a higher-level overview and is allowed to mention outcomes in passing.

CLI:
    python -m scripts.ddd.narrative_coherence <spec_path>
    exits 0 on pass, 1 on fail, 2 on usage error.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Union

from scripts.ddd.spec_io import load_spec
import yaml

from scripts.ddd.schemas.models import UnifiedSpec, Verdict


# ---------------------------------------------------------------------------
# Outcome pattern catalog.
#
# Each entry: (regex, label, why_it's_an_outcome). The regex is matched
# case-insensitively against scene.show + scene.concept_claim. Patterns are
# chosen for HIGH PRECISION — they should not false-positive on legitimate
# input-config mentions ("100 buildings per work area", "opportunity 123").
#
# When adding patterns, prefer specificity over recall: a missed leak is OK,
# a false-positive that blocks a real spec is costly.
# ---------------------------------------------------------------------------

OUTCOME_PATTERNS: list[tuple[str, str, str]] = [
    (
        r"\b\d+\s+work\s+areas?\b(?!\s+(?:apiece|each))",
        "work-area count",
        "the per-plan work-area count is computed by the materializer from the boundary + building density — not knowable in advance",
    ),
    (
        r"\b\d+(?:\.\d+)?\s*%\s*imbalance\b",
        "workload imbalance %",
        "workload imbalance is a KPI computed after work areas are assigned — output, not input",
    ),
    (
        r"\bimbalance\s+(?:of\s+|at\s+)?\d+(?:\.\d+)?\s*%",
        "workload imbalance %",
        "workload imbalance is a KPI computed after work areas are assigned — output, not input",
    ),
    (
        r"\bfit\s+(?:score\s+)?\d+(?:\.\d+)?\b",
        "fit score",
        "fit score is a KPI computed from work areas + footprints — output, not input",
    ),
    (
        r"★\s*\d+(?:\.\d+)?",
        "fit score (★)",
        "the ★-marked fit score is a KPI computed by the system — output, not input",
    ),
    (
        r"\b\d+(?:\.\d+)?\s*km(?:\s+(?:max\s+)?travel|\s+max\s+spread)\b",
        "max travel / spread (km)",
        "max travel km / max spread km is a KPI computed from work-area geometry — output",
    ),
    (
        r"\b\d+\s+Approved\b\s+\+\s+\d+\s+In\s+review\b",
        "lifecycle split count",
        "the Approved/In review split is the consequence of LLO decisions — output of beat 5, not knowable upfront",
    ),
    (
        r"\b\d+\s*/\s*\d+\s+(?:Approved|In\s+review|Excluded)\b",
        "lifecycle split count",
        "the Approved/In review/Excluded split is the consequence of LLO decisions — output, not knowable upfront",
    ),
]


def _check_scene_text(text: str) -> list[dict[str, str]]:
    """Return a list of outcome-leak findings from one scene's text blob."""
    findings: list[dict[str, str]] = []
    for pattern, label, reason in OUTCOME_PATTERNS:
        for m in re.finditer(pattern, text, flags=re.IGNORECASE):
            findings.append(
                {
                    "label": label,
                    "match": m.group(0).strip(),
                    "reason": reason,
                }
            )
    return findings


def _scene_blob(scene: Any) -> str:
    """Concatenate the per-scene fields we audit (show + concept_claim).

    Note: the top-level ``spec.narrative`` paragraph is intentionally NOT
    audited — that's an overview field allowed to mention outcomes in passing.
    """
    parts = []
    if getattr(scene, "show", None):
        parts.append(scene.show)
    if getattr(scene, "concept_claim", None):
        parts.append(scene.concept_claim)
    return "\n".join(parts)


def narrative_coherence(spec_obj_or_path: Union[str, Path, UnifiedSpec, dict, None]) -> Verdict:
    """Run narrative-coherence QA on a unified spec.

    Returns a ``Verdict``:
        - ``verdict="pass"`` when no outcome leakage is detected.
        - ``verdict="fail"`` with ``blocking_reason`` listing every leak.
    """
    # Load → spec object.
    spec: UnifiedSpec | None
    if spec_obj_or_path is None:
        return Verdict(
            dimensions={},
            overall_score=0.0,
            verdict="fail",
            blocking_reason="narrative_coherence: None passed",
        )
    if isinstance(spec_obj_or_path, UnifiedSpec):
        spec = spec_obj_or_path
    elif isinstance(spec_obj_or_path, dict):
        try:
            spec = UnifiedSpec(**spec_obj_or_path)
        except Exception as e:
            return Verdict(
                dimensions={},
                overall_score=0.0,
                verdict="fail",
                blocking_reason=f"narrative_coherence: spec did not parse: {e}",
            )
    else:
        path = Path(spec_obj_or_path)
        if not path.exists():
            return Verdict(
                dimensions={},
                overall_score=0.0,
                verdict="fail",
                blocking_reason=f"narrative_coherence: spec path not found: {path}",
            )
        try:
            spec = load_spec(path)
        except Exception as e:
            return Verdict(
                dimensions={},
                overall_score=0.0,
                verdict="fail",
                blocking_reason=f"narrative_coherence: spec did not parse: {e}",
            )

    # Per-scene audit.
    leak_lines: list[str] = []
    leak_count = 0
    for i, scene in enumerate(spec.scenes, start=1):
        blob = _scene_blob(scene)
        if not blob:
            continue
        findings = _check_scene_text(blob)
        if not findings:
            continue
        leak_count += len(findings)
        title = getattr(scene, "title", "") or f"scene {i}"
        for f in findings:
            leak_lines.append(
                f"  scene {i} ({title!r}): outcome-leak — {f['label']} {f['match']!r}; {f['reason']}"
            )

    if leak_lines:
        return Verdict(
            dimensions={"outcome_leakage": {"score": 1.0, "weight": 1.0}},
            overall_score=1.0,
            verdict="fail",
            blocking_reason=(
                f"narrative_coherence: {leak_count} outcome-leak{'s' if leak_count != 1 else ''} "
                f"in per-scene fields (show / concept_claim). A beat is allowed to describe the "
                f"persona's ACTION but cannot pre-commit to system-generated VALUES; those are "
                f"revealed by the rendered demo. Leaks:\n" + "\n".join(leak_lines)
            ),
        )

    return Verdict(
        dimensions={"outcome_leakage": {"score": 5.0, "weight": 1.0}},
        overall_score=5.0,
        verdict="pass",
    )


def _cli() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m scripts.ddd.narrative_coherence <spec_path>", file=sys.stderr)
        return 2
    verdict = narrative_coherence(sys.argv[1])
    if verdict.verdict == "pass":
        print("narrative_coherence: pass")
        return 0
    print(verdict.blocking_reason or "narrative_coherence: fail", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(_cli())
