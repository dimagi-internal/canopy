"""What the DDD loop is optimizing — the PRODUCT getting better, or the DEMO being right.

Why this module exists
----------------------
Until 0.2.559 there was one objective: every gating judge's ``overall_score``
>= 4, where each score is the weakest cell over ~7 scenes x ~5 dimensions from a
judge that starts every cell at 3/5 and moves +/-1 on a byte-identical frame.
On a mature product that is the right bar (``nutrition-demo`` converged on it).
On a v1 product it is unreachable and it points the loop at the wrong work:

* connect-labs ``supply-sophie-unanswered-round`` (2026-10-02..04): 4 runs,
  ~24 iterations, 12 PRs, ~33 h wall time, never converged. 25 of the final 27
  open findings were severity ``low`` ("outlined vs filled chip", "date shown
  three times", a scroll offset); every judge sat at 3/5.
* The work the loop DID do accreted. Each "this is unclear" became an
  explanatory sentence or a narrow rule in product code, and "make the narrated
  claim literally on screen" became product changes. Total UI text on the
  pre-redesign screens: ~5.2k words, 36 lines of 20+ words. One ten-minute human
  look at the slides ("walls of text", "AI-generated prose instead of facts",
  "no clear state of this procurement", "we renamed round to tender") produced
  the redesign that deleted 4,148 lines; the clean screens carry ~3.3k words and
  10 such lines. No judge had ever raised those as blockers.

So there are now two objectives, picked once per run and then sticky:

``demo``
    Today's loop, unchanged. The walkthrough is the deliverable; every gating
    judge must reach the threshold.

``product``
    The demo is a PROBE of the product. Findings are partitioned:

    * **blocking** — a product dimension (task completion, trust, clarity,
      design soundness, use-case soundness, or a product-lens / product-lint
      finding) at a blocking severity (default ``high``/``medium``). These drive
      ``continue`` and decide convergence.
    * **ride-along** — accuracy findings (the narration overstates the screen).
      Always a narration edit, never product code (:func:`route_accuracy`), so
      they are cheap recipe-scope fixes that travel with any batch.
    * **deferred** — presentation dimensions (visual polish, variety, motion,
      arc, claim/reality wording) and low-severity product nits. Stamped
      ``route: DEFER`` with the original route kept in ``deferred_route``; they
      are applied ONCE, as the final polish pass, not chased every iteration.

    Converged = no gating verdict blocked, every product dimension's weakest
    cell >= ``product.floor`` (3), no presentation cell below
    ``product.presentation_floor`` (2 — "broken", not "unpolished"), and no open
    blocking finding.

``auto`` resolves on the first FULL judged pass: ``product`` when it has
``loop.backlog_min_findings`` or more open findings (the same signal that already
picks backlog mode), else ``demo``.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

PRODUCT = "product"
DEMO = "demo"
AUTO = "auto"

#: Dimensions that measure the PRODUCT (would a user be able to do the job, and
#: trust and understand what they see?). Everything else measures the demo.
PRODUCT_DIMENSIONS = frozenset(
    {
        # user-artifact judge
        "task_completion",
        "trust",
        "clarity",
        # concept judge
        "design_soundness",
        "use_case_soundness",
        # product lens (skills/ddd-product-review) + product lint
        "product_coherence",
        "missing_view",
        "information_density",
        "consistency",
        "domain_correctness",
        "terminology",
        "design_system",
        "prose_density",
    }
)

#: Sources whose findings are product findings whatever their dimension says.
PRODUCT_SOURCES = frozenset({"product_lens", "product_lint"})

DEFERRED_BY = "objective:product"


def _dim(f: dict) -> str:
    return str(f.get("dimension") or "").strip().lower()


def _severity(f: dict) -> str:
    return str(f.get("severity") or "medium").strip().lower()


def is_product_finding(f: dict) -> bool:
    return str(f.get("source") or "") in PRODUCT_SOURCES or _dim(f) in PRODUCT_DIMENSIONS


def resolve(configured: str, state: Any, open_findings: int, *, backlog_min_findings: int, judge_full: bool) -> str:
    """The run's objective. Explicit config wins; ``auto`` is decided once, on a full pass."""
    if configured in (PRODUCT, DEMO):
        return configured
    current = getattr(state, "objective", None)
    if current in (PRODUCT, DEMO):
        return current
    if not judge_full:
        # An incremental pass's count includes reused cells; wait for a fresh read.
        return DEMO if open_findings < backlog_min_findings else PRODUCT
    return PRODUCT if open_findings >= backlog_min_findings else DEMO


