"""One command per iteration step — render it, then publish it.

    python -m scripts.ddd.iteration render  <run_id> --spec <recipe> [--base-url URL] [--cookies F | --storage-state F] [-- <extra recorder args>]
    python -m scripts.ddd.iteration publish <run_id> --spec <recipe> [--title NAME] [--private]

Why
---
``ddd-run`` Steps 2 and 2b were prose plus copy-paste bash: stamp a start time,
run ``record_video.py`` under the watchdog with eleven flags, keep its exit code,
``render_check`` against both, generate the deck, upload the deck and the clip
(grepping a ``View:`` line out of each, never the ``Share:`` one), hand the deck
URL to the clip as a companion, then edit ``run_state`` with inline Python to
stamp the URLs. On the first live product-objective run
(connect-labs ``supply-sophie-unanswered-round-2026-10-04-001``) the orchestrator
re-assembled that by hand every iteration — went looking for a previous run's
render log to recover the command, wrote ``s.iteration_decks[...] = ...`` in an
inline ``python -c``, and hit a runtime without Playwright. Each hand assembly is
a place for the contract to drift (a wrong flag, the wrong URL line, a stale
render uploaded), which is the exact failure ``render_check`` exists to stop.

``render``
    Stamps ``<run_dir>/.render_start``, picks ``--base-url`` from the pass's
    stamped target (``target plan``: an inner-loop pass renders the local build),
    runs the recorder with the DDD flag set under the watchdog (``render`` step
    budget from ``timeouts:``) into ``render-iter<N>.log``, and writes the exit
    code to ``.render_rc``. Exit code = the recorder's.

``publish``
    ``render_check`` against that start stamp and exit code (refuses — exit 1 —
    a failed or stale render, stamping the verdict), generates
    ``iter<N>_deck.html``, uploads deck then clip (with the deck, the narrative
    review and the spec's app pages as the clip's companion links), and stamps
    ``iteration_decks`` / ``iteration_clips`` through ``runstate.save`` (so the
    run store writes it through). An upload failure is logged to
    ``upload-errors.md`` and leaves that URL unset — the judges still score the
    local PNGs; never a ``file://`` fallback.

Both print one JSON object on stdout.
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: The canopy runtime root this module ships in (scripts/ddd/iteration.py -> root).
RUNTIME = Path(__file__).resolve().parents[2]
RECORDER = RUNTIME / "scripts" / "walkthrough" / "record_video.py"
GENERATOR = RUNTIME / "scripts" / "walkthrough" / "generate_presentation.py"
UPLOADER = RUNTIME / "scripts" / "walkthrough-share" / "upload.py"

START_FILE = ".render_start"
RC_FILE = ".render_rc"
_VIEW = re.compile(r"^View: (https?://\S+)", re.MULTILINE)


def _py(script: Path, *args: str, browser: bool = False) -> list[str]:
    """``uv run`` a runtime script; ``browser`` pulls the recorder's Playwright extra."""
    cmd = ["uv", "run", "--project", str(RUNTIME)]
    if browser:
        cmd += ["--extra", "browser"]
    return [*cmd, "python", str(script), *args]


