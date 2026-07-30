#!/usr/bin/env python3
"""
record_video.py — Silent video recording for canopy:walkthrough specs.

Reads the spec YAML and replays each scene's URL + actions through a Playwright
Chromium context with ``record_video`` enabled, then converts the resulting
webm to mp4 via ffmpeg. Produces one silent mp4 alongside the HTML deck.

The recording loop lives in :class:`walkthrough._lib.orchestrator.Recorder` —
this script is a thin CLI over it. To customise behaviour (skip nav when the
URL hasn't changed, alternate viewport, custom hooks), subclass ``Recorder``
in a one-off script and call ``.run(page, scenes)``; no need to fork this CLI.

Usage:
    python3 record_video.py \\
        --spec docs/walkthroughs/<name>.yaml \\
        --output screenshots/walkthroughs/<name>.mp4 \\
        [--cookies /tmp/walkthrough-cookies.json | --storage-state /tmp/state.json] \\
        [--input /tmp/walkthrough-run-data.json] \\
        [--scene 2,4 | --scene 2-4 | --scene name-match] \\
        [--skip-same-url] \\
        [--report run-report.json] \\
        [--snapshots screenshots/walkthroughs/<name>/] \\
        [--snapshot-empty-scenes] \\
        [--prewarm | --no-prewarm] \\
        [--skip-setup]

``--spec`` is the source of truth for scenes. ``--input`` is accepted for
backward compatibility: a walkthrough-run-data.json from canopy:walkthrough
narrows the spec's scenes to the ones that were actually captured (so a
``--scene 3`` partial run records exactly that scene). When ``--input`` is
absent the full spec is recorded.

Specs with a ``setup:`` block (the data-setup contract) get their synthetic
generator command run BEFORE recording — honoring ``rerun: per_render | once``
— and the ``${var}`` placeholders in scene URLs / action targets resolved from
the command's outputs JSON. ``--skip-setup`` skips the command (but still
loads the outputs) for fast re-renders when the data is known-fresh; demos
that mutate state during recording must not use it.

Specs with ``prewarm: true`` (or CLI ``--prewarm``; ``--no-prewarm`` wins the
other way) get a pre-warm pass after setup + auth and BEFORE the recorded
context exists: a separate non-recorded context visits each unique resolved
scene URL once, so cold caches are paid off camera instead of as frozen
frames. Best-effort — see :func:`run_prewarm`.
"""

from __future__ import annotations

import argparse
import datetime
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("ERROR: pyyaml not installed. Run: pip install pyyaml")

from scripts.walkthrough._lib.capture_mode import snapshot_full_page
from scripts.walkthrough.identities import mint_identities
from scripts.ddd.spec_io import load_spec_raw

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit(
        "ERROR: playwright not installed.\n"
        "  pip install 'playwright>=1.40' && python -m playwright install chromium\n"
        "  (or install canopy's optional browser deps: pip install -e '<canopy>[browser]')"
    )

# Recorder lib lives next to this script in _lib/. Add this script's dir to the
# path so `python3 record_video.py` (invoked by path) can import it.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Placeholder substitution is shared with scripts/ddd/spec_qa.py (single source
# of truth for what `${var}` means) — it lives at the repo root, so put that on
# the path too for by-path invocations (`python3 record_video.py`).
#
# ORDER MATTERS: the repo root must be on sys.path BEFORE _lib.orchestrator is
# imported. The orchestrator imports scripts.narrative.substitution defensively
# and silently degrades to no-op stubs (has_unresolved=False, resolve_string=
# identity) when the import fails — importing it first froze those stubs in
# by-path invocations, so the unresolved-${var} nav guard never fired and every
# late-bound capture var stayed a literal `${...}` in scene URLs (filmed as a
# 404 page).
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from _lib.config import RecorderConfig  # noqa: E402
from _lib.orchestrator import Recorder, SkipSameUrlRecorder  # noqa: E402
from _lib.recorder import CURSOR_OVERLAY_JS  # noqa: E402
from manifest import build_manifest  # noqa: E402
from scripts.narrative.substitution import (  # noqa: E402
    UnresolvedPlaceholderError,
    has_unresolved,
    ordered_placeholder_violations,
    scene_capture_vars,
    scenes_placeholders,
    substitute_scenes,
)


# --------------------------------------------------------------------------- #
# helpers


def check_ffmpeg() -> str:
    p = shutil.which("ffmpeg")
    if not p:
        sys.exit("ERROR: ffmpeg not found on PATH. Install: brew install ffmpeg")
    return p


