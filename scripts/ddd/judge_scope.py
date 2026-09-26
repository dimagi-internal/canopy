"""Judge scope — re-judge only what changed; reuse cells whose inputs are identical.

The problem, measured
---------------------
Every DDD iteration re-rendered AND re-judged every scene. One judge round on a
13-scene narrative costs ~14 minutes and ~600k tokens; a fix cycle costs ~4
minutes and no judge tokens (connect-labs ``.canopy/ddd/learnings.md``,
2026-07-30). On a freshly-built (v1) product the loop spends most of its budget
re-judging scenes no fix touched, and a re-draw of byte-identical inputs only
samples judge noise (+/-1 per cell) — it cannot tell the loop anything new.

The protocol
------------
Rendering stays FULL every iteration (it is cheap, and it is what makes the
fingerprints below comparable). Judging is scoped:

1. ``plan``   — fingerprint every scene's judge INPUTS (after/before frames,
   captured page text minus the render stamp, the scene's spec entry, its action
   trace, plus run-wide context such as the why-brief and rubric). Compare with
   the ledger of the last judged iteration. A scene whose fingerprint is
   identical is REUSED; anything else is RE-JUDGED. A full pass
   (``state.next_judge_full``, or no ledger yet) re-judges everything. The arc
   judge re-runs on a full pass or when any scene is re-judged. Writes
   ``judge-scope.json``.
2. ``carry``  — before the concept judge runs, archive stale pass files of the
   scenes being re-judged and restore the reused scenes' SEALED pass files
   (payload + seal, byte-for-byte — the seal stays valid because nothing about
   the pass changed). The concept eval then scores from ``passes/concept/`` as
   always; reused scenes' confirmed cells flow in unchanged.
3. ``merge-user`` — the user-artifact judge dispatches only the re-judged
   scenes; this merges their ``per_scene`` rows over the ledger's so the
   verdict still covers every scene.
4. ``record`` — after assembly, snapshot this iteration's fingerprints, pass
   files and verdicts into ``judge-cache/`` as the next iteration's ledger.

Convergence is never declared on a reused cell: ``compute_auto_iterate`` answers
``confirm_full`` for an incremental pass that would converge.

    python -m scripts.ddd.judge_scope plan   <run_dir> <spec> [--full] [--context F ...]
    python -m scripts.ddd.judge_scope carry  <run_dir>
    python -m scripts.ddd.judge_scope merge-user <run_dir> <partial-verdict-user.yaml>
    python -m scripts.ddd.judge_scope record <run_dir> <spec> --iteration N [--context F ...]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

SCOPE_FILE = "judge-scope.json"
CACHE_DIR = "judge-cache"
INDEX_FILE = "index.json"
_SCENE_PASS_RE = re.compile(r"^scene_(\d+)(?:_r\d+)?\.json(?:\.seal\.json)?$")
_ARC_FILES = ("verdict-arc.yaml", "arc_findings.json")


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":")).encode()


def _find(run_dir: Path, name: str) -> Path | None:
    for base in (run_dir / "snapshots", run_dir):
        p = base / name
        if p.exists():
            return p
    return None


def _page_text_payload(path: Path | None) -> Any:
    """Captured page text WITHOUT the per-render stamp (render_id changes every take)."""
    if path is None:
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return path.read_bytes().hex()
    if isinstance(data, dict):
        data = {k: v for k, v in data.items() if k != "render_id"}
    return data


def _trace_by_scene(run_dir: Path) -> dict[int, list]:
    report = run_dir / "run-report.json"
    if not report.exists():
        return {}
    try:
        from scripts.walkthrough._lib.results import action_trace_by_scene

        traces = action_trace_by_scene(json.loads(report.read_text()))
    except Exception:
        return {}
    # Only what a judge reasons over and is stable take-to-take — notes can
    # carry timings.
    return {
        int(k): [
            [a.get("kind"), a.get("target"), bool(a.get("ok")), bool(a.get("must_succeed"))]
            for a in v
        ]
        for k, v in traces.items()
    }


def context_salt(paths: list[str | Path]) -> str:
    """Run-wide judge context (why-brief, rubric). A change re-judges every scene."""
    h = hashlib.sha256()
    for p in sorted(str(x) for x in paths):
        path = Path(p)
        h.update(path.name.encode())
        h.update(path.read_bytes() if path.exists() else b"<missing>")
    return h.hexdigest()


def spec_scenes(spec_path: str | Path) -> dict[int, dict]:
    """``{1-based scene index: scene dict}`` from a (possibly two-file) spec."""
    from scripts.ddd.spec_io import load_spec_raw

    raw = load_spec_raw(spec_path)
    scenes = raw.get("scenes") or []
    return {i: (s if isinstance(s, dict) else {}) for i, s in enumerate(scenes, start=1)}


def fingerprints(
    run_dir: str | Path, scenes: dict[int, dict], *, salt: str = ""
) -> dict[str, str]:
    """``{scene index (str): sha256}`` over every input a scene's judges read."""
    run = Path(run_dir)
    traces = _trace_by_scene(run)
    out: dict[str, str] = {}
    for idx, scene in sorted(scenes.items()):
        h = hashlib.sha256()
        h.update(salt.encode())
        for name in (f"scene_{idx}.png", f"scene_{idx}_before.png"):
            p = _find(run, name)
            h.update(name.encode())
            h.update(p.read_bytes() if p else b"<absent>")
        h.update(_canonical(_page_text_payload(_find(run, f"scene_{idx}_page_text.json"))))
        h.update(_canonical(scene))
        h.update(_canonical(traces.get(idx, [])))
        out[str(idx)] = h.hexdigest()
    return out