def _env() -> dict[str, str]:
    """The runtime root on PYTHONPATH: the runtime's scripts run BY PATH import
    ``scripts.*``, which only resolves from the root (the live session passed
    ``PYTHONPATH=$DDD_REPO`` by hand; without it the recorder dies on import)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(RUNTIME), env.get("PYTHONPATH", "")) if p)
    return env


def _state_and_dir(run_id: str):
    from scripts.ddd.runstate import load, run_dir_for

    state = load(run_id)
    run_dir = run_dir_for(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    return state, run_dir


def _pass_base_url(state: Any) -> str | None:
    """The base URL ``target plan`` stamped for THIS iteration (inner loop), else None."""
    tgt = getattr(state, "current_target", None) or {}
    if tgt.get("iteration") != state.iteration:
        return None
    return tgt.get("base_url") if tgt.get("target") == "inner" else None


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------


def recorder_command(
    spec: str | Path,
    run_dir: Path,
    iteration: int,
    *,
    base_url: str | None = None,
    cookies: str | None = None,
    storage_state: str | None = None,
    extra: list[str] | None = None,
) -> list[str]:
    """The DDD flag set for ``record_video.py`` — the one place it is written down."""
    args = [
        "--spec", str(Path(spec).resolve()),
        "--output", str(run_dir / f"iter{iteration}_clip.mp4"),
        "--snapshots", str(run_dir / "snapshots") + "/",
        "--report", str(run_dir / "run-report.json"),
        "--manifest", str(run_dir / "walkthrough-run-data.json"),
        "--skip-empty-scenes",
        "--skip-same-url",
        "--capture-action-frames",
        "--ddd-orchestrated",
    ]
    if base_url:
        args += ["--base-url", base_url]
    if cookies:
        args += ["--cookies", cookies]
    if storage_state:
        args += ["--storage-state", storage_state]
    return _py(RECORDER, *args, *(extra or []), browser=True)


def render(
    run_id: str,
    spec: str | Path,
    *,
    base_url: str | None = None,
    cookies: str | None = None,
    storage_state: str | None = None,
    extra: list[str] | None = None,
    popen=subprocess.Popen,
) -> dict[str, Any]:
    from scripts.ddd import loop_config, watchdog
    from scripts.ddd.runstate import load, save

    state, run_dir = _state_and_dir(run_id)
    it = state.iteration
    base_url = base_url or _pass_base_url(state)
    cmd = recorder_command(spec, run_dir, it, base_url=base_url, cookies=cookies, storage_state=storage_state, extra=extra)

    started = time.time()
    (run_dir / START_FILE).write_text(f"{started:.3f}\n")
    (run_dir / RC_FILE).unlink(missing_ok=True)
    log_path = run_dir / f"render-iter{it}.log"

    cfg = loop_config.load()
    budget = cfg.timeouts.for_step("render")
    watchdog.start(state, "render", timeout_minutes=budget, heartbeat_minutes=cfg.timeouts.heartbeat_minutes)
    save(state)
    with log_path.open("w") as log:
        log.write(f"$ {' '.join(cmd)}\n")
        log.flush()
        out = watchdog.run_command(
            cmd,
            timeout_s=budget * 60,
            popen=functools.partial(popen, stdout=log, stderr=subprocess.STDOUT, cwd=str(RUNTIME), env=_env()),
        )
        log.write(f"rc={out['exit_code']}\n")
    state = load(run_id)
    watchdog.finish(state, "render", out["status"], reason=out["reason"])
    save(state)
    (run_dir / RC_FILE).write_text(f"{out['exit_code']}\n")
    return {
        "run_id": run_id,
        "iteration": it,
        "status": out["status"],
        "exit_code": out["exit_code"],
        "elapsed_s": out["elapsed_s"],
        "base_url": base_url,
        "log": str(log_path),
        "reason": out["reason"],
    }


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------


def _upload(path: Path, args: list[str], *, run=subprocess.run) -> tuple[str | None, str | None]:
    """``(view_url, error)`` — the ``View:`` line, never ``Share:`` (it carries a token)."""
    proc = run(_py(UPLOADER, str(path), *args), capture_output=True, text=True, cwd=str(RUNTIME), env=_env())
    found = _VIEW.findall(proc.stdout or "")
    if proc.returncode == 0 and found:
        return found[-1], None
    tail = ((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()[-3:]
    return None, f"exit {proc.returncode}: {' | '.join(tail) or 'no View: line'}"


def _log_upload_error(run_dir: Path, iteration: int, what: str, error: str) -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with (run_dir / "upload-errors.md").open("a") as fh:
        fh.write(f"- [{stamp}] iter {iteration} {what}: {error}\n")


def _spec_title(spec: Path, fallback: str) -> str:
    try:
        import yaml

        data = yaml.safe_load(spec.read_text()) or {}
        return str(data.get("name") or data.get("title") or fallback)
    except Exception:
        return fallback


def publish(
    run_id: str,
    spec: str | Path,
    *,
    title: str | None = None,
    public: bool = True,
    run=subprocess.run,
) -> dict[str, Any]:
    from scripts.ddd import render_check
    from scripts.ddd.runstate import load, save

    state, run_dir = _state_and_dir(run_id)
    it = state.iteration
    spec = Path(spec).resolve()
    start_file, rc_file = run_dir / START_FILE, run_dir / RC_FILE
    if not start_file.exists():
        return {"ok": False, "reason": f"no {START_FILE} in {run_dir} — render with `iteration render` first"}
    since = float(start_file.read_text().strip())
    rc = int(rc_file.read_text().strip()) if rc_file.exists() else None
    clip = run_dir / f"iter{it}_clip.mp4"

    verdict = render_check.check(run_dir, since=since, exit_code=rc, clip=clip if clip.exists() else None)
    render_check.stamp(run_id, verdict)
    if not verdict["ok"]:
        return {"ok": False, "iteration": it, "render_check": verdict,
                "reason": f"NOT publishing — {verdict['reason']}; re-render"}

    title = title or _spec_title(spec, state.narrative_slug)
    common = ["--run-id", run_id, "--feature", state.narrative_slug] + (["--public"] if public else [])
    errors: list[str] = []

    deck = run_dir / f"iter{it}_deck.html"
    gen = run(_py(GENERATOR, "--input", str(run_dir / "walkthrough-run-data.json"), "--output", str(deck)),
              capture_output=True, text=True, cwd=str(RUNTIME), env=_env())
    deck_url = None
    if gen.returncode != 0 or not deck.exists():
        errors.append(f"deck generation failed (exit {gen.returncode})")
        _log_upload_error(run_dir, it, "deck generation", (gen.stderr or "").strip()[-300:])
    else:
        deck_url, err = _upload(deck, [*common, "--title", f"{title} iter{it}", "--role", "deck"], run=run)
        if err:
            errors.append(f"deck upload: {err}")
            _log_upload_error(run_dir, it, "deck upload", err)

    clip_url = None
    if clip.exists():
        clip_args = [*common, "--title", f"{title} iter{it} (video)", "--role", "clip", "--spec", str(spec)]
        if deck_url:
            clip_args += ["--companion-url", deck_url]
        if state.narrative_review_url:
            clip_args += ["--narrative-url", state.narrative_review_url]
        clip_url, err = _upload(clip, clip_args, run=run)
        if err:
            errors.append(f"clip upload: {err}")
            _log_upload_error(run_dir, it, "clip upload", err)

    state = load(run_id)
    if deck_url:
        state.iteration_decks[it] = deck_url
    if clip_url:
        state.iteration_clips[it] = clip_url
    save(state)
    return {
        "ok": True,
        "iteration": it,
        "render_check": verdict,
        "deck_url": deck_url,
        "clip_url": clip_url,
        "upload_errors": errors,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    extra: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(prog="python -m scripts.ddd.iteration")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render", help="record this iteration under the watchdog")
    r.add_argument("run_id")
    r.add_argument("--spec", required=True)
    r.add_argument("--base-url", default=None, help="default: the inner-loop URL `target plan` stamped")
    r.add_argument("--cookies", default=None)
    r.add_argument("--storage-state", default=None, help="Playwright storage state (an OAuth-only app's session)")
    p = sub.add_parser("publish", help="render_check, deck, upload, stamp run_state")
    p.add_argument("run_id")
    p.add_argument("--spec", required=True)
    p.add_argument("--title", default=None)
    p.add_argument("--private", action="store_true", help="dimagi-OAuth only (no share token)")
    args = ap.parse_args(argv)

    if args.cmd == "render":
        out = render(args.run_id, args.spec, base_url=args.base_url, cookies=args.cookies,
                     storage_state=args.storage_state, extra=extra)
        print(json.dumps(out, indent=1))
        if out["exit_code"] != 0:
            print(f"render: {out['status']} ({out['reason']}) — see {out['log']}", file=sys.stderr)
        return int(out["exit_code"])
    out = publish(args.run_id, args.spec, title=args.title, public=not args.private)
    print(json.dumps(out, indent=1, default=str))
    if not out["ok"]:
        print(f"publish: {out['reason']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
