"""Unified verdict loader — one schema over six judge artifacts (canopy#265 item 1).

DDD's judges historically emitted three structural schemas:

  * concept / user_artifact / actionability / why — the canonical weighted-
    dimension ``Verdict`` YAML (``scripts.narrative.models.Verdict``)
  * ``verdict-timing.json`` — render_locally.py's deterministic shape
    (``{verdict, overallScore, coverage, findings[]}``, camelCase, no dimensions)
  * ``verdict-video.json`` — the video judge's per-scene shape
    (``{scenes: [{beat, scores{}, overall, verdict, findings[]}]}``)

``load_verdict(path)`` normalizes any of them into the one ``Verdict`` model and
stamps ``kind`` / ``gate`` / ``live_state_verified`` defaults per verdict family
(from ``KIND_DEFAULTS``) when the emitter didn't set them. Downstream code
(``run_pipeline.compute_convergence_all``, reporting) is generic over the result —
adding a judge means adding a KIND_DEFAULTS row, not editing the assembler.

Returns ``None`` when the artifact carries nothing to aggregate (timing eval
n/a on a no-form walkthrough; video verdict with zero scenes).
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from scripts.ddd.schemas.models import Dimension, Verdict

# kind -> (gate, live_state_verified). gate is convergence participation;
# live_state_verified says whether the family's grading anchor touches live
# state (screenshots / the mp4) or AI-authored text (the inflation zone —
# see LIVE_STATE_UNVERIFIED_CAP in scripts.narrative.models).
KIND_DEFAULTS: dict[str, tuple[str, bool]] = {
    "concept": ("gating", True),  # scores live per-scene screenshots
    "user_artifact": ("gating", True),  # visual-judge on live screenshots
    # Gating, per ddd-arc-eval's own declaration ("a demo with no arc is not
    # converged"): it is the ONLY lens that sees the scenes as a sequence, so
    # repetition, a sagging middle and a payoff landing before its setup are
    # invisible to every other judge. Absent from this table, verdict-arc.yaml
    # was written to the run dir and then never loaded — an arc FAIL could not
    # reach compute_convergence, so a narrative with no arc converged on the
    # per-scene pair alone, which is the exact blindness the lens exists to remove.
    "arc": ("gating", True),  # judges the rendered scenes in sequence
    "why": ("advisory", False),  # AI text graded against AI text
    "actionability": ("advisory", False),  # narration graded against features[]
    "timing": ("advisory", True),  # deterministic, measured off the real mp4
    "video": ("advisory", True),  # multimodal judge watches the produced video
}

# filename stem -> kind, for the standard artifact names
_FILENAME_KINDS = {
    "verdict-concept": "concept",
    "verdict-user": "user_artifact",
    "verdict-arc": "arc",
    "verdict-why": "why",
    "verdict-actionability": "actionability",
    "verdict-timing": "timing",
    "verdict-video": "video",
}

# The out-of-chain verdict artifacts a run dir may carry beyond the gating pair
# (concept / user_artifact). /canopy:ddd-run Step 4 discovers whichever of these
# are present and feeds them through the aggregator (canopy#273 item 1).
EXTRA_VERDICT_FILENAMES: tuple[str, ...] = (
    "verdict-arc.yaml",
    "verdict-timing.json",
    "verdict-video.json",
    "verdict-why.yaml",
    "verdict-actionability.yaml",
)


def discover_extra_verdicts(
    run_dir: str | Path,
) -> tuple[dict[str, Verdict], dict[str, str]]:
    """Load every extra (non-gating-pair) verdict artifact present in *run_dir*.

    Scans for ``EXTRA_VERDICT_FILENAMES`` (timing / video / why / actionability),
    loads each through :func:`load_verdict` (which stamps kind / gate /
    live_state_verified from KIND_DEFAULTS), and skips artifacts that carry
    nothing to aggregate (``load_verdict`` returned ``None``).

    Returns ``(verdicts_by_kind, paths_by_kind)`` — shaped for direct pass-through
    to ``run_pipeline.compute_convergence(extra=verdicts_by_kind)`` and
    ``run_pipeline.assemble_run_state(extra_verdict_paths=paths_by_kind)``.
    Missing files are simply absent from the result; an empty run dir yields
    ``({}, {})``.
    """
    run_dir = Path(run_dir)
    verdicts: dict[str, Verdict] = {}
    paths: dict[str, str] = {}
    for name in EXTRA_VERDICT_FILENAMES:
        path = run_dir / name
        if not path.exists():
            continue
        verdict = load_verdict(path)
        if verdict is None:
            continue  # n/a artifact (timing with no fields; video with no scenes)
        kind = verdict.kind or _FILENAME_KINDS[path.stem]
        verdicts[kind] = verdict
        paths[kind] = str(path)
    return verdicts, paths


def load_verdict(path: str | Path, *, kind: str | None = None) -> Verdict | None:
    """Load any DDD verdict artifact as a unified ``Verdict``.

    ``kind`` overrides filename inference (needed for non-standard filenames).
    Raises ``ValueError`` when the kind can't be determined.
    """
    path = Path(path)
    if kind is None:
        kind = _FILENAME_KINDS.get(path.stem)
    if kind is None:
        raise ValueError(
            f"cannot infer verdict kind from filename {path.name!r} — pass kind="
        )

    if kind == "timing":
        return _from_timing(json.loads(path.read_text()), kind)
    if kind == "video":
        return _from_video(json.loads(path.read_text()), kind)

    raw = yaml.safe_load(path.read_text())
    return _fill_kind_defaults(raw, kind)


def _fill_kind_defaults(raw: dict, kind: str) -> Verdict:
    """Stamp kind/gate/live_state_verified defaults where the emitter didn't."""
    gate, verified = KIND_DEFAULTS.get(kind, ("advisory", False))
    raw = dict(raw)
    raw.setdefault("kind", kind)
    raw.setdefault("gate", gate)
    raw.setdefault("live_state_verified", verified)
    return Verdict.model_validate(raw)


