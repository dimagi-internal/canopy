"""DDD artifact validator (SP0.3).

Exposes:
  validate(kind, obj_or_path) -> tuple[bool, list[str]]

  dump_json_schemas(out_dir="scripts/narrative/schema/json") -> None

CLI:
  python -m scripts.ddd.validate <kind> <path>   # exits 0 on valid, 1 on invalid

Supported kinds: why_brief, unified_spec, verdict, review_request, run_state
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

# Generic narrative/eval/review models come from the neutral substrate; the
# DDD-only RunState stays in scripts.ddd.schemas.models.
from scripts.ddd.identity import scene_id
from scripts.ddd.schemas.models import RunState
from scripts.narrative.models import (
    Decision,
    Feature,
    ReviewRequest,
    UnifiedSpec,
    Verdict,
    WhyBrief,
)

_MODEL_MAP = {
    "why_brief": WhyBrief,
    "unified_spec": UnifiedSpec,
    "verdict": Verdict,
    "review_request": ReviewRequest,
    "run_state": RunState,
}


def _load(path: Path) -> Any:
    """Load YAML or JSON from *path*."""
    text = path.read_text()
    if path.suffix.casefold() == ".json":
        return json.loads(text)
    return yaml.safe_load(text)


def _semantic_why_brief(obj: WhyBrief) -> list[str]:
    """Semantic rules for WhyBrief.

    (a) grounded SpineItem must have ≥1 evidence with kind != 'assumed'
    (c) every Gap.claim_ref must resolve to a SpineItem.id
    (d) duplicate SpineItem.id is an error
    """
    problems: list[str] = []

    # (d) duplicate spine ids
    seen_ids: set[str] = set()
    for item in obj.spine:
        if item.id in seen_ids:
            problems.append(f"duplicate spine id: {item.id}")
        seen_ids.add(item.id)

    spine_ids = seen_ids  # reuse the set we just built

    for item in obj.spine:
        if item.status == "grounded":
            real = [e for e in item.evidence if e.kind != "assumed"]
            if not real:
                problems.append(
                    f"SpineItem '{item.id}' is grounded but has no non-assumed evidence."
                )

    for gap in obj.gaps:
        if gap.claim_ref not in spine_ids:
            problems.append(
                f"Gap '{gap.id}' has claim_ref '{gap.claim_ref}' "
                f"which does not match any SpineItem.id."
            )

    return problems


def _semantic_unified_spec(obj: UnifiedSpec, spec_path: Path | None) -> list[str]:
    """Semantic rules for UnifiedSpec.

    (b) when a why_brief is resolvable (relative to spec file), every
        Scene.provenance must match some SpineItem.id
    (e) Scene.persona must be a key in the personas dict
    (f) why_brief declared but not resolvable/invalid → problem (not silent)
    (v3-a) feature id must be unique within a scene
    (v3-b) every feature must have non-empty description AND non-empty verify
    """
    problems: list[str] = []

    # (e) every scene persona must exist in the personas map
    for scene in obj.scenes:
        if scene.persona not in obj.personas:
            problems.append(
                f"scene references undefined persona: {scene.persona}"
            )

    # (v3-a,b) feature checks per scene
    for scene in obj.scenes:
        seen_feature_ids: set[str] = set()
        for feature in scene.features:
            # (v3-a) duplicate feature ids within the scene
            if feature.id in seen_feature_ids:
                problems.append(
                    f"scene '{scene.title}' feature '{feature.id}': duplicate feature id — "
                    "feature ids must be unique within a scene"
                )
            seen_feature_ids.add(feature.id)

            # (v3-b) non-empty description
            if not feature.description.strip():
                problems.append(
                    f"scene '{scene.title}' feature '{feature.id}': description is empty — "
                    "must be a concrete, buildable unit description"
                )

            # (v3-b) non-empty verify
            if not feature.verify.strip():
                problems.append(
                    f"scene '{scene.title}' feature '{feature.id}': verify is empty — "
                    "must describe how to validate this feature is done "
                    "(e.g. an API assertion, a UI state check, a test command)"
                )

    # (build_order) if non-empty: each slug must map to an existing scene, no duplicates
    if obj.build_order:
        # Scene identity comes from scripts.ddd.identity — the SAME function
        # that GENERATES build_order in build_narrative_review_request. These
        # were two separate slug expressions and they disagreed the moment a
        # scene carried an explicit id.
        scene_slugs: set[str] = {scene_id(scene) for scene in obj.scenes}
        seen_order_slugs: set[str] = set()
        for slug in obj.build_order:
            if slug in seen_order_slugs:
                problems.append(
                    f"build_order contains duplicate slug: '{slug}'"
                )
            seen_order_slugs.add(slug)
            if slug not in scene_slugs:
                problems.append(
                    f"build_order references unknown scene id '{slug}' "
                    "(no scene declares this id)"
                )

    # (b) + (f) provenance cross-check when why_brief is declared
    if obj.why_brief:
        wb_path: Path | None = None
        if spec_path is not None:
            wb_path = spec_path.parent / obj.why_brief

        if wb_path is None or not wb_path.exists():
            problems.append(
                f"why_brief declared but not resolvable: {obj.why_brief}"
            )
        else:
            try:
                wb_data = _load(wb_path)
                wb = WhyBrief.model_validate(wb_data)
                spine_ids = {s.id for s in wb.spine}
                for scene in obj.scenes:
                    if scene.provenance not in spine_ids:
                        problems.append(
                            f"Scene '{scene.title}' has provenance '{scene.provenance}' "
                            f"which does not match any SpineItem.id in the why_brief."
                        )
            except (ValidationError, yaml.YAMLError, OSError):
                problems.append(
                    f"why_brief declared but not resolvable: {obj.why_brief}"
                )

    return problems


def validate(
    kind: str,
    obj_or_path: Any,
) -> tuple[bool, list[str]]:
    """Validate an artifact of *kind*.

    Parameters
    ----------
    kind:
        One of: why_brief, unified_spec, verdict, review_request, run_state
    obj_or_path:
        Either a ``Path`` / path-like to a YAML/JSON file, or a plain dict.

    Returns
    -------
    (ok, problems)
        ok=True means the artifact is structurally and semantically valid.
        problems is an empty list when ok=True, otherwise a list of strings.
    """
    if kind not in _MODEL_MAP:
        return False, [f"Unknown kind '{kind}'. Choose from {list(_MODEL_MAP)}."]

    model_cls = _MODEL_MAP[kind]
    spec_path: Path | None = None

    # Load if given a path
    if isinstance(obj_or_path, (str, Path)):
        spec_path = Path(obj_or_path)
        try:
            if kind == "unified_spec":
                # A spec may be a legacy unified file OR a recipe + narrative
                # lock pair; spec_io knows the difference and composes. Only
                # this kind is two-file — why_briefs, verdicts and run state
                # are single documents and load plainly.
                from scripts.ddd.spec_io import load_spec_raw

                raw = load_spec_raw(spec_path)
            else:
                raw = _load(spec_path)
        except Exception as exc:
            return False, [f"Failed to load file: {exc}"]
    else:
        raw = obj_or_path

    # Structural validation via Pydantic
    try:
        obj = model_cls.model_validate(raw)
    except ValidationError as exc:
        problems = [
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
            for e in exc.errors()
        ]
        return False, problems

    # Semantic validation
    problems: list[str] = []
    if kind == "why_brief":
        problems = _semantic_why_brief(obj)  # type: ignore[arg-type]
    elif kind == "unified_spec":
        problems = _semantic_unified_spec(obj, spec_path)  # type: ignore[arg-type]

    return (len(problems) == 0), problems


def dump_json_schemas(out_dir: str | Path = "scripts/narrative/schema/json") -> None:
    """Write JSON Schema files for every narrative + DDD model into *out_dir*.

    The generic narrative/eval/review models are the canonical cross-repo
    contract; ``RunState`` (DDD-only) is emitted alongside them.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    all_models = [
        WhyBrief,
        Feature,
        UnifiedSpec,
        Verdict,
        Decision,
        ReviewRequest,
        RunState,
    ]
    for model in all_models:
        schema = model.model_json_schema()
        dest = out / f"{model.__name__}.json"
        dest.write_text(json.dumps(schema, indent=2))
        print(f"Wrote {dest}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: python -m scripts.ddd.validate <kind> <path>", file=sys.stderr)
        sys.exit(2)

    _kind = sys.argv[1]
    _path = sys.argv[2]

    _ok, _problems = validate(_kind, _path)
    if _ok:
        print(f"Valid {_kind}.")
        sys.exit(0)
    else:
        print(f"Invalid {_kind}:")
        for p in _problems:
            print(f"  - {p}")
        sys.exit(1)
