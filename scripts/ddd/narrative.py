"""Narrative-agreement gate for demo-driven-development v3 (ddd-v3).

This module adds the missing narrative-agreement step to the DDD loop.
Before rendering, judging, or routing gaps, the user must explicitly AGREE
to the demo narrative — the story the demo tells to a prospective user.

Public API (pure functions — no network):
    build_narrative_review_request(spec, run_id) -> ReviewRequest
    apply_narrative_edits(spec_path, response_json) -> dict

CLI (touches network via review.post_review_request):
    python -m scripts.ddd.narrative post <spec_path> <run_id>
    python -m scripts.ddd.narrative apply <spec_path> <response_json_file>
    python -m scripts.ddd.narrative pull <slug> <dir>

Ownership (L1): canopy-web owns the story, git owns the render recipe, and
``pull`` is a one-way read into a generated ``<slug>.narrative.lock.json``.
There is no sync and no merge — see the Ownership block further down and
docs/superpowers/specs/2026-07-26-narrative-storyboard-and-ownership-design.md
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import yaml

from scripts.ddd.schemas.models import Decision, Gate, NarrationItem, ReviewRequest, UnifiedSpec
from scripts.ddd.spec_io import load_spec
from scripts.ddd.review import _review_id_from_url


# ---------------------------------------------------------------------------
# Narrative sentence helpers — used by build_narrative_review_request to show
# the LITERAL sentence per scene (so what the user reads in the top paragraph
# matches what they see in each scene card), and by apply_narrative_edits to
# round-trip an edited sentence back into spec.narrative (not concept_claim).
#
# Falls back to scene.concept_claim when the sentence count doesn't match the
# scene count — that keeps multi-sentence scenes (per gap-flexible-scene-length)
# and short narratives working without breaking the read side.
# ---------------------------------------------------------------------------

import re as _re

_SENTENCE_SPLIT_RE = _re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'])")

# Trailing "-YYYY-MM-DD-NNN" stamp on a run_id — kept in lockstep with
# canopy-web's apps/common/ddd.narrative_slug_from_run_id so the narrative slug
# derived here matches the one canopy-web groups artifacts under.
_RUN_ID_STAMP_RE = _re.compile(r"-\d{4}-\d{2}-\d{2}-\d+$")


def _narrative_slug_from_run_id(run_id: str) -> str:
    """``'verified-monitoring-2026-06-04-001'`` -> ``'verified-monitoring'``."""
    base = _RUN_ID_STAMP_RE.sub("", run_id or "").strip("-")
    return base or run_id or "(untitled)"


# ---------------------------------------------------------------------------
# Narrative lock — an approved narrative is durable INPUT.
#
# Once the narrative-agreement gate returns ``approve``, the spec is the
# human-owned narrative artifact: ddd-spec must not regenerate it and a new run
# reuses the whole spec verbatim. ``redraft`` clears the lock so it can be
# re-authored. The flag lives in the spec file (UnifiedSpec.narrative_locked) so
# it travels with the narrative, not the run.
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _set_narrative_lock(raw: dict, decision: str) -> bool:
    """Mutate ``raw``'s narrative-lock fields per a gate decision.

    ``approve`` → locked (+ timestamp); ``redraft`` → unlocked. Returns True iff
    the lock state changed. Any other decision is a no-op.
    """
    was = bool(raw.get("narrative_locked"))
    if decision == "approve":
        raw["narrative_locked"] = True
        raw["narrative_locked_at"] = _now_iso()
        return not was
    if decision == "redraft":
        raw["narrative_locked"] = False
        raw.pop("narrative_locked_at", None)
        return was
    return False


def is_narrative_locked(spec_path) -> bool:
    """True iff the spec file exists and is marked ``narrative_locked``.

    ddd-spec and the orchestrator call this before (re)authoring a spec: a locked
    narrative is reused verbatim, never regenerated.
    """
    p = Path(spec_path)
    if not p.exists():
        return False
    try:
        raw = yaml.safe_load(p.read_text()) or {}
    except Exception:
        return False
    return bool(raw.get("narrative_locked"))


def set_narrative_lock(spec_path, locked: bool) -> dict:
    """Explicitly lock/unlock a spec file (CLI + programmatic). Returns the new
    lock state and whether it changed."""
    p = Path(spec_path)
    raw = yaml.safe_load(p.read_text()) or {}
    changed = _set_narrative_lock(raw, "approve" if locked else "redraft")
    if changed:
        p.write_text(yaml.dump(raw, default_flow_style=False, allow_unicode=True))
    return {"narrative_locked": bool(raw.get("narrative_locked")), "changed": changed}


def _split_narrative_sentences(narrative: str) -> list[str]:
    """Split a paragraph into sentences. Conservative: splits on sentence-ending
    punctuation followed by whitespace + a capital letter (or opening quote).
    Returns sentence strings with leading/trailing whitespace stripped, in
    original order.
    """
    if not narrative:
        return []
    normalized = " ".join(narrative.split())
    parts = _SENTENCE_SPLIT_RE.split(normalized)
    return [p.strip() for p in parts if p.strip()]


def _scene_text_for_review(spec: "UnifiedSpec", scene_idx_zero_based: int) -> str:
    """The text shown for one scene in the review surface.

    Resolution order:
    1. ``scene.narrative`` if non-empty — the canonical per-scene narrative
       text. Supports multi-sentence scenes (gap-flexible-scene-length).
    2. Sentence-split of ``spec.narrative`` by position when the sentence
       count matches the scene count (1:1 fallback for legacy specs).
    3. ``scene.concept_claim`` as last-resort.
    """
    scene = spec.scenes[scene_idx_zero_based]
    s_nar = getattr(scene, "narrative", "")
    if s_nar and s_nar.strip():
        return s_nar.strip()
    sentences = _split_narrative_sentences(spec.narrative)
    if len(sentences) == len(spec.scenes):
        return sentences[scene_idx_zero_based]
    return scene.concept_claim


def _rebuild_spec_narrative(raw: dict) -> None:
    """Rebuild ``raw['narrative']`` as the join of per-scene narratives.

    For each scene in raw['scenes'], use ``scene['narrative']`` when set; else
    fall back to the sentence at that scene's position in the OLD narrative
    paragraph (if 1:1). Mutates raw in place. Used after apply_narrative_edits
    has set scene.narrative on the edited scenes so the top paragraph stays
    consistent with per-scene text.
    """
    scenes = raw.get("scenes") or []
    if not scenes:
        return
    old_paragraph = raw.get("narrative", "") or ""
    old_sentences = _split_narrative_sentences(old_paragraph)
    sentence_mode_fallback = len(old_sentences) == len(scenes)
    parts: list[str] = []
    for i, scene in enumerate(scenes):
        s_nar = (scene.get("narrative") or "").strip()
        if s_nar:
            parts.append(s_nar)
        elif sentence_mode_fallback:
            parts.append(old_sentences[i])
        else:
            parts.append(
                (scene.get("concept_claim") or "").strip()
                or scene.get("title", "")
                or ""
            )
    raw["narrative"] = " ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Slug helper (shared by build and apply so slugs always agree)
# ---------------------------------------------------------------------------


# Scene identity lives in scripts/ddd/identity.py so the validator can reach it
# without importing this module (which pulls in the network layer via
# scripts.ddd.review). Re-exported under the historical private names so every
# existing call site and test keeps working.
from scripts.ddd.identity import scene_id as _scene_id  # noqa: E402
from scripts.ddd.identity import slugify as _title_slug  # noqa: E402


def _beat_status_map(why_brief: dict | None) -> dict[str, str]:
    """Map each why-brief spine id -> ``"built"`` or ``"new"``.

    A beat is ``new`` (still to build, the "frontier") when its spine item is a
    gap (``status`` != ``grounded``) OR a why-brief gap (CAPABILITY / RESEARCH /
    DECISION) references it by ``claim_ref`` — a grounded claim with an open
    capability gap is still something we'd build, so it reads as ``new``, not
    ``built``. Everything else (grounded, no open gap) is ``built``.

    This mirrors canopy-web's client-side ``sceneIsFrontier`` exactly, so the
    BUILD SEQUENCE panel, the per-scene badges, and this API status all agree.
    Unknown / unprovenanced ids default to ``built`` only when grounded; absent
    from the spine they fall through to ``new`` at the call site.
    """
    referenced_by_gap: set[str] = set()
    for gap in (why_brief or {}).get("gaps") or []:
        if isinstance(gap, dict) and gap.get("claim_ref"):
            referenced_by_gap.add(gap["claim_ref"])

    status: dict[str, str] = {}
    for item in (why_brief or {}).get("spine") or []:
        if not isinstance(item, dict):
            continue
        sid = item.get("id")
        if not sid:
            continue
        is_frontier = (item.get("status") or "grounded") != "grounded" or sid in referenced_by_gap
        status[sid] = "new" if is_frontier else "built"
    return status


# ---------------------------------------------------------------------------
# Pure build function
# ---------------------------------------------------------------------------


def build_narrative_review_request(
    spec: UnifiedSpec,
    run_id: str,
    actionability: dict | None = None,
    why_brief: dict | None = None,
    narrative_slug: str | None = None,
) -> ReviewRequest:
    """Build a ReviewRequest for the narrative-agreement gate (DDD v3).

    This is a pre-render review — no video cut has been made yet, so
    ``video`` is an empty dict.  The narration list presents one item per
    scene, each carrying the scene's ``concept_claim`` as the editable
    story beat AND the scene's declared ``features[]`` (concrete buildable
    units).  The single decision uses the v3 approve/redraft shape.

    Parameters
    ----------
    spec:
        The fully-parsed UnifiedSpec for the feature under review.
    run_id:
        The DDD run identifier (e.g. ``"rooftop-surveys-2026-01-01-001"``).
    actionability:
        Optional actionability block populated by the caller after
        ``ddd-narrative-actionability-eval`` has run.  If provided, it is
        set on the returned ReviewRequest so the human reviewer can see the
        actionability score alongside the narration.  Leave ``None`` (the
        default) when posting before the eval has run.

    Returns
    -------
    ReviewRequest
        A ReviewRequest with gate="concept_change" ready to post via
        ``review.post_review_request``.
    """
    # Explicit narrative slug — the source of truth canopy-web files this review
    # under (request_json.narrative_slug). Falls back to the run_id slug (date
    # stamp stripped), matching canopy-web's own narrative_slug_from_run_id().
    resolved_narrative_slug = (narrative_slug or "").strip() or _narrative_slug_from_run_id(run_id)
    # Derive built|new per beat from the why-brief (mirrors canopy-web's
    # sceneIsFrontier): grounded-and-ungapped spine items are built, gaps are new.
    beat_status = _beat_status_map(why_brief)
    narration = [
        NarrationItem(
            scene=i,
            id=_scene_id(scene),
            title=scene.title,
            persona=scene.persona,
            provenance=scene.provenance,
            text=_scene_text_for_review(spec, i - 1),
            features=scene.features,
            status=beat_status.get(scene.provenance, "new"),
        )
        for i, scene in enumerate(spec.scenes, start=1)
    ]

    # build_order: use spec's explicit order when set, else default to scene order
    build_order: list[str] = (
        spec.build_order
        if spec.build_order
        else [_scene_id(scene) for scene in spec.scenes]
    )

    decision = Decision(
        id="narrative-verdict",
        prompt=(
            "Approve this narrative as the build plan, or send it back to re-draft?"
        ),
        options=["approve", "redraft"],
        recommended="approve",
        **{"class": Gate.CONCEPT_CHANGE},
    )

    return ReviewRequest(
        run_id=run_id,
        narrative_slug=resolved_narrative_slug,
        gate=Gate.CONCEPT_CHANGE,
        video={},
        narration=narration,
        narrative=spec.narrative,
        personas={k: p.model_dump() for k, p in spec.personas.items()},
        why_brief=why_brief or {},
        autonomous_audit=[],
        decisions=[decision],
        actionability=actionability,
        build_order=build_order,
    )


# ---------------------------------------------------------------------------
# Disk-touching apply function
# ---------------------------------------------------------------------------


def _generate_feature_id(description: str, existing_ids: set[str]) -> str:
    """Generate a stable id for a new feature from its description.

    Uses the same slug technique as ``_title_slug`` to produce a deterministic,
    human-readable id.  If the candidate collides with an existing id, appends
    a numeric suffix.
    """
    base = re.sub(r"[^a-z0-9]+", "-", description.lower()).strip("-")[:40]
    if not base:
        base = "feature"
    candidate = base
    n = 1
    while candidate in existing_ids:
        n += 1
        candidate = f"{base}-{n}"
    return candidate


_PERSONA_FIELDS = ("name", "role", "intro", "org", "color")
_SPINE_FIELDS = ("claim", "rationale")
_GAP_FIELDS = ("detail", "proposed_action")


def _apply_persona_edits(raw: dict, response_json: dict) -> int:
    """Apply ``edited_personas`` onto ``raw['personas']`` in place.

    Payload shape: ``{"<key>": {"name": ..., "org": ..., "role": ..., "intro": ...}}``
    (partial — only changed fields). Unknown keys are ignored (the key is the
    persona's stable identity and is never created/renamed here). Returns the
    number of fields changed.
    """
    edited: dict = response_json.get("edited_personas") or {}
    if not edited:
        return 0
    personas: dict = raw.get("personas") or {}
    changed = 0
    for key, fields in edited.items():
        if key not in personas or not isinstance(fields, dict):
            continue
        for f in _PERSONA_FIELDS:
            if f in fields and personas[key].get(f) != fields[f]:
                personas[key][f] = fields[f]
                changed += 1
    if changed:
        raw["personas"] = personas
    return changed


def _apply_why_brief_edits(spec_path: Path, raw: dict, response_json: dict) -> int:
    """Apply ``edited_why_brief`` onto the why-brief file referenced by the spec.

    Payload shape::

        {"problem": "...",
         "spine": {"<id>": {"claim": "...", "rationale": "..."}},
         "gaps":  {"<id>": {"detail": "...", "proposed_action": "..."}}}

    Only prose fields are editable; ids/status/type/claim_ref are structural and
    left untouched. Writes the why-brief file back in place. Returns the number of
    fields changed (0 if no edits, no why_brief link, or the file is unreadable).
    """
    edited: dict = response_json.get("edited_why_brief") or {}
    wb_rel = raw.get("why_brief")
    if not edited or not wb_rel:
        return 0
    wb_path = (Path(spec_path).parent / wb_rel).resolve()
    try:
        wb = yaml.safe_load(wb_path.read_text())
    except Exception:
        return 0
    if not isinstance(wb, dict):
        return 0

    changed = 0
    if "problem" in edited and wb.get("problem") != edited["problem"]:
        wb["problem"] = edited["problem"]
        changed += 1

    spine_edits: dict = edited.get("spine") or {}
    for item in wb.get("spine") or []:
        e = spine_edits.get(item.get("id"))
        if not isinstance(e, dict):
            continue
        for f in _SPINE_FIELDS:
            if f in e and item.get(f) != e[f]:
                item[f] = e[f]
                changed += 1

    gap_edits: dict = edited.get("gaps") or {}
    for gap in wb.get("gaps") or []:
        e = gap_edits.get(gap.get("id"))
        if not isinstance(e, dict):
            continue
        for f in _GAP_FIELDS:
            if f in e and gap.get(f) != e[f]:
                gap[f] = e[f]
                changed += 1

    if changed:
        wb_path.write_text(yaml.dump(wb, default_flow_style=False, allow_unicode=True, sort_keys=False))
    return changed


def apply_narrative_edits(
    spec_path: str | Path,
    response_json: dict,
) -> dict:
    """Apply narration edits from a resolved review response back onto the spec.

    Loads the spec YAML, reconciles scenes and features against the payload,
    writes the spec back to disk, and returns a structured result dict.

    The function supports two payload shapes:

    **New shape** (``edited_scenes`` key present)::

        {
            "decisions": {"narrative-verdict": "approve" | "redraft"},
            "edited_scenes": [
                {
                    "id": "<slug or 'new-<n>'>",
                    "title": "...",
                    "narration": "...",
                    "deleted": false,
                    "features": [
                        {"id": "<id or 'new-<n>'>", "description": "...",
                         "verify": "...", "feedback": "<optional>"}
                    ],
                    "feedback": "<optional per-scene>"
                }
            ],
            "overall_feedback": "<optional>"
        }

    Scene reconciliation rules:

    - ``"deleted": true`` → remove the matching ``Scene`` by slug id.
    - ``"new-*"`` id → append a new ``Scene`` (empty ``provenance``,
      first persona key, ``concept_claim`` from ``narration``).
    - existing id → update the matching ``Scene``'s ``concept_claim`` and
      reconcile its ``features``: update matching features, add ``new-*``
      features with stable generated ids, remove features absent from payload.

    **Legacy shape** (``narration_edits`` key, no ``edited_scenes``)::

        {
            "decisions": {"narrative-verdict": "approve" | "redraft"},
            "narration_edits": {"<scene-slug>": "<new concept_claim>", ...}
        }

    Legacy v2 decision values are coerced:
    ``"agree"``/``"edit"`` → ``"approve"``; ``"rethink"`` → ``"redraft"``.

    Parameters
    ----------
    spec_path:
        Path to the unified spec YAML file.
    response_json:
        The ``response_json`` dict from the resolved review.

    Returns
    -------
    dict
        New shape::

            {
                "decision": "approve" | "redraft",
                "applied": {"updated": n, "added": n, "deleted": n, "features_changed": n},
                "needs_grounding": ["<new scene title>", ...],
                "feedback": [
                    {"scope": "feature" | "scene" | "overall", "ref": str, "text": str}
                ]
            }

        Legacy shape (``narration_edits`` path) also returns ``"edited"`` for
        backward compatibility::

            {"decision": ..., "applied": ..., "needs_grounding": ...,
             "feedback": ..., "edited": n}
    """
    spec_path = Path(spec_path)
    raw = yaml.safe_load(spec_path.read_text())

    decisions: dict[str, str] = response_json.get("decisions", {}) or {}
    raw_decision: str = decisions.get("narrative-verdict", "approve")

    # Normalise to v3 vocabulary: legacy "agree"/"edit" → "approve"; "rethink" → "redraft".
    _LEGACY_MAP = {
        "agree": "approve",
        "edit": "approve",
        "rethink": "redraft",
    }
    decision: str = _LEGACY_MAP.get(raw_decision, raw_decision)

    # Lock-on-approve: an approved narrative becomes durable input (ddd-spec will
    # reuse the whole spec verbatim instead of regenerating it); redraft clears
    # the lock so it can be re-authored. Applied to `raw` here so it persists
    # through whichever write path runs below.
    lock_changed = _set_narrative_lock(raw, decision)

    # Persona + why-brief edits are independent of the scene-edit shape; apply
    # both up front. Persona edits mutate `raw` (written with the spec below);
    # why-brief edits write their own file.
    personas_changed = _apply_persona_edits(raw, response_json)
    why_brief_changed = _apply_why_brief_edits(Path(spec_path), raw, response_json)

    scenes: list[dict] = raw.get("scenes", [])

    # ------------------------------------------------------------------
    # NEW shape: edited_scenes
    # ------------------------------------------------------------------
    if "edited_scenes" in response_json:
        edited_scenes: list[dict] = response_json.get("edited_scenes") or []
        overall_feedback: str = response_json.get("overall_feedback", "") or ""

        # Collect feedback
        feedback: list[dict] = []
        if overall_feedback:
            feedback.append({"scope": "overall", "ref": "", "text": overall_feedback})

        # Build id→index map for existing scenes. Keyed on the STABLE scene id
        # (explicit `id`, else legacy title slug) so an edit that reworded the
        # title still lands on the right scene.
        slug_to_index: dict[str, int] = {}
        for idx, scene in enumerate(scenes):
            slug_to_index[_scene_id(scene)] = idx

        # Determine first persona key from the spec
        personas: dict = raw.get("personas", {})
        first_persona = next(iter(personas), "")

        # Counters
        updated = 0
        added = 0
        deleted = 0
        features_changed = 0

        needs_grounding: list[str] = []

        # Track which existing scene indices to keep (complement of deleted)
        indices_to_delete: set[int] = set()

        for es in edited_scenes:
            scene_id: str = es.get("id", "")
            scene_title: str = es.get("title", "")
            narration: str = es.get("narration", "")
            is_deleted: bool = bool(es.get("deleted", False))
            scene_feedback: str = es.get("feedback", "") or ""
            payload_features: list[dict] = es.get("features") or []

            if scene_id.startswith("new-"):
                if is_deleted:
                    # New scene marked deleted — just skip it
                    continue
                # ADD new scene
                new_features: list[dict] = []
                existing_feat_ids: set[str] = set()
                for f in payload_features:
                    feat_id = f.get("id", "")
                    feat_desc = f.get("description", "")
                    feat_verify = f.get("verify", "")
                    feat_feedback = f.get("feedback", "") or ""
                    if feat_id.startswith("new-"):
                        feat_id = _generate_feature_id(feat_desc, existing_feat_ids)
                    existing_feat_ids.add(feat_id)
                    new_features.append({
                        "id": feat_id,
                        "description": feat_desc,
                        "verify": feat_verify,
                    })
                    if feat_feedback:
                        feedback.append({
                            "scope": "feature",
                            "ref": feat_id,
                            "text": feat_feedback,
                        })

                new_scene: dict = {
                    # Mint the stable id ONCE, here, from the title it was born
                    # with. From this point the title is free to change.
                    "id": _title_slug(scene_title),
                    "persona": first_persona,
                    "title": scene_title,
                    "show": narration,
                    "concept_claim": narration,
                    "provenance": "",
                    "design_intent": None,
                    "features": new_features,
                }
                scenes.append(new_scene)
                # Update slug map for the newly added scene
                slug_to_index[_title_slug(scene_title)] = len(scenes) - 1
                needs_grounding.append(scene_title)
                added += 1
                features_changed += len(new_features)

                if scene_feedback:
                    feedback.append({
                        "scope": "scene",
                        "ref": _title_slug(scene_title),
                        "text": scene_feedback,
                    })

            else:
                # Existing scene
                idx = slug_to_index.get(scene_id)
                if idx is None:
                    # Unknown slug — silently skip
                    continue

                if is_deleted:
                    indices_to_delete.add(idx)
                    deleted += 1
                    continue

                # UPDATE existing scene.
                #
                # Canonical narrative roundtrip (v2 — supports multi-sentence
                # scenes per gap-flexible-scene-length):
                # - When narration changes, write to scene.narrative (the
                #   canonical per-scene field). spec.narrative is rebuilt as
                #   the join of per-scene narratives after the apply loop.
                # - concept_claim is no longer touched by narration edits;
                #   it stays a separate testable claim.
                scene_dict = scenes[idx]
                if narration:
                    old_text = (scene_dict.get("narrative") or "").strip()
                    if not old_text:
                        # First edit: derive old_text from the legacy mapping
                        # so we don't false-positive a no-op edit as a change.
                        sentences = _split_narrative_sentences(raw.get("narrative", "") or "")
                        if len(sentences) == len(scenes):
                            old_text = sentences[idx].strip()
                        else:
                            old_text = (scene_dict.get("concept_claim") or "").strip()
                    if narration.strip() != old_text:
                        scene_dict["narrative"] = narration.strip()
                        updated += 1

                # Reconcile features
                existing_features: list[dict] = scene_dict.get("features") or []
                existing_feat_map: dict[str, dict] = {
                    f.get("id", ""): f for f in existing_features
                }
                payload_feat_ids: set[str] = set()
                new_feature_list: list[dict] = []
                all_feat_ids: set[str] = set(existing_feat_map.keys())

                for f in payload_features:
                    feat_id = f.get("id", "")
                    feat_desc = f.get("description", "")
                    feat_verify = f.get("verify", "")
                    feat_feedback = f.get("feedback", "") or ""

                    if feat_id.startswith("new-"):
                        feat_id = _generate_feature_id(feat_desc, all_feat_ids)
                        all_feat_ids.add(feat_id)
                        new_feature_list.append({
                            "id": feat_id,
                            "description": feat_desc,
                            "verify": feat_verify,
                        })
                        features_changed += 1
                    else:
                        payload_feat_ids.add(feat_id)
                        if feat_id in existing_feat_map:
                            existing_f = existing_feat_map[feat_id]
                            changed = False
                            if feat_desc and existing_f.get("description") != feat_desc:
                                existing_f["description"] = feat_desc
                                changed = True
                            if feat_verify and existing_f.get("verify") != feat_verify:
                                existing_f["verify"] = feat_verify
                                changed = True
                            if changed:
                                features_changed += 1
                            new_feature_list.append(existing_f)
                        else:
                            # New feature with explicit id
                            new_feature_list.append({
                                "id": feat_id,
                                "description": feat_desc,
                                "verify": feat_verify,
                            })
                            features_changed += 1

                    if feat_feedback:
                        feedback.append({
                            "scope": "feature",
                            "ref": feat_id,
                            "text": feat_feedback,
                        })

                # Features in spec but absent from payload → removed (not appended)
                removed_count = len(existing_feat_map) - len(
                    [k for k in existing_feat_map if k in payload_feat_ids]
                )
                if removed_count > 0:
                    features_changed += removed_count

                scene_dict["features"] = new_feature_list
                scenes[idx] = scene_dict

                if scene_feedback:
                    feedback.append({
                        "scope": "scene",
                        "ref": scene_id,
                        "text": scene_feedback,
                    })

        # Remove deleted scenes (in reverse index order to preserve positions)
        for idx in sorted(indices_to_delete, reverse=True):
            scenes.pop(idx)

        raw["scenes"] = scenes

        # Now that per-scene narrative edits are applied (incl. multi-sentence
        # scenes), rebuild spec.narrative as the join of per-scene texts. The
        # top "demo" paragraph stays consistent with the per-scene cards.
        _rebuild_spec_narrative(raw)

        # ------------------------------------------------------------------
        # build_order: read from response, validate against surviving scenes,
        # drop deleted slugs, append newly-added scene slugs.
        # ------------------------------------------------------------------
        surviving_slugs: set[str] = {_scene_id(s) for s in scenes}
        # Slugs of scenes that were newly added in this edit cycle
        newly_added_slugs: list[str] = [
            _title_slug(t) for t in needs_grounding
        ]
        response_build_order: list[str] | None = response_json.get("build_order")
        if response_build_order is not None:
            # Keep only slugs that map to surviving scenes (drops deleted + unknown ones)
            build_order_out: list[str] = [
                slug for slug in response_build_order if slug in surviving_slugs
            ]
            # Append newly-added scene slugs not already in the list
            listed_set: set[str] = set(build_order_out)
            for slug in newly_added_slugs:
                if slug not in listed_set and slug in surviving_slugs:
                    build_order_out.append(slug)
                    listed_set.add(slug)
        else:
            # No build_order in response: preserve the spec's existing value,
            # but still append newly-added scene slugs at the end.
            existing_bo: list[str] = raw.get("build_order") or []
            # Drop any slugs that no longer map to surviving scenes
            build_order_out = [s for s in existing_bo if s in surviving_slugs]
            listed_set = set(build_order_out)
            for slug in newly_added_slugs:
                if slug not in listed_set and slug in surviving_slugs:
                    build_order_out.append(slug)
                    listed_set.add(slug)

        raw["build_order"] = build_order_out
        spec_path.write_text(
            yaml.dump(raw, default_flow_style=False, allow_unicode=True)
        )

        return {
            "decision": decision,
            "narrative_locked": bool(raw.get("narrative_locked")),
            "applied": {
                "updated": updated,
                "added": added,
                "deleted": deleted,
                "features_changed": features_changed,
                "personas_changed": personas_changed,
                "why_brief_changed": why_brief_changed,
            },
            "needs_grounding": needs_grounding,
            "feedback": feedback,
            "build_order": build_order_out,
        }

    # ------------------------------------------------------------------
    # LEGACY shape: narration_edits
    # ------------------------------------------------------------------
    narration_edits: dict[str, str] = response_json.get("narration_edits", {}) or {}

    # Build a slug→scene-index mapping from the on-disk spec
    slug_to_index_legacy: dict[str, int] = {}
    for idx, scene in enumerate(scenes):
        slug_to_index_legacy[_scene_id(scene)] = idx

    edited = 0
    for slug, new_claim in narration_edits.items():
        idx = slug_to_index_legacy.get(slug)
        if idx is None:
            # Unknown slug — silently skip
            continue
        if scenes[idx].get("concept_claim") != new_claim:
            scenes[idx]["concept_claim"] = new_claim
            edited += 1

    # ------------------------------------------------------------------
    # build_order (legacy path): read from response, validate against
    # surviving scenes, preserve existing spec value when not provided.
    # ------------------------------------------------------------------
    surviving_slugs_legacy: set[str] = {_scene_id(s) for s in scenes}
    response_build_order_legacy: list[str] | None = response_json.get("build_order")
    if response_build_order_legacy is not None:
        # Filter to only surviving slugs (ignore unknown/bogus ones)
        build_order_legacy: list[str] = [
            slug for slug in response_build_order_legacy
            if slug in surviving_slugs_legacy
        ]
        # Append any surviving scene slugs not already listed
        listed_legacy: set[str] = set(build_order_legacy)
        for scene in scenes:
            slug = _scene_id(scene)
            if slug not in listed_legacy:
                build_order_legacy.append(slug)
                listed_legacy.add(slug)
        raw["build_order"] = build_order_legacy
    else:
        # Preserve whatever the spec already has (may be absent)
        build_order_legacy = raw.get("build_order") or []

    # Write back (only if there were edits OR build_order changed OR the lock
    # state changed, but always write to keep round-trip clean)
    if edited > 0 or response_build_order_legacy is not None or personas_changed > 0 or lock_changed:
        raw["scenes"] = scenes
        spec_path.write_text(
            yaml.dump(raw, default_flow_style=False, allow_unicode=True)
        )

    return {
        "decision": decision,
        "narrative_locked": bool(raw.get("narrative_locked")),
        "applied": {
            "updated": edited,
            "added": 0,
            "deleted": 0,
            "features_changed": 0,
            "personas_changed": personas_changed,
            "why_brief_changed": why_brief_changed,
        },
        "needs_grounding": [],
        "feedback": [],
        # back-compat key
        "edited": edited,
        "build_order": build_order_legacy,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def load_why_brief(spec_path: str | Path, spec: UnifiedSpec) -> dict:
    """Load the why-brief dict referenced by ``spec.why_brief`` (a path relative
    to the spec file).  Returns ``{}`` if no why_brief is declared or it can't be
    read/parsed — the review surface degrades gracefully without it.
    """
    if not spec.why_brief:
        return {}
    wb_path = (Path(spec_path).parent / spec.why_brief).resolve()
    try:
        data = yaml.safe_load(wb_path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Ownership (L1). There is no merge, because there is nothing to merge.
#
#   <slug>.recipe.yaml          git-owned. How to film it.
#   canopy-web                  cloud-owned. The story.
#   <slug>.narrative.lock.json  generated by `pull`, committed, never edited.
#
# The two are joined on the stable scene `id` (L0) by scripts/ddd/spec_io.py.
# `pull` is a one-way read; nothing writes across the boundary. See
# docs/superpowers/specs/2026-07-26-narrative-storyboard-and-ownership-design.md
# ---------------------------------------------------------------------------


def web_narrative_to_spec_parts(request_json: dict) -> dict:
    """Extract the web-owned narrative fields from a review ``request_json``."""
    scenes: list[dict] = []
    for n in request_json.get("narration") or []:
        if not isinstance(n, dict):
            continue
        scenes.append(
            {
                # The web's narration id IS the scene id — the join key the local
                # recipe is matched on. Legacy narratives stored the title slug
                # here, which is exactly what the backfill writes locally.
                "id": (n.get("id") or "").strip() or _title_slug(n.get("title", "")),
                "title": n.get("title", ""),
                "persona": n.get("persona", ""),
                "provenance": n.get("provenance", ""),
                # The reviewer-approved line is the VOICEOVER. It is NOT the
                # concept_claim, which is local-owned, never transmitted
                # (NarrationItem carries no such field), and must not be
                # clobbered — reconstructing it here was silent data loss.
                "narrative": (n.get("text") or "").strip(),
                "features": n.get("features") or [],
            }
        )
    # The narrative slug; older stored narratives carry it as `feature`.
    slug = request_json.get("narrative_slug") or request_json.get("feature") or ""
    return {
        "name": slug,
        "narrative": request_json.get("narrative") or "",
        "personas": request_json.get("personas") or {},
        "build_order": request_json.get("build_order") or [],
        "scenes": scenes,
    }


def reconstruct_why_brief(request_json: dict) -> dict:
    """Recover the why_brief dict stored on the web narrative (lossless).

    Maps the legacy ``feature`` key → ``narrative_slug`` so it validates against
    the current WhyBrief model.
    """
    wb = dict(request_json.get("why_brief") or {})
    if "feature" in wb and "narrative_slug" not in wb:
        wb["narrative_slug"] = wb.pop("feature")
    return wb


def _tokenized_review_url(result: dict) -> str | None:
    """Token-bearing review URL from a post result ``{id, url, share_token}``.

    Prefers an already-tokenized ``url``; otherwise appends ``?t=<share_token>``
    so a non-owner viewer (e.g. the user reading on another device) can open it.
    """
    url = (result.get("url") or "").strip()
    if not url:
        return None
    token = (result.get("share_token") or "").strip()
    if token and "t=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}t={token}"
    return url


def _internal_review_url(result: dict, base_url: str) -> str | None:
    """Owner (internal) review URL from a post result ``{id, url, share_token}``.

    The ``?t=<share_token>`` query forces canopy-web into standalone share mode
    with NO left rail — that's for recipients who are not signed in. The signed-in
    owner wants the page WITHOUT the token, which opens inside the workbench (left
    rail intact). Prefer reconstructing ``<base>/review/<id>/`` from the review id;
    fall back to stripping the query off the returned ``url``. Returns an absolute
    URL so it is click-ready regardless of whether the server returned a relative
    or absolute ``url``.
    """
    base = (base_url or "").rstrip("/")
    rid = (result.get("id") or "").strip()
    raw = (result.get("url") or "").strip()
    if rid:
        path = f"/review/{rid}/"
    elif raw:
        path = raw.split("?", 1)[0]
    else:
        return None
    if path.startswith("http://") or path.startswith("https://"):
        return path.split("?", 1)[0]
    return f"{base}{path}"


def _stamp_run_state(run_id: str, result: dict) -> None:
    """Deterministically record the posted narrative review on run_state.yaml.

    Writes ``narrative_review_id`` (the raw ReviewRequest UUID) and
    ``narrative_review_url`` (token-bearing) so ddd-upload can attach this run's
    artifacts to the exact narrative version — and so its upload guard sees
    proof the narrative gate ran. Replaces the old hand-run Python snippet that
    the model had to remember (and silently skipped). A missing run_state is a
    warning, not a failure: the post already succeeded.
    """
    from scripts.ddd import runstate as rs

    review_id = (result.get("id") or "").strip()
    try:
        state = rs.load(run_id)
    except FileNotFoundError:
        print(
            f"WARNING: posted narrative review {review_id or '(unknown id)'} but "
            f"run_state for {run_id!r} was not found — could not stamp "
            f"narrative_review_id. ddd-upload will re-verify against canopy-web.",
            file=sys.stderr,
        )
        return
    if review_id:
        state.narrative_review_id = review_id
    url = _tokenized_review_url(result)
    if url:
        state.narrative_review_url = url
    rs.save(state)


def post_narrative_version(spec_path_str: str, run_id: str, rv=None) -> dict:
    """Post a narrative version for ``spec_path`` + stamp the run and spec.

    The reusable post+stamp+sync core shared by the interactive ``narrative post``
    command and the routine ``auto_version_if_changed`` path:

      1. Build the narrative ReviewRequest from the current spec (regenerating
         the story from the live narrative fields).
      2. POST it — canopy-web assigns the next monotonic ``version`` AT POST TIME
         and ``build_narrative`` treats the latest-posted version as
         ``current_version`` (independent of pending/resolved status), so the
         posted version is immediately the current/active narrative. No human
         approval is required for it to become current.
      3. Stamp ``run_state.narrative_review_id`` so the run attaches to this exact
         version. canopy-web owns the story from here; `pull` fetches it
         back into the narrative lock.

    Returns the raw post result ``{id, url, share_token}`` from canopy-web.
    Raises if the spec file is missing. ``rv`` is injectable for tests.
    """
    if rv is None:
        from scripts.ddd import review as rv  # local import — network-touching

    spec_path = Path(spec_path_str)
    if not spec_path.exists():
        raise FileNotFoundError(f"spec file not found: {spec_path}")

    # The narrative slug this review belongs to: prefer the run's own
    # narrative_slug (handles a run_id whose slug differs from its narrative_slug
    # after a rename), else derive from the run_id stamp.
    narrative_slug: str | None = None
    try:
        from scripts.ddd import runstate as rs

        narrative_slug = rs.load(run_id).narrative_slug
    except FileNotFoundError:
        narrative_slug = None

    spec = load_spec(spec_path)
    why_brief = load_why_brief(spec_path, spec)
    request = build_narrative_review_request(
        spec, run_id, why_brief=why_brief, narrative_slug=narrative_slug
    )
    result = rv.post_review_request(request)
    _stamp_run_state(run_id, result)
    # Close the round-trip: the local spec is now the version we just posted, so
    # stamp its sync fields. Without this a later `pull` would see the local hash
    # diverge from a stale stamp and refuse a clean fast-forward.
    if narrative_slug:
        _stamp_spec_sync(spec_path, narrative_slug, rv)
    return result


def _cmd_post(spec_path_str: str, run_id: str) -> None:
    """Post the narrative review request, stamp run_state, print {id, url, share_token}."""
    from scripts.ddd import review as rv  # local import — network-touching

    spec_path = Path(spec_path_str)
    if not spec_path.exists():
        print(f"ERROR: spec file not found: {spec_path}", file=sys.stderr)
        sys.exit(1)

    result = post_narrative_version(spec_path_str, run_id, rv=rv)
    # Surface BOTH link forms explicitly so callers (and skills) never hand the
    # user the no-rail share link by mistake:
    #   internal_url — owner view, opens inside the workbench (LEFT RAIL). Default.
    #   share_url    — token-bearing standalone share link (NO rail), externals only.
    base = rv._resolve_base_url(None)
    out = dict(result)
    internal = _internal_review_url(result, base)
    if internal:
        out["internal_url"] = internal
    share = _tokenized_review_url(result)
    if share:
        out["share_url"] = share if share.startswith("http") else f"{base.rstrip('/')}{share}"
    # Human-readable hint to stderr (the JSON on stdout stays machine-parseable).
    if internal:
        print(f"internal (owner, left rail): {internal}", file=sys.stderr)
    if out.get("share_url"):
        print(f"external (share, no rail):   {out['share_url']}", file=sys.stderr)
    print(json.dumps(out))


def _cmd_apply(spec_path_str: str, response_json_file: str) -> None:
    """Apply narration edits from a response JSON file and print the result dict."""
    response_path = Path(response_json_file)
    if not response_path.exists():
        print(f"ERROR: response JSON file not found: {response_path}", file=sys.stderr)
        sys.exit(1)

    response_json = json.loads(response_path.read_text())
    result = apply_narrative_edits(spec_path_str, response_json)
    print(json.dumps(result))


def write_lock(base_dir, slug: str, version: int, parts: dict):
    """Write ``<slug>.narrative.lock.json`` — the committed read-through cache of
    the cloud-owned story at a pinned version.

    Generated, never hand-edited. Carries STORY only: a recipe field appearing in
    here is the signal that someone edited it by hand (see
    ``scripts.ddd.check_locks``). Sorted keys + trailing newline so re-pulling an
    unchanged version is a no-op in git rather than a diff.
    """
    from scripts.ddd.spec_io import lock_path

    payload = {
        "slug": slug,
        "version": version,
        "fetched_at": _now_iso(),
        "name": parts.get("name") or slug,
        "narrative": parts.get("narrative") or "",
        "personas": parts.get("personas") or {},
        "build_order": parts.get("build_order") or [],
        "scenes": [
            {
                "id": s["id"],
                "title": s.get("title", ""),
                "persona": s.get("persona", ""),
                "provenance": s.get("provenance", ""),
                "narrative": s.get("narrative", ""),
                "features": s.get("features") or [],
            }
            for s in (parts.get("scenes") or [])
        ],
    }
    p = lock_path(base_dir, slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return p


def _cmd_pull(slug: str, target: str) -> None:
    """Fetch the narrative from canopy-web into ``<slug>.narrative.lock.json``.

    A one-way READ. canopy-web owns the story; the lock is a generated,
    committed cache of it at a pinned version. Nothing is merged, nothing is
    stamped, and there is no ``--force`` — with one writer per field there is
    nothing to reconcile and therefore nothing to force.

    ``target`` is the directory the lock is written to (a file path is accepted
    for backward compatibility and its parent is used), alongside the sibling
    ``<slug>.why_brief.yaml`` the review payload carries.
    """
    from scripts.ddd import review as rv

    t = Path(target)
    base_dir = t.parent if t.suffix else t

    detail = rv.get_narrative(slug)
    cur = (detail or {}).get("current_version") or {}
    web_version = cur.get("version")
    review_id = cur.get("review_id")
    if web_version is None or not review_id:
        print(
            f"ERROR: canopy-web has no narrative for {slug!r}. "
            f"Post one first via the narrative-review gate.",
            file=sys.stderr,
        )
        sys.exit(1)

    full = rv.get_review(review_id)
    request_json = full.get("request_json") if isinstance(full, dict) else None
    if not isinstance(request_json, dict):
        print(
            f"ERROR: could not read narrative payload for {slug!r} (review {review_id}).",
            file=sys.stderr,
        )
        sys.exit(1)

    parts = web_narrative_to_spec_parts(request_json)
    lock = write_lock(base_dir, slug, web_version, parts)

    # The why-brief is cloud-derived too — recovered from the same payload.
    wb = reconstruct_why_brief(request_json)
    wb_name = f"{slug}.why_brief.yaml"
    if wb:
        base_dir.mkdir(parents=True, exist_ok=True)
        (base_dir / wb_name).write_text(
            yaml.dump(wb, default_flow_style=False, allow_unicode=True, sort_keys=False)
        )

    print(
        json.dumps(
            {
                "action": "pulled",
                "slug": slug,
                "version": web_version,
                "lock_path": str(lock),
                "why_brief": wb_name if wb else None,
                "scenes": len(parts.get("scenes") or []),
            }
        )
    )


def _cmd_status(run_id: str) -> None:
    """Report whether *run_id* has a narrative the upload step will accept.

    Prints a JSON status: ``{run_id, narrative_slug, narrative_review_id,
    stamped, narrative_exists, ok}``. ``ok`` is True when the run is stamped OR
    canopy-web already has a narrative version for its narrative_slug — i.e.
    ``ddd-upload`` would NOT refuse it. The orchestrator calls this before
    render/upload so a renamed or never-posted narrative is caught early (and
    re-posted under the right slug) instead of surfacing as "no narrative" after
    publish. Exit code is 0 when ``ok`` is True, 1 otherwise — so a shell gate
    can branch on it.
    """
    from scripts.ddd import review as rv
    from scripts.ddd import runstate as rs

    try:
        state = rs.load(run_id)
        narrative_slug = state.narrative_slug
        review_id = (getattr(state, "narrative_review_id", None) or "").strip() or None
        if not review_id:
            review_id = _review_id_from_url(
                getattr(state, "narrative_review_url", None)
            )
    except FileNotFoundError:
        narrative_slug = _narrative_slug_from_run_id(run_id)
        review_id = None

    stamped = bool(review_id)
    narrative_exists = rv.narrative_version_exists(narrative_slug)
    ok = stamped or narrative_exists
    print(
        json.dumps(
            {
                "run_id": run_id,
                "narrative_slug": narrative_slug,
                "narrative_review_id": review_id,
                "stamped": stamped,
                "narrative_exists": narrative_exists,
                "ok": ok,
            }
        )
    )
    sys.exit(0 if ok else 1)


def main() -> None:
    """Entry point for ``python -m scripts.ddd.narrative``."""
    if len(sys.argv) < 2:
        print(
            "Usage:\n"
            "  python -m scripts.ddd.narrative post <spec_path> <run_id>\n"
            "  python -m scripts.ddd.narrative sync <spec_path> <run_id>   # reconcile: fold any resolved web review edits onto the spec, THEN version any change (no pause); exit 2 on conflict. The 'I edited on the web, now continue' command.\n"
            "  python -m scripts.ddd.narrative apply <spec_path> <response_json_file>\n"
            "  python -m scripts.ddd.narrative status <run_id>     # prints narrative status JSON; exit 1 if upload would refuse\n"
            "  python -m scripts.ddd.narrative pull <slug> <dir>                   # fetch the narrative into <slug>.narrative.lock.json (one-way read)\n"
            "  python -m scripts.ddd.narrative locked <spec_path>   # prints locked|unlocked\n"
            "  python -m scripts.ddd.narrative lock <spec_path>\n"
            "  python -m scripts.ddd.narrative unlock <spec_path>",
            file=sys.stderr,
        )
        sys.exit(2)

    subcmd = sys.argv[1]

    if subcmd == "post":
        if len(sys.argv) != 4:
            print(
                "Usage: python -m scripts.ddd.narrative post <spec_path> <run_id>",
                file=sys.stderr,
            )
            sys.exit(2)
        _cmd_post(sys.argv[2], sys.argv[3])

    elif subcmd == "sync":
        if len(sys.argv) != 4:
            print(
                "Usage: python -m scripts.ddd.narrative sync <spec_path> <run_id>",
                file=sys.stderr,
            )
            sys.exit(2)
        _cmd_sync(sys.argv[2], sys.argv[3])

    elif subcmd == "status":
        if len(sys.argv) != 3:
            print(
                "Usage: python -m scripts.ddd.narrative status <run_id>",
                file=sys.stderr,
            )
            sys.exit(2)
        _cmd_status(sys.argv[2])

    elif subcmd == "pull":
        args = [a for a in sys.argv[2:] if a != "--force"]
        if len(args) != 2:
            print(
                "Usage: python -m scripts.ddd.narrative pull <slug> <dir>",
                file=sys.stderr,
            )
            sys.exit(2)
        _cmd_pull(args[0], args[1])

    elif subcmd == "apply":
        if len(sys.argv) != 4:
            print(
                "Usage: python -m scripts.ddd.narrative apply <spec_path> <response_json_file>",
                file=sys.stderr,
            )
            sys.exit(2)
        _cmd_apply(sys.argv[2], sys.argv[3])

    elif subcmd == "locked":
        if len(sys.argv) != 3:
            print("Usage: python -m scripts.ddd.narrative locked <spec_path>", file=sys.stderr)
            sys.exit(2)
        print("locked" if is_narrative_locked(sys.argv[2]) else "unlocked")

    elif subcmd in ("lock", "unlock"):
        if len(sys.argv) != 3:
            print(f"Usage: python -m scripts.ddd.narrative {subcmd} <spec_path>", file=sys.stderr)
            sys.exit(2)
        print(json.dumps(set_narrative_lock(sys.argv[2], subcmd == "lock")))

    else:
        print(
            f"ERROR: unknown subcommand {subcmd!r}. Use 'post', 'sync', 'status', 'pull', 'apply', 'locked', 'lock', or 'unlock'.",
            file=sys.stderr,
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