def _from_timing(raw: dict, kind: str) -> Verdict | None:
    """Normalize render_locally.py's verdict-timing.json.

    ``verdict: null`` means the walkthrough has no narrated form fields — the
    warp never engaged and there is nothing to sync (expected on dashboard/map
    reads), so there is no verdict to aggregate.
    """
    if raw.get("verdict") is None:
        return None
    score = float(raw["overallScore"])
    findings = [str(f) for f in raw.get("findings", [])]
    return _fill_kind_defaults(
        {
            "dimensions": {"field_sync": Dimension(score=score, weight=1.0)},
            "overall_score": score,
            "verdict": raw["verdict"],
            "fix_recommendation": "; ".join(findings) or None,
        },
        kind,
    )


_VERDICT_SEVERITY = {"pass": 0, "warn": 1, "fail": 2, "blocked": 3}


def _from_video(raw: dict, kind: str) -> Verdict | None:
    """Normalize the video judge's per-scene verdict-video.json.

    Aggregation mirrors the skill's documented rule: overall = mean of per-scene
    overalls, verdict = the worst scene's verdict. Dimensions become the mean of
    each per-scene score key, weighted equally (per-scene weights aren't emitted).
    """
    scenes = raw.get("scenes") or []
    if not scenes:
        return None
    overall = sum(float(s["overall"]) for s in scenes) / len(scenes)
    worst = max((s["verdict"] for s in scenes), key=_VERDICT_SEVERITY.__getitem__)
    keys = sorted({k for s in scenes for k in s.get("scores", {})})
    dimensions = {
        k: Dimension(
            score=sum(float(s["scores"][k]) for s in scenes if k in s.get("scores", {}))
            / max(1, sum(1 for s in scenes if k in s.get("scores", {}))),
            weight=1.0 / len(keys) if keys else 1.0,
        )
        for k in keys
    }
    findings = [
        f"[{f.get('route', '?')}] {f.get('text', '')}"
        for s in scenes
        for f in s.get("findings", [])
    ]
    return _fill_kind_defaults(
        {
            "dimensions": dimensions,
            "overall_score": round(overall, 3),
            "verdict": worst,
            "fix_recommendation": "; ".join(findings) or None,
        },
        kind,
    )
