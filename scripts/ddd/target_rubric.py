"""Converge on a caller-supplied target rubric, not on a fixed weakest-link bar.

Why this module exists
----------------------
Until now a run converged when every gating judge's ``overall_score`` reached 4
(``demo``) or every product dimension's weakest cell reached 3 (``product``).
Both are a MINIMUM over ~60 noisy integer cells, and neither says what the run is
FOR. From Hal's audit of the last two days of runs (canopy#790):

* Neither run converged. ACE Spark (``demo``): five runs, scores stuck at 2-3,
  ``-005`` had every judge at 3.0 and was published unconverged by instruction.
  connect-labs supply-sophie-sheets (``product``, floor 3): score_history
  ``[2,2,2,3,3,3,3,3,3,3,2]``, ``stopped_not_converged`` — the last full pass
  flipped 3 -> 2 with no relevant change (#492: the min of ~60 cells whose
  per-cell draw is +/-1 on identical frames).
* Generic dimensions kept raising findings unrelated to the run's purpose: arc
  ``visual_variety`` never cleared in 11 supply iterations; email findings cost
  about six rounds (#783).
* Jonathan: "the arbitrary 4 goal might be too high and maybe it's better to
  have a rubric we send in that we are trying to hit."

The target rubric
-----------------
A run (or a narrative) carries the outcomes it must demonstrate and which generic
dimensions block. Precedence: ``run_state.target_rubric`` (``set`` below) >
the spec's ``target_rubric:`` > ``.canopy/ddd/config.yaml`` ``target_rubric:``
> the default for the objective::

    target_rubric:
      pass_score: 3                 # the bar a criterion's cells are read against
      draws: 3                      # a criterion passes on the majority of its last N full passes
      block_severities: [high]      # an out-of-rubric finding blocks only at these severities
      blocking_dimensions: [task_completion, trust, clarity]
      outcomes:
        - id: state-at-a-glance
          claim: Sophie sees the state of every tender at a glance
          pass_when: the tender list shows quoted / silent / missing per supplier without opening a tender
          scenes: [1, 2]            # optional, 1-based; default every scene
          dimensions: [task_completion, clarity]   # optional; default blocking_dimensions
          min_score: 3              # optional; default pass_score

Criteria = one per outcome + one per blocking dimension.

* A criterion is read over its CELLS (scene x dimension, from the gating
  judges' per-scene rows). It passes this pass when the MEDIAN cell reaches its
  bar and none of its cells is a confirmed break (a capping cell whose confirmed
  score is <= 2 — ``ddd-concept-eval`` Step 4a re-judges those k=3). The median,
  not the minimum: one cell drawing a point low no longer fails the run.
* An outcome the concept judge scored directly (``target_outcomes:`` in
  ``verdict-concept.yaml`` — k=3 draws, majority) uses that result instead.
* NOISE across passes: a criterion is passing when it passed on the MAJORITY of
  its last ``draws`` full judged passes (a tie goes to the current pass). One
  pass flipping 3 -> 2 with nothing changed no longer un-converges a run.

Findings: a finding on a FAILING criterion (its dimension, or an outcome's scene
and dimension) at ``high``/``medium`` blocks; everything else — a finding on a
passing criterion, or on a dimension the rubric does not name — is advisory
(``route: DEFER``, ``deferred_by: target_rubric``) unless its severity is in
``block_severities`` (default ``high``).

Converged = every criterion passing AND no blocking finding AND no gating
verdict ``blocked`` / never-live.

Defaults: ``product`` (a build) — blocking dimensions are the product dimensions
the judges scored, bar 3. ``demo`` (a polish) — every gating dimension, bar 4,
but read by median + majority, not by minimum.

CLI::

    python -m scripts.ddd.target_rubric show <run_id> [--spec PATH]   # the resolved rubric
    python -m scripts.ddd.target_rubric set <run_id> <rubric.yaml>    # pin a rubric to a run
    python -m scripts.ddd.target_rubric clear <run_id>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import median
from typing import Any, Iterable

import yaml

DEFAULT_DRAWS = 3
DEFAULT_BLOCK_SEVERITIES = ("high",)
CRITERION_SEVERITIES = ("high", "medium")
BREAK = 2.0  # a confirmed cell at or below this is broken, not unpolished
DEFERRED_BY = "target_rubric"


@dataclass(frozen=True)
class Outcome:
    id: str
    claim: str = ""
    pass_when: str = ""
    scenes: tuple[int, ...] = ()
    dimensions: tuple[str, ...] = ()
    min_score: float | None = None


@dataclass(frozen=True)
class Rubric:
    source: str = "default"
    pass_score: float = 3.0
    draws: int = DEFAULT_DRAWS
    block_severities: tuple[str, ...] = DEFAULT_BLOCK_SEVERITIES
    blocking_dimensions: tuple[str, ...] = ()
    outcomes: tuple[Outcome, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Parsing + resolution
# ---------------------------------------------------------------------------


def _num(v: Any, default: float | None) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _strs(v: Any) -> tuple[str, ...]:
    if isinstance(v, str):
        v = [v]
    return tuple(str(x).strip().lower() for x in v or [] if str(x).strip())


def _ints(v: Any) -> tuple[int, ...]:
    out = []
    for x in v or []:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return tuple(out)


def parse(raw: Any, *, source: str, defaults: Rubric) -> Rubric | None:
    """A rubric from a mapping; missing keys take ``defaults``. ``None`` if not a mapping."""
    if not isinstance(raw, dict):
        return None
    outcomes = []
    for i, o in enumerate(raw.get("outcomes") or []):
        if not isinstance(o, dict):
            continue
        outcomes.append(
            Outcome(
                id=str(o.get("id") or f"outcome-{i + 1}"),
                claim=str(o.get("claim") or ""),
                pass_when=str(o.get("pass_when") or ""),
                scenes=_ints(o.get("scenes")),
                dimensions=_strs(o.get("dimensions")),
                min_score=_num(o.get("min_score"), None),
            )
        )
    draws = int(_num(raw.get("draws"), defaults.draws) or defaults.draws)
    return Rubric(
        source=source,
        pass_score=_num(raw.get("pass_score"), defaults.pass_score) or defaults.pass_score,
        draws=max(draws, 1),
        block_severities=_strs(raw.get("block_severities")) or defaults.block_severities,
        blocking_dimensions=_strs(raw.get("blocking_dimensions")) or defaults.blocking_dimensions,
        outcomes=tuple(outcomes),
    )


def default_for(objective: str | None, verdicts: dict[str, Any] | None = None) -> Rubric:
    """The rubric a run gets when nobody supplied one."""
    from scripts.ddd.floor import ADVISORY_DIMENSIONS
    from scripts.ddd.objective import PRODUCT_DIMENSIONS

    scored = sorted(
        {
            str(d).lower()
            for v in (verdicts or {}).values()
            if getattr(v, "gate", None) == "gating"
            for d in (getattr(v, "dimensions", None) or {})
            if str(d).lower() not in ADVISORY_DIMENSIONS
        }
    )
    if objective == "product":
        dims = tuple(d for d in scored if d in PRODUCT_DIMENSIONS) or tuple(sorted(PRODUCT_DIMENSIONS))
        return Rubric(source="default:product", pass_score=3.0, blocking_dimensions=dims)
    return Rubric(source="default:demo", pass_score=4.0, blocking_dimensions=tuple(scored))


def resolve(
    *,
    objective: str | None,
    verdicts: dict[str, Any] | None = None,
    run_rubric: Any = None,
    spec_rubric: Any = None,
    config_rubric: Any = None,
) -> Rubric:
    base = default_for(objective, verdicts)
    for raw, source in ((run_rubric, "run"), (spec_rubric, "spec"), (config_rubric, "config")):
        r = parse(raw, source=source, defaults=base)
        if r is not None:
            return r
    return base


def spec_rubric(spec_path: str | Path | None) -> Any:
    if not spec_path:
        return None
    try:
        raw = yaml.safe_load(Path(spec_path).read_text()) or {}
    except Exception:
        return None
    return raw.get("target_rubric") if isinstance(raw, dict) else None


def config_rubric(ddd_dir: str | Path | None) -> Any:
    try:
        if ddd_dir is None:
            from scripts.ddd.runstate import _resolve_ddd_dir

            ddd_dir = _resolve_ddd_dir()
        p = Path(ddd_dir) / "config.yaml"
        raw = yaml.safe_load(p.read_text()) if p.exists() else None
    except Exception:
        return None
    return raw.get("target_rubric") if isinstance(raw, dict) else None


# ---------------------------------------------------------------------------
# Cells
# ---------------------------------------------------------------------------


def cells(run_dir: str | Path | None, verdicts: dict[str, Any]) -> dict[str, dict[int, float]]:
    """``{dimension: {scene: score}}`` over the gating per-scene judges.

    A dimension two judges both score keeps the lower of the two per scene (the
    same conservative read the weakest-link rule took, applied per cell only).
    A gating verdict with no per-scene rows contributes its dimension score as
    one whole-run cell (scene 0) — the arc judge scores the sequence.
    """
    from scripts.ddd.floor import ADVISORY_DIMENSIONS, JUDGE_OF, VERDICT_FILE, scene_scores

    out: dict[str, dict[int, float]] = {}
    run = Path(run_dir) if run_dir else None
    for key, v in verdicts.items():
        if getattr(v, "gate", None) != "gating" or key not in JUDGE_OF:
            continue
        raw: dict = {}
        if run is not None:
            p = run / VERDICT_FILE[JUDGE_OF[key]]
            try:
                raw = (yaml.safe_load(p.read_text()) if p.exists() else None) or {}
            except Exception:
                raw = {}
        per = scene_scores(raw) if isinstance(raw, dict) else {}
        raw_dims = (raw.get("dimensions") if isinstance(raw, dict) else None) or {}
        entries = getattr(v, "dimensions", None) or {}
        for name, e in entries.items():
            if str(name).lower() in ADVISORY_DIMENSIONS:
                continue
            d = str(name).lower()
            whole = e.get("score") if isinstance(e, dict) else getattr(e, "score", None)
            rd = raw_dims.get(name)
            just = rd.get("justification") if isinstance(rd, dict) else None
            rows = {s: r[name] for s, r in per.items() if name in r} or _per_scene_list(just)
            if not rows:
                w = _num(whole, None)
                rows = {0: w} if w is not None else {}
            slot = out.setdefault(d, {})
            for s, val in rows.items():
                slot[s] = min(slot.get(s, val), val)
    return out


_PER_SCENE_LIST = re.compile(r"per-scene\s*\[([\d.,\s]+)\]", re.I)


def _per_scene_list(justification: Any) -> dict[int, float]:
    """``per-scene [3.0, 4.0, ...]`` in a concept dimension's justification, scene 1..n."""
    m = _PER_SCENE_LIST.search(str(justification or ""))
    if not m:
        return {}
    out = {}
    for i, x in enumerate(p for p in m.group(1).split(",") if p.strip()):
        v = _num(x.strip(), None)
        if v is not None:
            out[i + 1] = v
    return out


