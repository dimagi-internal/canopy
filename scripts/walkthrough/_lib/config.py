"""Timing configuration for the walkthrough recorder.

Before this module the recorder's pacing was a constellation of magic numbers
scattered across the primitives — a 250ms here, a 900ms there, a 45ms typing
delay in three different places. Spec authors who wanted a faster-typing demo
or a longer post-click settle had no knob to turn; you had to fork the lib.

Now a single :class:`RecorderConfig` dataclass owns every timing constant. Three
presets — ``fast``, ``medium`` (default), ``slow`` — cover the common video
pacing choices. A spec can override any subset of fields via
``video_recorder_config: { typing_delay_ms: 20 }`` in the YAML.

Two layers of knobs:

- **Scene-level** (``initial_hold_ms``, ``final_hold_ms``, ``min_hold_ms``,
  ``scroll_speed_px_s``) — consumed by the orchestrator between actions.
- **Action-level** (``typing_delay_ms``, ``pre_click_dwell_ms``,
  ``post_click_settle_ms``, …) — consumed by individual primitives.

A primitive that wants to tune its own pacing reads from ``self.config`` (or
the ``config=`` kwarg). A primitive that takes ``config=None`` defaults to
:class:`RecorderConfig` (medium pace) so existing call sites keep working.

THE TIMING MODEL — one map, so the next person doesn't do archaeology
======================================================================

The authoritative author-facing doc is the **"Recording time & dead space"**
section of ``plugins/canopy/skills/walkthrough/SKILL.md``. Code-side summary:

**Off camera** (never films): the spec's ``setup:`` command (runs before any
browser opens) and the ``prewarm`` pass (a separate non-recorded context that
visits each unique scene URL to heat cold caches — ``UnifiedSpec.prewarm`` /
``--prewarm``, knobs ``prewarm_settle_ms`` + ``prewarm_page_timeout_ms``).

**On camera** (every millisecond is film): scene navigation + ``goto_settle_ms``,
``initial_hold_ms``, every action's glide/dwell/settle, ``wait_for`` polling
time, ``hold`` actions, and the end-of-scene final hold.

**Dwell hierarchy** (which knob holds a frame, in precedence order):

1. ``hold`` actions — explicit mid-scene dwells; the recommended way to let a
   viewer sit with a moment (placed exactly where the moment is).
2. ``scene.video_hold_seconds`` — legacy per-scene override of the
   end-of-scene hold (replaces ``final_hold_ms`` for that scene only).
3. ``final_hold_ms`` — the global end-of-scene floor, from the pace preset /
   ``video_recorder_config``.

**Dead-space eliminations** (automatic): a leading ``wait_for`` skips both
``goto_settle_ms`` and ``initial_hold_ms`` (the wait IS the settle); a no-nav
scene skips ``initial_hold_ms``; redundant leading ``goto`` actions are
stripped; ``--skip-empty-scenes`` drops action-less scenes from the film;
``--skip-same-url`` skips re-navs; ``crossfade`` hides the nav white-flash.

**Accounting-only / dead knobs**: ``min_hold_ms`` only floors the REPORTED
per-scene seconds (the "~Ns of footage" total) — it does not pad the film.
``scroll_speed_px_s`` is retained for back-compat but unconsumed since the
static-scene scroll-pan fallback was removed (the ``scroll`` action's eased
scroll has its own internal duration cap).
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace

# --------------------------------------------------------------------------- #
# Scene pace — teach vs flow (per-scene tempo, orthogonal to video_pace)
# ===========================================================================
#
# ``video_pace`` (fast | medium | slow) is the WHOLE video's tempo — it picks a
# RecorderConfig preset. ``Scene.pace`` (teach | flow) is a PER-SCENE modifier
# layered on top of whatever preset is in force, so an author can mark which
# individual beats need full read-time and which are just connective tissue:
#
#   • teach  — explain the mechanic. Full read-time pacing: the viewer is
#              meeting this UI / concept for the first time, so holds, settles,
#              and a deliberate cursor glide all matter. This is the DEFAULT and
#              is byte-for-byte the pre-existing behavior — a scene with no
#              ``pace:`` field is a teach scene.
#
#   • flow   — the feature is already established; this beat just shows
#              CONTINUITY (navigate there, glance, move on). Compress it:
#              clamp the blind holds to a small ceiling, drop the post-nav
#              settle dwell, and move the cursor faster. Pair with terse or no
#              VO — a flow scene is a transition, not a lesson.
#
# Only ``flow`` transforms the config; ``teach`` / ``None`` / unknown return the
# config unchanged (fully backward-compatible). The transform is a pure function
# (:func:`apply_scene_pace`) so it's unit-testable without a browser.

#: The ceiling (ms) every blind hold is clamped to in a ``flow`` scene —
#: ``initial_hold_ms``, ``final_hold_ms``, ``goto_settle_ms``, the per-primitive
#: ``post_*_settle`` dwells, and an explicit ``hold`` action's duration (see
#: ``HOLD_ACTION_FLOW_CEILING_MS``). 600ms is long enough that a frame doesn't
#: read as a jump-cut but short enough to feel like brisk forward motion. Holds
#: already SHORTER than this (a fast-preset 400ms settle) are left alone — flow
#: only ever clamps DOWN, never pads up.
FLOW_HOLD_CEILING_MS: int = 600

#: ``goto_settle_ms`` is dropped to this in a flow scene — a flow beat doesn't
#: need the post-navigation dwell to let a first-time viewer orient; the
#: crossfade already hides the nav flash, so cut almost all of it.
FLOW_GOTO_SETTLE_MS: int = 200

#: Explicit ``hold`` actions in a flow scene are clamped to this ceiling. An
#: author who dropped a ``hold 4`` in a beat they later mark ``flow`` shouldn't
#: get a 4s freeze in a connective scene — but a hold is still an INTENTIONAL
#: dwell, so flow keeps a touch more than the generic ``FLOW_HOLD_CEILING_MS``.
HOLD_ACTION_FLOW_CEILING_MS: int = 700

#: Cursor travel speed multiplier for a flow scene. ``slow_move`` animates the
#: glide one mouse-move step at a time, so FEWER steps == a FASTER-reading
#: cursor. 1.8x means ``cursor_steps`` (and the short re-centre count) are
#: divided by 1.8 (floored at a small minimum so the glide still animates rather
#: than teleports). Within the requested 1.6–2x band.
FLOW_CURSOR_SPEEDUP: float = 1.8

#: Floor on the divided cursor-step counts — below this the glide reads as a
#: teleport (a jump-cut) instead of fast travel. Keeps flow brisk, not janky.
FLOW_CURSOR_STEPS_MIN: int = 6


def apply_scene_pace(config: "RecorderConfig", pace: str | None) -> "RecorderConfig":
    """Return a per-scene config for ``pace`` ∈ {``teach``, ``flow``, ``None``}.

    ``teach`` / ``None`` / any unknown value → the input config unchanged (so a
    spec that never sets ``pace`` records identically to before this field
    existed — full backward compatibility).

    ``flow`` → a compressed copy: every blind hold/settle clamped DOWN to
    :data:`FLOW_HOLD_CEILING_MS` (the post-nav settle to the tighter
    :data:`FLOW_GOTO_SETTLE_MS`), and the cursor-step counts divided by
    :data:`FLOW_CURSOR_SPEEDUP` (floored at :data:`FLOW_CURSOR_STEPS_MIN`) so the
    cursor travels ~1.8x faster. Clamps are ``min(...)`` — a hold already shorter
    than the ceiling (e.g. under the fast preset) is left as-is; flow never pads.

    Pure function — no I/O, no Page — so the pace→durations resolution is unit
    tested directly (see test_config_pace.py).
    """
    if pace != "flow":
        return config

    def _clamp(value: int, ceiling: int) -> int:
        return min(int(value), ceiling)

    def _faster_steps(steps: int) -> int:
        return max(FLOW_CURSOR_STEPS_MIN, int(int(steps) / FLOW_CURSOR_SPEEDUP))

    return replace(
        config,
        # scene-level blind holds → flow ceiling
        initial_hold_ms=_clamp(config.initial_hold_ms, FLOW_HOLD_CEILING_MS),
        final_hold_ms=_clamp(config.final_hold_ms, FLOW_HOLD_CEILING_MS),
        min_hold_ms=_clamp(config.min_hold_ms, FLOW_HOLD_CEILING_MS),
        # post-nav settle → tighter flow goto ceiling (crossfade hides the flash)
        goto_settle_ms=_clamp(config.goto_settle_ms, FLOW_GOTO_SETTLE_MS),
        # explicit `hold` actions get a (slightly looser) ceiling in flow scenes
        hold_action_ceiling_ms=HOLD_ACTION_FLOW_CEILING_MS,
        # per-primitive settle dwells → flow ceiling
        post_click_settle_ms=_clamp(config.post_click_settle_ms, FLOW_HOLD_CEILING_MS),
        menu_click_settle_ms=_clamp(config.menu_click_settle_ms, FLOW_HOLD_CEILING_MS),
        post_fill_settle_ms=_clamp(config.post_fill_settle_ms, FLOW_HOLD_CEILING_MS),
        post_select_settle_ms=_clamp(config.post_select_settle_ms, FLOW_HOLD_CEILING_MS),
        scroll_settle_ms=_clamp(config.scroll_settle_ms, FLOW_HOLD_CEILING_MS),
        glide_dwell_ms=_clamp(config.glide_dwell_ms, FLOW_HOLD_CEILING_MS),
        click_dwell_ms=_clamp(config.click_dwell_ms, FLOW_HOLD_CEILING_MS),
        pre_click_dwell_ms=_clamp(config.pre_click_dwell_ms, FLOW_HOLD_CEILING_MS),
        # faster cursor travel
        cursor_steps=_faster_steps(config.cursor_steps),
        cursor_steps_short=_faster_steps(config.cursor_steps_short),
    )


@dataclass(frozen=True)
class RecorderConfig:
    """Every timing knob for the recorder.

    Defaults match the medium-pace recorder behavior before the refactor —
    so changing nothing in a spec gets you the same video pace.
    """

    # ---- scene-level (consumed by orchestrator) --------------------------
    initial_hold_ms: int = 1500
    """Hold after navigating into a scene, before actions run."""

    final_hold_ms: int = 1000
    """Hold after a scene's actions finish, before moving to the next scene."""

    min_hold_ms: int = 4000
    """Floor on a scene's REPORTED elapsed time (the "~Ns of footage" total).
    Accounting only — it does not pad the recording with extra wait."""

    scroll_speed_px_s: int = 600
    """DEAD KNOB — unconsumed since the static-scene scroll-pan fallback was
    removed. Retained so external ``RecorderConfig(...)`` constructors and old
    ``video_recorder_config`` blocks don't break; scheduled for removal."""

    # ---- action-level: cursor motion -------------------------------------
    cursor_steps: int = 36
    """Mouse-move steps for a normal glide — enough to animate the cursor overlay."""

    cursor_steps_short: int = 10
    """Mouse-move steps for a final re-centring before a click."""

    cursor_path: str = "linear"
    """Shape of every cursor glide: ``linear`` (Playwright's straight,
    constant-speed move — the default, unchanged) or ``natural`` (a gentle arc
    with minimum-jerk timing, see ``_lib.cursor_path``). ``record_video`` turns
    ``natural`` on for ``style: recorded`` specs unless the spec's
    ``video_recorder_config`` says otherwise."""

    pre_click_dwell_ms: int = 250
    """Pause after the cursor lands on a click target, before mouse-down."""

    # ---- action-level: per-primitive settle ------------------------------
    glide_dwell_ms: int = 350
    """Default ``_glide_to`` dwell — the pause the cursor takes once it arrives."""

    click_dwell_ms: int = 500
    """Pre-click ``_glide_to`` dwell — slightly longer so the viewer registers it."""

    post_click_settle_ms: int = 900
    """Hold after a regular text/CSS click, for the UI to react."""

    menu_click_settle_ms: int = 700
    """Hold after a dropdown/popover menu item click (usually quicker than a button)."""

    pre_fill_dwell_ms: int = 200
    """Pause after the cursor lands on a fill target, before focusing the field."""

    post_fill_settle_ms: int = 300
    """Hold after a fill, for live updates (typeahead, debounced search) to render."""

    pre_select_dwell_ms: int = 200
    """Pause after the cursor lands on a ``<select>``, before flipping the option."""

    post_select_settle_ms: int = 300
    """Hold after picking an option, for the page to react to the change."""

    scroll_settle_ms: int = 600
    """Hold after a ``scroll_to`` or page-level scroll."""

    goto_settle_ms: int = 1200
    """Hold after a ``goto`` (navigation + brief idle)."""

    crossfade: bool = True
    """Crossfade the outgoing scene's frame over the incoming page to hide the
    browser's white navigation flash. Set False to disable (e.g. when debugging
    raw page state, or for specs where the flash is wanted)."""

    hold_action_ceiling_ms: int | None = None
    """Optional cap (ms) on an explicit ``hold`` action's duration. ``None`` (the
    default) = unbounded: a ``hold 4`` waits the full 4s, exactly as before. Set
    to a ceiling and any longer ``hold`` is clamped DOWN to it. Used only by a
    ``flow`` scene's per-scene config (``apply_scene_pace`` sets it to
    ``HOLD_ACTION_FLOW_CEILING_MS``) so a deliberate dwell in a beat later marked
    ``flow`` doesn't freeze a connective scene; teach scenes leave it ``None``."""

    # ---- action-level: native <select> reveal ----------------------------
    select_reveal: bool = True
    """Render a synthetic dropdown over a native ``<select>`` before committing
    the choice, so the recording actually SHOWS the options and which one is
    picked. Native OS select popups can't be screen-recorded; without this the
    closed widget just silently flips value."""

    select_reveal_dwell_ms: int = 1000
    """How long the synthetic dropdown stays open (cursor gliding to the chosen
    option) before the value commits and it closes — long enough for a viewer
    to read the options and see which one is picked."""

    # ---- action-level: typing --------------------------------------------
    typing_delay_ms: int = 45
    """Per-keystroke delay during ``fill`` / ``type`` so the typing is visible."""

    # ---- timeouts --------------------------------------------------------
    glide_timeout_ms: int = 6000
    """Max time ``_glide_to`` waits for the target to exist before giving up."""

    interaction_timeout_ms: int = 6000
    """Max time ``click_text`` / ``fill_field`` / ``select_option`` wait for the target."""

    menu_timeout_ms: int = 5000
    """Max time ``click_menu_item`` waits for the menu item to render."""

    wait_for_timeout_ms: int = 12000
    """Max time ``wait_for`` waits for text/selector to appear."""

    goto_timeout_ms: int = 60000
    """Max time ``page.goto`` waits."""

    load_settle_timeout_ms: int = 8000
    """Max time the orchestrator waits for the ``load`` event after ``domcontentloaded``."""

    # ---- pre-warm pass (off camera) ---------------------------------------
    prewarm_settle_ms: int = 4000
    """Bounded settle per pre-warm page after ``domcontentloaded`` — the page
    gets up to this long to go network-idle (image fetches, lazy charts) so
    its caches are actually hot, then the pass moves on. Exits early on idle."""

    prewarm_page_timeout_ms: int = 15000
    """Per-page cap on the pre-warm navigation. A page slower than this is
    logged as a pre-warm failure and skipped — prewarm is best-effort and must
    never stall the render the way it would stall the film."""

    # ---- presets ---------------------------------------------------------
    @classmethod
    def for_pace(cls, pace: str | None) -> RecorderConfig:
        """Return a preset config for ``pace`` in {``fast``, ``medium``, ``slow``}.

        Unknown / ``None`` → ``medium`` (the default dataclass values). The
        preset adjusts both scene-level pacing and action-level pacing so a
        ``"fast"`` video also types faster and settles shorter, not just hops
        between scenes faster.
        """
        if pace == "fast":
            return cls(
                initial_hold_ms=800, final_hold_ms=500, min_hold_ms=2500,
                scroll_speed_px_s=1200,
                pre_click_dwell_ms=150, click_dwell_ms=300, glide_dwell_ms=200,
                post_click_settle_ms=400, menu_click_settle_ms=350,
                pre_fill_dwell_ms=100, post_fill_settle_ms=150,
                pre_select_dwell_ms=100, post_select_settle_ms=150,
                scroll_settle_ms=300, goto_settle_ms=600,
                typing_delay_ms=20,
            )
        if pace == "slow":
            return cls(
                initial_hold_ms=2500, final_hold_ms=1500, min_hold_ms=6000,
                scroll_speed_px_s=300,
                pre_click_dwell_ms=400, click_dwell_ms=800, glide_dwell_ms=550,
                post_click_settle_ms=1400, menu_click_settle_ms=1000,
                pre_fill_dwell_ms=350, post_fill_settle_ms=600,
                pre_select_dwell_ms=350, post_select_settle_ms=600,
                scroll_settle_ms=1000, goto_settle_ms=2000,
                typing_delay_ms=80,
            )
        return cls()

    def with_overrides(self, overrides: dict | None) -> RecorderConfig:
        """Return a copy with fields in ``overrides`` replaced.

        Used to apply a spec's ``video_recorder_config`` block on top of the
        pace preset. Unknown keys are ignored (forward-compat for specs that
        target a newer recorder).
        """
        if not overrides:
            return self
        valid = {f.name for f in fields(self)}
        clean = {k: v for k, v in overrides.items() if k in valid}
        return replace(self, **clean) if clean else self
