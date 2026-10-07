#!/usr/bin/env python3
"""Render a connect-videos spec to MP4 locally, on bare metal.

This is canopy's home for the general Remotion video engine's local
renderer. It stages a self-contained ``spec.yaml`` + master clip into the
engine's ``programs/<slug>/runs/<run>/`` tree and runs the host npm render
chain (Node / Chromium / esbuild on the host arch — fast, 1-3 min).

It is the **local-spec** path only — no Drive, no Django container. The
caller (e.g. canopy's DDD ``connect-ddd-walkthrough`` emitter) has already
produced a ``spec.yaml`` and a master clip::

    python render_locally.py --local-spec /path/to/spec.yaml \
        --master /path/to/walkthrough.mp4            # --draft preview
    python render_locally.py --local-spec spec.yaml --master clip.mp4 --final

The slug + run come from the spec (``slug:`` / ``--run``); the master is
copied to the path the spec's ``manifest.master: file:…`` ref names.

The engine project rendered into defaults to this script's own directory
(the canopy ``video-engine``); override with ``--engine-root`` /
``$CONNECT_VIDEOS_ROOT`` to render against a different install.

The server-side / Drive-publish path (the Django ``apps.videos.service``
chain) lives in ace-web, not here — canopy owns only the general,
container-free local render.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# The engine lives in this script's own directory (canopy video-engine).
ENGINE_DIR = Path(__file__).resolve().parent

# scripts.ddd.deadair holds the pure freeze∩silence detector (Layer 2). It lives
# in the canopy repo root's `scripts/` package; add the repo root to sys.path so
# this engine script can import it whether invoked as a module or a file.
_REPO_ROOT = ENGINE_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
try:
    from scripts.ddd import deadair as _deadair  # type: ignore
except Exception:  # noqa: BLE001 — detector is advisory; engine runs without it
    _deadair = None
try:
    from scripts.ddd import recorded as _recorded  # type: ignore
except Exception:  # noqa: BLE001 — only needed for style: recorded specs
    _recorded = None


def engine_root() -> Path:
    """The engine project to render into.

    Defaults to this script's own directory (the canopy video-engine).
    ``$CONNECT_VIDEOS_ROOT`` overrides it so a caller can target a
    different install. (Env var name kept for back-compat with ace-web's
    renderer and existing skills.)
    """
    env = os.environ.get("CONNECT_VIDEOS_ROOT")
    return Path(env).expanduser().resolve() if env else ENGINE_DIR


def load_dotenv_into_env(extra: Path | None = None) -> None:
    """Merge a `.env` into os.environ so the npm render inherits secrets.

    The renderer's per-beat voiceover (ElevenLabs) reads
    ``ELEVENLABS_API_KEY`` from process env. We look in, in order:
    an explicit ``--env-file``, ``./.env`` (cwd), the engine dir's ``.env``,
    and canopy's injected secrets file (``~/.claude/canopy/.env`` — produced by
    ``op inject -i .env.tpl``, the canopy convention). Existing env values
    always win. Keys with embedded newlines are skipped.
    """
    canopy_env = Path.home() / ".claude" / "canopy" / ".env"
    candidates = [p for p in (extra, Path.cwd() / ".env", ENGINE_DIR / ".env", canopy_env) if p]
    for env_path in candidates:
        if not env_path.is_file():
            continue
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if key and key not in os.environ:
                os.environ[key] = value


def host_npm(cv: Path, slug: str, run_id: str, *, draft: bool = True,
             captions: bool = False) -> None:
    """Run the npm render on the host (bare-metal). Stdout streams through.

    ``hydrate`` (Drive asset pull) and the clip-explorer build are omitted:
    a local spec's assets are already local files. ``captions`` defaults
    OFF for connect-ddd-walkthrough renders (the dashboard self-labels and
    the VO narrates); opt in per render.
    """
    render = ["npm", "run", "render", "--", f"--program={slug}", f"--run={run_id}"]
    if draft:
        render.append("--draft")
    if not captions:
        render.append("--no-captions")
    print(f"\n==> {' '.join(render)}")
    subprocess.run(render, cwd=cv, check=True)


def _load_spec(spec_path: Path) -> dict:
    """Parse a program spec. Prefers PyYAML; falls back to a regex reader
    for the two fields we need (top-level ``slug`` + ``manifest.master``)
    so the script runs under a bare stdlib interpreter."""
    text = spec_path.read_text()
    try:
        import yaml  # type: ignore

        doc = yaml.safe_load(text)
        if isinstance(doc, dict):
            return doc
    except Exception:
        pass
    slug_m = re.search(r"^slug:\s*[\"']?([A-Za-z0-9._-]+)", text, re.M)
    master_m = re.search(r"^\s*master:\s*[\"']?(\S+?)[\"']?\s*$", text, re.M)
    return {
        "slug": slug_m.group(1) if slug_m else None,
        "manifest": {"master": master_m.group(1) if master_m else None},
    }


def _copy_into_place(src: Path, dest: Path) -> None:
    """Copy src → dest, no-op when they're already the same file."""
    if dest.exists() and src.resolve() == dest.resolve():
        return
    shutil.copyfile(src, dest)


