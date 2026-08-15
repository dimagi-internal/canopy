"""The concept rubric is the authority for WHICH dimensions exist — read it, don't restate it.

canopy#491: the shipped ``rubric.yaml`` declared seven weighted dimensions while
``SKILL.md``'s dispatch prose said "It has six dimensions: …" and then told the
judge to read the rubric in full.  When the two disagreed the prose won, silently.
Three consecutive ACE runs judged six dimensions and recorded a weight vector that
matched neither file.  The omitted dimension was ``use_case_soundness`` — whose
findings are always ``route=CONCEPT, fix_kind=redesign`` — so the loop
under-detected exactly the class it most needed to catch.

This module is the single reader.  ``python -m scripts.ddd.concept_rubric
<rubric.yaml>`` prints the dimension set the judge must score; the companion test
(``tests/ddd/test_concept_eval_rubric.py``) fails CI if SKILL.md's prose ever
drifts from it again.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

#: Weights must sum to this, within tolerance.
WEIGHT_TOTAL = 1.0
WEIGHT_TOLERANCE = 1e-6


@dataclass(frozen=True)
class RubricDimension:
    id: str
    weight: float
    advisory: bool

    @property
    def gating(self) -> bool:
        """Advisory dimensions are scored and surfaced but never set the verdict."""
        return not self.advisory


@dataclass(frozen=True)
class ConceptRubric:
    dimensions: tuple[RubricDimension, ...]
    overall_rule: str

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(d.id for d in self.dimensions)

    @property
    def gating_ids(self) -> tuple[str, ...]:
        return tuple(d.id for d in self.dimensions if d.gating)

    @property
    def advisory_ids(self) -> tuple[str, ...]:
        return tuple(d.id for d in self.dimensions if d.advisory)

    @property
    def weights(self) -> dict[str, float]:
        return {d.id: d.weight for d in self.dimensions}

    def as_dict(self) -> dict:
        return {
            "overall_rule": self.overall_rule,
            "dimension_count": len(self.dimensions),
            "dimensions": [
                {"id": d.id, "weight": d.weight, "advisory": d.advisory} for d in self.dimensions
            ],
            "gating": list(self.gating_ids),
            "advisory": list(self.advisory_ids),
            "weight_total": round(sum(self.weights.values()), 6),
        }


def load(path: str | Path) -> ConceptRubric:
    """Parse ``rubric.yaml`` into the dimension set the judge must score."""
    raw = yaml.safe_load(Path(path).read_text()) or {}
    dims = []
    for d in raw.get("dimensions") or []:
        dims.append(
            RubricDimension(
                id=str(d["id"]),
                weight=float(d.get("weight", 0.0)),
                advisory=bool(d.get("advisory", False)),
            )
        )
    return ConceptRubric(dimensions=tuple(dims), overall_rule=str(raw.get("overall_rule", "lowest")))


def check(rubric: ConceptRubric) -> list[str]:
    """Structural problems with the rubric itself. Empty list == healthy."""
    problems: list[str] = []
    if not rubric.dimensions:
        problems.append("rubric declares no dimensions")
    total = sum(rubric.weights.values())
    if abs(total - WEIGHT_TOTAL) > WEIGHT_TOLERANCE:
        problems.append(f"weights sum to {total:.4f}, expected {WEIGHT_TOTAL}")
    if len(set(rubric.ids)) != len(rubric.ids):
        problems.append("duplicate dimension ids")
    if not rubric.gating_ids:
        problems.append("every dimension is advisory — nothing would ever gate")
    return problems


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv:
        print("usage: python -m scripts.ddd.concept_rubric <rubric.yaml>", file=sys.stderr)
        return 2
    rubric = load(argv[0])
    problems = check(rubric)
    print(json.dumps(rubric.as_dict(), indent=2))
    for p in problems:
        print(f"RUBRIC PROBLEM: {p}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["ConceptRubric", "RubricDimension", "check", "load", "main"]
