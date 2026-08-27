"""DDD schema module — re-export shim over the neutral narrative substrate.

The generic narrative / evidence-gap / eval / review-package models now live in
``scripts.narrative.models`` so non-DDD consumers (an ACE AI-video pipeline,
ace-web) can reuse them without importing the ``ddd`` namespace. This module
re-exports all of them and keeps the DDD-only ``RunState`` (the converge
lifecycle) defined here, so every ``from scripts.ddd.schemas.models import X``
importer and ``python -m scripts.ddd.*`` entry point keeps working unchanged.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from scripts.narrative.models import (
    ACTION_CLASSES,
    ACTION_KINDS,
    Action,
    ClickAction,
    ClickMenuAction,
    Decision,
    Dimension,
    DrawAction,
    Evidence,
    Feature,
    FillAction,
    Finding,
    Gap,
    Gate,
    GotoAction,
    HoldAction,
    HoverAction,
    NarrationItem,
    Persona,
    PressAction,
    ReviewRequest,
    Scene,
    ScrollAction,
    ScrollToAction,
    SelectAction,
    SetupBlock,
    SpineItem,
    TypeAction,
    UnifiedSpec,
    Verdict,
    WaitForAction,
    WhyBrief,
    _ActionBase,
)


class RunState(BaseModel):
    """The DDD converge lifecycle for one run (run_id), persisted as run_state.yaml.

    Phase milestones — who writes each ``phase`` value:

      * CODE-set (written by a .py module as the run advances):
          - ``judged``    — set by ``run_pipeline.assemble_run_state`` after the
            dual-judge verdict is assembled.
          - ``uploaded``  — set by ``upload.upload_run`` after the run package is
            published to canopy-web (terminal).
      * ORCHESTRATOR-ONLY milestones (NEVER written by .py — they label loop
        stages the orchestrator narrates): ``phase0``, ``spec``, ``render``,
        ``converged``. The default is ``phase0``; the Literal accepts them so an
        orchestrator-authored run_state validates, but no module assigns them.
      * ``promoted`` is a legacy READ-alias for ``uploaded`` — accepted on read so
        older on-disk run_state.yaml files still validate; new runs write
        ``uploaded`` (see ``upload.upload_run``, which treats both as published).
    """

    schema_version: int = 1
    run_id: str
    narrative_slug: str
    # "promoted" is a legacy alias for "uploaded" — accepted on read so older
    # on-disk run_state.yaml files still validate; new runs write "uploaded".
    phase: Literal[
        "phase0", "spec", "render", "judged", "converged", "uploaded", "promoted"
    ] = "phase0"
    iteration: int = 0
    why_brief: str | None = None
    verdicts: dict[str, str] = {}
    findings: list[dict] = []
    pending_review: str | None = None
    last_actor: str | None = None
    last_actor_at: str | None = None
    # Scene-filter metadata (0.2.128). A full-spec run sets scenes_run to the
    # complete list of spec indices and scene_filter to None. A partial run
    # (--scene <selector> on /canopy:ddd-run or /canopy:walkthrough) sets
    # scenes_run to the rendered subset and scene_filter to the raw selector.
    # /canopy:ddd-upload refuses any run with scene_filter != None.
    scenes_run: list[int] | None = None
    scene_filter: str | None = None
    # Auto-iterate signal (0.2.131). Computed by /canopy:ddd-run Step 5 after
    # the dual-judge verdict comes back; consumed by the /canopy:ddd
    # orchestrator's Converge-or-loop branch. Possible values:
    #   continue              — all non-DEFER findings are fix_kind=mechanical;
    #                           orchestrator may apply fixes and re-fire.
    #   stop_done             — converged; full-spec; ready for promotion.
    #   stop_partial          — converged on filtered scope; drop --scene to
    #                           promote.
    #   stop_concept_change   — a CONCEPT/redesign finding present; needs the
    #                           irreplaceable-taste pause.
    #   stop_unclear          — non-DEFER finding with fix_kind=options or
    #                           redesign; needs user pick.
    #   stop_max_iter         — MAX_ITERATIONS would be exceeded.
    auto_iterate_next_action: str | None = None
    auto_iterate_reason: str | None = None
    # Hosted artifact URLs per iteration (0.2.135). Populated by
    # /canopy:ddd-run's render-then-upload step: each iteration's rendered
    # deck (and clip, when present) is auto-uploaded to canopy-web and the
    # returned URL is stamped here. Surfaced findings and review-request
    # gates reference these URLs directly — no manual upload needed at
    # surface-time, no local file:// paths leaking into messages that the
    # user reads on another device.
    #
    # Shape:
    #   iteration_decks: {0: "https://canopy-web.../w/<uuid1>", 1: "<...>"}
    #   iteration_clips: {0: "https://canopy-web.../w/<uuid2>", 1: "<...>"}
    #
    # Deep-link to a specific scene by appending "#scene-<N>" — the deck
    # generator emits id="scene-<N>" anchors on every scene slide using the
    # original spec index, so the anchor is stable across partial/full runs.
    iteration_decks: dict[int, str] = {}
    iteration_clips: dict[int, str] = {}
    # Gating overall score per iteration (lower of the two judges). Appended by
    # /canopy:ddd-run Step 5; the progress-aware auto-iterate loop reads it to
    # tell "still improving → keep going" from "stalled/regressed → get a human",
    # replacing the old raw MAX_ITERATIONS=3 count.
    score_history: list[float] = []
    # Per-iteration finding FINGERPRINTS (scene::dimension::detail-hash), appended
    # by run_pipeline.compute_auto_iterate. Two consecutive iterations with an
    # identical set mean the loop is re-deriving the same defects rather than
    # progressing — the plateau detector. Unlike score_history this signal is not
    # noisy: an LLM's score for a cell wobbles +/-1 on identical frames, but the
    # defect it names does not.
    finding_fingerprints: list[list[str]] = []
    # Which KIND of ending this is, set by run_pipeline.classify_termination:
    #   converged_clean | converged_with_open_questions | stopped_not_converged |
    #   diverging | running
    # "converged, good", "converged, still failing" and "diverging" are genuinely
    # different outcomes and must never print the same way.
    terminal_status: str | None = None
    # How many times the concept gate has been HELD OPEN for pending mechanical
    # work, incremented by run_pipeline.compute_auto_iterate. A strategy redesign
    # finding used to preempt confidently-fixable defects sitting alongside it, so
    # the gate asked a human "is this the right direction?" over an artifact known
    # to be wrong, and the fixes were never applied (ACE
    # spark-facilitator/20260820-0817: five accuracy findings dropped, zero
    # iterations completed). The gate now waits for that work — but EXACTLY ONCE,
    # so a mechanical backlog that keeps regenerating cannot starve it. Bounded by
    # CONCEPT_GATE_MAX_DEFERRALS; never reset within a run.
    concept_gate_deferred: int = 0
    # Hosted narrative-review URL (0.2.150). Stamped by the ddd-narrative-review
    # gate after it posts the narrative to the canopy-web review surface — the
    # token-bearing /review/<id>/?t=<token> link the user approved. ddd-run's
    # upload step passes it as the video's `narrative` companion link so a
    # viewer watching the clip can jump back to the story that generated it.
    narrative_review_url: str | None = None
    # Hosted narrative-review ID (0.2.172). Stamped alongside narrative_review_url
    # by the ddd-narrative-review gate's `narrative post` command — the raw
    # ReviewRequest UUID, so ddd-upload can attach this run's artifacts to the
    # exact narrative version without regex-parsing it back out of the URL.
    # Its presence is also the upload gate's proof that a narrative review ran:
    # when it's None, upload re-verifies against canopy-web and refuses to
    # publish a run that has no narrative (see scripts/ddd/upload.py).
    narrative_review_id: str | None = None
    # Hosted product-findings review (review_mode: human). Stamped by
    # `python -m scripts.ddd.findings_review post <run_id>` after it posts the
    # clustered PRODUCT findings to the canopy-web review surface. The id is
    # the raw ReviewRequest UUID (what the orchestrator polls for resolution);
    # the url is the token-bearing share link. Overwritten on each judged
    # iteration that posts a findings review — the run tracks the LATEST one.
    findings_review_id: str | None = None
    findings_review_url: str | None = None


__all__ = [
    "ACTION_CLASSES",
    "ACTION_KINDS",
    "Action",
    "ClickAction",
    "ClickMenuAction",
    "Decision",
    "Dimension",
    "DrawAction",
    "Evidence",
    "Feature",
    "FillAction",
    "Finding",
    "Gap",
    "Gate",
    "GotoAction",
    "HoldAction",
    "HoverAction",
    "NarrationItem",
    "Persona",
    "PressAction",
    "ReviewRequest",
    "RunState",
    "Scene",
    "ScrollAction",
    "ScrollToAction",
    "SelectAction",
    "SetupBlock",
    "SpineItem",
    "TypeAction",
    "UnifiedSpec",
    "Verdict",
    "WaitForAction",
    "WhyBrief",
    "_ActionBase",
]