def stage_local_spec(cv: Path, spec_path: Path, run_id: str, master_path: Path | None) -> str:
    """Stage a local spec + master clip into the engine (no Drive).

    Writes ``programs/<slug>/runs/<run>/spec.yaml`` and copies the master
    clip to the path the spec's ``manifest.master: file:…`` ref names.
    Returns the resolved slug.
    """
    doc = _load_spec(spec_path)
    slug = doc.get("slug")
    if not slug:
        raise SystemExit(f"Could not read `slug` from {spec_path}")

    dest_spec = cv / "programs" / slug / "runs" / run_id / "spec.yaml"
    dest_spec.parent.mkdir(parents=True, exist_ok=True)
    _copy_into_place(spec_path, dest_spec)
    print(f"==> staged spec → {dest_spec}")

    if master_path:
        master_ref = (doc.get("manifest") or {}).get("master") or ""
        if not master_ref.startswith("file:"):
            raise SystemExit(
                f"--master given but spec manifest.master is not a file: ref ({master_ref!r}).\n"
                "Local-spec mode copies the master to the file: path the spec names; "
                "emit the spec with a file: master ref (the DDD emitter does this)."
            )
        # A file: master ref is public-relative (ProgramBody wraps clip.asset
        # in staticFile(), which serves from <engine>/public/). resolveAssetRefs
        # only symlinks *cache* refs into public/ — a file: ref must already
        # live there — so stage the master under public/, not the engine root.
        rel = master_ref[len("file:"):]
        dest_master = cv / "public" / rel
        dest_master.parent.mkdir(parents=True, exist_ok=True)
        _copy_into_place(master_path, dest_master)
        print(f"==> staged master → {dest_master}")

    return slug


def timing_report(cv: Path, slug: str, run_id: str) -> None:
    """Print clip-footage vs actual-duration (the held-frame VO overrun).

    Expected = sum of every beat's ``seconds`` in the spec. Actual = the
    rendered mp4's real duration. A large positive delta means the
    narration outran the footage and the renderer froze the last frame —
    trim narration (~2.2 words/sec for eleven_turbo_v2). Best-effort.
    """
    try:
        import json

        run_dir = cv / "programs" / slug / "runs" / run_id
        spec_text = (run_dir / "spec.yaml").read_text()
        beat_secs = [float(s) for s in re.findall(r"^\s*seconds:\s*([0-9.]+)\s*$", spec_text, re.M)]
        expected = sum(beat_secs)
        out = run_dir / "output.mp4"
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(out)],
            capture_output=True, text=True, check=True,
        )
        actual = float(json.loads(probe.stdout)["format"]["duration"])
        overrun = actual - expected
        print("\n==> Timing report")
        print(f"    clip footage (spec beats): {expected:6.1f}s")
        print(f"    rendered duration:         {actual:6.1f}s")
        flag = "  ⚠ VO overruns clips — trim narration to play continuously" if overrun > 3 else ""
        print(f"    held-frame overrun:        {overrun:+6.1f}s{flag}")
    except Exception as e:  # noqa: BLE001 — report is advisory
        print(f"\n==> Timing report skipped ({e})")


def dead_air_report(cv: Path, slug: str, run_id: str) -> dict | None:
    """Run the Layer-2 freeze∩silence detector on the rendered mp4 and print it.

    Layer 1 (the render-time beat cap) should prevent dead air; this is the
    independent QA check that flags any frozen+silent span that slipped through.
    Returns the report dict (or None if the detector isn't importable).
    """
    if _deadair is None:
        print("\n==> Dead-air detector skipped (scripts.ddd.deadair not importable)")
        return None
    out = cv / "programs" / slug / "runs" / run_id / "output.mp4"
    ignore = _card_ignore_ranges(cv, slug, run_id, str(out))
    report = _deadair.detect_dead_air(str(out), ignore_ranges=ignore)
    print(_deadair.format_report(report))
    return report


def _card_ignore_ranges(cv: Path, slug: str, run_id: str, mp4: str) -> list[tuple[float, float]]:
    """Designed-card spans (final-video seconds) to exclude from the dead-air
    report: the leading intro_title card and the trailing outro card (+ its
    music fade-out). Those are intentional held frames, NOT frozen footage, so
    Layer 1 leaves them and the QA report should too.

    The intro card is always [0, intro_secs]; the outro card is always
    [total - outro_secs, total] — both invariant to body-beat caps. Reads the
    first/last beat ``seconds`` from the spec; best-effort (empty on any miss).
    """
    try:
        import json

        run_dir = cv / "programs" / slug / "runs" / run_id
        spec = _load_spec(run_dir / "spec.yaml")
        beats = spec.get("beats") or []
        if not beats:
            return []
        ranges: list[tuple[float, float]] = []
        first = beats[0]
        if str(first.get("kind", "")).startswith("intro"):
            ranges.append((0.0, float(first.get("seconds", 0)) + 0.5))
        last = beats[-1]
        if str(last.get("kind", "")).startswith("outro"):
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "json", mp4],
                capture_output=True, text=True, check=True,
            )
            total = float(json.loads(probe.stdout)["format"]["duration"])
            ranges.append((total - float(last.get("seconds", 0)) - 0.5, total + 0.5))
        return ranges
    except Exception:  # noqa: BLE001 — advisory
        return []


