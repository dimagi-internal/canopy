"""DDD UnifiedSpec structural QA (SP2.2).

Pure-python, no LLM.  Delegates structural + semantic rules to validate.py;
adds QA-specific falsifiability checks on Scene.concept_claim.

Exposes:
    spec_qa(spec_obj_or_path) -> Verdict

Rules checked:
    (via validate.py → _semantic_unified_spec)
    (b) Scene.provenance must match a SpineItem.id in the linked why_brief
    (e) Scene.persona must be defined in the personas dict
    (f) why_brief declared but not resolvable → problem
    Plus Pydantic-required fields (name, narrative, base_url, personas, scenes)

    (QA-specific, not in validate.py)
    (g) every Scene.concept_claim must be non-empty / non-whitespace
    (h) every Scene.concept_claim must be falsifiable — fails if it matches
        a banned list of pure-marketing phrases, OR is too short to be specific.
    (i) ${...} placeholders in scene URLs / action targets ⇒ the spec must
        declare a `setup:` block with `outputs:` (the synthetic generator that
        mints those variables) OR bind them on camera with a `capture` action.
        Validation is ORDER-AWARE: a ${var} is valid iff a setup output OR a
        `capture` in an EARLIER scene provides it (a var used before anything
        binds it is a violation). Declared-but-unused outputs are fine.
    (j) "show, don't tell" — a scene that DECLARES actions but scripts ONLY
        non-effecting ones (hover/scroll_to/wait_for) while its narration /
        concept_claim promises an effecting verb (create/fill/submit/award/
        select/publish/enter/type) is a hover-only "claimed, not shown" demo.
        Scoped to the actions list (NOT a prose-only verb check — a scene with
        no actions is exempt). Distinct from the removed falsifiability
        verb-check, which scanned prose alone and false-positived.

Returns the ``Verdict`` model from scripts/ddd/schemas/models.py.
``verdict="pass"`` when all rules pass; ``verdict="fail"`` with a
``blocking_reason`` listing every violation when any rule fires.

On missing path, malformed input, or None, returns a ``fail`` Verdict
instead of raising — consistent with the ``-> Verdict`` contract.

CLI:
    python -m scripts.ddd.spec_qa <spec_path>
    exits 0 on pass, 1 on fail, 2 on usage error
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Union

import yaml

from scripts.ddd.schemas.models import UnifiedSpec, Verdict
from scripts.ddd.validate import validate
from scripts.narrative.substitution import (
    ordered_placeholder_violations,
    scene_capture_vars,
    scenes_placeholders,
)

# ---------------------------------------------------------------------------
# Banned marketing phrases (case-insensitive substring match).
# If a concept_claim contains any of these, it is considered non-falsifiable.
# ---------------------------------------------------------------------------

_BANNED_PHRASES: list[str] = [
    "world-class",
    "world class",
    "seamless",
    "powerful",
    "robust",
    "best-in-class",
    "best in class",
    "cutting-edge",
    "cutting edge",
    "state-of-the-art",
    "state of the art",
    "revolutionary",
    "game-changing",
    "game changing",
    "innovative",
    "next-generation",
    "next generation",
]


# ---------------------------------------------------------------------------
# Status-tag parentheticals that don't belong in a story-beat scene title.
# A scene title is a moment in the demo the viewer watches ("Maya picks the
# district"), NOT a design-doc status annotation ("Pick an area (frontier)").
# These leak build-status thinking into the narrative; the build status lives
# in the why_brief spine + feature provenance, not the title.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# "Show, don't tell" gate (action-fidelity).
#
# A scene that NARRATES an effecting act — "create / fill out / submit / award /
# select / publish / enter / type" — but scripts only NON-effecting actions
# (hover / scroll_to / wait_for) is the "claimed, not shown" demo: the
# walkthrough asserts an action it never performs. The judge can only see one
# still frame per scene, so a hover-only "create the solicitation" scene scores
# the same as one that genuinely fills + submits. Catch it structurally, before
# any render or judge, at the spec gate.
#
# SCOPE — deliberately narrow, to stay clear of the removed falsifiability
# verb-check (which scanned the claim for verbs and caused FALSE POSITIVES):
#   * This rule is about the ACTIONS LIST, not the prose. It fires ONLY when the
#     scene DECLARES actions (a non-empty `actions:` block) that are ALL
#     non-effecting. A scene with NO actions at all is the legacy scroll-pan
#     narrative beat — exempt (the narration is the whole point; there is no
#     "action it should have performed").
#   * The effecting verbs below are matched as whole words in the scene's
#     narrative / concept_claim only to decide whether the narration PROMISES an
#     action. The decision to BLOCK is gated on the actions list, never on the
#     prose alone — so a nominalized claim with no scripted actions never trips.
# ---------------------------------------------------------------------------

# Action kinds (from scripts.narrative.models.ACTION_KINDS) that EFFECT a state
# change. Defined locally so this structural gate has no recorder dependency.
_EFFECTING_ACTION_KINDS: frozenset[str] = frozenset(
    {"click", "click_menu", "fill", "select", "type", "press", "draw"}
)

# Concrete effecting verbs a narration uses to PROMISE an action happened.
# Matched as whole words (case-insensitive) against narrative + concept_claim.
_EFFECTING_VERBS: list[str] = [
    "create", "creates", "created", "creating",
    "fill", "fills", "filled", "filling", "fill out", "fills out",
    "submit", "submits", "submitted", "submitting",
    "award", "awards", "awarded", "awarding",
    "select", "selects", "selected", "selecting",
    "publish", "publishes", "published", "publishing",
    "enter", "enters", "entered", "entering",
    "type", "types", "typed", "typing",
]


def _narrated_effecting_verb(text: str) -> str | None:
    """Return the first effecting verb the text promises (whole-word), or None."""
    import re

    lowered = (text or "").lower()
    for verb in _EFFECTING_VERBS:
        # whole-word / phrase boundary match so "create" doesn't hit "created"
        # twice and "type" doesn't hit "prototype".
        if re.search(rf"(?<!\w){re.escape(verb)}(?!\w)", lowered):
            return verb
    return None


_BANNED_TITLE_TAGS: list[str] = [
    "(frontier)",
    "(gap)",
    "(the hero)",
    "(hero)",
    "(built)",
    "(wip)",
    "(future)",
    "(planned)",
    "(stretch)",
    "(tbd)",
    "(todo)",
    "(coming soon)",
    "(not built)",
    "(unbuilt)",
]


def _is_falsifiable(claim: str) -> bool:
    """Return True if the claim is falsifiable (not vacuous marketing copy).

    A claim is NOT falsifiable if:
    1. It is empty or whitespace-only.
    2. It contains any banned marketing phrase.
    3. It is fewer than 5 words (too short to be specific).

    Verb-pattern detection has been intentionally removed.  It caused false
    positives (blocking legitimate nominalized claims like "GPS pinning accuracy
    within 5 meters" or "Per-stratum allocation proportional to population") and
    false negatives (accepting fluff containing a copula like "The system is
    good").  Subtle vacuousness judgment — distinguishing a real claim from an
    articulate-but-empty one — belongs to the LLM concept judge (SP3), not this
    binary structural gate.
    """
    stripped = claim.strip()
    if not stripped:
        return False

    # Check for banned phrases (case-insensitive)
    lower = stripped.lower()
    for phrase in _BANNED_PHRASES:
        if phrase in lower:
            return False

    # Minimum specificity: a falsifiable claim needs enough substance.
    # Subtle vacuousness is the LLM concept judge's job (SP3), not this regex gate.
    if len(stripped.split()) < 5:
        return False

    return True


def spec_qa(
    spec_obj_or_path: Any,
) -> Verdict:
    """Run structural QA on a UnifiedSpec.

    Delegates structural + semantic rules to ``validate("unified_spec", ...)``
    (which covers persona-defined, provenance-to-spine-id, and required-field
    checks), then adds QA-specific falsifiability checks on every
    ``Scene.concept_claim`` that ``validate`` does not enforce.

    The why_brief is resolved by validate() from the spec file's own
    ``why_brief`` field (relative to the spec file path) — no separate
    path argument is needed.

    Parameters
    ----------
    spec_obj_or_path:
        Either a ``UnifiedSpec`` instance, a ``Path`` / string path to a
        YAML or JSON file containing a UnifiedSpec, or a plain dict.

    Returns
    -------
    Verdict
        ``verdict="pass"`` if all structural rules pass.
        ``verdict="fail"`` with ``blocking_reason`` listing every violation.
        Never raises — missing files and parse/validation errors are returned
        as a ``fail`` Verdict.
    """
    # ------------------------------------------------------------------ guard
    if spec_obj_or_path is None:
        return Verdict(
            schema_version=1,
            dimensions={},
            overall_score=0.0,
            verdict="fail",
            blocking_reason="spec_obj_or_path is None",
            fix_recommendation="Provide a valid UnifiedSpec object or path.",
        )

    # -------------------------------------------------------------- delegate
    # validate() handles loading, Pydantic validation, persona check, and
    # provenance cross-check.  We collect its problems and add our own.
    _ok, _validate_problems = validate("unified_spec", spec_obj_or_path)
    violations: list[str] = list(_validate_problems)

    # ------------------------------------------------- load spec for QA checks
    # We need the parsed spec object to run QA-specific checks.
    # If validate() already failed on loading, we may not be able to parse.
    spec: UnifiedSpec | None = None

    if isinstance(spec_obj_or_path, UnifiedSpec):
        spec = spec_obj_or_path
    elif isinstance(spec_obj_or_path, (str, Path)):
        path = Path(spec_obj_or_path)
        if path.exists():
            try:
                text = path.read_text()
                if path.suffix.casefold() == ".json":
                    raw = json.loads(text)
                else:
                    raw = yaml.safe_load(text)
                from pydantic import ValidationError

                try:
                    spec = UnifiedSpec.model_validate(raw)
                except ValidationError:
                    spec = None  # structural errors already captured via validate()
            except Exception:
                spec = None  # loading errors already captured via validate()
        # else: file not found — validate() already captured the error
    elif isinstance(spec_obj_or_path, dict):
        try:
            spec = UnifiedSpec.model_validate(spec_obj_or_path)
        except Exception:
            spec = None

    # --------------------------------------------------- QA-specific checks
    if spec is not None:
        # (i) data-setup contract + late binding: ${...} placeholders in scene
        # URLs / action targets are resolved either UP FRONT from setup.outputs
        # (ids minted before the render) or AT RUNTIME by an on-camera
        # ``capture`` action (ids minted during the render). A spec that uses a
        # ${var} with NEITHER source records a literal "/runs/${run_id}/" URL.
        # (The converse is fine: a setup block whose outputs declare variables
        # the scenes never use is not an error.)
        scene_dicts = [s.model_dump() for s in spec.scenes]
        placeholders = scenes_placeholders(scene_dicts)
        capture_bound: set[str] = set()
        for sd in scene_dicts:
            capture_bound.update(scene_capture_vars(sd))
        if placeholders:
            # Vars NOT bound by any capture must come from setup.outputs.
            from_setup_needed = placeholders - capture_bound
            if from_setup_needed:
                if spec.setup is None:
                    violations.append(
                        f"spec uses ${{...}} placeholder(s) ({', '.join(sorted(from_setup_needed))}) "
                        "that no `capture` action binds, but declares no `setup:` block — declare "
                        "setup.command + setup.outputs (the synthetic generator that mints these "
                        "variables), add a `capture` action that mints them on camera, or remove them"
                    )
                elif not spec.setup.outputs:
                    violations.append(
                        f"spec uses ${{...}} placeholder(s) ({', '.join(sorted(from_setup_needed))}) "
                        "that no `capture` action binds, but setup.outputs is not declared — the "
                        "recorder has no variables file to resolve them from; point setup.outputs at "
                        "the JSON the command emits (or bind them with a `capture` action)"
                    )
            # Order-aware: a ${var} used BEFORE any setup/capture provides it is
            # a violation even if SOME later capture binds the same name. Setup
            # outputs aren't known structurally (we don't run the command in QA),
            # so treat any non-capture-bound placeholder as a presumptive setup
            # output for ordering — only a use-before-capture of a CAPTURE-only
            # var is a hard order error here.
            setup_provided = placeholders - capture_bound
            for v in ordered_placeholder_violations(scene_dicts, setup_vars=setup_provided):
                violations.append(v)

        for scene in spec.scenes:
            title_lower = scene.title.lower()
            for tag in _BANNED_TITLE_TAGS:
                if tag in title_lower:
                    violations.append(
                        f"scene '{scene.title}': title contains the status tag '{tag}' — "
                        "a scene title is a story beat in the demo (what the viewer watches), "
                        "not a build-status annotation. Move build status to the why_brief "
                        "spine / feature provenance and retitle as a story moment."
                    )
                    break

            claim = scene.concept_claim
            if not _is_falsifiable(claim):
                if not claim.strip():
                    violations.append(
                        f"scene '{scene.title}': concept_claim is empty — "
                        "must describe an observable, falsifiable outcome"
                    )
                else:
                    violations.append(
                        f"scene '{scene.title}': concept_claim is not falsifiable — "
                        f"'{claim[:80]}' uses marketing language or is too short (fewer than 5 words); "
                        "write a specific, observable outcome instead"
                    )

            # "Show, don't tell" gate: a scene that scripts ONLY non-effecting
            # actions (hover/scroll_to/wait_for) while its narration promises an
            # effecting act is a hover-only "claimed, not shown" demo. Scoped to
            # the actions list — a scene with NO actions is exempt (legacy
            # narrative beat).
            # `overview` scenes (the goal-setting "why" opening) demonstrate no
            # capability — they set up the WHY, scripting only context actions
            # (wait_for/hold). Exempt them from the show-don't-tell gate.
            scene_actions = scene.actions or []
            if scene_actions and scene.role != "overview":
                action_kinds = {
                    (a.kind if hasattr(a, "kind") else (a.get("kind") if isinstance(a, dict) else ""))
                    for a in scene_actions
                }
                has_effecting = bool(action_kinds & _EFFECTING_ACTION_KINDS)
                if not has_effecting:
                    narrated = _narrated_effecting_verb(
                        f"{scene.narrative or ''} {scene.concept_claim or ''}"
                    )
                    if narrated:
                        non_effecting = ", ".join(sorted(k for k in action_kinds if k))
                        violations.append(
                            f"scene '{scene.title}' narrates '{narrated}' but performs no "
                            f"effecting action — its actions are only [{non_effecting}] "
                            "(hover/scroll/wait). Add the fill/click that effects the "
                            "narrated act, or soften the narration to match what the demo does."
                        )

            # DDD v3: every DEMO scene must have ≥1 feature with a non-vacuous
            # verify. An `overview` scene (the goal-setting "why" opening)
            # demonstrates no capability, so it is EXEMPT — it's judged on
            # whether its concept_claim + provenance establish the goal, not on
            # a feature. (A whole spec of overview scenes would build nothing;
            # keep them rare, usually exactly one, first.)
            if scene.role == "overview":
                pass
            elif not scene.features:
                violations.append(
                    f"scene '{scene.title}': has no features — "
                    "every demo scene must declare ≥1 Feature(id, description, verify) "
                    "so the narrative is buildable and verifiable "
                    "(or mark it `role: overview` if it only sets up the goal/why)"
                )
            else:
                for feature in scene.features:
                    verify_words = feature.verify.strip().split()
                    if len(verify_words) < 3:
                        violations.append(
                            f"scene '{scene.title}' feature '{feature.id}': "
                            f"verify is non-vacuous (needs ≥3 words) — "
                            f"'{feature.verify[:80]}' is too vague to be a real validation step; "
                            "write a concrete check (e.g. 'pytest: POST /form returns 200', "
                            "'assert confirm_message visible in DOM')"
                        )

    # A full narrated demo must open with a goal-setting `overview` scene (the
    # holistic "why", whose `narrative` is the opening voiceover) and give every
    # scene a `narrative` VO. Without them the render opens cold on scene 1 and
    # reads `show` aloud as the voiceover — narrating a screen description
    # instead of the story (the nutrition-demo regression). Scoped to real
    # walkthroughs (≥4 scenes); small utility demos and single-scene fixtures
    # are exempt. The 3 canonical narratives (verified-monitoring /
    # create-survey-solicitation / microplans-study-groups) are the template.
    if spec is not None and spec.scenes and len(spec.scenes) >= 4:
        overviews = [s for s in spec.scenes if (getattr(s, "role", "") or "") == "overview"]
        if not overviews:
            violations.append(
                "spec has no `role: overview` scene — a full demo (≥4 scenes) must open "
                "with a goal-setting overview scene (the holistic 'why', carrying its own "
                "`narrative` VO) so the video opens with an overview instead of cold on "
                "scene 1. See verified-monitoring / create-survey-solicitation / "
                "microplans-study-groups."
            )
        for s in spec.scenes:
            if not (getattr(s, "narrative", "") or "").strip():
                role = getattr(s, "role", "") or ""
                what = "the overview's narrative IS the opening voiceover" if role == "overview" else (
                    "without it the render reads `show` aloud as a screen description "
                    "instead of narrating the story"
                )
                violations.append(
                    f"scene '{s.title}': has no `narrative` VO — {what}. Write a "
                    "`narrative:` for the scene."
                )

    # --------------------------------------------------------------- verdict
    if not violations:
        return Verdict(
            schema_version=1,
            dimensions={},
            overall_score=1.0,
            verdict="pass",
            blocking_reason=None,
            fix_recommendation=None,
        )

    blocking_reason = "; ".join(violations)
    return Verdict(
        schema_version=1,
        dimensions={},
        overall_score=0.0,
        verdict="fail",
        blocking_reason=blocking_reason,
        fix_recommendation=(
            "Fix the listed violations before running the concept judge. "
            "Each concept_claim must describe a specific, observable, falsifiable "
            "outcome — e.g. 'Users can filter the task list by status and see only "
            "open tasks' not 'a world-class seamless experience'. "
            "Each scene must declare ≥1 Feature(id, description, verify) where verify "
            "is a concrete validation step of ≥3 words — e.g. "
            "'pytest: POST /form returns 200' or 'assert confirm_message visible in DOM'. "
            "Persona must be defined in the personas dict. "
            "Provenance must match a SpineItem.id in the linked why_brief. "
            "All required fields (name, narrative, base_url, personas, scenes) must be present."
        ),
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(
            "Usage: python -m scripts.ddd.spec_qa <spec_path>",
            file=sys.stderr,
        )
        sys.exit(2)

    _spec_path = sys.argv[1]

    _result = spec_qa(_spec_path)

    if _result.verdict == "pass":
        print("spec_qa: pass")
        sys.exit(0)
    else:
        print("spec_qa: fail")
        print(f"  blocking_reason: {_result.blocking_reason}")
        if _result.fix_recommendation:
            print(f"  fix_recommendation: {_result.fix_recommendation}")
        sys.exit(1)
