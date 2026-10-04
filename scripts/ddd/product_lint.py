"""Product lint — deterministic checks for the defects the judges never blocked on.

    python -m scripts.ddd.product_lint <run_dir> [--spec <spec.yaml>] [--json]

Writes ``<run_dir>/lint_findings.json`` (read by ``scripts.ddd.assemble`` as
``source: product_lint``) and prints one line per finding. Always exits 0: these
are findings to fix, not a broken take.

Why each check exists — connect-labs ``supply-sophie-unanswered-round``
(2026-10-02..04), ~24 judged iterations, none of which flagged any of these as a
blocker; one human look at the slides flagged all of them:

* **prose density / long lines** — fixers answered every "this is unclear" with
  another explanatory sentence in the template. The pre-redesign screens carried
  ~5.2k words and 36 lines of 20+ words; the redesigned ones ~3.3k and 10.
* **new long lines** — the same accretion measured against the run's OWN first
  render (``lint-baseline.json``), so a screen that was already wordy is not
  re-flagged forever, but a loop that keeps adding prose is caught on the batch
  that adds it.
* **terminology** — "round" had been renamed "tender" in the product (#2037) and
  DDD batches put it back. ``product.lint.glossary`` maps banned -> preferred.
* **design system fonts** — the screens drifted off the labs font. Checked from
  the recorder's ``scene_<N>_visual.json`` (``font_family`` per text element,
  captured since 0.2.559) against ``product.lint.fonts``.

Thresholds live in ``.canopy/ddd/config.yaml`` ``product: lint:`` (see
:mod:`scripts.ddd.loop_config`). A check whose config is empty is skipped.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

BASELINE_FILE = "lint-baseline.json"
OUT_FILE = "lint_findings.json"
_GENERIC_FONTS = {"", "serif", "sans-serif", "monospace", "system-ui", "ui-monospace", "ui-sans-serif", "inherit"}


def _snapshots_dir(run_dir: Path) -> Path:
    snap = run_dir / "snapshots"
    return snap if snap.is_dir() else run_dir


def _scene_texts(snap: Path) -> dict[int, str]:
    out: dict[int, str] = {}
    for p in sorted(snap.glob("scene_*_page_text.json")):
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        m = re.match(r"scene_(\d+)_page_text\.json", p.name)
        idx = int(data.get("scene_index") or (m.group(1) if m else 0))
        out[idx] = str(data.get("page_text") or "")
    return out


def _long_lines(text: str, min_words: int) -> list[str]:
    """Prose lines of ``min_words``+ words. A tab-separated line is a table row —
    structure, not prose — and never counts."""
    return [
        ln.strip()
        for ln in text.split("\n")
        if "\t" not in ln and len(ln.split()) >= min_words
    ]


def _finding(scene: int | None, dimension: str, detail: str, fix: str, **extra: Any) -> dict:
    f = {
        "scene": scene,
        "dimension": dimension,
        "route": "PRODUCT",
        "fix_kind": "mechanical",
        "severity": "medium",
        "source": "product_lint",
        "detail": detail,
        "fix_recommendation": fix,
    }
    f.update(extra)
    return f


def _narration_texts(spec_path: Path | None) -> dict[int, str]:
    if not spec_path or not spec_path.exists():
        return {}
    try:
        import yaml

        spec = yaml.safe_load(spec_path.read_text()) or {}
    except Exception:
        return {}
    out: dict[int, str] = {}
    for i, sc in enumerate(spec.get("scenes") or [], start=1):
        if not isinstance(sc, dict):
            continue
        parts = [str(sc.get(k) or "") for k in ("title", "narration", "concept_claim")]
        nar = sc.get("narrative")
        if isinstance(nar, list):
            parts.extend(str(x.get("text") if isinstance(x, dict) else x) for x in nar)
        elif nar:
            parts.append(str(nar))
        out[i] = " ".join(parts)
    return out


def lint(run_dir: str | Path, *, spec: str | Path | None = None, cfg: Any = None) -> dict[str, Any]:
    """Run every configured check over the run's latest snapshots."""
    run = Path(run_dir)
    if cfg is None:
        from scripts.ddd import loop_config

        cfg = loop_config.load().product.lint
    snap = _snapshots_dir(run)
    texts = _scene_texts(snap)
    findings: list[dict] = []

    baseline_path = run / BASELINE_FILE
    if baseline_path.exists():
        baseline = json.loads(baseline_path.read_text())
    else:
        baseline = {
            "long_lines": {str(s): _long_lines(t, cfg.long_line_words) for s, t in texts.items()},
            "words": {str(s): len(t.split()) for s, t in texts.items()},
        }
        if texts:
            baseline_path.write_text(json.dumps(baseline, indent=1) + "\n")

    for scene, text in sorted(texts.items()):
        words = len(text.split())
        if cfg.max_words_per_screen and words > cfg.max_words_per_screen:
            findings.append(
                _finding(
                    scene,
                    "prose_density",
                    f"Scene {scene}'s screen carries {words} words of UI text (budget "
                    f"{cfg.max_words_per_screen}).",
                    f"Cut scene {scene}'s screen to <= {cfg.max_words_per_screen} words: replace "
                    "explanatory sentences with labels, status chips, counts or table structure. "
                    "Do not move the prose into tooltips or a collapsed panel.",
                    measured=words,
                )
            )
        longs = _long_lines(text, cfg.long_line_words)
        if len(longs) > cfg.max_long_lines_per_screen:
            sample = "; ".join(f"'{ln[:90]}'" for ln in longs[:3])
            findings.append(
                _finding(
                    scene,
                    "prose_density",
                    f"Scene {scene} has {len(longs)} lines of {cfg.long_line_words}+ words "
                    f"(budget {cfg.max_long_lines_per_screen}): {sample}",
                    "Turn each explanatory sentence into structure (a labelled field, a chip, a "
                    "row in a table) or delete it. A UI states facts; it does not narrate them.",
                    measured=len(longs),
                )
            )
        before = set((baseline.get("long_lines") or {}).get(str(scene)) or [])
        added = [ln for ln in longs if ln not in before]
        if str(scene) in (baseline.get("long_lines") or {}) and len(added) > cfg.max_new_long_lines:
            sample = "; ".join(f"'{ln[:90]}'" for ln in added[:3])
            findings.append(
                _finding(
                    scene,
                    "prose_density",
                    f"This run ADDED {len(added)} explanatory line(s) to scene {scene} since its "
                    f"first render: {sample}",
                    "Remove the added sentences and express what they explain as structure. The "
                    "loop must not answer a clarity finding by adding copy.",
                    measured=len(added),
                    severity="high",
                )
            )

    if cfg.glossary:
        narration = _narration_texts(Path(spec) if spec else None)
        for scene in sorted(set(texts) | set(narration)):
            for where, text in (("screen", texts.get(scene, "")), ("narration", narration.get(scene, ""))):
                hits = Counter()
                for banned in cfg.glossary:
                    n = len(re.findall(rf"\b{re.escape(banned)}s?\b", text, re.IGNORECASE))
                    if n:
                        hits[banned] = n
                if hits:
                    terms = ", ".join(f"'{b}' x{n} (use '{cfg.glossary[b]}')" for b, n in hits.items())
                    findings.append(
                        _finding(
                            scene,
                            "terminology",
                            f"Scene {scene} {where} uses banned term(s): {terms}.",
                            (
                                f"Replace in every user-facing string and seeded demo name — {terms}."
                                if where == "screen"
                                else f"Edit the scene's narration — {terms}."
                            ),
                            fix_scope="product" if where == "screen" else "narrative",
                        )
                    )

    if cfg.fonts:
        allowed = {f.lower() for f in cfg.fonts}
        for p in sorted(snap.glob("scene_*_visual.json")):
            try:
                cap = json.loads(p.read_text())
            except Exception:
                continue
            fams = Counter(
                str(e.get("font_family") or "").strip()
                for e in cap.get("elements") or []
                if isinstance(e, dict) and e.get("text") and "font_family" in e
            )
            off = {f: n for f, n in fams.items() if f.lower() not in allowed and f.lower() not in _GENERIC_FONTS}
            if off:
                scene = int(cap.get("scene_index") or 0) or None
                desc = ", ".join(f"{f} ({n} element(s))" for f, n in sorted(off.items(), key=lambda kv: -kv[1]))
                findings.append(
                    _finding(
                        scene,
                        "design_system",
                        f"Scene {scene} renders text in font(s) outside the design system: {desc}. "
                        f"Allowed: {', '.join(cfg.fonts)}.",
                        "Use the product's design-system typography (its existing classes / tokens) "
                        "instead of a new font stack; extend the design system if something is missing.",
                    )
                )

    out = {"run_dir": str(run), "scenes": sorted(texts), "findings": findings}
    (run / OUT_FILE).write_text(json.dumps(out, indent=1) + "\n")
    return out


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.ddd.product_lint")
    ap.add_argument("run_dir")
    ap.add_argument("--spec", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    out = lint(args.run_dir, spec=args.spec)
    if args.json:
        print(json.dumps(out, indent=1))
        return 0
    fs = out["findings"]
    print(f"product_lint: {len(fs)} finding(s) over scene(s) {out['scenes']} -> {OUT_FILE}")
    for f in fs:
        print(f"  [s{f['scene']}|{f['dimension']}|{f['severity']}] {f['detail'][:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