def webm_to_mp4(ffmpeg: str, webm: Path, out: Path) -> None:
    """Re-encode the Playwright-recorded webm to a faststart mp4 via ffmpeg."""
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"Converting {webm.name} → {out.name}")
    result = subprocess.run(
        [
            ffmpeg, "-y", "-i", str(webm),
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-preset", "fast", "-crf", "23",
            str(out),
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        sys.exit(f"ERROR: ffmpeg failed (exit {result.returncode}):\n{result.stderr[-2000:]}")


def _is_empty_scene(scene: dict) -> bool:
    """A scene with no ``actions`` is a narrative-only beat.

    Used by ``--skip-empty-scenes`` to drop those from the recording loop —
    the deck still shows them as title-card slides built from spec.scenes
    independently, so the narrative survives. Same gate as
    ``Recorder.take_snapshot``'s ``has_actions`` check (kept here as a
    separate helper so the filter in ``main`` and any future caller share
    one definition of "empty")."""
    return not bool(scene.get("actions") or [])


def filter_empty_scenes(scenes: list[dict]) -> list[dict]:
    """Drop scenes whose ``actions`` list is empty, preserving order and
    each surviving scene's 1-based ORIGINAL spec ``scene_index``.

    Pure function, no I/O — exercised by unit tests without spinning a
    browser. ``record_video.main`` calls this when ``--skip-empty-scenes``
    is set."""
    return [s for s in scenes if not _is_empty_scene(s)]


# --------------------------------------------------------------------------- #
# pre-warm pass (spec.prewarm / --prewarm — heat cold caches OFF camera)


def resolve_prewarm(cli_value: bool | None, spec_value: object) -> bool:
    """Decide whether the pre-warm pass runs. CLI wins; spec is the default.

    ``cli_value`` is ``True`` for ``--prewarm``, ``False`` for ``--no-prewarm``,
    ``None`` when neither flag was passed (→ fall back to the spec's
    ``prewarm:`` value; absent → off). Pure function — unit tested directly.
    """
    if cli_value is not None:
        return bool(cli_value)
    return bool(spec_value)


def collect_prewarm_urls(scenes: list[dict]) -> list[str]:
    """The unique resolved scene URLs to pre-warm, in spec order.

    Takes the scene records ``build_scenes_from_spec`` produced — i.e. AFTER
    up-front ``${var}`` substitution and URL absolutization, so what gets warmed
    is exactly what gets filmed. Continuation scenes (``url is None`` — they stay
    on the previous scene's page) contribute nothing; duplicate URLs are
    visited once (first occurrence wins the ordering).

    A URL that STILL contains a ``${var}`` is capture-bound (its id is minted on
    camera by a ``capture`` action LATER, so it isn't known yet) — pre-warm
    can't resolve it and must skip it rather than visit a literal placeholder
    URL. It still films fine; it just stays cold like any pre-prewarm page.
    """
    seen: set[str] = set()
    out: list[str] = []
    for scene in scenes:
        url = scene.get("url")
        if not url or url in seen:
            continue
        if has_unresolved(url):
            print(f"  · prewarm: skipping capture-bound URL (unresolved ${{var}}): {url}")
            continue
        seen.add(url)
        out.append(url)
    return out


def run_prewarm(
    context,
    urls: list[str],
    *,
    settle_ms: int = 4000,
    page_timeout_ms: int = 15000,
    auth_url: str | None = None,
) -> dict:
    """Visit each URL once in a NON-recorded context so caches are hot on film.

    The legacy hand-built recorder had ``defer_record=True``: visit everything
    once off camera (~20s), THEN start filming. This is canopy's equivalent.
    Cold-cache waits — a 15s first-hit page render, a remote-image cold fetch —
    are real seconds, but they only need to be PAID once; without prewarm they
    get paid on camera as frozen frames.

    Per page: ``goto(wait_until="domcontentloaded", timeout=page_timeout_ms)``
    then a bounded settle — up to ``settle_ms`` (clipped to the page's
    remaining time budget) waiting for network idle, so image/chart fetches
    actually complete and warm their caches; exits early once idle.

    Best-effort by contract: every per-page failure is logged (one line) and
    recorded in the provenance, never raised — a page that can't pre-warm
    simply stays cold and films like it did before prewarm existed.

    Returns the provenance dict that rides on the RunReport:
    ``{"pages": N, "duration_seconds": S, "failures": [{"url", "error"}]}``.
    """
    started = time.monotonic()
    failures: list[dict] = []
    page = context.new_page()
    if auth_url:
        # URL-based auth (magic-link login) lives in THIS context's cookie jar
        # only — replay it so authenticated pages actually render (a login
        # redirect would warm the login page, not the scene).
        try:
            page.goto(auth_url, wait_until="domcontentloaded", timeout=page_timeout_ms)
        except Exception as e:  # noqa: BLE001 — best-effort, like everything here
            print(f"  ! prewarm auth nav failed: {e}", file=sys.stderr)
    for url in urls:
        page_started = time.monotonic()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=page_timeout_ms)
            elapsed_ms = (time.monotonic() - page_started) * 1000
            settle_budget = min(settle_ms, max(0, page_timeout_ms - int(elapsed_ms)))
            if settle_budget > 0:
                try:
                    page.wait_for_load_state("networkidle", timeout=settle_budget)
                except Exception:  # noqa: BLE001 — long-poll apps never go idle; bounded by budget
                    pass
            print(f"  · prewarm {url} ({time.monotonic() - page_started:.1f}s)")
        except Exception as e:  # noqa: BLE001
            failures.append({"url": url, "error": str(e)})
            print(f"  ! prewarm {url} failed ({e}) — continuing")
    return {
        "pages": len(urls),
        "duration_seconds": round(time.monotonic() - started, 2),
        "failures": failures,
    }


# --------------------------------------------------------------------------- #
# data setup (spec.setup — the synthetic generator contract)


class SetupError(RuntimeError):
    """The spec's setup command failed (nonzero exit, timeout, bad outputs).

    Raised by :func:`run_setup` so unit tests can assert on it; ``main``
    converts it to a loud ``sys.exit``. A failed setup means the world is NOT
    in a recordable state — rendering anyway films the wrong UI.
    """


def resolve_setup_cwd(spec_path: Path) -> Path:
    """The directory ``setup.command`` runs in.

    The git toplevel containing the spec file (``git rev-parse
    --show-toplevel`` from the spec's directory), falling back to the spec's
    own directory when it isn't in a git repo. Setup commands are written
    repo-root-relative — matching how humans run them — regardless of where
    the recorder itself is invoked from.
    """
    spec_dir = spec_path.resolve().parent
    try:
        result = subprocess.run(
            ["git", "-C", str(spec_dir), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    return spec_dir


def load_setup_outputs(outputs_path: Path) -> dict:
    """Parse the setup command's outputs JSON into a substitution map.

    The contract is a flat JSON object with string/number values — the
    variables the synthetic generator minted (run IDs, entity IDs, dates).
    Anything else (a list, nested objects, a non-object top level) is a
    :class:`SetupError`: the generator and the spec have drifted, and a vague
    KeyError twelve scenes later would hide that.
    """
    if not outputs_path.exists():
        raise SetupError(
            f"setup outputs file not found: {outputs_path} — the setup command "
            "declared `outputs:` but did not write it (or wrote it elsewhere)."
        )
    try:
        data = json.loads(outputs_path.read_text())
    except json.JSONDecodeError as e:
        raise SetupError(f"setup outputs file is not valid JSON: {outputs_path} ({e})")
    if not isinstance(data, dict):
        raise SetupError(
            f"setup outputs must be a flat JSON object, got {type(data).__name__}: {outputs_path}"
        )
    for key, value in data.items():
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            raise SetupError(
                f"setup outputs values must be strings or numbers — "
                f"key '{key}' is {type(value).__name__}: {outputs_path}"
            )
    return data


def run_setup(setup: dict, spec_path: Path, *, skip_setup: bool = False) -> dict:
    """Execute the spec's ``setup`` block and return its provenance record.

    Honors the ``rerun`` semantics: ``per_render`` (default) runs the command
    on every invocation; ``once`` skips it when the outputs file already
    exists. ``skip_setup`` (the ``--skip-setup`` escape hatch) skips the
    command unconditionally but still loads the outputs file — for fast
    re-renders when the operator KNOWS the data is fresh. State-mutating demos
    must not use it: their recording changes the world, so every render needs
    a reseed.

    Output streams to the recorder's log (inherited stdout/stderr — the
    generator's own progress lines show up live). Nonzero exit or timeout
    raises :class:`SetupError`; ``main`` aborts loudly before any browser
    opens.

    Returns the provenance dict that rides on the RunReport and (with
    ``--snapshots``) lands in ``setup-vars.json``: the command, cwd, rerun
    mode, whether/why it was skipped, exit code, duration, and the resolved
    variables. The data a film was made on is part of the run's evidence
    chain.
    """
    command = setup.get("command") or ""
    if not command.strip():
        raise SetupError("spec.setup.command is empty — declare the synthetic generator command")
    rerun = setup.get("rerun", "per_render")
    if rerun not in ("per_render", "once"):
        raise SetupError(f"spec.setup.rerun must be per_render | once (got: {rerun!r})")
    timeout_seconds = int(setup.get("timeout_seconds", 1200))

    cwd = resolve_setup_cwd(spec_path)
    outputs_rel = setup.get("outputs")
    outputs_path = (cwd / outputs_rel) if outputs_rel else None

    provenance: dict = {
        "command": command,
        "cwd": str(cwd),
        "rerun": rerun,
        "outputs": outputs_rel,
        "skipped": False,
        "skip_reason": None,
        "exit_code": None,
        "duration_seconds": None,
        "variables": {},
    }

    skip_reason: str | None = None
    if skip_setup:
        skip_reason = "--skip-setup"
    elif rerun == "once" and outputs_path is not None and outputs_path.exists():
        skip_reason = f"rerun=once and outputs file exists ({outputs_path})"

    if skip_reason:
        provenance["skipped"] = True
        provenance["skip_reason"] = skip_reason
        print(f"Setup: skipped ({skip_reason})")
    else:
        print(f"Setup: running synthetic generator (cwd={cwd}, timeout={timeout_seconds}s)")
        print(f"  $ {command}")
        started = time.monotonic()
        try:
            # No capture: the generator's output streams straight into the
            # recorder's log so a hung seed is visible, not silent.
            result = subprocess.run(
                command, shell=True, cwd=str(cwd), timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            duration = time.monotonic() - started
            raise SetupError(
                f"setup command timed out after {timeout_seconds}s "
                f"({duration:.0f}s elapsed): {command}"
            )
        duration = time.monotonic() - started
        provenance["exit_code"] = result.returncode
        provenance["duration_seconds"] = round(duration, 2)
        if result.returncode != 0:
            raise SetupError(
                f"setup command failed (exit {result.returncode} after {duration:.0f}s): {command}\n"
                "The world is not in a recordable state — refusing to render."
            )
        print(f"Setup: done in {duration:.1f}s (exit 0)")

    if outputs_path is not None:
        provenance["variables"] = load_setup_outputs(outputs_path)
    return provenance


def build_scenes_from_spec(
    spec: dict, base_url: str, *, run_data: dict | None, args=None
) -> list[dict]:
    """Resolve spec.scenes to the scene records the Recorder consumes.

    Each scene is ``{"url": str | None, "title": str, "actions": [...]}``.
    The URL comes from one of (in priority order):
      1. An explicit ``url:`` on the scene (the cleanest authoring path).
      2. A LEADING ``goto`` action's target (actions[0] only) — the scene's own
         canonical start. A goto deeper in the list is a scripted mid-scene
         navigation and must NOT be hoisted to scene start (it may depend on a
         ``${var}`` captured by an earlier action in the same scene).
      3. The matching slide in ``run_data`` (legacy capture path).
      4. ``None`` — the orchestrator stays on the previous scene's ending page.

    The ``None`` default matters: a multi-scene narrative often runs like
    "scene 2 clicks a button that navigates, and scene 3 continues from
    there". Forcing a nav to ``base/`` for those scenes would wipe the JS
    state scene 2 just built. Authors who want a hard reset use
    ``url: /...`` (or a ``goto`` action) explicitly.

    If ``run_data`` is provided, only scenes with a matching captured slide
    are returned — so ``--scene 3`` upstream is honoured here. Without
    ``run_data`` we record every scene in the spec.
    """
    spec_scenes = spec.get("scenes") or []
    base = base_url.rstrip("/")

    # Build a 1-based index → captured URL map from run_data (if present).
    captured_urls: dict[int, str] = {}
    captured_filter: list[int] | None = None
    if run_data is not None:
        captured_filter = []
        for slide in run_data.get("slides", []):
            if slide.get("type") != "scene":
                continue
            idx = slide.get("scene_index")
            if idx is None:
                continue
            captured_filter.append(int(idx))
            if slide.get("url"):
                captured_urls[int(idx)] = slide["url"]

    def _absolutize(u: str) -> str:
        u = (u or "").strip()
        if not u:
            return ""
        return u if u.startswith("http") else base + u

    scenes: list[dict] = []
    for i, s in enumerate(spec_scenes, 1):
        if captured_filter is not None and i not in captured_filter:
            continue

        actions = list(s.get("actions") or [])
        url: str | None = None
        explicit = s.get("url")
        if explicit:
            url = _absolutize(explicit)
        if not url:
            # Only a LEADING goto is the scene's canonical start URL. A goto
            # deeper in the actions list is a scripted mid-scene navigation
            # (e.g. publish → capture the new id → goto the ${id} detail page)
            # — hoisting it to scene start navigates before the actions that
            # bind its ${var} have run, filming a literal `/x/${id}/` 404.
            leading = actions[0] if actions and isinstance(actions[0], dict) else None
            if leading is not None and (leading.get("kind") or "") == "goto":
                url = _absolutize(leading.get("target") or leading.get("value") or "")
        if not url:
            url = captured_urls.get(i)

        # Drop a redundant leading ``goto`` action when its target matches
        # ``scene.url`` (after _absolutize) — the orchestrator already
        # navigates to ``scene.url`` at the top of ``run_scene``, so leaving
        # the goto in causes a visible page reload 1-2s into every scene.
        # This was the load-bearing bug behind ~2.5s of scene-start dead-air
        # on every recording.
        #
        # Conservative: only drop the FIRST action, only when it's a goto,
        # only when its absolutized target equals the resolved scene.url.
        # An intentional reload-then-elsewhere pattern (url: /x then
        # goto /y) is preserved — the leading goto's target won't match.
        if url and actions:
            first = actions[0]
            if (first.get("kind") or "") == "goto":
                first_target = _absolutize(first.get("target") or first.get("value") or "")
                if first_target and first_target == url:
                    actions = actions[1:]
                    print(
                        f"  · scene {i}: dropping redundant first goto "
                        f"(target matches scene.url) — use scene.url instead"
                    )

        scenes.append({
            "url": url,  # may be None → orchestrator stays on previous URL
            "title": s.get("title", f"Scene {i}"),
            "video_hold_seconds": s.get("video_hold_seconds"),
            "actions": actions,
            # Optional per-scene viewport override (Scene.viewport in the
            # Pydantic schema). Recorder.run_scene resizes BEFORE the goto if
            # present, restores the spec-level size after the scene's
            # final_hold_ms. None → no override → spec-level viewport.
            "viewport": s.get("viewport"),
            # Per-scene capture mode. ``full_page: false`` → viewport snapshot, for
            # pages that are a tall table + a map/chart (the plan-review page), so the
            # map is the hero instead of a sliver atop a 16,000px strip. Omit (default
            # full-page) for normal pages. Stripping this here was the bug that made
            # map+table scenes capture as unreadable strips.
            "full_page": snapshot_full_page(s, args),
            # Per-scene tempo (Scene.pace in the schema): "teach" (default — full
            # read-time pacing, unchanged behavior) or "flow" (this beat is just
            # continuity, so the recorder compresses holds/settles and speeds the
            # cursor — see _lib.config.apply_scene_pace). Absent → teach. Threaded
            # through so the orchestrator can adjust THIS scene's config.
            "pace": s.get("pace"),
            # 1-based ORIGINAL spec index — preserved even when ``--input`` /
            # ``--scene`` filters narrow the list (so ``scene_index=3`` on a
            # partial run still means "spec scene 3", not "third in the
            # filtered list"). Snapshots and ActionResult.scene_index both
            # consume this.
            "scene_index": i,
        })
    return scenes


# --------------------------------------------------------------------------- #
# main


def _write_render_artifacts(args, spec, recorder, setup_provenance, total_seconds) -> None:
    """Write the RunReport (--report) + the canonical manifest (--manifest).

    Called in main()'s finally so a PARTIAL render still emits both. ``scenes_run``
    is derived from the scenes the recorder ACTUALLY recorded (a failed scene never
    gets a timing entry), so a partial manifest contains only rendered scenes — not
    placeholder slides for scenes that never ran.

    The manifest (walkthrough-run-data.json) is a superset of the legacy report:
    per-scene slides with screenshot paths + base64, narration, persona, mp4 offset,
    and the URLs each scene actually visited. generate_presentation.py and the eval
    fixtures read it; downstream judges and the deck builder consume it instead of
    re-driving the page.
    """
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(recorder.report.to_json())
        print(f"Wrote report: {args.report}")

    if args.manifest:
        manifest_snap_dir = (
            Path(args.snapshots) if args.snapshots else Path(args.manifest).parent
        )
        scenes_run = sorted(
            {
                s["scene_index"]
                for s in getattr(recorder.report, "scenes", [])
                if s.get("scene_index") is not None
            }
        )
        manifest = build_manifest(
            spec=spec,
            report=recorder.report,
            snapshots_dir=manifest_snap_dir,
            scenes_run=scenes_run,
            scene_filter=spec.get("scene_filter") or None,
            substitution_vars=(setup_provenance or {}).get("variables", {}),
            generated_at=datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            duration_seconds=total_seconds,
        )
        manifest_path = Path(args.manifest)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2))
        print(f"Wrote manifest: {args.manifest}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True, help="walkthrough YAML spec (source of truth for scenes)")
    ap.add_argument("--output", required=True, help="output mp4 path")
    ap.add_argument("--input", help="walkthrough run JSON (optional — narrows scenes to captured set)")
    ap.add_argument("--cookies", help="optional cookies JSON exported by `browse cookies`")
    ap.add_argument(
        "--storage-state",
        help=(
            "alternative to --cookies: a Playwright storage_state JSON (path). "
            "Use when the `browse cookies` export isn't available or isn't "
            "sticking across contexts — storage_state is applied at context "
            "creation, so it also carries localStorage/origins, not just cookies. "
            "Mutually exclusive with --cookies; if both are given, --storage-state wins."
        ),
    )
    ap.add_argument(
        "--skip-same-url",
        action="store_true",
        help="don't re-navigate between scenes whose URL hasn't changed (preserves JS state)",
    )
    ap.add_argument(
        "--report",
        help="optional path to write the JSON RunReport (per-action results + summary)",
    )
    ap.add_argument(
        "--snapshots",
        help=(
            "optional dir for per-scene screenshots + page-text JSON "
            "(scene_<N>.png + scene_<N>_page_text.json — captured at each "
            "scene's steady state). Used by canopy:walkthrough eval + DDD "
            "concept judges."
        ),
    )
    ap.add_argument(
        "--snapshot-empty-scenes",
        action="store_true",
        help=(
            "snapshot scenes with no actions too (default: skip — they would "
            "duplicate the previous scene's steady-state frame)."
        ),
    )
    ap.add_argument(
        "--capture-action-frames",
        action="store_true",
        help=(
            "for each scene with an effecting action (click/fill/select/type/"
            "press/draw), ALSO capture a before-frame (scene_<N>_before.png) at "
            "the action loop's starting line, so a judge can see the state "
            "CHANGE the actions produced (the canonical scene_<N>.png is the "
            "after frame). Single-frame scenes are unchanged."
        ),
    )
    ap.add_argument(
        "--skip-empty-scenes",
        action="store_true",
        help=(
            "don't record scenes whose actions list is empty (the narrative-"
            "only back half of long specs). The mp4 then skips those scenes "
            "entirely — the deck still shows them as title-card slides built "
            "from spec.scenes independently, so the narrative survives. "
            "Default: record every scene (back-compat)."
        ),
    )
    ap.add_argument(
        "--prewarm",
        dest="prewarm",
        action="store_true",
        default=None,
        help=(
            "visit each unique resolved scene URL once in a separate NON-"
            "recorded context before filming, so cold caches (first-hit page "
            "renders, remote image fetches) are paid OFF camera instead of as "
            "frozen frames. Overrides the spec's `prewarm:` value; default is "
            "the spec value (absent → off). Best-effort: failing pages are "
            "logged and skipped."
        ),
    )
    ap.add_argument(
        "--no-prewarm",
        dest="prewarm",
        action="store_false",
        help="disable the pre-warm pass even when the spec sets `prewarm: true`.",
    )
    ap.add_argument(
        "--skip-setup",
        action="store_true",
        help=(
            "skip running spec.setup.command (the synthetic generator) and "
            "just load its outputs file for ${var} substitution. Escape hatch "
            "for fast re-renders when the operator KNOWS the data is fresh. "
            "Demos that MUTATE state during recording must NOT use this — "
            "their scenes change the world, so every render needs a reseed."
        ),
    )
    ap.add_argument(
        "--ddd-orchestrated",
        action="store_true",
        help=(
            "Set by /canopy:ddd-run when it drives the render as part of a DDD "
            "run. Suppresses the hand-drive guard below. Do NOT pass this by "
            "hand — it exists so the orchestrator is the only quiet way to "
            "render into a DDD run dir."
        ),
    )
    ap.add_argument(
        "--full-page-snapshots",
        action="store_true",
        help=(
            "Keep the historical full-page snapshot on a --ddd-orchestrated run. "
            "DDD runs otherwise capture the VIEWPORT, because that is what the "
            "person watching actually sees and therefore what the judges should "
            "score."
        ),
    )
    ap.add_argument(
        "--force-hand-render",
        action="store_true",
        help=(
            "Override the DDD hand-drive guard and render into a run dir anyway "
            "(e.g. one-off debugging). Prefer /canopy:ddd-run — hand-rendering "
            "does NOT persist the dual-judge verdict to run_state.yaml."
        ),
    )
    ap.add_argument("--manifest", help="path to write the render manifest (walkthrough-run-data.json)")
    args = ap.parse_args()

    # ---- Guardrail: don't hand-drive a DDD run's render ----------------------
    # Calling this recorder directly (instead of going through /canopy:ddd-run)
    # is the #1 way DDD runs end up broken: the dual-judge verdict is never
    # assembled into run_state.yaml, the run can't be resumed cleanly, and
    # ddd-upload has no converged verdict to publish (you get loose /w/ clips,
    # not a navigable /ddd/<slug>/<run_id> package). If the output is landing
    # inside a `.canopy/ddd/runs/<run_id>/` directory, the caller MUST be the
    # orchestrator (--ddd-orchestrated) or explicitly override (--force-hand-render).
    _out_paths = " ".join(
        str(p) for p in (args.snapshots, args.output, args.report) if p
    )
    if ".canopy/ddd/runs/" in _out_paths and not (
        args.ddd_orchestrated or args.force_hand_render
    ):
        sys.exit(
            "\n"
            "════════════════════════════════════════════════════════════════════\n"
            "  ⛔  Refusing to hand-render into a DDD run directory.\n"
            "\n"
            "      This output path lives under .canopy/ddd/runs/. Rendering it\n"
            "      directly bypasses /canopy:ddd-run, so the dual-judge verdict is\n"
            "      NEVER written to run_state.yaml — the run looks stale/done, can't\n"
            "      be resumed cleanly, and ddd-upload publishes loose /w/ clips\n"
            "      instead of a /ddd/<slug>/<run_id> package.\n"
            "\n"
            "      ➜  Run  /canopy:ddd-run <run_id>  instead. It renders AND judges\n"
            "         AND persists run_state in one step, so a later /canopy:ddd\n"
            "         --resume <run_id> just works.\n"
            "\n"
            "      (Standalone /canopy:walkthrough renders OUTSIDE a run dir and is\n"
            "       unaffected. For a deliberate one-off, pass --force-hand-render.)\n"
            "════════════════════════════════════════════════════════════════════\n"
        )

    ffmpeg = check_ffmpeg()
    # Composes <slug>.recipe.yaml + <slug>.narrative.lock.json when that is the
    # on-disk shape; reads a legacy unified spec otherwise. Raw (unvalidated)
    # on purpose: older specs predate required concept_claim and the recorder
    # has always filmed them as plain dicts.
    spec = load_spec_raw(Path(args.spec))
    run_data: dict | None = None
    if args.input:
        run_data = json.loads(Path(args.input).read_text())

    # ---- Data setup (spec.setup — the synthetic generator contract) -----------
    # Runs BEFORE any browser/context/page exists: a failed seed must abort the
    # render, and the minted IDs must be substituted into scenes before the
    # first navigation. Never mutates the spec file on disk.
    setup = spec.get("setup") or None
    raw_scenes = spec.get("scenes") or []
    placeholders = scenes_placeholders(raw_scenes)
    # Vars minted ON CAMERA by ``capture`` actions — these are bound at RUNTIME
    # (late binding), not from setup outputs, so they're allowed to be absent
    # at setup time. The order-aware validator below confirms each is captured
    # BEFORE it's used.
    capture_bound: set[str] = set()
    for s in raw_scenes:
        capture_bound.update(scene_capture_vars(s))
    setup_provenance: dict | None = None
    if setup:
        try:
            setup_provenance = run_setup(setup, Path(args.spec), skip_setup=args.skip_setup)
        except SetupError as e:
            sys.exit(f"ERROR: {e}")
    setup_vars: dict = (setup_provenance or {}).get("variables", {})
    # A ${...} with neither a setup block NOR an earlier capture to bind it is
    # misconfiguration — filming would navigate to a literal "/runs/${run_id}/".
    unbacked = placeholders - set(setup_vars) - capture_bound
    if unbacked and setup is None:
        sys.exit(
            "ERROR: spec uses ${...} placeholders but declares no `setup:` block and "
            f"no `capture` action binds them: {', '.join(sorted(unbacked))}. Declare "
            "setup.command + setup.outputs (the synthetic generator that mints these), "
            "add a `capture` action that mints them on camera, or remove the placeholders."
        )
    # Order-aware validation: every ${var} must be available WHEN USED — a setup
    # output OR a capture in an EARLIER scene. A var used before any capture/setup
    # provides it is a hard error (filming a literal placeholder).
    order_violations = ordered_placeholder_violations(raw_scenes, setup_vars=set(setup_vars))
    if order_violations:
        sys.exit(
            "ERROR: order-aware ${...} validation failed:\n  - "
            + "\n  - ".join(order_violations)
        )
    if placeholders or setup_provenance:
        # Substitute ONLY the setup-known vars up front (partial substitution).
        # Capture-bound ${var} are left intact for the recorder to resolve
        # lazily at runtime, after the on-camera capture fires.
        try:
            spec["scenes"] = substitute_scenes(
                raw_scenes, setup_vars, allow_unresolved=capture_bound
            )
        except UnresolvedPlaceholderError as e:
            sys.exit(f"ERROR: {e}")
        resolved_now = sorted(placeholders & set(setup_vars))
        if resolved_now:
            print(f"Setup: resolved ${{...}} variables: {', '.join(resolved_now)}")
        if capture_bound & placeholders:
            print(
                "Late binding: ${...} resolved on camera by capture: "
                f"{', '.join(sorted(capture_bound & placeholders))}"
            )

    # Build the RecorderConfig: pace preset, optional spec override.
    pace = spec.get("video_pace", "fast")
    if pace not in ("fast", "medium", "slow"):
        sys.exit(f"ERROR: video_pace must be fast | medium | slow (got: {pace!r})")
    config = RecorderConfig.for_pace(pace).with_overrides(spec.get("video_recorder_config") or {})

    viewport_w = int(spec.get("video_viewport_width", 1280))
    viewport_h = int(spec.get("video_viewport_height", 720))
    base_url = (spec.get("base_url") or "").rstrip("/")

    scenes = build_scenes_from_spec(spec, base_url, run_data=run_data, args=args)
    if args.skip_empty_scenes:
        # Drop scenes with no actions from the recording loop entirely. The
        # deck is built from spec.scenes separately (generate_presentation),
        # so narrative-only beats still appear as title-card slides — we just
        # don't waste 4-6s of clip on a static page that holds min_hold_ms
        # on whatever the previous scene's last URL was. Filter AFTER
        # build_scenes_from_spec so the surviving scenes keep their 1-based
        # ORIGINAL spec scene_index (matches snapshot + ActionResult tagging).
        before = len(scenes)
        scenes = filter_empty_scenes(scenes)
        skipped = before - len(scenes)
        if skipped:
            print(f"  · --skip-empty-scenes: dropped {skipped} action-empty scene(s) from the recording")
    if not scenes:
        sys.exit("ERROR: no scenes resolved from spec (check --input filtering)")

    print(f"Recording {len(scenes)} scenes at pace={pace} ({viewport_w}x{viewport_h})")

    prewarm_enabled = resolve_prewarm(args.prewarm, spec.get("prewarm"))

    # Parse the cookies export once — used by both the pre-warm context and
    # the recorded context (same auth, two contexts).
    cookies_data: list | None = None
    if args.cookies and not args.storage_state:
        cookies_data = json.loads(Path(args.cookies).read_text()) or None

    with tempfile.TemporaryDirectory(prefix="walkthrough-video-") as td:
        video_dir = Path(td)
        with sync_playwright() as p:
            # SwiftShader so headless Chromium can render WebGL — Mapbox GL,
            # three.js, deck.gl all fail to initialize without a GPU otherwise,
            # leaving a blank canvas the cursor clicks into (a map `draw` then
            # places no vertices). SwiftShader is Chromium's CPU GL backend; the
            # explicit flag is required since Chrome dropped the auto-fallback.
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--enable-unsafe-swiftshader",
                    "--use-angle=swiftshader",
                    "--ignore-gpu-blocklist",
                ],
            )
            # ---- Pre-warm pass (OFF camera) --------------------------------
            # Runs AFTER setup (so ${var}-resolved URLs are final) and with the
            # same auth as the recording, but BEFORE the recorded context is
            # created — Playwright's video capture starts at page creation, so
            # nothing here can land on film. Cold caches (first-hit page
            # renders, remote-image cold fetches) get paid now instead of as
            # frozen frames mid-scene. Best-effort throughout: any failure is
            # logged and the render proceeds with whatever stayed cold.
            prewarm_provenance: dict | None = None
            if prewarm_enabled:
                prewarm_urls = collect_prewarm_urls(scenes)
                if prewarm_urls:
                    print(f"Prewarm: visiting {len(prewarm_urls)} unique scene URL(s) off camera")
                    try:
                        prewarm_kwargs: dict = dict(
                            viewport={"width": viewport_w, "height": viewport_h},
                        )
                        if args.storage_state:
                            prewarm_kwargs["storage_state"] = args.storage_state
                        prewarm_context = browser.new_context(**prewarm_kwargs)
                        try:
                            if cookies_data:
                                prewarm_context.add_cookies(cookies_data)
                            prewarm_auth_url: str | None = None
                            if not args.cookies and not args.storage_state:
                                auth = spec.get("auth") or {}
                                if auth.get("type") == "url" and auth.get("url"):
                                    prewarm_auth_url = base_url + auth["url"]
                            prewarm_provenance = run_prewarm(
                                prewarm_context,
                                prewarm_urls,
                                settle_ms=config.prewarm_settle_ms,
                                page_timeout_ms=config.prewarm_page_timeout_ms,
                                auth_url=prewarm_auth_url,
                            )
                        finally:
                            prewarm_context.close()
                    except Exception as e:  # noqa: BLE001 — prewarm must never kill the render
                        print(f"  ! prewarm pass aborted ({e}) — recording proceeds cold")
                        prewarm_provenance = {
                            "pages": len(prewarm_urls),
                            "duration_seconds": 0.0,
                            "failures": [{"url": "*", "error": str(e)}],
                        }
                    failed = len((prewarm_provenance or {}).get("failures") or [])
                    print(
                        f"Prewarm: done in {prewarm_provenance['duration_seconds']:.1f}s "
                        f"({prewarm_provenance['pages']} page(s), {failed} failure(s))"
                    )
                else:
                    print("Prewarm: enabled but no scene URLs to warm — skipping")

            # Sign every persona this spec visits in BEFORE the recorded context
            # exists, so none of it is on the film. Each gets a throwaway context;
            # the recorder swaps cookies per scene. No-op unless the spec declares
            # `auth.type: form`, so every existing spec is unaffected.
            identities = mint_identities(browser, spec, base_url)
            if identities:
                print(f"Identities: {len(identities)} persona(s) signed in off camera")

            context_kwargs = dict(
                viewport={"width": viewport_w, "height": viewport_h},
                record_video_dir=str(video_dir),
                record_video_size={"width": viewport_w, "height": viewport_h},
            )
            # storage_state must be supplied at context construction (Playwright
            # can't load it onto an existing context). It seeds the auth before
            # any page opens, so the first scene navigation is already logged in.
            if args.storage_state:
                context_kwargs["storage_state"] = args.storage_state
            context = browser.new_context(**context_kwargs)
            # Synthetic cursor + click ripple (headless Chromium draws no OS
            # cursor). add_init_script runs at document-create on every nav, so
            # the cursor survives the per-scene page changes.
            context.add_init_script(CURSOR_OVERLAY_JS)
            # Auto-accept window.confirm/alert dialogs (e.g. destructive
            # "regenerate?" prompts) so a scripted click doesn't hang the render.
            context.on("dialog", lambda d: d.accept())

            if cookies_data:
                context.add_cookies(cookies_data)

            page = context.new_page()
            # Capture client-side redirects / SPA route changes via Playwright's
            # framenavigated event (main frame only). Action-boundary page.url
            # snapshots miss redirects that fire BETWEEN actions (e.g. an
            # audit→workflow redirect after a completion click while the
            # recorder holds). The orchestrator clears this list at each scene
            # start and folds it into that scene's urls_visited at scene end.
            _nav_sink: list[str] = []

            def _on_frame_navigated(frame):
                try:
                    if frame is page.main_frame:
                        _nav_sink.append(frame.url)
                except Exception:  # noqa: BLE001 — nav telemetry must never break a render
                    pass

            page.on("framenavigated", _on_frame_navigated)
            # Playwright's video capture starts when the page opens — this is
            # second zero of the recording timeline. Captured here (NOT at
            # Recorder.run) so any pre-scene auth navigation below counts
            # toward scene 1's start offset, keeping per-scene timestamps
            # aligned with the produced mp4.
            recording_started = time.monotonic()

            # URL-based auth (e.g. /auth/e2e-login?token=...) for specs that
            # use a magic-link login instead of cookie import. Skipped when
            # --storage-state already seeded the session.
            if not args.cookies and not args.storage_state:
                auth = spec.get("auth") or {}
                if auth.get("type") == "url" and auth.get("url"):
                    try:
                        page.goto(base_url + auth["url"], wait_until="networkidle", timeout=30000)
                    except Exception as e:
                        print(f"  ! auth nav warning: {e}", file=sys.stderr)

            recorder_cls = SkipSameUrlRecorder if args.skip_same_url else Recorder
            recorder = recorder_cls(
                config=config,
                base_url=base_url,
                snapshot_dir=Path(args.snapshots) if args.snapshots else None,
                snapshot_empty_scenes=bool(args.snapshot_empty_scenes),
                capture_action_frames=bool(args.capture_action_frames),
                # Per-scene viewport overrides (Scene.viewport) are restored
                # back to this size after each overridden scene's final hold.
                default_viewport={"width": viewport_w, "height": viewport_h},
                # Seed the LIVE late-binding ${var} map with the setup outputs.
                # On-camera ``capture`` actions extend it during the render; the
                # orchestrator resolves scene urls + action target/value against
                # it lazily, so an id minted in an earlier scene flows forward.
                variables=setup_vars,
                # persona -> cookies, minted above off camera. The recorder
                # swaps them before a scene's nav when its persona changes.
                identities=identities,
            )
            recorder.recording_epoch = recording_started
            # Provenance: the data this film is made on is part of the run's
            # evidence chain — the resolved vars + setup command + exit code +
            # duration ride on the RunReport, and land in the snapshots dir.
            recorder.report.setup = setup_provenance
            recorder.report.prewarm = prewarm_provenance
            if args.snapshots and setup_provenance is not None:
                snap_dir = Path(args.snapshots)
                snap_dir.mkdir(parents=True, exist_ok=True)
                (snap_dir / "setup-vars.json").write_text(
                    json.dumps(setup_provenance, indent=2)
                )
            # Write the report + manifest in a finally so a PARTIAL render (a
            # must_succeed action aborts mid-spec) still emits them — the
            # snapshots for completed scenes are already on disk, so the
            # manifest/report of what DID render are exactly what you need to
            # debug the failure. The render error is preserved and re-raised
            # after, so the process still exits non-zero (loud, not silent).
            render_error: Exception | None = None
            total_seconds = 0.0
            try:
                total_seconds = recorder.run(page, scenes, nav_sink=_nav_sink)
            except Exception as e:  # noqa: BLE001 — re-raised below after artifacts are written
                render_error = e
            finally:
                context.close()  # flush video
                browser.close()
                recorder.print_summary()
                _write_render_artifacts(
                    args, spec, recorder, setup_provenance, total_seconds
                )
            if render_error is not None:
                raise render_error

        webms = list(video_dir.glob("*.webm"))
        if not webms:
            sys.exit("ERROR: no video file produced by Playwright")
        out_path = Path(args.output)
        webm_to_mp4(ffmpeg, webms[0], out_path)
        size_mb = out_path.stat().st_size / (1024 * 1024)
        print(f"✓ {out_path} ({size_mb:.1f} MB, ~{total_seconds:.0f}s of footage)")


if __name__ == "__main__":
    main()