def confirmed_breaks(distribution: dict | None) -> set[tuple[int, str]]:
    """(scene, dimension) cells the concept judge CONFIRMED broken (median of k <= 2)."""
    from scripts.ddd.floor import _scene_key

    out: set[tuple[int, str]] = set()
    for c in (distribution or {}).get("capping_cells") or []:
        if not isinstance(c, dict) or "confirmed" not in c:
            continue
        val = _num(c.get("confirmed"), None)
        s = _scene_key(c.get("scene"))
        if val is not None and s is not None and val <= BREAK:
            out.add((s, str(c.get("dimension") or "").lower()))
    return out


def judged_outcomes(run_dir: str | Path | None) -> dict[str, bool]:
    """``target_outcomes:`` the concept judge wrote — ``{id: pass}`` (majority of its draws)."""
    if not run_dir:
        return {}
    p = Path(run_dir) / "verdict-concept.yaml"
    try:
        raw = yaml.safe_load(p.read_text()) if p.exists() else None
    except Exception:
        return {}
    out: dict[str, bool] = {}
    for o in (raw or {}).get("target_outcomes") or [] if isinstance(raw, dict) else []:
        if not isinstance(o, dict) or not o.get("id"):
            continue
        draws = o.get("draws")
        if isinstance(draws, list) and draws:
            votes = [bool(x) for x in draws]
            out[str(o["id"])] = sum(votes) * 2 > len(votes)
        elif "pass" in o:
            out[str(o["id"])] = bool(o["pass"])
    return out


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _criterion(cid: str, kind: str, bar: float, scenes: Iterable[int], dims: Iterable[str],
               grid: dict[str, dict[int, float]], breaks: set[tuple[int, str]]) -> dict:
    scenes = set(scenes)
    vals: list[float] = []
    broken: list[str] = []
    for d in dims:
        for s, v in (grid.get(d) or {}).items():
            if scenes and s not in scenes and s != 0:
                continue
            vals.append(v)
            if (s, d) in breaks:
                broken.append(f"scene {s} {d}")
    if not vals:
        return {"id": cid, "kind": kind, "bar": bar, "passed": False, "cells": 0,
                "why": "no judged cell — not demonstrated"}
    med = median(vals)
    passed = med >= bar and not broken
    why = f"median {med:g} of {len(vals)} cell(s) vs {bar:g}"
    if broken:
        why += f"; confirmed broken: {', '.join(broken)}"
    return {"id": cid, "kind": kind, "bar": bar, "passed": passed, "cells": len(vals),
            "median": med, "why": why}


