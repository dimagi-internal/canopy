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

from scripts.ddd.schemas.models import RunState, Verdict

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Runaway backstop only — NOT the normal stop. The loop stops on real gates,
# options/redesign findings, or a score stall/regression long before this.
HARD_CAP: int = 10
# Back-compat alias for older callers; no longer a hard 3-iteration cap.
MAX_ITERATIONS: int = HARD_CAP

# How many times pending MECHANICAL work may hold the concept gate open. The gate
# buys a human's taste judgment on DIRECTION; opening it over an artifact that
# still carries confidently-fixable defects spends that judgment on a
# misrepresentation. One deferral = at most one extra render, then the gate opens
# regardless — the bound is what stops a self-regenerating mechanical backlog from
# starving it.
CONCEPT_GATE_MAX_DEFERRALS: int = 1


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


def compute_auto_iterate(
    state: RunState,
    concept_verdict: Verdict,
    user_verdict: Verdict,
    findings: list[dict],
    *,
    converged: bool | None = None,
    hard_cap: int = HARD_CAP,
    unattended: bool | None = None,
) -> tuple[str, str]:
    """Decide the next loop action from the SCORE TRAJECTORY, not an iteration count.

    DDD's point is to loop autonomously until the findings it can act on are
    exhausted. A raw count stopped good runs mid-progress and was blind to
    regressions. This gates on whether the run is still making progress:

    - converged (both judges >= threshold)        -> ``stop_done`` / ``stop_partial``
    - a STRATEGY CONCEPT/redesign finding, with
      mechanical fixes still pending (once only)  -> ``continue`` (gate deferred)
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
    4. **Lets pending mechanical work run BEFORE the concept gate opens**, exactly
       once (:data:`CONCEPT_GATE_MAX_DEFERRALS`, counted in
       ``state.concept_gate_deferred``). A strategy redesign is maximally uncertain
       — it is precisely "we may have built the wrong thing" — so letting it jump
       ahead of confident fixes inverted this function's own invariant. It also
       spends the gate badly: the human is asked "is this the right direction?"
       over an artifact wrong in ways nobody disputes, and the score and video
       they judge measure a product that is about to stop existing.

    Mutates ``state.score_history`` (this iteration's gating score),
    ``state.finding_fingerprints`` (this iteration's fingerprint set) and
    ``state.terminal_status`` (see :func:`classify_termination`), and returns
    ``(action, reason)``. The gating score is the lower of the two judges'
    overall_score (claim_reality_coherence is already excluded upstream).

    ``unattended`` (default: auto-detect via :func:`scripts.ddd.gates.is_unattended`)
    does not change WHICH action is returned — it changes whether a stuck stop is
    TERMINAL. With no human present, a stuck stop must end the run with an honest
    report rather than wait on a click nobody will make.
    """
    from scripts.ddd import denoise, finding_class, gates

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

    # "stalled" = the last two iterations produced no new best, judged through the
    # noise band so a wobble inside +/-NOISE_BAND is neither progress nor regression.
    stalled = False
    if len(hist) >= 3:
        best_before = max(hist[:-2])
        stalled = all(
            denoise.improved(best_before, h) is not True for h in hist[-2:]
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
        and denoise.improved(hist[-2], hist[-1]) is not True
    )

    all_findings = [
        {
            "route": f.get("route", "PRODUCT"),
            "fix_kind": f.get("fix_kind", "options"),
            "finding_class": f.get("finding_class", finding_class.UNCLASSIFIED),
        }
        for f in findings
    ]
    for d in (user_verdict.dimensions or {}).values():
        if isinstance(d, dict) and d.get("fix_kind"):
            all_findings.append(
                {
                    "route": "PRODUCT",
                    "fix_kind": d["fix_kind"],
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

    if converged and not getattr(state, "scene_filter", None):
        return _finish("stop_done", "Both judges passed full spec — ready for promotion.")
    if converged and getattr(state, "scene_filter", None):
        return _finish(
            "stop_partial", "Both judges passed the filtered scope — drop --scene and re-fire."
        )
    # Mechanical fixes come FIRST — a confident fix must never sit behind an
    # uncertain one, and a strategy REDESIGN is the most uncertain finding there
    # is. So pending mechanical work holds the concept gate open, ONCE, and the
    # human gets the direction question over a clean artifact instead of one
    # carrying defects nobody disputes. A plateau still suppresses it: re-applying
    # fixes that already failed to move anything is not worth the gate's wait.
    defer_concept_gate = (
        bool(strategy_redesign)
        and bool(mechanical)
        and not plateau
        and state.concept_gate_deferred < CONCEPT_GATE_MAX_DEFERRALS
    )
    if defer_concept_gate:
        state.concept_gate_deferred += 1
        return _finish(
            "continue",
            f"{len(mechanical)} mechanical (confident) fix(es) remain alongside a strategy "
            "finding — apply + re-fire so the concept question is asked over a clean "
            f"artifact. The gate opens next pass if the strategy finding persists "
            f"(deferral {state.concept_gate_deferred}/{CONCEPT_GATE_MAX_DEFERRALS}, "
            f"history={hist}).",
        )
    if strategy_redesign:
        return _finish(
            "stop_concept_change",
            "Strategy finding (the artifact, not the wording, is wrong) — needs user "
            "judgment on direction."
            + (" Unattended: reported, not waited on." if unattended else ""),
        )
    # A plateau means re-applying the mechanical fixes is not producing change.
    if mechanical and not plateau:
        return _finish(
            "continue",
            f"{len(mechanical)} mechanical (confident) fix(es) remain — apply + re-fire "
            f"before surfacing any options (history={hist}).",
        )
    if stalled:
        return _finish(
            "stop_max_iter",
            f"Score stalled/regressed across the last 2 iterations (history={hist}, "
            f"noise band +/-{denoise.NOISE_BAND}) — fixes aren't converging; needs a human look.",
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
    return _finish(
        "continue",
        f"No options/redesign and score still moving (history={hist}) — re-fire.",
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
