"""DDD run-pipeline glue — SP4.1 + SP4.2.

Assembles verdict paths and findings into a RunState, and decides convergence
from two judge verdicts (concept + user-artifact).

Public API
----------
assemble_run_state(state, concept_verdict, user_verdict, findings, *, concept_path, user_path) -> RunState
    Mutates state in place: sets verdicts, findings, and phase="judged".
    Returns the mutated state.

compute_convergence(concept_verdict, user_verdict, *, threshold) -> bool
    Returns True iff BOTH verdicts have overall_score >= threshold AND neither
    verdict is "blocked".  Threshold defaults to 4.0.

HARD_CAP
    Module constant: runaway backstop on refinement iterations. The loop is
    progress-aware (keep going while mechanical findings are still improving the
    score; stop on a stall/regression) — HARD_CAP only catches a pathological
    non-converging loop. See ``compute_auto_iterate``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from scripts.ddd.schemas.models import RunState, Verdict
from scripts.narrative.models import FIX_KINDS, UNROUTABLE_FIX_KIND_FALLBACK

if TYPE_CHECKING:
    from scripts.ddd.loop_config import LoopConfig, ProductConfig

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Runaway backstop only — NOT the normal stop. The loop stops on real gates,
# options/redesign findings, or a score stall/regression long before this.
HARD_CAP: int = 10
# Back-compat alias for older callers; no longer a hard 3-iteration cap.
MAX_ITERATIONS: int = HARD_CAP

# There is deliberately NO count bound on how many passes pending MECHANICAL work
# may hold the concept gate open. The gate buys a human's taste judgment on
# DIRECTION; opening it over an artifact that still carries confidently-fixable
# defects spends that judgment on a misrepresentation — and "clean" is a property
# of the artifact, not a count of passes. The bound is EXHAUSTION instead: after
# the first (free) deferral, each further one must be paid for by the previous
# pass moving the score outside denoise.NOISE_BAND. A flat pass — one that applied
# nothing, or applied fixes that changed nothing — buys no more waiting, so a
# self-regenerating backlog cannot starve the gate and a crashed pass cannot
# spend it. HARD_CAP is the runaway backstop. See compute_auto_iterate and
# canopy#588 (a count of 1 cut consecutive clean runs off mid-climb).


# ---------------------------------------------------------------------------
# SP4.1 — assemble_run_state
# ---------------------------------------------------------------------------


def assemble_run_state(
    state: RunState,
    concept_verdict: Verdict,
    user_verdict: Verdict,
    findings: list[dict],
    *,
    concept_path: str = "verdict-concept.yaml",
    user_path: str = "verdict-user.yaml",
    manifest: dict | None = None,
    extra_verdict_paths: dict[str, str] | None = None,
) -> RunState:
    """Assemble both verdict paths and merged findings into *state*.

    Mutates *state* in place (Pydantic v2 models are mutable by default) and
    returns it so callers can chain or ignore the return value.

    Parameters
    ----------
    state:
        The run's RunState.  Must already have a valid run_id and narrative_slug.
    concept_verdict:
        The Verdict produced by the ddd-concept-eval judge.
    user_verdict:
        The Verdict produced by the user-artifact judge (canopy:visual-judge
        with audience="narrative_slug user").
    findings:
        Merged list of design_finding dicts (from design_findings.json).
    concept_path:
        Path (relative to run dir or absolute) of the concept verdict YAML.
        Default: "verdict-concept.yaml".
    user_path:
        Path (relative to run dir or absolute) of the user-artifact verdict YAML.
        Default: "verdict-user.yaml".
    manifest:
        Optional render manifest (walkthrough-run-data.json). When provided, its
        ``scenes_run`` / ``scene_filter`` are carried onto the run state — the
        render engine is the single source of truth for which scenes were
        rendered, so the upload's partial-run guard reads what the engine
        actually emitted. When ``None``, those fields are left untouched.
    extra_verdict_paths:
        Optional additional verdict artifacts to record, keyed by verdict kind
        (e.g. ``{"timing": "verdict-timing.json", "why": "verdict-why.yaml"}``).
        Recorded alongside the gating pair in ``state.verdicts`` — the assembler
        is generic over kinds (canopy#265 item 1). The ``concept`` /
        ``user_artifact`` keys cannot be shadowed.

    Returns
    -------
    RunState
        The mutated *state* (same object).
    """
    state.verdicts = {
        **(extra_verdict_paths or {}),
        "concept": concept_path,
        "user_artifact": user_path,
    }
    state.findings = list(findings)
    state.phase = "judged"
    if manifest is not None:
        state.scenes_run = manifest.get("scenes_run")
        state.scene_filter = manifest.get("scene_filter")
    return state


# ---------------------------------------------------------------------------
# SP4.2 — compute_convergence
# ---------------------------------------------------------------------------


def compute_convergence_all(
    verdicts: dict[str, Verdict],
    *,
    threshold: float = 4.0,
) -> bool:
    """Generic convergence over N verdicts (canopy#265 item 1).

    Only verdicts with ``gate == "gating"`` participate; ``advisory`` verdicts
    (timing, video, why, actionability) are recorded and reported but a low
    score never blocks convergence. Every gating verdict must:

    1. have ``overall_score >= threshold``
    2. not be ``blocked``
    3. not carry ``live_state_verified is False`` — an eval whose grading anchor
       never touched live state cannot converge a run, whatever its score says
       (the out-of-chain fitness law, canopy#265 item 3). ``None`` (legacy
       emitters, unknown) is allowed for back-compat.

    Returns False when no gating verdict is present at all — convergence must be
    demonstrated, not defaulted.

    The weakest-link rule (embedded in each judge) means overall_score already
    reflects the lowest gating dimension, so checking overall_score is
    sufficient — no need to re-inspect individual dimensions here.
    """
    gating = {k: v for k, v in verdicts.items() if v.gate == "gating"}
    if not gating:
        return False
    for v in gating.values():
        if v.verdict == "blocked":
            return False
        if v.live_state_verified is False:
            return False
        if v.overall_score < threshold:
            return False
    return True


def compute_convergence(
    concept_verdict: Verdict,
    user_verdict: Verdict,
    *,
    threshold: float = 4.0,
    extra: dict[str, Verdict] | None = None,
) -> bool:
    """Return True iff the run's verdicts satisfy the convergence criteria.

    The documented two-verdict entry point — delegates to
    ``compute_convergence_all`` over the gating pair plus any ``extra`` verdicts
    (whose ``gate`` field decides whether they participate).

    claim_reality_coherence is advisory and excluded upstream from the judge's
    overall_score, so it does not appear here.

    Parameters
    ----------
    concept_verdict:
        Verdict from the ddd-concept-eval judge.
    user_verdict:
        Verdict from the user-artifact judge.
    threshold:
        Minimum overall_score required for every gating verdict.  Default: 4.0.
    extra:
        Optional additional verdicts by kind (e.g. from
        ``scripts.ddd.verdicts.load_verdict``).

    Returns
    -------
    bool
        True iff convergence criteria are satisfied; False otherwise.
    """
    return compute_convergence_all(
        {
            **(extra or {}),
            "concept": concept_verdict,
            "user_artifact": user_verdict,
        },
        threshold=threshold,
    )


# ---------------------------------------------------------------------------
# Verdict report line (cap visibility — canopy#273 item 3)
# ---------------------------------------------------------------------------


def format_verdict_line(verdict: Verdict) -> str:
    """Render one verdict as a report line, keeping the out-of-chain cap VISIBLE.

    A capped verdict (``live_state_verified: false`` whose pre-cap score exceeded
    ``LIVE_STATE_UNVERIFIED_CAP``; the schema validator records the original in
    ``uncapped_overall_score``) must never render indistinguishably from an
    honest score. It renders as::

        4.0/5 (pass — capped from 4.8, not live-state verified)

    while an uncapped verdict renders as ``4.5/5 (pass)``. The ddd-run summary
    (and any other reporter) should call this instead of formatting scores
    inline, so the cap annotation can't drift out of the prose.
    """
    if verdict.uncapped_overall_score is not None:
        return (
            f"{verdict.overall_score:.1f}/5 ({verdict.verdict} — capped from "
            f"{verdict.uncapped_overall_score:.1f}, not live-state verified)"
        )
    return f"{verdict.overall_score:.1f}/5 ({verdict.verdict})"


# ---------------------------------------------------------------------------
# Progress-aware auto-iterate (replaces the old raw MAX_ITERATIONS=3 stop)
# ---------------------------------------------------------------------------


def _routable_fix_kind(raw: object) -> str:
    """Map a finding's ``fix_kind`` onto a value this function can actually route.

    A recognised kind passes through untouched. Anything else — a judge emitting
    ``targeted``, a typo, ``None``, a non-string — becomes
    :data:`UNROUTABLE_FIX_KIND_FALLBACK`.

    Without this an unrecognised kind is not merely mis-routed, it is INVISIBLE:
    the branches below select ``mechanical`` and ``options``/``redesign``
    explicitly, so a third value matches neither list and the finding drops out
    of the decision. The loop then reports "No options/redesign ... re-fire" —
    actively asserting there is nothing needing a human — and re-fires the
    expensive judge dispatch on a defect it will never apply and never escalate,
    until the hard cap. Measured on canopy#547's ``fix_kind: targeted``.

    Falling back to ``options`` surfaces it to a human. That direction is the
    safe one and the choice is not symmetric: routing an unclassified finding to
    ``mechanical`` would hand the loop autonomy over something nobody has
    classified. This is deliberately a ROUTING repair only — the label is not
    rewritten on ``state.findings``, so the judge's original word stays readable
    in the artifact, and ``validate("findings", ...)`` is what stops it being
    emitted in the first place.
    """
    return raw if isinstance(raw, str) and raw in FIX_KINDS else UNROUTABLE_FIX_KIND_FALLBACK


def compute_auto_iterate(
    state: RunState,
    concept_verdict: Verdict,
    user_verdict: Verdict,
    findings: list[dict],
    *,
    converged: bool | None = None,
    hard_cap: int = HARD_CAP,
    unattended: bool | None = None,
    distribution: dict | None = None,
    judge_full: bool = True,
    loop_config: "LoopConfig | None" = None,
    target: str | None = None,
    judges: list[str] | None = None,
    held: list | None = None,
    inner_loop_policy: dict | None = None,
    product_config: "ProductConfig | None" = None,
    extra_verdicts: dict[str, Verdict] | None = None,
    floor: dict | None = None,
    edit_scope: dict | None = None,
    target_rubric: object | None = None,
    run_dir: object | None = None,
) -> tuple[str, str]:
    """Decide the next loop action from the SCORE TRAJECTORY, not an iteration count.

    DDD's point is to loop autonomously until the findings it can act on are
    exhausted. A raw count stopped good runs mid-progress and was blind to
    regressions. This gates on whether the run is still making progress:

    - converged (both judges >= threshold)        -> ``stop_done`` / ``stop_partial``
    - a STRATEGY CONCEPT/redesign finding, with
      mechanical fixes still pending and the score
      still climbing (or first deferral)          -> ``continue`` (gate deferred)
    - a STRATEGY CONCEPT/redesign finding          -> ``stop_concept_change``
    - any options/redesign finding                -> ``stop_unclear``
    - score stalled/regressed over last 2 iters   -> ``stop_max_iter`` (needs a human)
    - identical findings two iterations running   -> ``stop_max_iter`` (plateau)
    - hit ``hard_cap`` without converging         -> ``stop_max_iter`` (runaway backstop)
    - else (mechanical + still improving)         -> ``continue`` (keep looping)

    Three things this does BEFORE deciding, each fixing a measured failure:

    1. **Normalizes findings** through :mod:`scripts.ddd.finding_class`. An
       ACCURACY finding — the narration asserts something the artifact itself
       contradicts — is autonomously fixable by construction, so it is forced to
       ``fix_kind: mechanical`` and never reaches a human gate. Only STRATEGY
       findings (the artifact is wrong, not the words) can open
       ``stop_concept_change``.
    2. **Reads progress through a noise band** (:data:`scripts.ddd.denoise.NOISE_BAND`).
       Per-cell judge variance is +/-1 on byte-identical frames, so a sub-half-point
       move is not evidence of improvement OR of regression, and must not be read
       as either.
    3. **Detects a finding PLATEAU**, not just a score stall. The score for a cell
       wobbles; the defect it names does not. Two iterations producing the same
       finding fingerprints means the loop is re-deriving rather than progressing,
       whatever the numbers did.
    4. **Lets pending mechanical work run BEFORE the concept gate opens**, for as
       long as that work is demonstrably cleaning the artifact. A strategy
       redesign is maximally uncertain — it is precisely "we may have built the
       wrong thing" — so letting it jump ahead of confident fixes inverted this
       function's own invariant. It also spends the gate badly: the human is
       asked "is this the right direction?" over an artifact wrong in ways nobody
       disputes, and the score and video they judge measure a product that is
       about to stop existing. The bound is exhaustion, not a count
       (canopy#588): the first deferral is free, and every further one requires
       the previous pass to have moved the score outside the noise band
       (``denoise.improved(hist[-2], hist[-1]) is True``). A stall, a plateau, or
       the hard cap ends it regardless. ``state.concept_gate_deferred`` records
       how many were taken; it is a ledger, not a budget.

    Mutates ``state.score_history`` (this iteration's gating score),
    ``state.finding_fingerprints`` (this iteration's fingerprint set) and
    ``state.terminal_status`` (see :func:`classify_termination`), and returns
    ``(action, reason)``. The gating score is the lower of the two judges'
    overall_score (claim_reality_coherence is already excluded upstream).

    ``unattended`` (default: auto-detect via :func:`scripts.ddd.gates.is_unattended`)
    does not change WHICH action is returned — it changes whether a stuck stop is
    TERMINAL. With no human present, a stuck stop must end the run with an honest
    report rather than wait on a click nobody will make.

    v1 / backlog loop (see :mod:`scripts.ddd.progress`):

    - ``distribution`` is the concept verdict's ``distribution:`` block
      (:func:`scripts.ddd.progress.load_distribution`). With it, every iteration
      records a progress point (score, open findings, mean cell, confirmed caps)
      in ``state.progress_history``, and STALL means none of the four improved
      across the last two iterations — not merely that the floor did not move.
      The stall check now runs BEFORE ``mechanical -> continue``; before this it
      could never fire while a mechanical finding existed.
    - ``judge_full`` says whether this pass judged every scene fresh. An
      incremental pass (backlog mode reuses unchanged scenes' cells) can never
      declare convergence: it returns ``confirm_full`` and the next pass is a
      full render + full judge. Fidelity of the final decision is unchanged.
    - ``loop_config`` (default: auto mode, backlog at >= 8 open findings, full
      re-judge every 3rd batch) picks backlog vs polish on full passes and sets
      ``state.next_judge_full`` for the next pass.

    Inner loop + judge tiering (see :mod:`scripts.ddd.target`):

    - ``target`` is where this pass rendered (``deploy`` | ``inner``) and
      ``judges`` which judges ran fresh (``None`` = all). A pass on the inner
      target, or one that ran only some judges, can never DECIDE: any stop, gate
      or convergence it would return becomes ``checkpoint`` — land the batches,
      then a full render + every judge on the deploy target decides. The target
      is recorded on the progress point.
    - ``inner_loop_policy`` (:func:`scripts.ddd.target.inner_loop_policy`) is
      ``{status, reason}``. A ``missing`` policy -- a deploy gate and no
      ``inner_loop:`` or reasoned ``inner_loop: off`` -- refuses to continue a
      BACKLOG loop: every fix batch would pay PR + CI + deploy before a frame is
      judged. The pass returns ``stop_inner_loop_required`` instead of
      ``continue``. ``None`` (callers that predate it) is not checked.

    Objective (see :mod:`scripts.ddd.objective`): ``loop_config.objective``
    resolves to ``product`` or ``demo`` once per run (``state.objective``). In
    the PRODUCT objective findings are partitioned blocking / ride-along /
    deferred, convergence is :func:`scripts.ddd.objective.converged` (product
    dimensions >= floor, no blocking product finding) instead of every judge >=
    4, the progress score is the weakest product dimension, and the deferred
    polish findings are applied ONCE as a final ``continue`` (the polish pass)
    before ``stop_done``. ``product_config`` is the repo's ``product:`` block and
    ``extra_verdicts`` the advisory/arc verdicts (for the floor check).

    Floor (see :mod:`scripts.ddd.floor`, canopy#780): ``floor`` is the cell
    holding the gating score down (:func:`scripts.ddd.floor.locate`) and
    ``edit_scope`` what this loop may edit (``{narrative_locked,
    fixed_surfaces, declined}``). Both are optional; without ``floor`` nothing
    below changes.

    - The floor and its findings, each classified in or out of the loop's edit
      scope, are stamped on ``state.gating_floor`` — the next ``judge_scope
      plan`` re-judges that cell first.
    - When the floor HAS findings and every one is out of scope (DEFER, a
      narration edit under a locked narrative, data / registry / a configured
      fixed surface, or declined by a fixer), the run stops with
      ``stop_out_of_scope`` (terminal status ``blocked_out_of_scope``) instead
      of iterating to the stall rule: nothing the loop can do moves the gate.
      Like every stop, a pass that cannot decide hands it to a checkpoint.
    - On ``continue`` (``loop.floor_first``, default on) the in-scope floor
      findings are stamped ``floor: true`` and ordered first in
      ``state.findings``, and the reason opens with them, so the batch fixes
      what holds the gate before anything else.

    Target rubric (see :mod:`scripts.ddd.target_rubric`, canopy#790):
    ``target_rubric`` is a resolved :class:`~scripts.ddd.target_rubric.Rubric`, or
    its sources ``{run, spec, config}`` (resolved here, once the objective is
    settled, so the default matches it), and ``run_dir`` where the judges' per-scene rows live. When given, it
    DECIDES convergence: every criterion (each outcome, each blocking dimension)
    passing on the majority of its last ``draws`` full passes, read by median
    cell rather than minimum, and no blocking finding. Findings outside a
    failing criterion become advisory (``route: DEFER``, ``deferred_by:
    target_rubric``) unless their severity is in ``block_severities``. Without
    it (callers that predate it) nothing changes.
    """
    import copy

    from scripts.ddd import denoise, finding_class, fix_scope, gates, parking, progress
    from scripts.ddd import objective as objective_mod
    from scripts.ddd.loop_config import LoopConfig, ProductConfig

    if loop_config is None:
        loop_config = LoopConfig()
    if product_config is None:
        product_config = ProductConfig()

    # What a recipe re-judge must be able to put back: it re-assesses the SAME
    # iteration, so this call must leave no history point behind.
    snapshot = {k: copy.deepcopy(getattr(state, k)) for k in _REJUDGE_ROLLBACK}
    rr = state.recipe_rejudge if isinstance(state.recipe_rejudge, dict) else None
    recipe_rejudged_this_iteration = bool(rr and rr.get("iteration") == state.iteration)
    if recipe_rejudged_this_iteration and rr.get("status") == "pending":
        state.recipe_rejudge = {**rr, "status": "done"}

    state.batch_plan = None  # re-stamped by a `continue` below
    if converged is None:
        converged = compute_convergence(concept_verdict, user_verdict)
    if unattended is None:
        unattended = gates.is_unattended()

    findings = finding_class.normalize_findings(findings or [])
    # Scenes parked on a pending gate decision: their findings are withheld from
    # the batch (their direction may change) but stay visible and counted.
    parked = parking.parked_scenes(state)
    findings = parking.mark(findings, parked)

    # WHAT the loop optimizes — decided once per run, then sticky.
    state.objective = objective_mod.resolve(
        loop_config.objective,
        state,
        sum(1 for f in findings if f.get("route", "PRODUCT") != "DEFER"),
        backlog_min_findings=loop_config.backlog_min_findings,
        judge_full=judge_full,
    )
    product_objective = state.objective == objective_mod.PRODUCT
    polish = state.polish_pass if isinstance(state.polish_pass, dict) else None
    if polish and polish.get("status") == "pending" and polish.get("iteration") == state.iteration:
        state.polish_pass = {**polish, "status": "done"}
    polish_done = bool(state.polish_pass and state.polish_pass.get("status") == "done")
    all_verdicts = {**(extra_verdicts or {}), "concept": concept_verdict, "user_artifact": user_verdict}
    if product_objective:
        findings = objective_mod.partition(
            findings, block_severities=product_config.block_severities
        )
        converged, product_why = objective_mod.converged(all_verdicts, findings, product_config)
    target_why: str | None = None
    if target_rubric is not None:
        from scripts.ddd import target_rubric as target_rubric_mod

        if isinstance(target_rubric, dict):
            # Sources, not a rubric: the default depends on the objective, which
            # is only settled above.
            target_rubric = target_rubric_mod.resolve(
                objective=state.objective,
                verdicts=all_verdicts,
                run_rubric=target_rubric.get("run"),
                spec_rubric=target_rubric.get("spec"),
                config_rubric=target_rubric.get("config"),
            )
        tr = target_rubric_mod.evaluate(
            target_rubric,
            state,
            findings,
            run_dir=run_dir,
            verdicts=all_verdicts,
            distribution=distribution,
            judge_full=judge_full,
        )
        findings = tr["findings"]
        converged = tr["converged"]
        target_why = product_why = tr["why"]

    from scripts.ddd import floor as floor_mod

    scope_ctx = {
        "narrative_locked": bool((edit_scope or {}).get("narrative_locked")),
        "fixed_surfaces": tuple((edit_scope or {}).get("fixed_surfaces") or ()),
        "declined": list((edit_scope or {}).get("declined") or []),
    }
    floor_cls = (
        floor_mod.classify_floor(floor, findings, verdicts=all_verdicts, **scope_ctx)
        if floor
        else None
    )
    floor_first: list[dict] = []
    if floor_cls is not None:
        for f in floor_mod.floor_findings(floor, findings):
            if floor_mod.edit_scope(f, **scope_ctx)[0] == floor_mod.IN:
                floor_first.append(f)
        if floor_first and loop_config.floor_first:
            ids = {id(f) for f in floor_first}
            for f in findings:
                if id(f) in ids:
                    f["floor"] = True
            findings = [f for f in findings if id(f) in ids] + [f for f in findings if id(f) not in ids]
        state.gating_floor = {
            **floor,
            "iteration": state.iteration,
            "judged_full": bool(judge_full),
            "all_out_of_scope": floor_cls["all_out"],
            "findings": [
                {
                    "scene": r.get("scene"),
                    "dimension": r.get("dimension"),
                    "fix_recommendation": str(r.get("fix_recommendation") or r.get("detail") or "")[:400],
                    "edit_scope": r["edit_scope"],
                    "edit_scope_reason": r["edit_scope_reason"],
                }
                for r in floor_cls["findings"]
            ],
        }
    else:
        state.gating_floor = None
    state.findings = findings

    score = min(concept_verdict.overall_score, user_verdict.overall_score)
    if product_objective:
        ps = objective_mod.product_score(all_verdicts)
        score = ps if ps is not None else score
    state.score_history = (state.score_history or []) + [float(score)]
    hist = state.score_history

    fingerprints = denoise.fingerprint_findings(findings)
    state.finding_fingerprints = (state.finding_fingerprints or []) + [fingerprints]
    fp_hist = state.finding_fingerprints

    point = progress.measure(findings, distribution, score, full=judge_full)
    point["target"] = target or "deploy"
    # Which judges produced this point's findings: a concept-only pass reports
    # fewer than a full one, so open_findings is only compared like for like
    # (scripts.ddd.progress._comparable).
    from scripts.ddd import target as _target_mod

    point["judges"] = sorted(judges) if judges else sorted(_target_mod.ALL_JUDGES)
    state.progress_history = (state.progress_history or []) + [point]
    prog = state.progress_history

    # Mode is (re)chosen on FULL passes only — an incremental pass's finding count
    # includes reused cells and is not a fresh read of the backlog.
    if judge_full or state.loop_mode is None:
        state.loop_mode = progress.select_mode(
            loop_config.mode,
            point["open_findings"],
            backlog_min_findings=loop_config.backlog_min_findings,
        )
    state.last_judge_full = bool(judge_full)
    state.batches_since_full = 0 if judge_full else state.batches_since_full + 1

    # "stalled" = the last two iterations produced no new best on ANY progress
    # signal (score, open findings, mean cell, confirmed caps), each read through
    # its own noise band. Falls back to the score alone for a run whose progress
    # history is shorter than its score history (resumed from before 0.2.5xx).
    stalled = False
    if len(prog) >= 3:
        stalled = progress.stalled(prog)
    elif len(hist) >= 3:
        best_before = max(hist[:-2])
        stalled = all(
            denoise.improved(best_before, h) is not True for h in hist[-2:]
        )
    # Did the LAST pass move anything? Progress-aware when both points exist.
    if len(prog) >= 2:
        last_pass_improved = progress.last_step_progressed(prog)
    else:
        last_pass_improved = (
            len(hist) >= 2 and denoise.improved(hist[-2], hist[-1]) is True
        )
    # "plateau" = the same defects came back unchanged AND the score did not
    # genuinely improve. Identical findings alone is not enough — a stub-shaped
    # findings list can repeat while real progress happens — but identical
    # findings PLUS a score move inside the noise band means the move was noise
    # and nothing actually got fixed. Not noisy the way the score alone is: an
    # LLM's score for a cell wobbles +/-1 on identical frames; the defect it
    # names does not.
    plateau = (
        len(fp_hist) >= 2
        and bool(fp_hist[-1])
        and fp_hist[-1] == fp_hist[-2]
        and len(hist) >= 2
        and not last_pass_improved
    )

    all_findings = [
        {
            "route": f.get("route", "PRODUCT"),
            "fix_kind": _routable_fix_kind(f.get("fix_kind", "options")),
            "finding_class": f.get("finding_class", finding_class.UNCLASSIFIED),
            "scene": f.get("scene"),
            "parked": bool(f.get("parked")),
        }
        for f in findings
    ]
    for d in (user_verdict.dimensions or {}).values():
        if isinstance(d, dict) and d.get("fix_kind"):
            all_findings.append(
                {
                    "route": "PRODUCT",
                    "fix_kind": _routable_fix_kind(d["fix_kind"]),
                    "finding_class": finding_class.UNCLASSIFIED,
                }
            )
    non_defer = [f for f in all_findings if f["route"] != "DEFER"]
    mechanical_all = [f for f in non_defer if f["fix_kind"] == "mechanical"]
    # Only mechanical work on UNPARKED scenes is actionable this pass.
    mechanical = [f for f in mechanical_all if not f.get("parked")]
    unclear = [f for f in non_defer if f["fix_kind"] in ("options", "redesign")]
    # Only a STRATEGY finding can open the concept gate. An accuracy finding was
    # already forced mechanical above, so this can no longer fire on "the wrong
    # word is on screen" — which is what escalated two fixable defects to a human.
    strategy_all = [
        f
        for f in non_defer
        if f["route"] == "CONCEPT"
        and f["fix_kind"] == "redesign"
        and f["finding_class"] != finding_class.ACCURACY
    ]
    # A strategy finding on an already-parked scene is already waiting on its
    # decision; only a NEW one can ask for a gate.
    strategy_redesign = [f for f in strategy_all if not f.get("parked")]

    def _finish(action: str, reason: str) -> tuple[str, str]:
        # Fidelity: an inner-loop or concept-only pass may not decide anything.
        from scripts.ddd import target as target_mod

        if action in _CHECKPOINT_BEFORE and target_mod.decision_needs_checkpoint(target, judges, held):
            state.next_judge_full = True
            state.terminal_status = "running"
            if (target or "deploy") != "deploy":
                where = "the local inner-loop build"
            elif held:
                where = f"a recipe-scoped pass (scene(s) {list(held)} held to their last cells)"
            elif judges and set(judges) != {"concept"}:
                where = f"a partial-judge pass ({', '.join(sorted(judges))})"
            else:
                where = "a concept-only pass"
            return (
                "checkpoint",
                f"This pass ran on {where}, which cannot decide {action!r}. Checkpoint: apply "
                "the pending mechanical fixes (recipe ones included), land every batch since the "
                "last checkpoint (PR, CI, deploy, judge_gate set-fix-sha), then render against "
                "the deploy target and run every judge in full. That pass decides. "
                f"Deferred decision: {reason}",
            )
        # M17: a confirmed cap whose fix is a RECIPE edit (no deploy) is fixed,
        # and its scene re-judged, before it may open a gate or end the run.
        # Once per iteration: the re-assessment after the re-judge decides.
        if action in _RECIPE_PREEMPTS and not recipe_rejudged_this_iteration:
            caps = fix_scope.recipe_caps(distribution, findings)
            if caps:
                for k, v in snapshot.items():
                    setattr(state, k, v)
                scenes = sorted({int(c["scene"]) for c in caps})
                state.recipe_rejudge = {
                    "iteration": state.iteration,
                    "scenes": scenes,
                    "cells": caps,
                    "status": "pending",
                    "deferred_action": action,
                }
                state.terminal_status = "running"
                cells = "; ".join(
                    f"scene {c['scene']} {c['dimension']} {c['confirmed']}: {c['fix_recommendation']}"
                    for c in caps
                )
                return (
                    "rejudge_scenes",
                    f"Before deciding {action!r}: confirmed cap(s) whose fix is a RECIPE edit "
                    f"(no deploy) — {cells}. Apply the recipe fix, re-render, re-judge scene(s) "
                    f"{scenes} (judge_scope plan reuses every unchanged scene), and re-run "
                    "assemble for THIS iteration (do not bump state.iteration). The deferred "
                    f"decision was: {reason}",
                )
        state.terminal_status = classify_termination(
            action,
            converged=bool(converged),
            score_history=hist,
            findings=findings,
            unattended=bool(unattended),
            progress_history=prog,
        )["status"]
        return action, reason

    def _continue(reason: str, action: str = "continue") -> tuple[str, str]:
        """``continue`` + the next pass's judge scope (backlog vs polish)."""
        if floor_cls is not None and loop_config.floor_first:
            reason = _floor_first_reason(floor, floor_cls) + reason
        if state.loop_mode == "backlog":
            state.next_judge_full = (
                state.batches_since_full + 1 >= loop_config.full_rejudge_every
            )
            scope = (
                "full re-judge (every "
                f"{_ordinal(loop_config.full_rejudge_every)} batch)"
                if state.next_judge_full
                else "re-judge only scenes whose frame/text/spec changed"
            )
            reason += (
                f" Backlog mode: apply ALL mechanical findings as ONE batch (one PR, one "
                f"deploy), then `iteration render` (it re-films only what the batch changed); "
                f"next judge: {scope}."
            )
        else:
            state.next_judge_full = True
        # What the batch touches: a RECIPE-ONLY batch (recorder framing, narration,
        # why-brief — no product code) has nothing to merge, CI or deploy.
        bp = fix_scope.batch_plan(findings, for_iteration=state.iteration + 1)
        state.batch_plan = bp
        if bp and bp["scope"] == fix_scope.RECIPE:
            reason += (
                f" RECIPE-ONLY batch ({bp['findings']} finding(s), scene(s) {bp['scenes']}): "
                "no product code changes, so do NOT open a product PR or wait on CI/deploy "
                "and do NOT run judge_gate set-fix-sha — edit the recipe/spec in the local "
                "checkout (commit it with the next product batch), `iteration render`, and the "
                "next pass skips the deploy gate"
                + (
                    f" and re-judges only scene(s) {bp['judge_scenes']}"
                    if bp.get("judge_scenes") and not state.next_judge_full
                    else ""
                )
                + "."
            )
        if parked:
            reason += (
                f" Scene(s) {sorted(parked)} are PARKED on a pending decision — withhold "
                "their findings from the batch; they are still rendered and judged."
            )
        if state.loop_mode == "backlog" and (inner_loop_policy or {}).get("status") == "missing":
            # Everything above is already scheduled, so once the config is fixed a
            # logged `decision override` resumes exactly the pass this would have been.
            return _finish(
                "stop_inner_loop_required",
                "Refusing to run a backlog (v1-product) fix loop with only the deploy target: "
                f"{inner_loop_policy.get('reason')}. Every batch would merge, wait on CI and "
                "deploy before a frame is judged. Configure inner_loop (or declare it off with "
                "a reason), then `python -m scripts.ddd.decision override <run_id> --reason "
                f"\"<what you configured>\"` and proceed as {action!r}: {reason}",
            )
        return _finish(action, reason)
    if converged and not judge_full and not getattr(state, "scene_filter", None):
        # An incremental pass reused unchanged scenes' cells. Convergence is only
        # ever declared on a full render + full judge.
        state.next_judge_full = True
        return _finish(
            "confirm_full",
            "Every gating judge passed on an INCREMENTAL pass (unchanged scenes' cells "
            "were reused). Re-render and judge every scene fresh, arc included, before "
            "declaring convergence — no fixes to apply.",
        )
    if (
        product_objective
        and converged
        and product_config.polish_pass
        and not polish_done
        and not getattr(state, "scene_filter", None)
    ):
        from scripts.ddd import target as target_mod

        to_polish = objective_mod.polish_findings(findings)
        if to_polish and not target_mod.decision_needs_checkpoint(target, judges, held):
            # The product is done. Apply the deferred presentation / low-severity
            # findings ONCE, as one batch, then the next full pass decides.
            findings = objective_mod.restore_deferred(findings)
            state.findings = findings
            state.polish_pass = {"iteration": state.iteration + 1, "status": "pending"}
            return _continue(
                f"Product converged ({product_why}). POLISH PASS: apply the "
                f"{len(to_polish)} deferred mechanical presentation / low-severity "
                "finding(s) as ONE batch — never add explanatory copy to do it — then "
                "re-render; the next full pass decides stop_done. One polish pass per run."
            )
    if converged and not getattr(state, "scene_filter", None):
        if product_objective:
            deferred = sum(1 for f in findings if f.get("objective_role") == "deferred")
            return _finish(
                "stop_done",
                f"Product objective converged ({product_why})"
                + (f"; {deferred} deferred presentation finding(s) reported, not chased." if deferred else "."),
            )
        if target_why:
            return _finish("stop_done", f"Target rubric met ({target_why}) — ready for promotion.")
        return _finish("stop_done", "Both judges passed full spec — ready for promotion.")
    if converged and getattr(state, "scene_filter", None):
        return _finish(
            "stop_partial", "Both judges passed the filtered scope — drop --scene and re-fire."
        )
    if floor_cls is not None and floor_cls["all_out"] and not converged:
        # The gate is a minimum: while its floor cell is held only by findings
        # this loop may not touch, no batch can move the score. Iterating to the
        # stall rule (run -004: three more passes) only re-confirms that.
        lines = "; ".join(
            f"scene {r.get('scene') or '?'} {r.get('dimension')}: "
            f"{str(r.get('fix_recommendation') or r.get('detail') or '')[:160]} "
            f"[{r['edit_scope_reason']}]"
            for r in floor_cls["findings"]
        )
        return _finish(
            "stop_out_of_scope",
            f"The gating floor ({_floor_label(floor)}) is held only by finding(s) outside "
            f"this loop's edit scope — {lines}. No fix batch the loop may make can move "
            "the gating score. Fix them at their source (or widen the edit scope), then "
            f"re-run (history={hist}).",
        )
    # Mechanical fixes come FIRST — a confident fix must never sit behind an
    # uncertain one, and a strategy REDESIGN is the most uncertain finding there
    # is. So pending mechanical work holds the concept gate open WHILE IT IS
    # STILL CLEANING THE ARTIFACT, and the human gets the direction question over
    # a clean artifact instead of one carrying defects nobody disputes. The bound
    # is exhaustion, not a count (canopy#588): the first deferral is free; each
    # further one must be paid for by the last pass moving the score outside the
    # noise band. A flat pass buys nothing — which is what makes a crashed pass
    # (applied nothing, score unchanged) and a spent one (applied fixes, score
    # unchanged) end the same way: the gate opens. A stall or plateau still
    # suppresses it (re-applying fixes that already failed to move anything is
    # not worth the gate's wait), and the hard cap is the runaway backstop.
    first_deferral = state.concept_gate_deferred == 0
    under_cap = len(hist) < hard_cap

    defer_concept_gate = (
        bool(strategy_redesign)
        and bool(mechanical)
        and not plateau
        and not stalled
        and under_cap
        and (first_deferral or last_pass_improved)
    )
    if defer_concept_gate:
        state.concept_gate_deferred += 1
        if first_deferral:
            why = (
                "First deferral. The gate opens next pass unless that pass moves the "
                f"score by more than the +/-{denoise.NOISE_BAND} noise band"
            )
        else:
            moved = ", ".join(progress.improved_signals(prog[-2:-1], prog[-1])) or "score"
            why = (
                f"Deferral {state.concept_gate_deferred}: the last pass moved the score "
                f"{hist[-2]} -> {hist[-1]} and improved [{moved}] beyond its noise "
                "band, so the fixes are still cleaning the artifact. The gate opens the "
                "first pass that goes flat, regresses, or plateaus"
            )
        return _continue(
            f"{len(mechanical)} mechanical (confident) fix(es) remain alongside a strategy "
            "finding — apply + re-fire so the concept question is asked over a clean "
            f"artifact. {why} (history={hist}).",
        )
    if strategy_redesign:
        # Park only the scenes the decision is about; keep working on the rest
        # while there is progress to make there. A decision about every scene
        # (or one whose scenes cannot be read) still stops the run.
        affected = parking.affected_scenes(
            [f for f in findings if _is_strategy(f, finding_class) and not f.get("parked")]
        )
        outside = [f for f in mechanical if not (parking.scene_refs(f.get("scene")) & affected)]
        if affected and outside and not stalled and not plateau and under_cap:
            state.park_request = {
                "scenes": sorted(affected),
                "reason": f"{len(strategy_redesign)} strategy finding(s) need a direction call",
                "iteration": state.iteration,
            }
            return _continue(
                f"Strategy decision needed on scene(s) {sorted(affected)} — post the "
                "concept_change gate, then `parking park` those scenes and keep fixing: "
                f"{len(outside)} mechanical fix(es) remain on scenes the decision does not "
                f"touch (history={hist}).",
                action="park_and_continue",
            )
        return _finish(
            "stop_concept_change",
            "Strategy finding (the artifact, not the wording, is wrong) — needs user "
            "judgment on direction."
            + (" Unattended: reported, not waited on." if unattended else ""),
        )
    if strategy_all:
        # Every strategy finding sits on a parked scene: its decision is pending.
        if mechanical and not stalled and not plateau and under_cap:
            return _continue(
                f"{len(mechanical)} mechanical fix(es) remain on unparked scenes while the "
                f"decision on scene(s) {sorted(parked)} is pending (history={hist}).",
            )
        return _finish(
            "stop_concept_change",
            f"Only the parked scene(s) {sorted(parked)} have work left, and it waits on a "
            "pending decision."
            + (" Unattended: reported, not waited on." if unattended else ""),
        )
    # A stall is checked BEFORE pending mechanical work: that branch used to come
    # first, so on a v1 product (which always has mechanical findings) stall
    # detection could never fire. Stall is now progress-aware, so a run whose
    # floor is pinned while its backlog shrinks is NOT stalled and keeps going.
    if stalled:
        return _finish(
            "stop_max_iter",
            f"Stalled: no progress signal improved across the last 2 iterations "
            f"(score, open findings, mean cell, confirmed caps; history={hist}, "
            f"progress={[_short(p) for p in prog[-3:]]}, score noise band "
            f"+/-{denoise.NOISE_BAND}) — fixes aren't converging; needs a human look.",
        )
    # A plateau means re-applying the mechanical fixes is not producing change.
    # The hard cap applies here too — a backstop that a `continue` can step over
    # is not a backstop.
    if mechanical and not plateau and under_cap:
        return _continue(
            f"{len(mechanical)} mechanical (confident) fix(es) remain — apply + re-fire "
            f"before surfacing any options (history={hist}).",
        )
    if plateau:
        return _finish(
            "stop_max_iter",
            f"Finding plateau — iterations {len(fp_hist) - 1} and {len(fp_hist)} produced "
            f"an identical set of {len(fingerprints)} finding(s) with no real score move; "
            f"the loop is re-deriving, not progressing (history={hist}).",
        )
    if len(hist) >= hard_cap:
        return _finish(
            "stop_max_iter", f"Hit the {hard_cap}-iteration backstop (history={hist})."
        )
    if unclear:
        return _finish(
            "stop_unclear",
            f"{len(unclear)} options/redesign finding(s) and no mechanical fixes left "
            "to auto-apply — surface ONLY these for a user pick."
            + (" Unattended: reported, not waited on." if unattended else ""),
        )
    return _continue(
        f"No options/redesign and score still moving (history={hist}) — re-fire.",
    )


def _floor_label(floor: dict | None) -> str:
    if not floor:
        return "unknown"
    scenes = floor.get("scenes") or []
    return (
        f"{'/'.join(floor.get('judges') or [])} "
        f"{', '.join(floor.get('dimensions') or []) or 'overall'} = {floor.get('score')}"
        + (f" on scene(s) {scenes}" if scenes else "")
    )


def _floor_first_reason(floor: dict | None, cls: dict) -> str:
    """The FLOOR-FIRST preamble of a ``continue`` reason (canopy#780)."""
    rows = [r for r in cls.get("findings") or [] if r.get("edit_scope") == "in"]
    if not rows:
        return ""
    fixes = "; ".join(
        f"scene {r.get('scene') or '?'} {r.get('dimension')}: "
        f"{str(r.get('fix_recommendation') or r.get('detail') or '')[:200]}"
        for r in rows
    )
    return (
        f"FLOOR FIRST — the gate is held by {_floor_label(floor)}. Fix these "
        f"{len(rows)} finding(s) first and make sure they are in this batch (stamped "
        f"`floor: true`, ordered first in state.findings): {fixes}. "
    )


#: Decisions an inner-loop / concept-only pass must hand to a checkpoint.
_CHECKPOINT_BEFORE = frozenset(
    {
        "stop_done",
        "stop_out_of_scope",
        "stop_concept_change",
        "stop_max_iter",
        "stop_unclear",
        "park_and_continue",
        "confirm_full",
    }
)

#: Terminal (or gate-opening) actions a recipe re-judge must come before (M17).
_RECIPE_PREEMPTS = frozenset(
    {"stop_concept_change", "stop_max_iter", "stop_unclear", "park_and_continue"}
)

#: RunState fields compute_auto_iterate mutates that a recipe re-judge rolls back.
_REJUDGE_ROLLBACK = (
    "score_history",
    "finding_fingerprints",
    "progress_history",
    "loop_mode",
    "last_judge_full",
    "batches_since_full",
    "next_judge_full",
    "concept_gate_deferred",
    "park_request",
    "batch_plan",
    "polish_pass",
)


def _is_strategy(finding: dict, finding_class) -> bool:
    return (
        finding.get("route", "PRODUCT") == "CONCEPT"
        and _routable_fix_kind(finding.get("fix_kind", "options")) == "redesign"
        and finding.get("finding_class", finding_class.UNCLASSIFIED) != finding_class.ACCURACY
    )


def _ordinal(n: int) -> str:
    """``1st``, ``2nd``, ``3rd``, ``4th``, ``11th``, ``22nd`` …"""
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _short(point: dict) -> str:
    """Compact progress point for a reason string."""
    return (
        f"s={point.get('score')} f={point.get('open_findings')} "
        f"m={point.get('mean_cell')} c={point.get('confirmed_caps')}"
    )


# ---------------------------------------------------------------------------
# Termination — the loop owns its own stopping story
# ---------------------------------------------------------------------------

#: Every action the loop can return, and whether it hands control back.
TERMINAL_ACTIONS = frozenset(
    {
        "stop_done",
        "stop_partial",
        "stop_out_of_scope",
        "stop_concept_change",
        "stop_unclear",
        "stop_max_iter",
        "stop_inner_loop_required",
    }
)


def classify_termination(
    action: str,
    *,
    converged: bool,
    score_history: list[float] | None = None,
    findings: list[dict] | None = None,
    unattended: bool = False,
    progress_history: list[dict] | None = None,
) -> dict:
    """Say WHICH kind of ending this is — the loop's own answer, not the caller's.

    An orchestrator inventing "hard stop after this pass" is the symptom of a loop
    that does not own its termination. The four endings are genuinely different
    and must not print the same way:

    ``converged_clean``
        Both judges passed and nothing strategic is outstanding. Ship it.
    ``converged_with_open_questions``
        Passed, but strategy findings remain that only a human can answer. The
        artifact is good; the story may not be the right one.
    ``stopped_not_converged``
        Out of moves without passing — plateau, stall, or the runaway backstop.
        This is "converged, still failing": stable, and stably bad.
    ``diverging``
        The run is going backwards. With a progress history (two or more
        points) that means the LAST step fell on the floor AND the mean cell AND
        the open-findings count, each beyond its noise band
        (:func:`scripts.ddd.progress.declined`) — a single capped cell pins the
        floor and is not a decline (M17). Without one (legacy runs) the score
        trend alone decides, as before.
    ``blocked_out_of_scope``
        The gating floor is held only by findings this loop may not fix
        (``stop_out_of_scope``, canopy#780). Not a stall and not divergence:
        the loop stopped because nothing it can do moves the gate.
    ``running``
        Not terminal — keep looping.
    """
    from scripts.ddd import denoise, finding_class

    open_strategy = [
        f
        for f in (findings or [])
        if f.get("finding_class") == finding_class.STRATEGY and f.get("route") != "DEFER"
    ]
    from scripts.ddd import progress

    direction = denoise.trend(score_history)
    prog = [p for p in (progress_history or []) if isinstance(p, dict)]
    if len(prog) >= 2:
        is_diverging = progress.declined(prog)
    else:
        is_diverging = direction == "regressing"

    if action not in TERMINAL_ACTIONS:
        status = "running"
    elif action == "stop_inner_loop_required":
        status = "needs_config"
    elif action == "stop_out_of_scope":
        status = "blocked_out_of_scope"
    elif action in ("stop_done", "stop_partial") or converged:
        status = "converged_with_open_questions" if open_strategy else "converged_clean"
    elif is_diverging:
        status = "diverging"
    else:
        status = "stopped_not_converged"

    return {
        "status": status,
        "terminal": status != "running",
        "action": action,
        "converged": bool(converged),
        "trend": direction,
        "open_strategy_findings": len(open_strategy),
        "unattended": bool(unattended),
        "summary": _TERMINATION_SUMMARY[status],
    }


_TERMINATION_SUMMARY = {
    "needs_config": (
        "Refused to continue: a backlog loop on a repo with a deploy gate needs an inner_loop "
        "(or inner_loop: off with a reason) in .canopy/ddd/config.yaml."
    ),
    "converged_clean": "Converged and clean — every gating judge passed and nothing strategic is open.",
    "converged_with_open_questions": (
        "Converged, with open strategy questions — the artifact passes, but findings remain "
        "that only a human can answer (is this the right story to tell?)."
    ),
    "stopped_not_converged": (
        "Stopped without converging — out of autonomous moves (plateau, stall, or backstop). "
        "Stable, and stably failing."
    ),
    "diverging": (
        "Diverging — the floor, the mean cell and the open-findings count all fell beyond "
        "their noise bands. Fixes are fighting each other; more iterations will not help."
    ),
    "blocked_out_of_scope": (
        "Stopped on an out-of-scope floor — the cell holding the gating score down is held only "
        "by findings this loop may not fix (data, registry, a locked narration, a deferred or "
        "declined finding). Fix them at their source, then re-run."
    ),
    "running": "Still iterating.",
}