# ---------------------------------------------------------------------------
# Ledger (judge-cache/)
# ---------------------------------------------------------------------------


def load_ledger(run_dir: str | Path) -> dict | None:
    p = Path(run_dir) / CACHE_DIR / INDEX_FILE
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def decide_scope(
    current: dict[str, str], ledger: dict | None, *, force_full: bool
) -> dict[str, Any]:
    """Pure decision: which scenes to re-judge, which to reuse, whether arc re-runs."""
    scenes = sorted(current, key=int)
    prior = (ledger or {}).get("fingerprints") or {}
    if force_full or not ledger:
        reason = "full pass requested" if force_full else "no ledger from a prior judged iteration"
        return {
            "full": True,
            "rejudge": [int(s) for s in scenes],
            "reuse": [],
            "arc": True,
            "reason": reason,
        }
    changed = [s for s in scenes if prior.get(s) != current[s]]
    same = [s for s in scenes if s not in changed]
    return {
        "full": False,
        "rejudge": [int(s) for s in changed],
        "reuse": [int(s) for s in same],
        "arc": bool(changed),
        "reason": (
            f"{len(changed)} scene(s) changed since iteration {ledger.get('iteration')}; "
            f"{len(same)} identical scene(s) reuse their cells"
            + ("" if changed else " — NOTHING changed: the last batch had no visible effect")
        ),
    }


def plan(
    run_dir: str | Path,
    spec_path: str | Path,
    *,
    force_full: bool = False,
    context: list[str | Path] | None = None,
) -> dict[str, Any]:
    run = Path(run_dir)
    ctx = list(context or [])
    wb = run / "why_brief.yaml"
    if wb.exists() and str(wb) not in {str(c) for c in ctx}:
        ctx.append(wb)
    current = fingerprints(run, spec_scenes(spec_path), salt=context_salt(ctx))
    ledger = load_ledger(run)
    # A ledger recorded against a different scene set cannot be reused safely.
    if ledger and set((ledger.get("fingerprints") or {})) != set(current):
        scope = decide_scope(current, None, force_full=True)
        scope["reason"] = "scene set changed since the ledger — full pass"
    else:
        scope = decide_scope(current, ledger, force_full=force_full)
    scope.update(
        {
            "planned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ledger_iteration": (ledger or {}).get("iteration"),
            "fingerprints": current,
        }
    )
    (run / SCOPE_FILE).write_text(json.dumps(scope, indent=1) + "\n")
    return scope


def load_scope(run_dir: str | Path) -> dict | None:
    p = Path(run_dir) / SCOPE_FILE
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _archive_dir(run: Path, iteration: Any, kind: str) -> Path:
    d = run / f"iter{iteration if iteration is not None else 'prev'}-archive" / "passes" / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


def _move(src: Path, dest_dir: Path) -> None:
    dest = dest_dir / src.name
    if dest.exists():
        dest = dest_dir / f"{src.name}.{datetime.now().strftime('%H%M%S%f')}"
    shutil.move(str(src), str(dest))