def evaluate_pass(rubric: Rubric, *, run_dir: str | Path | None, verdicts: dict[str, Any],
                  distribution: dict | None) -> list[dict]:
    """This pass's criteria, each ``{id, kind, passed, why, ...}``."""
    grid = cells(run_dir, verdicts)
    breaks = confirmed_breaks(distribution)
    judged = judged_outcomes(run_dir)
    out = []
    for o in rubric.outcomes:
        bar = o.min_score if o.min_score is not None else rubric.pass_score
        if o.id in judged:
            out.append({"id": o.id, "kind": "outcome", "bar": bar, "passed": judged[o.id],
                        "why": "judged against its pass condition (majority of draws)"})
            continue
        out.append(_criterion(o.id, "outcome", bar, o.scenes, o.dimensions or rubric.blocking_dimensions,
                              grid, breaks))
    for d in rubric.blocking_dimensions:
        out.append(_criterion(f"dim:{d}", "dimension", rubric.pass_score, (), (d,), grid, breaks))
    if not out:
        # Nothing named and nothing scored per dimension: read each gating
        # verdict's overall score, so an empty rubric can never pass vacuously.
        for k, v in verdicts.items():
            if getattr(v, "gate", None) != "gating":
                continue
            score = _num(getattr(v, "overall_score", None), None)
            ok = score is not None and score >= rubric.pass_score
            out.append({"id": f"overall:{k}", "kind": "overall", "bar": rubric.pass_score, "passed": ok,
                        "why": f"overall {score:g} vs {rubric.pass_score:g}" if score is not None else "unscored"})
    return out