def route_accuracy(f: dict) -> dict:
    """An accuracy finding is fixed in the WORDS, never in the product.

    ``finding_class.normalize_findings`` already substitutes
    ``CANONICAL_ACCURACY_FIX`` ("change the words, never the artifact") — but left
    the judge's ``route: PRODUCT`` in place, so ``fix_scope.lands_in_product``
    sent a narration edit to a product PR. In the product objective that is the
    exact pressure that turned "the narration says X" into hard-coded copy and
    rules. Stamp ``fix_scope: narrative`` so the batch plan treats it as a
    recipe-scope edit.
    """
    g = dict(f)
    g["fix_scope"] = "narrative"
    g.setdefault("fix_direction", "narration follows the product")
    return g


_NARRATION_SIDE = re.compile(
    r"\bnarrat|\bvoice-?over\b|\bvo\b|\bconcept_claim\b|\bscene\s+title\b|\bthe\s+script\b|\bre-?script",
    re.IGNORECASE,
)
_ASSERTION_DIMENSIONS = frozenset({"claim_reality_coherence", "why_groundedness"})


def is_narration_fix(f: dict) -> bool:
    """An accuracy finding that is really about the NARRATION (vs the screen).

    ``finding_class`` grants ``accuracy`` on phrases like "labelled" or "does not
    match" — which also describe a PRODUCT inconsistency ("the same act is
    labelled 'Open draft' here and 'Remind' there"). On the real run that turned
    product consistency defects into "restate the narration" edits. Only an
    assertion dimension or a finding that names the narration/script is one.
    """
    if _dim(f) in _ASSERTION_DIMENSIONS:
        return True
    return bool(_NARRATION_SIDE.search(f"{f.get('detail') or ''} {f.get('fix_recommendation_original') or ''}"))


def _unaccuracy(f: dict) -> dict:
    """Undo a canonical narration fix substituted onto a product finding."""
    g = dict(f)
    if g.get("fix_recommendation_original"):
        g["fix_recommendation"] = g.pop("fix_recommendation_original")
    override = g.pop("fix_kind_override", None)
    if isinstance(override, dict) and override.get("from"):
        g["fix_kind"] = override["from"]
    g["finding_class"] = "unclassified"
    g["finding_class_reason"] = "product objective: an accuracy phrase about the product, not the narration"
    return g


def partition(findings: list[dict], *, block_severities: Iterable[str]) -> list[dict]:
    """Stamp every finding blocking / ride-along / deferred (see module doc).

    Returns NEW dicts. Deferred findings become ``route: DEFER`` (the loop's
    existing "report, don't act" route) with ``deferred_route`` + ``deferred_by``
    so the final polish pass can restore them.
    """
    from scripts.ddd import finding_class

    block = {s.lower() for s in block_severities}
    out: list[dict] = []
    for raw in findings or []:
        f = dict(raw)
        route = str(f.get("route") or "PRODUCT").upper()
        if route == "DEFER":
            f["objective_role"] = "deferred"
            out.append(f)
            continue
        if f.get("finding_class") == finding_class.ACCURACY and not is_narration_fix(f):
            f = _unaccuracy(f)
        if f.get("finding_class") == finding_class.ACCURACY:
            f = route_accuracy(f)
            f["objective_role"] = "ride_along"
        elif is_product_finding(f) and _severity(f) in block:
            f["objective_role"] = "blocking"
        else:
            f["deferred_route"] = f.get("route") or "PRODUCT"
            f["deferred_by"] = DEFERRED_BY
            f["route"] = "DEFER"
            f["objective_role"] = "deferred"
        out.append(f)
    return out