def recorded_gate(cv: Path, slug: str, run_id: str) -> dict | None:
    """For a ``style: recorded`` cut, gate the rendered length (warn > 30 s,
    fail > 40 s) and write ``verdict-recorded.json`` beside ``output.mp4``.

    Returns the gate report, or None for any other style. A recorded cut whose
    length cannot be measured FAILS — an unmeasured cut is not a passing one.
    """
    run_dir = cv / "programs" / slug / "runs" / run_id
    try:
        spec = _load_spec(run_dir / "spec.yaml")
    except Exception:  # noqa: BLE001
        return None
    if (spec.get("style") or "explainer") != "recorded":
        return None
    if _recorded is None:
        print("\n==> Recorded-cut gate skipped (scripts.ddd.recorded not importable)")
        return None
    import json

    out = run_dir / "output.mp4"
    words = sum(
        _recorded.word_count(t) for t in ((spec.get("narration") or {}).get("by_beat") or {}).values()
    )
    report = _recorded.gate_cut_durations({slug: _recorded.probe_duration(out)}, {slug: words})
    (run_dir / "verdict-recorded.json").write_text(json.dumps(report, indent=2))
    print("\n" + _recorded.format_gate(report))
    return report


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--local-spec", required=True,
                   help="Path to a connect-videos spec.yaml to render. "
                        "Slug + run come from the spec.")
    p.add_argument("--master", default=None,
                   help="Master clip copied to the spec's manifest.master file: path.")
    p.add_argument("--run", default=None, help="Run id (default run-001).")
    p.add_argument("--final", action="store_true",
                   help="Render at final quality (skip the default --draft preview).")
    p.add_argument("--captions", action="store_true",
                   help="Burn captions in (default OFF for connect-ddd-walkthrough — "
                        "the dashboard self-labels + the VO narrates).")
    p.add_argument("--engine-root", default=None,
                   help="Override the engine project to render into "
                        "(also honored via $CONNECT_VIDEOS_ROOT).")
    p.add_argument("--env-file", default=None,
                   help="Extra .env to load ELEVENLABS_API_KEY etc. from "
                        "(also checks ./.env and the engine dir's .env).")
    p.add_argument("--trim-dead-air-fallback", action="store_true",
                   help="If the Layer-2 detector still finds dead air >3s after "
                        "the render, exit non-zero (so a caller can re-render). "
                        "Layer 1 should prevent this — default is report-only.")
    args = p.parse_args()

    if args.engine_root:
        os.environ["CONNECT_VIDEOS_ROOT"] = args.engine_root
    cv = engine_root()
    if not (cv / "package.json").is_file():
        print(f"ERROR: no video engine at {cv}.\n"
              "       Point at an install via --engine-root / $CONNECT_VIDEOS_ROOT.",
              file=sys.stderr)
        return 2

    load_dotenv_into_env(Path(args.env_file) if args.env_file else None)
    if not os.environ.get("ELEVENLABS_API_KEY"):
        print(
            "\nERROR: ELEVENLABS_API_KEY not found in env, --env-file, ./.env, "
            "or the engine dir's .env.\n"
            "       The renderer refuses to render a silent video by default.\n"
            "       Export the key (e.g. from 1Password) and re-run.",
            file=sys.stderr,
        )
        return 2

    run_id = args.run or "run-001"
    print(f"==> local-spec render: spec={args.local_spec} run={run_id} "
          f"quality={'final' if args.final else 'draft'} root={cv}")
    master = Path(args.master) if args.master else None
    slug = stage_local_spec(cv, Path(args.local_spec), run_id, master)

    print("\n==> Run npm render on host (bare-metal)")
    host_npm(cv, slug, run_id, draft=not args.final, captions=args.captions)

    timing_report(cv, slug, run_id)
    report = dead_air_report(cv, slug, run_id)
    gate = recorded_gate(cv, slug, run_id)

    out = cv / "programs" / slug / "runs" / run_id / "output.mp4"
    print(f"\n==> Done. Output: {out}")

    if args.trim_dead_air_fallback and report and report.get("has_dead_air"):
        # Opt-in strict mode: surface the >3s dead air as a non-zero exit so an
        # orchestrator can re-render. Layer 1 should make this unreachable.
        print(
            f"\nERROR: --trim-dead-air-fallback: {len(report['over_threshold'])} "
            f"dead-air span(s) >{report['threshold_seconds']}s remain after render.",
            file=sys.stderr,
        )
        return 3
    if gate and gate.get("verdict") == "fail":
        # style: recorded — a cut over the 40 s ceiling is not shippable. The
        # mp4 is left in place for inspection; the exit code tells a caller
        # (ddd-recorded-walkthrough) to trim the cut's narration and re-render.
        print(
            f"\nERROR: recorded cut is over the {gate['ceiling_seconds']:.0f}s ceiling "
            "— trim its narration and re-render.",
            file=sys.stderr,
        )
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
