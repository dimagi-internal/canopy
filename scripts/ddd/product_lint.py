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

Pre-judge frame lint (DDD-2) — always on, no config
---------------------------------------------------
ACE noora (``newborn-care-classes-programme-cascade-2026-10-15-001``) held its
score at 2.0 for six iterations on findings a function finds in milliseconds;
the judges rediscovered them, in prose, every pass:

* **internal_id_leak** — "synthetic, ace noora-health/20261015-1129" in the
  report H1 and the opportunity picker (``\\d{8}-\\d{4}``, ``ace <opp>/``). Owner
  ``demo_data``: the seeded names carry the run id.
* **jargon** — the legend "n<1: too few records" (:data:`DEFAULT_JARGON`).
* **text_occlusion** — the floating "Ask an agent" pill over footer text and a
  chart; a line cut off under the fixed app header. A floater that sits at the
  same place on several scenes is page chrome: owner ``shared_chrome`` (it is
  fixed in the base template, not in the scene's page), severity ``low``.
* **horizontal_overflow** — the partner table clipped at the right edge
  ("Attent…"): content wider than the viewport, or than a clipping container.
* **text_truncation** — "AT PAYMENT FLO…": an ellipsis or line clamp. innerText
  still holds the whole string, so no text lens can see it.

The geometry checks read ``scene_<N>_visual.json`` (box ``x``/``y``, floating
layers, scroll widths — captured by the recorder since DDD-2); an older capture
skips them and says so in ``checks``. Every finding carries its scene index, so
it flows through the same pipeline as a judge's (``assemble`` reads
``lint_findings.json``) — and a scene with a lint blocker is still judged on
every other dimension: nothing here removes a scene from the judge scope.
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


# ---------------------------------------------------------------------------
# Pre-judge frame lint (DDD-2): leaks, jargon, occlusion, overflow, truncation
# ---------------------------------------------------------------------------

#: An internal run id on screen: ``20261015-1129`` (ACE run ids) and
#: ``ace noora-health/`` (an ACE opp path). Both came off the seeded names.
INTERNAL_ID_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b\d{8}-\d{4}\b", "an internal run id (YYYYMMDD-HHMM)"),
    (r"\bace\s+[a-z0-9][\w.-]*/", "an internal ACE opportunity path ('ace <opp>/')"),
)

#: Shorthand a non-technical reader cannot parse. Pattern -> what to say instead.
DEFAULT_JARGON: dict[str, str] = {
    r"\bn\s*[<≤]\s*\d+": "statistical shorthand ('n<N') — say it in words, e.g. 'too few records to score'",
}

#: A floater wider or taller than this share of the viewport is a BAR (header,
#: sidebar): content legitimately scrolls under it, so only a line it cuts in
#: half (partly visible) is a defect. Anything smaller is a floating widget.
BAR_SHARE = 0.6
#: Share of a text box a floating widget must cover to count as occluding it.
OCCLUDE_MIN = 0.2
#: A bar "cuts" a line when it covers between these shares of the line's box.
BAR_CUT = (0.15, 0.85)
#: Recorder overlays (synthetic cursor, transition fade) are not the product.
_RECORDER_OVERLAY = re.compile(r"__synth|#__canopy", re.I)


def _lines_matching(text: str, pattern: str) -> list[str]:
    rx = re.compile(pattern, re.I)
    return [ln.strip() for ln in text.split("\n") if rx.search(ln)]


def text_leak_findings(texts: dict[int, str], *, jargon: dict[str, str] | None = None) -> list[dict]:
    """Internal ids and jargon in the captured page text, one finding per scene and kind."""
    out: list[dict] = []
    for scene, text in sorted(texts.items()):
        hits: list[str] = []
        what: list[str] = []
        for pat, desc in INTERNAL_ID_PATTERNS:
            lines = _lines_matching(text, pat)
            if lines:
                hits.extend(lines)
                what.append(desc)
        if hits:
            sample = "; ".join(f"'{ln[:100]}'" for ln in list(dict.fromkeys(hits))[:3])
            out.append(_finding(
                scene, "internal_id_leak",
                f"Scene {scene}'s screen shows {' and '.join(what)}: {sample}.",
                "Remove the internal run identifier from every user-facing name (seeded programme / "
                "opportunity / workflow names, report titles); keep it in metadata a reader never sees.",
                severity="high", owner="demo_data", fix_scope="product",
                measured=len(set(hits)),
            ))
        for pat, hint in (jargon if jargon is not None else DEFAULT_JARGON).items():
            lines = _lines_matching(text, pat)
            if lines:
                sample = "; ".join(f"'{ln[:100]}'" for ln in list(dict.fromkeys(lines))[:3])
                out.append(_finding(
                    scene, "jargon",
                    f"Scene {scene}'s screen uses {hint.split(' — ')[0]}: {sample}.",
                    f"Replace the shorthand with plain words — {hint}.",
                    owner="product_copy", fix_scope="product", measured=len(lines),
                ))
    return out


