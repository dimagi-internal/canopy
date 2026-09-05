"""Action results + run report for the walkthrough recorder.

Before this module the recorder swallowed failures: a missing click target
printed a warning, the video showed a cursor twitching in place, and the run
reported success. The microplans-10-wards recording shipped at ``Created 0 of
10 plans · 10 errors`` looking fine from the outside until someone checked
plan counts on labs.

Now every action returns an :class:`ActionResult`. The orchestrator
accumulates them in a :class:`RunReport` and prints a per-scene + per-run
summary so silent failures stop hiding. Specs can also set
``must_succeed: true`` on an action; the runner raises instead of swallowing,
turning the recording into a quasi-test.

Error kinds (the ``error_kind`` field) are tagged so a downstream grader can
distinguish "target not found" (a spec drift bug — the UI changed) from
"playwright timeout" (a flaky page) from "unknown_kind" (a spec uses a verb
this recorder doesn't know).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class ActionResult:
    """The outcome of executing one declarative ``Action``.

    Returned by ``execute_action`` (was ``None`` before). Consumed by
    :class:`RunReport` for telemetry, and by the orchestrator's
    ``after_action`` hook for custom downstream handling.
    """

    kind: str
    """The action verb that ran (``click``, ``fill``, ``select``, …)."""

    ok: bool
    """``True`` if the action succeeded; ``False`` if it was a no-op or errored."""

    target: str | None = None
    value: str | None = None
    note: str | None = None
    elapsed_ms: int = 0

    start_seconds: float | None = None
    """Offset (seconds) into the recording timeline where this action BEGAN —
    ``time.monotonic()`` at dispatch minus the recorder's ``recording_epoch``,
    the same origin as :attr:`RunReport.scenes` ``start_seconds`` and
    :attr:`RunReport.load_waits`. Stamped by the orchestrator's action loop.

    This is the action's mark in the RAW master clip. The DDD explainer
    (``scripts.ddd.snippets``) maps it through the same de-dwell + load-wait
    excision the footage gets, into ON-SCREEN seconds, and binds it to the moment
    the voiceover speaks the matching field word so the footage time-warps to land
    each field on its narration (see ``video-engine/docs/action-word-sync.md``).
    ``None`` for actions executed outside a recording (direct ``execute_action``
    test calls), so old reports/call sites are unchanged."""

    warning: str | None = None
    """A non-fatal note about how the action behaved, or ``None``.

    Distinct from :attr:`error_message`, which only ever accompanies
    ``ok: False``. A warning means the action ran and reported success while
    doing less than the spec asked for. The case it was added for: a
    ``scroll_to`` whose requested position falls outside the scroll range, so
    the browser clamps it, the page does not move, and the scene captures the
    screen the PREVIOUS scene left behind — a frame that looks entirely
    legitimate and is of the wrong thing. Marking that ``ok: False`` would make
    the arc judge skip the run (it refuses to judge a take with a failed
    action) and would also fail the common, supported case of a defensive
    ``scroll_to`` onto an already-framed element. So it rides here instead, and
    reaches the judges through :func:`action_trace_by_scene`."""

    error_kind: str | None = None
    """One of: ``target_not_found``, ``timeout``, ``unknown_kind``, ``playwright``, ``other``. ``None`` on success."""

    error_message: str | None = None
    """Free-form message for logs; not parsed by anything machine-readable."""

    say: str | None = None
    """The narration word this action's field is named by, from the spec's
    ``say:`` (or ``word:``) key.

    Carried onto the result so it survives into the run report, which is the ONLY
    thing ``scripts.ddd.snippets`` sees when it builds action↔word marks. Without
    it the explicit-override branch of ``_mark_words`` was unreachable from the
    record → emit path: a spec could declare ``say: deadline`` and the mark would
    still be tagged with tokens scraped from the field id and the note, which
    collide across marks and read to the timing eval as narration naming things
    out of order. The eval's own remediation text tells an author to add a
    ``say:`` hint, so it has to actually do something."""

    must_succeed: bool = False
    """Whether the spec marked this action ``must_succeed: true``.

    Mirrored from the action dict onto the result so the run-report persists it
    — the DDD dual-judge needs to distinguish a failed ``must_succeed`` action
    (the demo's load-bearing step silently failed) from a failed optional one.
    Defaults False so old call sites and reports are unchanged.
    """

    scene_index: int | None = None
    """1-based original spec index of the scene this action belongs to.

    Stamped by the orchestrator (``Recorder.run_scene``) so downstream tooling
    can group action results by scene without re-parsing the spec. ``None``
    means the action was executed outside any scene loop (e.g. a direct
    ``execute_action`` test call).
    """

    capture_var: str | None = None
    """For a ``capture`` action: the variable name it binds (``var``). ``None``
    for every other kind. Lets the run-report show what the late-binding
    capture produced — ``kind=capture, var, ok, value`` — so a failed or empty
    capture is debuggable from the report alone."""

    capture_value: str | None = None
    """For a ``capture`` action: the value captured off the live page (the id
    threaded into ``${var}`` for later scenes), or ``None`` if nothing was
    captured. Distinct from ``value`` (the action's input field); this is the
    OUTPUT of the capture."""


@dataclass
class RunReport:
    """All :class:`ActionResult` records from a recorder run, plus summary helpers.

    The orchestrator owns one of these for the lifetime of a recording and
    prints its summary at the end. Downstream tools (canopy:walkthrough eval)
    can also read :meth:`as_dict` for grading.
    """

    results: list[ActionResult] = field(default_factory=list)

    scenes: list[dict] = field(default_factory=list)
    """Per-scene timing entries, in recording order. INVARIANT: ``_scene_timing_index``
    mirrors these SAME dict objects by identity, so any new code that appends a
    scene-timing entry MUST go through :meth:`record_scene_timing` (never
    ``self.scenes.append(...)`` directly) to keep the mirror in sync. Each entry::

        {"scene_index": int,        # 1-based ORIGINAL spec index
         "title": str,
         "start_seconds": float,    # offset into the recording timeline
         "duration_seconds": float}

    ``start_seconds`` is measured from the recorder's ``recording_epoch``
    (set by ``record_video.main`` at page creation — the moment Playwright's
    webm starts; the final mp4 is that webm re-encoded, so offsets carry
    over). Scenes dropped by ``--skip-empty-scenes`` never run, so they get
    no entry. Downstream consumers (the DDD product-findings review) use
    these to deep-link a video at the scene a finding is about — see
    :func:`scene_timestamps`."""

    setup: dict | None = None
    """Data-setup provenance (the spec's ``setup:`` block execution record):
    command, cwd, rerun mode, skipped/skip_reason, exit code, duration, and
    the resolved ``${var}`` substitution variables. Stamped by
    ``record_video.main`` when the spec declares a setup block — the data a
    film was made on is part of the run's evidence chain. ``None`` for specs
    with no setup block (the key is then omitted from :meth:`as_dict`, so
    existing report consumers are unchanged)."""

    prewarm: dict | None = None
    """Pre-warm pass provenance: ``{"pages": int, "duration_seconds": float,
    "failures": [{"url": str, "error": str}, ...]}``. Stamped by
    ``record_video.main`` when the pre-warm pass ran (spec ``prewarm: true``
    or CLI ``--prewarm``). ``None`` when prewarm was off — the key is then
    omitted from :meth:`as_dict`, mirroring ``setup``."""

    load_waits: list[dict] = field(default_factory=list)
    """Ground-truth loading-wait spans — one per ``wait_for`` action, in
    recording-timeline seconds (same ``recording_epoch`` origin as
    :attr:`scenes` ``start_seconds``). Each entry::

        {"scene_index": int, "start_seconds": float,
         "duration_seconds": float, "target": str | None}

    The explainer (``scripts.ddd.snippets``) excises the long ones from the
    walkthrough segments so a mid-scene load (a "Generating…"/"Reviewing…"
    spinner) collapses to a brief lead-in + jump-cut to the result — driven by
    what the recorder KNEW it waited for, not a pixel-based freeze guess (which
    an animated spinner defeats). Empty ⇒ key omitted from :meth:`as_dict`."""

    _scene_timing_index: dict[int, dict] = field(default_factory=dict, repr=False)
    """``scene_index -> timing entry`` mirror of :attr:`scenes`, kept in sync by
    :meth:`record_scene_timing`. Lets :meth:`record_scene_urls` and
    :meth:`scene_timing_for` reach a scene's entry by index in O(1) without
    re-scanning the list. The mirrored dicts are the SAME objects stored in
    :attr:`scenes`, so mutations (adding ``urls_visited``) show up in both —
    and :meth:`as_dict`/`:meth:`to_json` serialize them correctly."""

    def record(self, r: ActionResult) -> None:
        """Append one result. Called by ``execute_action`` after each action."""
        self.results.append(r)

    def record_scene_timing(
        self,
        *,
        scene_index: int,
        title: str,
        start_seconds: float,
        duration_seconds: float,
    ) -> None:
        """Append one per-scene timing entry. Called by ``Recorder.run_scene``."""
        entry = {
            "scene_index": int(scene_index),
            "title": title,
            "start_seconds": round(float(start_seconds), 3),
            "duration_seconds": round(float(duration_seconds), 3),
        }
        self.scenes.append(entry)
        self._scene_timing_index[int(scene_index)] = entry

    def record_load_wait(
        self,
        *,
        scene_index: int | None,
        start_seconds: float,
        duration_seconds: float,
        target: str | None = None,
    ) -> None:
        """Record one loading-wait span (a ``wait_for`` action's footage span).

        ``start_seconds`` is the recording-timeline offset where the wait began
        (``time.monotonic()`` at dispatch minus the ``recording_epoch``);
        ``duration_seconds`` is how long it blocked before the target appeared.
        See :attr:`load_waits`. Called by ``Recorder.run_scene`` per ``wait_for``."""
        self.load_waits.append(
            {
                "scene_index": int(scene_index) if scene_index is not None else None,
                "start_seconds": round(float(start_seconds), 3),
                "duration_seconds": round(float(duration_seconds), 3),
                "target": target,
            }
        )

    def record_scene_urls(self, *, scene_index: int, urls: list[str]) -> None:
        """Record the URLs a scene navigated to, deduped + order-preserving.

        Stores them under ``urls_visited`` on the scene's timing entry (the
        same dict held in :attr:`scenes`, so it serializes via
        :meth:`as_dict`). Called by ``Recorder.run_scene`` with the list of
        ``page.url`` snapshots collected across the scene's actions. If no
        timing entry exists yet for ``scene_index`` (e.g. a test calls this
        before :meth:`record_scene_timing`), a bare entry is created so the
        URLs aren't dropped."""
        entry = self._scene_timing_index.get(int(scene_index))
        if entry is None:
            entry = {"scene_index": int(scene_index)}
            self.scenes.append(entry)
            self._scene_timing_index[int(scene_index)] = entry
        seen: set[str] = set()
        deduped: list[str] = []
        for u in urls or []:
            if u in seen:
                continue
            seen.add(u)
            deduped.append(u)
        entry["urls_visited"] = deduped

    def scene_timing_for(self, scene_index: int) -> dict:
        """Return the timing entry for ``scene_index`` (or ``{}`` if none)."""
        return self._scene_timing_index.get(int(scene_index), {})

    def ok_count(self) -> int:
        return sum(1 for r in self.results if r.ok)

    def fail_count(self) -> int:
        return sum(1 for r in self.results if not r.ok)

    def failures(self) -> list[ActionResult]:
        return [r for r in self.results if not r.ok]

    def summary(self) -> str:
        """One-line summary like ``"28 actions: 23 ok, 5 failed"``."""
        n = len(self.results)
        ok = self.ok_count()
        bad = n - ok
        if not n:
            return "0 actions"
        if not bad:
            return f"{n} actions: all ok"
        return f"{n} actions: {ok} ok, {bad} failed"

    def as_dict(self) -> dict:
        d = {
            "total": len(self.results),
            "ok": self.ok_count(),
            "failed": self.fail_count(),
            "actions": [asdict(r) for r in self.results],
            "scenes": list(self.scenes),
        }
        if self.setup is not None:
            d["setup"] = self.setup
        if self.prewarm is not None:
            d["prewarm"] = self.prewarm
        if self.load_waits:
            d["load_waits"] = list(self.load_waits)
        return d

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.as_dict(), indent=indent)


def scene_timestamps(report: dict) -> dict[int, float]:
    """Read ``scene_index -> start_seconds`` from a run-report dict.

    ``report`` is the parsed JSON written by ``record_video.py --report``
    (i.e. :meth:`RunReport.as_dict` output). Scenes that were skipped
    (``--skip-empty-scenes``) have no entry. Returns ``{}`` for reports
    written before per-scene timing existed — callers degrade to "no video
    deep-links" rather than crashing on old artifacts.

    Stdlib-only on purpose: ``scripts.ddd.findings_review`` imports this
    without dragging in Playwright (this module has no browser deps).
    """
    out: dict[int, float] = {}
    for entry in report.get("scenes") or []:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("scene_index")
        start = entry.get("start_seconds")
        if idx is None or start is None:
            continue
        try:
            out[int(idx)] = float(start)
        except (TypeError, ValueError):
            continue
    return out


# Action kinds that EFFECT a state change the demo is claiming to perform.
# A scene whose narration asserts "create / fill / submit / select / award /
# publish" must contain at least one of these in its trace, or the task was
# CLAIMED but not SHOWN. ``hover`` / ``scroll_to`` / ``scroll`` / ``wait_for`` /
# ``hold`` only move the camera or wait — they never effect anything. ``goto``
# navigates but does not, on its own, effect a form action (it's the entry, not
# the act). The judges use this set to apply action-fidelity deductions.
EFFECTING_ACTION_KINDS: frozenset[str] = frozenset(
    {"click", "click_menu", "fill", "select", "type", "press", "draw", "map_click"}
)


def action_trace_by_scene(report: dict) -> dict[int, list[dict]]:
    """Group a run-report's ``actions`` by 1-based ``scene_index``.

    ``report`` is the parsed JSON written by ``record_video.py --report``
    (i.e. :meth:`RunReport.as_dict` output). Returns
    ``{scene_index: [{kind, target, ok, must_succeed, note, warning}, ...]}`` — the
    per-scene action trace the DDD dual-judge needs to tell a scene that
    actually filled+submitted a form from one that only HOVERED (same
    end-frame, same screenshot, but a different *act*).

    Only the fields a judge reasons over are kept (kind/target/ok/
    must_succeed/note/warning) — the cursor timing, error_message, and value are
    dropped so the trace handed to an LLM judge stays compact and free of
    noise. Actions with no ``scene_index`` (a direct ``execute_action`` test
    call, never a real scene) are skipped.

    Returns ``{}`` for reports written before action_index existed, or with no
    actions — callers degrade to "no action_trace, behave as today" rather
    than crashing on old artifacts. Stdlib-only (no Playwright import), so
    ``scripts.ddd`` can call it the same way it calls :func:`scene_timestamps`.
    """
    out: dict[int, list[dict]] = {}
    for entry in report.get("actions") or []:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("scene_index")
        if idx is None:
            continue
        try:
            key = int(idx)
        except (TypeError, ValueError):
            continue
        out.setdefault(key, []).append(
            {
                "kind": entry.get("kind"),
                "target": entry.get("target"),
                "ok": bool(entry.get("ok", True)),
                "must_succeed": bool(entry.get("must_succeed", False)),
                "note": entry.get("note"),
                # A no-op scroll is invisible in `ok` by design (see
                # ActionResult.warning), so without this the one lens that could
                # explain a duplicate frame would never be shown the reason.
                "warning": entry.get("warning"),
            }
        )
    return out


def scene_effecting_summary(trace: list[dict]) -> dict:
    """Summarize one scene's action trace for action-fidelity judging.

    ``trace`` is one scene's entry from :func:`action_trace_by_scene`. Returns
    ``{has_effecting, only_non_effecting, any_failed, any_required_failed,
    kinds}`` — the booleans a judge's deduction rule keys off:

    - ``has_effecting`` — ≥1 action in :data:`EFFECTING_ACTION_KINDS` ran (the
      scene actually fills/clicks/selects/etc., not just hovers/scrolls).
    - ``only_non_effecting`` — the scene scripted actions but NONE effect
      anything (hover/scroll/wait only). This is the "claimed, not shown" smell.
    - ``any_failed`` — ≥1 action came back ``ok: false`` (a demo action
      failed/timed out, e.g. the award click that silently timed out).
    - ``any_required_failed`` — ≥1 ``must_succeed`` action failed.
    - ``kinds`` — the sorted distinct action kinds present (for the report).

    An empty trace (a narrative-only scene with no actions) returns all-False —
    such scenes are the legacy scroll-pan case and carry no action claim to
    deduct against.
    """
    kinds = [str(a.get("kind") or "") for a in trace]
    effecting = [k for k in kinds if k in EFFECTING_ACTION_KINDS]
    return {
        "has_effecting": bool(effecting),
        "only_non_effecting": bool(kinds) and not effecting,
        "any_failed": any(not a.get("ok", True) for a in trace),
        "any_required_failed": any(
            a.get("must_succeed") and not a.get("ok", True) for a in trace
        ),
        "kinds": sorted(set(k for k in kinds if k)),
    }


class SessionExpiredError(RuntimeError):
    """Raised when a navigation is bounced to a sign-in page.

    Distinct from :class:`ActionAssertError` because the cause is entirely
    different and so is the fix: nothing is wrong with the spec, the
    recorder's stored session simply died. Without this the run reads as a
    spec bug — the recorder happily films the login page and dies several
    scenes later on a selector that "isn't there", after burning the whole
    capture.
    """


class ActionAssertError(RuntimeError):
    """Raised by ``execute_action`` when ``must_succeed=true`` on a failing action.

    Lets a spec opt into fail-loud behavior on a per-action basis — for the
    critical "without this, the rest of the scene is gibberish" steps —
    while keeping the default behavior of "log + continue, one bad step
    must not kill the render".
    """