def record(state: Any, criteria: list[dict], *, judge_full: bool) -> None:
    """Append this FULL pass's criteria to ``state.target_history`` (an incremental pass decides nothing)."""
    if not judge_full:
        return
    row = {"iteration": getattr(state, "iteration", None),
           "criteria": {c["id"]: bool(c["passed"]) for c in criteria}}
    hist = [h for h in (state.target_history or []) if h.get("iteration") != row["iteration"]]
    state.target_history = hist + [row]


def standing(rubric: Rubric, criteria: list[dict], history: list[dict]) -> list[dict]:
    """Each criterion with its noise-handled status: the majority of its last ``draws`` full passes."""
    out = []
    for c in criteria:
        votes = [h["criteria"][c["id"]] for h in history if c["id"] in (h.get("criteria") or {})]
        window = votes[-rubric.draws:] or [bool(c["passed"])]
        yes = sum(window)
        if yes * 2 == len(window):
            ok = bool(window[-1])
        else:
            ok = yes * 2 > len(window)
        out.append({**c, "window": window, "passing": ok})
    return out


def _sev(f: dict) -> str:
    return str(f.get("severity") or "medium").strip().lower()


def _dim(f: dict) -> str:
    return str(f.get("dimension") or "").strip().lower()


def _scene(f: dict) -> int | None:
    from scripts.ddd.floor import _scene_key

    return _scene_key(f.get("scene"))


def partition(findings: list[dict], rubric: Rubric, status: list[dict]) -> list[dict]:
    """Blocking vs advisory by the rubric (see module doc). Returns NEW dicts."""
    failing_dims = {c["id"][4:] for c in status if c["kind"] == "dimension" and not c["passing"]}
    failing_outcomes = [o for o in rubric.outcomes
                        if any(c["id"] == o.id and not c["passing"] for c in status)]
    block = set(rubric.block_severities)
    out = []
    for raw in findings or []:
        f = dict(raw)
        if str(f.get("route") or "PRODUCT").upper() == "DEFER":
            out.append(f)
            continue
        d, s, sev = _dim(f), _scene(f), _sev(f)
        on_failing = d in failing_dims or any(
            (not o.scenes or s in o.scenes) and d in (o.dimensions or rubric.blocking_dimensions)
            for o in failing_outcomes
        )
        if (on_failing and sev in CRITERION_SEVERITIES) or sev in block:
            f["target_role"] = "blocking"
        else:
            f["deferred_route"] = f.get("route") or "PRODUCT"
            f["deferred_by"] = DEFERRED_BY
            f["route"] = "DEFER"
            f["target_role"] = "advisory"
            if f.get("objective_role") == "blocking":
                f["objective_role"] = "deferred"
        out.append(f)
    return out