def _box(e: dict) -> tuple[float, float, float, float] | None:
    try:
        x, y, w, h = (float(e.get(k)) for k in ("x", "y", "w", "h"))
    except (TypeError, ValueError):
        return None
    return (x, y, w, h) if w > 0 and h > 0 else None


def _overlap(a: tuple, b: tuple) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    return ix * iy


def _rendered(e: dict) -> bool:
    from scripts.ddd.visual_geometry import _is_rendered

    return _is_rendered(e)


def _has_geometry(cap: dict) -> bool:
    return isinstance(cap.get("viewport"), list) and any("x" in e for e in cap.get("elements") or [])


def _floaters(cap: dict) -> list[dict]:
    return [
        e for e in cap.get("elements") or []
        if e.get("position") in ("fixed", "sticky") and _rendered(e) and _box(e)
        and not _RECORDER_OVERLAY.search(str(e.get("path") or ""))
    ]


def _floater_key(e: dict) -> tuple:
    b = _box(e) or (0, 0, 0, 0)
    return (str(e.get("path") or ""), tuple(round(v / 8) for v in b))


def geometry_findings(captures: dict[int, dict], narration: dict[int, str] | None = None) -> tuple[list[dict], str]:
    """Occlusion, horizontal overflow and truncation from the visual captures.

    Returns ``(findings, status)``; ``status`` says when the captures predate
    the geometry fields (the checks are then skipped, never guessed).
    """
    from scripts.ddd.visual_geometry import _narration_hits

    with_geo = {s: c for s, c in captures.items() if _has_geometry(c)}
    if not with_geo:
        return [], ("skipped: no scene_<N>_visual.json with box geometry (captured before DDD-2)"
                    if captures else "skipped: no visual captures")
    narration = narration or {}
    # A floater at the same place on several scenes lives in the page chrome.
    seen: dict[tuple, set[int]] = {}
    for s, cap in with_geo.items():
        for f in _floaters(cap):
            seen.setdefault(_floater_key(f), set()).add(s)
    out: list[dict] = []
    for scene, cap in sorted(with_geo.items()):
        vw, vh = (float(v) for v in (cap.get("viewport") or [0, 0])[:2])
        nar = narration.get(scene, "")
        texts = [
            e for e in cap.get("elements") or []
            if (e.get("text") or "").strip() and _rendered(e) and _box(e)
            and not e.get("in_floating") and e.get("position") not in ("fixed", "sticky")
        ]
        # --- occlusion ---------------------------------------------------------
        for f in _floaters(cap):
            fb = _box(f)
            bar = fb[2] >= BAR_SHARE * vw or fb[3] >= BAR_SHARE * vh
            covered: list[str] = []
            for t in texts:
                tb = _box(t)
                share = _overlap(fb, tb) / (tb[2] * tb[3])
                if (BAR_CUT[0] <= share <= BAR_CUT[1]) if bar else share >= OCCLUDE_MIN:
                    covered.append(str(t.get("text"))[:60])
            if not covered:
                continue
            chrome = len(seen.get(_floater_key(f), ())) > 1
            label = f.get("label") or f.get("path") or "a fixed element"
            narrated = any(_narration_hits(c, nar) for c in covered)
            sample = "; ".join(f"'{c}'" for c in list(dict.fromkeys(covered))[:3])
            out.append(_finding(
                scene, "text_occlusion",
                (f"Scene {scene}: the fixed {'bar' if bar else 'floating element'} '{label[:50]}' "
                 + ("cuts these lines in half" if bar else "covers this text")
                 + f" in the frame: {sample}"
                 + (" — it sits at the same place on every scene: page chrome." if chrome else ".")),
                ("A floating widget from the shared page chrome: move or dock it so it never overlays "
                 "content (or reserve space for it), in the base template — or park it out of scope "
                 "if the chrome is not this run's to change."
                 if chrome else
                 "Reposition the floating element, or reserve space for it, so no content renders "
                 "beneath it; for a bar, scroll the scene so no line is half under it."),
                severity="high" if narrated and not chrome else ("low" if chrome else "medium"),
                owner="shared_chrome" if chrome else "product_layout", fix_scope="product",
                selector=f.get("path"), measured=len(covered),
            ))
        # --- horizontal overflow -----------------------------------------------
        offenders: list[str] = []
        try:
            page_over = float(cap.get("doc_scroll_w") or 0) - float(cap.get("doc_client_w") or 0)
        except (TypeError, ValueError):
            page_over = 0.0
        for t in texts:
            x, _, w, _ = _box(t)
            if vw and x < vw and x + w > vw + 2:
                offenders.append(f"'{str(t.get('text'))[:40]}' runs off the right edge")
        for e in cap.get("elements") or []:
            if not _rendered(e) or e.get("text_overflow") == "ellipsis" or "scroll_w" not in e:
                continue
            if str(e.get("overflow_x") or "visible") == "visible":
                continue
            try:
                hidden = float(e["scroll_w"]) - float(e.get("client_w") or 0)
            except (TypeError, ValueError):
                continue
            if hidden > 8:
                offenders.append(f"{e.get('path')} hides {hidden:.0f}px ({e.get('overflow_x')})")
        if page_over > 2 or offenders:
            detail = (f"Scene {scene}: content is wider than the frame"
                      + (f" (the page is {page_over:.0f}px wider than the viewport)" if page_over > 2 else "")
                      + (": " + "; ".join(list(dict.fromkeys(offenders))[:3]) if offenders else "") + ".")
            out.append(_finding(
                scene, "horizontal_overflow", detail,
                "Make the content fit the viewport: let the table / panel wrap, drop or abbreviate "
                "low-value columns, or give it a deliberate horizontal scroll affordance — never "
                "content cut at the frame edge.",
                owner="product_layout", fix_scope="product", measured=len(offenders) or round(page_over),
            ))
        # --- ellipsis / line-clamp truncation ------------------------------------
        cut = [
            str(e.get("text") or e.get("path"))[:50]
            for e in cap.get("elements") or []
            if _rendered(e) and (e.get("line_clamped") or (
                e.get("text_overflow") == "ellipsis"
                and float(e.get("scroll_w") or 0) > float(e.get("client_w") or 0) + 1))
        ]
        if cut:
            narrated = any(_narration_hits(c, nar) for c in cut)
            sample = "; ".join(f"'{c}'" for c in list(dict.fromkeys(cut))[:4])
            out.append(_finding(
                scene, "text_truncation",
                f"Scene {scene}: {len(cut)} label(s) are cut off with an ellipsis / line clamp: {sample}.",
                "Give the label room (wider column, smaller tile title, wrap) or shorten the label "
                "itself; a truncated label hides the word the reader needs.",
                severity="high" if narrated else "medium",
                owner="product_layout", fix_scope="product", measured=len(cut),
            ))
    return out, "ran"


def _visual_captures(snap: Path) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for p in sorted(snap.glob("scene_*_visual.json")):
        try:
            cap = json.loads(p.read_text())
        except Exception:
            continue
        m = re.match(r"scene_(\d+)_visual\.json", p.name)
        idx = int(cap.get("scene_index") or (m.group(1) if m else 0))
        if idx:
            out[idx] = cap
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

    jargon = getattr(cfg, "jargon", None)
    findings.extend(text_leak_findings(texts, jargon=jargon if isinstance(jargon, dict) and jargon else None))
    geo, geo_status = geometry_findings(_visual_captures(snap), _narration_texts(Path(spec) if spec else None))
    findings.extend(geo)

    out = {
        "run_dir": str(run),
        "scenes": sorted(texts),
        "findings": findings,
        "checks": {"text_leaks": "ran" if texts else "skipped: no page text", "geometry": geo_status},
    }
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
