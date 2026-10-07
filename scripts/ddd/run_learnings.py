"""Write what a finished run learned into the repo's DDD learnings — every run (canopy#788).

The ddd agent's "Persist + self-tune" step tells the orchestrator to append
learnings by hand after every cycle. It did not happen: the supply agent said at
23:29 on 2026-10-06 "Nothing has gone back into the DDD process", and
connect-labs had no ``.canopy/ddd/learnings.md`` at all after six runs. The next
run's bootstrap reads that file, so a run that ends without writing it hands the
next one nothing — it re-discovers the same unfixable floor, the same cells that
never cleared, the same remote-only data.

:func:`record` runs inside ``assemble`` on every pass that ENDS the run (a
``stop_*`` decision) and appends one block, from run state alone — no judgment:

* how it ended (the decision and its reason, the terminal status);
* the score trajectory and the number of passes;
* cells still open after recurring for several passes (the fixes did not land);
* floor findings outside the loop's edit scope, with where their fix lives;
* time per pass and how much judging was reused (``pass_timings``);
* an inner loop that was dropped or overridden.

Idempotent per run: a block already written for this run id is replaced, so a
re-assemble or a resumed run never duplicates it.

    python -m scripts.ddd.run_learnings <run_id>    # (re)write the block by hand
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

LEARNINGS = "learnings.md"


def _marker(run_id: str) -> tuple[str, str]:
    return f"<!-- ddd-run:{run_id} -->", f"<!-- /ddd-run:{run_id} -->"


def _short(text: Any, n: int = 300) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def summarize(state: Any) -> str:
    """The learnings block for a finished run (markdown, no markers)."""
    from scripts.ddd import fix_scope

    run_id = getattr(state, "run_id", "?")
    lines = [f"### Run {run_id} — {getattr(state, 'narrative_slug', '')}".rstrip(" —")]
    action = getattr(state, "auto_iterate_next_action", None)
    lines.append(
        f"- Ended `{action}` ({getattr(state, 'terminal_status', None)}) after "
        f"{len(getattr(state, 'score_history', None) or [])} judged pass(es); "
        f"objective {getattr(state, 'objective', None)}."
    )
    hist = getattr(state, "score_history", None) or []
    if hist:
        lines.append(f"- Score trajectory: {', '.join(f'{s:g}' for s in hist)}.")
    reason = getattr(state, "auto_iterate_reason", None)
    if reason:
        lines.append(f"- Why it stopped: {_short(reason, 500)}")

    recurring = [
        f for f in getattr(state, "findings", None) or []
        if isinstance(f, dict) and int(f.get("recurring") or 0) >= 2
    ]
    if recurring:
        cells = sorted(
            {f"{fix_scope.scene_key(f.get('scene')) or '?'}:{f.get('dimension')} "
             f"(open {f.get('recurring')} passes)" for f in recurring}
        )
        lines.append(
            "- Never cleared — the fixes tried did not land; brief a different approach "
            f"next run: {', '.join(cells)}."
        )

    floor = getattr(state, "gating_floor", None) or {}
    out = [r for r in floor.get("findings") or [] if isinstance(r, dict) and r.get("edit_scope") == "out"]
    for r in out:
        lines.append(
            f"- Floor outside the loop's edit scope — scene {r.get('scene') or '?'} "
            f"{r.get('dimension')}: {_short(r.get('fix_recommendation'), 200)} "
            f"[{r.get('edit_scope_reason')}]. Fix it at its source before the next run."
        )

    timings = [t for t in getattr(state, "pass_timings", None) or [] if isinstance(t, dict)]
    if timings:
        wall = sum(float(t.get("wall_minutes") or 0) for t in timings)
        rejudged = sum(int(t.get("rejudged") or 0) for t in timings)
        reused = sum(int(t.get("reused") or 0) for t in timings)
        captures = [t.get("capture") for t in timings if t.get("capture")]
        lines.append(
            f"- Time: {wall:.0f} min over {len(timings)} pass(es); scenes re-judged {rejudged}, "
            f"reused {reused}"
            + (f"; captures {', '.join(f'{c}×{captures.count(c)}' for c in sorted(set(captures)))}" if captures else "")
            + "."
        )

    policy = getattr(state, "inner_loop_policy", None) or {}
    if policy.get("status") in ("dropped", "off", "missing"):
        lines.append(
            f"- Inner loop {policy.get('status')}: {_short(policy.get('reason'), 240)} "
            "— make the run's data reproducible locally before the next run."
        )
    return "\n".join(lines) + "\n"


def record(state: Any, ddd_dir: str | Path) -> Path:
    """Write (or replace) this run's block in ``<ddd_dir>/learnings.md``."""
    path = Path(ddd_dir) / LEARNINGS
    path.parent.mkdir(parents=True, exist_ok=True)
    start, end = _marker(getattr(state, "run_id", "?"))
    block = f"{start}\n{summarize(state)}{end}\n"
    text = path.read_text() if path.exists() else "# DDD learnings\n\n"
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end) + r"\n?", re.S)
    if pattern.search(text):
        text = pattern.sub(lambda _m: block, text)
    else:
        text = text.rstrip("\n") + "\n\n" + block
    path.write_text(text)
    return path


def ends_run(action: str | None) -> bool:
    return bool(action) and str(action).startswith("stop_")


def _main(argv: list[str] | None = None) -> int:
    from scripts.ddd.runstate import _resolve_ddd_dir, load

    ap = argparse.ArgumentParser(prog="python -m scripts.ddd.run_learnings")
    ap.add_argument("run_id")
    args = ap.parse_args(argv)
    ddd_dir = _resolve_ddd_dir()
    print(record(load(args.run_id, ddd_dir=ddd_dir), ddd_dir))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