def converged(status: list[dict], findings: list[dict], verdicts: dict[str, Any]) -> tuple[bool, str]:
    gating = {k: v for k, v in verdicts.items() if getattr(v, "gate", None) == "gating"}
    if not gating:
        return False, "no gating verdict — convergence must be demonstrated"
    for k, v in gating.items():
        if getattr(v, "verdict", None) == "blocked":
            return False, f"{k} verdict is blocked"
        if getattr(v, "live_state_verified", None) is False:
            return False, f"{k} verdict never touched live state"
    if not status:
        return False, "the target rubric has no criterion — convergence must be demonstrated"
    failing = [c for c in status if not c["passing"]]
    if failing:
        return False, "target criteria not met: " + "; ".join(f"{c['id']} ({c['why']})" for c in failing)
    blocking = [f for f in findings or [] if f.get("target_role") == "blocking"]
    if blocking:
        return False, f"every target criterion passes, but {len(blocking)} high-severity finding(s) are open"
    return True, f"every target criterion passes ({len(status)}: " + ", ".join(c["id"] for c in status) + ")"


def evaluate(
    rubric: Rubric,
    state: Any,
    findings: list[dict],
    *,
    run_dir: str | Path | None,
    verdicts: dict[str, Any],
    distribution: dict | None,
    judge_full: bool,
) -> dict:
    """One pass, end to end: criteria -> history -> status -> partition -> convergence.

    Mutates ``state.target_history`` (full passes only) and ``state.target``.
    """
    criteria = evaluate_pass(rubric, run_dir=run_dir, verdicts=verdicts, distribution=distribution)
    record(state, criteria, judge_full=judge_full)
    history = list(state.target_history or [])
    if not judge_full:
        # Read provisionally (an incremental pass decides nothing — it can only
        # ask for confirm_full), but read the current cells, not just the past.
        history.append({"iteration": getattr(state, "iteration", None),
                        "criteria": {c["id"]: bool(c["passed"]) for c in criteria}})
    status = standing(rubric, criteria, history)
    parted = partition(findings, rubric, status)
    ok, why = converged(status, parted, verdicts)
    state.target = {
        "source": rubric.source,
        "iteration": getattr(state, "iteration", None),
        "converged": ok,
        "why": why,
        "criteria": [{k: c.get(k) for k in ("id", "kind", "bar", "passed", "passing", "window", "why")}
                     for c in status],
    }
    return {"converged": ok, "why": why, "findings": parted, "status": status}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    from scripts.ddd.runstate import _resolve_ddd_dir, load, save

    ap = argparse.ArgumentParser(prog="python -m scripts.ddd.target_rubric")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sh = sub.add_parser("show")
    sh.add_argument("run_id")
    sh.add_argument("--spec")
    st = sub.add_parser("set")
    st.add_argument("run_id")
    st.add_argument("rubric", help="a YAML file: the rubric mapping, or {target_rubric: ...}")
    cl = sub.add_parser("clear")
    cl.add_argument("run_id")
    a = ap.parse_args(argv)

    ddd_dir = _resolve_ddd_dir()
    state = load(a.run_id, ddd_dir=ddd_dir)
    if a.cmd == "show":
        from scripts.ddd import loop_config

        objective = state.objective or loop_config.load(ddd_dir).loop.objective
        r = resolve(objective=objective, run_rubric=state.target_rubric, spec_rubric=spec_rubric(a.spec),
                    config_rubric=config_rubric(ddd_dir))
        print(json.dumps({**r.to_dict(), "standing": state.target}, indent=1))
        return 0
    if a.cmd == "set":
        raw = yaml.safe_load(Path(a.rubric).read_text()) or {}
        if isinstance(raw, dict) and isinstance(raw.get("target_rubric"), dict):
            raw = raw["target_rubric"]
        if parse(raw, source="run", defaults=default_for(state.objective)) is None:
            print("ERROR: the rubric must be a mapping", file=sys.stderr)
            return 1
        state.target_rubric = raw
        save(state, ddd_dir=ddd_dir)
        print(json.dumps({"run_id": a.run_id, "target_rubric": raw}))
        return 0
    state.target_rubric = None
    save(state, ddd_dir=ddd_dir)
    print(json.dumps({"run_id": a.run_id, "target_rubric": None}))
    return 0


__all__ = [
    "Outcome",
    "Rubric",
    "converged",
    "default_for",
    "evaluate",
    "evaluate_pass",
    "parse",
    "partition",
    "resolve",
    "standing",
]

if __name__ == "__main__":
    raise SystemExit(_main())