def carry(run_dir: str | Path) -> dict[str, Any]:
    """Prepare ``passes/`` for a scoped judge pass (see module docstring)."""
    run = Path(run_dir)
    scope = load_scope(run)
    if scope is None:
        raise ValueError(f"no {SCOPE_FILE} in {run} — run `judge_scope plan` first")
    rejudge = {int(s) for s in scope.get("rejudge") or []}
    reuse = {int(s) for s in scope.get("reuse") or []}
    prev = scope.get("ledger_iteration")
    concept = run / "passes" / "concept"
    concept.mkdir(parents=True, exist_ok=True)
    cache = run / CACHE_DIR

    archived: list[str] = []
    for f in sorted(concept.iterdir()):
        m = _SCENE_PASS_RE.match(f.name)
        if not m:
            continue
        scene = int(m.group(1))
        if scope.get("full") or scene in rejudge:
            _move(f, _archive_dir(run, prev, "concept"))
            archived.append(f.name)

    restored: list[str] = []
    for f in sorted((cache / "concept").glob("*")) if (cache / "concept").is_dir() else []:
        m = _SCENE_PASS_RE.match(f.name)
        if not m or int(m.group(1)) not in reuse:
            continue
        dest = concept / f.name
        if dest.exists() and dest.read_bytes() == f.read_bytes():
            continue
        if dest.exists():
            _move(dest, _archive_dir(run, prev, "concept"))
        shutil.copy2(f, dest)
        restored.append(f.name)
    missing = sorted(
        s for s in reuse if not (concept / f"scene_{s}.json").exists()
    )

    arc = run / "passes" / "arc"
    if scope.get("arc"):
        if arc.is_dir():
            for f in sorted(arc.iterdir()):
                _move(f, _archive_dir(run, prev, "arc"))
    else:
        # Arc is reused: make sure its verdict AND sealed pass are present.
        if (cache / "arc").is_dir():
            arc.mkdir(parents=True, exist_ok=True)
            for f in sorted((cache / "arc").iterdir()):
                if not (arc / f.name).exists():
                    shutil.copy2(f, arc / f.name)
        for name in _ARC_FILES:
            if not (run / name).exists() and (cache / name).exists():
                shutil.copy2(cache / name, run / name)

    return {
        "archived": archived,
        "restored": restored,
        "reuse_missing_cache": missing,
        "rejudge": sorted(rejudge),
        "reuse": sorted(reuse),
        "arc": bool(scope.get("arc")),
        "expect_concept_passes": len(
            [f for f in concept.iterdir() if f.is_file() and not f.name.endswith(".seal.json")]
        ),
    }


def reused_user_rows(run_dir: str | Path) -> dict[str, Any]:
    """The ledger's user-artifact ``per_scene`` rows for the scenes being reused."""
    run = Path(run_dir)
    scope = load_scope(run) or {}
    reuse = {str(s) for s in scope.get("reuse") or []}
    prior = _load_yaml(run / CACHE_DIR / "verdict-user.yaml") or {}
    rows = {str(k): v for k, v in (prior.get("per_scene") or {}).items()}
    return {k: v for k, v in rows.items() if k in reuse}