def restore_deferred(findings: list[dict]) -> list[dict]:
    """Undo :func:`partition`'s deferral for what the polish pass applies.

    Only mechanical, unparked findings come back — an ``options`` finding is
    still a choice nobody made, and stays deferred (reported, not applied).
    """
    out = []
    for raw in findings or []:
        f = dict(raw)
        if f.get("deferred_by") == DEFERRED_BY and f.get("fix_kind") == "mechanical" and not f.get("parked"):
            f["route"] = f.pop("deferred_route", "PRODUCT")
            f.pop("deferred_by", None)
            f["objective_role"] = "polish"
        out.append(f)
    return out


def polish_findings(findings: list[dict]) -> list[dict]:
    """Deferred findings the polish pass can apply (mechanical, not parked)."""
    return [
        f
        for f in findings or []
        if f.get("deferred_by") == DEFERRED_BY
        and f.get("fix_kind") == "mechanical"
        and not f.get("parked")
    ]


def _dimension_scores(verdict: Any) -> dict[str, float]:
    out: dict[str, float] = {}
    for name, dim in (getattr(verdict, "dimensions", None) or {}).items():
        score = getattr(dim, "score", None)
        if score is None and isinstance(dim, dict):
            score = dim.get("score")
        try:
            out[str(name).lower()] = float(score)
        except (TypeError, ValueError):
            continue
    return out


def product_score(verdicts: dict[str, Any]) -> float | None:
    """Weakest PRODUCT-dimension score across the gating verdicts (None if none scored)."""
    scores = [
        s
        for v in verdicts.values()
        if getattr(v, "gate", None) == "gating"
        for d, s in _dimension_scores(v).items()
        if d in PRODUCT_DIMENSIONS
    ]
    return min(scores) if scores else None


def converged(verdicts: dict[str, Any], findings: list[dict], cfg: Any) -> tuple[bool, str]:
    """Product-objective convergence over already-:func:`partition`-ed findings.

    ``cfg`` is a :class:`scripts.ddd.loop_config.ProductConfig`.
    """
    gating = {k: v for k, v in verdicts.items() if getattr(v, "gate", None) == "gating"}
    if not gating:
        return False, "no gating verdict — convergence must be demonstrated"
    for k, v in gating.items():
        if v.verdict == "blocked":
            return False, f"{k} verdict is blocked"
        if v.live_state_verified is False:
            return False, f"{k} verdict never touched live state"
    low_product: list[str] = []
    broken: list[str] = []
    for k, v in gating.items():
        for d, s in _dimension_scores(v).items():
            if d in PRODUCT_DIMENSIONS and s < cfg.floor:
                low_product.append(f"{k}.{d}={s:g}")
            elif d not in PRODUCT_DIMENSIONS and s < cfg.presentation_floor:
                broken.append(f"{k}.{d}={s:g}")
    if low_product:
        return False, f"product dimension(s) below the {cfg.floor:g} floor: {', '.join(low_product)}"
    if broken:
        return False, (
            f"presentation dimension(s) below {cfg.presentation_floor:g} (broken, not unpolished): "
            + ", ".join(broken)
        )
    blocking = [f for f in findings or [] if f.get("objective_role") == "blocking"]
    if blocking:
        return False, f"{len(blocking)} blocking product finding(s) open"
    return True, "every product dimension >= floor and no blocking product finding"


def summary(findings: list[dict]) -> dict[str, int]:
    roles = {"blocking": 0, "ride_along": 0, "deferred": 0, "polish": 0}
    for f in findings or []:
        r = f.get("objective_role")
        if r in roles:
            roles[r] += 1
    return roles


__all__ = [
    "AUTO",
    "DEMO",
    "PRODUCT",
    "PRODUCT_DIMENSIONS",
    "converged",
    "is_product_finding",
    "partition",
    "polish_findings",
    "product_score",
    "resolve",
    "restore_deferred",
    "route_accuracy",
    "summary",
]
