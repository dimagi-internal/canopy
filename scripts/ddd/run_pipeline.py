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
    from scripts.ddd.loop_config import LoopConfig

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
    """
    from scripts.ddd import denoise, finding_class, gates, progress
    from scripts.ddd.loop_config import LoopConfig

    if loop_config is None:
        loop_config = LoopConfig()

    if converged is None:
        converged = compute_convergence(concept_verdict, user_verdict)
    if unattended is None:
        unattended = gates.is_unattended()

    findings = finding_class.normalize_findings(findings or [])
    state.findings = findings

    score = min(concept_verdict.overall_score, user_verdict.overall_score)
    state.score_history = (state.score_history or []) + [float(score)]
    hist = state.score_history

    fingerprints = denoise.fingerprint_findings(findings)
    state.finding_fingerprints = (state.finding_fingerprints or []) + [fingerprints]
    fp_hist = state.finding_fingerprints

    point = progress.measure(findings, distribution, score, full=judge_full)
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
    mechanical = [f for f in non_defer if f["fix_kind"] == "mechanical"]
    unclear = [f for f in non_defer if f["fix_kind"] in ("options", "redesign")]
    # Only a STRATEGY finding can open the concept gate. An accuracy finding was
    # already forced mechanical above, so this can no longer fire on "the wrong
    # word is on screen" — which is what escalated two fixable defects to a human.
    strategy_redesign = [
        f
        for f in non_defer
        if f["route"] == "CONCEPT"
        and f["fix_kind"] == "redesign"
        and f["finding_class"] != finding_class.ACCURACY
    ]

    def _finish(action: str, reason: str) -> tuple[str, str]:
        state.terminal_status = classify_termination(
            action,
            converged=bool(converged),
            score_history=hist,
            findings=findings,
            unattended=bool(unattended),
        )["status"]
        return action, reason

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
    if converged and not getattr(state, "scene_filter", None):
        return _finish("stop_done", "Both judges passed full spec — ready for promotion.")
    if converged and getattr(state, "scene_filter", None):
        return _finish(
            "stop_partial", "Both judges passed the filtered scope — drop --scene and re-fire."
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

    def _continue(reason: str) -> tuple[str, str]:
        """``continue`` + the next pass's judge scope (backlog vs polish)."""
        if state.loop_mode == "backlog":
            state.next_judge_full = (
                state.batches_since_full + 1 >= loop_config.full_rejudge_every
            )
            scope = (
                "full re-judge (every "
                f"{loop_config.full_rejudge_every}th batch)"
                if state.next_judge_full
                else "re-judge only scenes whose frame/text/spec changed"
            )
            reason += (
                f" Backlog mode: apply ALL mechanical findings as ONE batch (one PR, one "
                f"deploy), then full re-render; next judge: {scope}."
            )
        else:
            state.next_judge_full = True
        return _finish("continue", reason)
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
        return _finish(
            "stop_concept_change",
            "Strategy finding (the artifact, not the wording, is wrong) — needs user "
            "judgment on direction."
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
    {"stop_done", "stop_partial", "stop_concept_change", "stop_unclear", "stop_max_iter"}
)


def classify_termination(
    action: str,
    *,
    converged: bool,
    score_history: list[float] | None = None,
    findings: list[dict] | None = None,
    unattended: bool = False,
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
        The score is going backwards beyond the noise band. Fixes are fighting
        each other; more iterations will make it worse, not better.
    ``running``
        Not terminal — keep looping.
    """
    from scripts.ddd import denoise, finding_class

    open_strategy = [
        f
        for f in (findings or [])
        if f.get("finding_class") == finding_class.STRATEGY and f.get("route") != "DEFER"
    ]
    direction = denoise.trend(score_history)

    if action not in TERMINAL_ACTIONS:
        status = "running"
    elif action in ("stop_done", "stop_partial") or converged:
        status = "converged_with_open_questions" if open_strategy else "converged_clean"
    elif direction == "regressing":
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
        "Diverging — the gating score is moving backwards beyond the noise band. Fixes are "
        "fighting each other; more iterations will not help."
    ),
    "running": "Still iterating.",
}