def _load_yaml(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def merge_user(prior: dict, partial: dict, reuse: list[int]) -> dict:
    """Merge a partial user-artifact verdict (re-judged scenes) over the prior one.

    ``per_scene`` = the partial's rows plus the prior's rows for REUSED scenes.
    Each dimension's score is the minimum over the merged rows (``overall_rule:
    lowest``); its justification / fix_recommendation / fix_kind come from
    whichever verdict owns the minimum — the prior's when the floor sits on a
    reused scene, the partial's otherwise. ``overall_score`` is the minimum
    dimension. A reused scene cannot be dropped: if the prior has no row for it,
    this raises rather than writing a verdict that silently covers fewer scenes.
    """
    prior_rows = {str(k): v for k, v in (prior.get("per_scene") or {}).items()}
    rows = {str(k): v for k, v in (partial.get("per_scene") or {}).items()}
    for s in reuse:
        key = str(s)
        if key in rows:
            continue
        if key not in prior_rows:
            raise ValueError(f"reused scene {key} has no per_scene row in the prior user verdict")
        rows[key] = prior_rows[key]
    merged = dict(partial)
    dims_prior = prior.get("dimensions") or {}
    dims_new = partial.get("dimensions") or {}
    merged_dims: dict[str, Any] = {}
    reuse_keys = {str(s) for s in reuse}
    for dim in sorted(set(dims_prior) | set(dims_new)):
        scores = [
            (float(r[dim]), k) for k, r in rows.items() if isinstance(r, dict) and dim in r
        ]
        base = dims_new.get(dim) or dims_prior.get(dim) or {}
        if not scores:
            merged_dims[dim] = base
            continue
        low, owner = min(scores)
        source = dims_prior if owner in reuse_keys and dim in dims_prior else dims_new
        entry = dict(source.get(dim) or base)
        entry["score"] = low
        merged_dims[dim] = entry
    merged["dimensions"] = merged_dims
    ordered = sorted(rows, key=lambda k: (0, int(k), "") if k.isdigit() else (1, 0, k))
    merged["per_scene"] = {(int(k) if k.isdigit() else k): rows[k] for k in ordered}
    if merged_dims:
        overall = min(float(d.get("score")) for d in merged_dims.values() if d.get("score") is not None)
        merged["overall_score"] = overall
        merged["verdict"] = "pass" if overall >= 4 else ("warn" if overall >= 3 else "fail")
    merged["reused_scenes"] = sorted(int(s) for s in reuse)
    return merged


def record(
    run_dir: str | Path,
    spec_path: str | Path,
    *,
    iteration: int,
    context: list[str | Path] | None = None,
) -> dict[str, Any]:
    """Snapshot this judged iteration as the next iteration's ledger."""
    run = Path(run_dir)
    ctx = list(context or [])
    wb = run / "why_brief.yaml"
    if wb.exists() and str(wb) not in {str(c) for c in ctx}:
        ctx.append(wb)
    fps = fingerprints(run, spec_scenes(spec_path), salt=context_salt(ctx))
    scope = load_scope(run) or {}
    cache = run / CACHE_DIR
    tmp = run / f"{CACHE_DIR}.tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    (tmp / "concept").mkdir(parents=True)
    (tmp / "arc").mkdir(parents=True)
    concept = run / "passes" / "concept"
    n_concept = 0
    if concept.is_dir():
        for f in concept.iterdir():
            if _SCENE_PASS_RE.match(f.name):
                shutil.copy2(f, tmp / "concept" / f.name)
                n_concept += 1
    arc = run / "passes" / "arc"
    if arc.is_dir():
        for f in arc.iterdir():
            if f.is_file():
                shutil.copy2(f, tmp / "arc" / f.name)
    for name in (*_ARC_FILES, "verdict-user.yaml", "verdict-concept.yaml"):
        if (run / name).exists():
            shutil.copy2(run / name, tmp / name)
    index = {
        "iteration": iteration,
        "full": bool(scope.get("full", True)),
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fingerprints": fps,
    }
    (tmp / INDEX_FILE).write_text(json.dumps(index, indent=1) + "\n")
    if cache.exists():
        shutil.rmtree(cache)
    tmp.rename(cache)
    return {"iteration": iteration, "scenes": len(fps), "concept_pass_files": n_concept}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.ddd.judge_scope")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("run_dir")
    p.add_argument("spec")
    p.add_argument("--full", action="store_true", help="force a full judge pass")
    p.add_argument("--context", action="append", default=[])
    c = sub.add_parser("carry")
    c.add_argument("run_dir")
    u = sub.add_parser("reused-user")
    u.add_argument("run_dir")
    m = sub.add_parser("merge-user")
    m.add_argument("run_dir")
    m.add_argument("partial")
    r = sub.add_parser("record")
    r.add_argument("run_dir")
    r.add_argument("spec")
    r.add_argument("--iteration", type=int, required=True)
    r.add_argument("--context", action="append", default=[])
    args = ap.parse_args(argv)
    try:
        if args.cmd == "plan":
            out = plan(args.run_dir, args.spec, force_full=args.full, context=args.context)
            out = {k: v for k, v in out.items() if k != "fingerprints"}
        elif args.cmd == "carry":
            out = carry(args.run_dir)
        elif args.cmd == "reused-user":
            out = reused_user_rows(args.run_dir)
        elif args.cmd == "merge-user":
            run = Path(args.run_dir)
            scope = load_scope(run) or {}
            prior = _load_yaml(run / CACHE_DIR / "verdict-user.yaml") or {}
            partial = _load_yaml(Path(args.partial))
            if partial is None:
                raise ValueError(f"{args.partial}: not a YAML mapping")
            merged = merge_user(prior, partial, [int(s) for s in scope.get("reuse") or []])
            (run / "verdict-user.yaml").write_text(yaml.safe_dump(merged, sort_keys=False))
            out = {
                "wrote": str(run / "verdict-user.yaml"),
                "overall_score": merged.get("overall_score"),
                "reused_scenes": merged.get("reused_scenes"),
            }
        else:
            out = record(args.run_dir, args.spec, iteration=args.iteration, context=args.context)
    except (ValueError, OSError) as exc:
        print(f"judge_scope {args.cmd}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
