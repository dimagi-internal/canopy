"""Canonical Pydantic v2 schemas for the neutral narrative substrate.

These models are methodology-agnostic: the narrative spine + evidence gaps
(``WhyBrief``), the demo spec (``UnifiedSpec`` / ``Scene`` / ``Action``), the
eval verdict (``Verdict``), and the review package (``ReviewRequest`` and
friends). ``scripts.ddd`` orchestrates them, and so can other consumers (an
ACE AI-video pipeline, ace-web) — directly in Python, or via TS types generated
from the JSON Schema published under ``scripts/narrative/schema/json/``.

The DDD-only ``RunState`` (the converge lifecycle) lives in
``scripts.ddd.schemas.models``, which re-exports everything here so existing
``from scripts.ddd.schemas.models import X`` importers keep working.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ---------------------------------------------------------------------------
# Gates — the human pause points in the DDD loop
# ---------------------------------------------------------------------------


class Gate(str, Enum):
    """The three human-pause gates, as a single source of truth.

    A ``str`` subclass so a member drops in anywhere the old string literal was
    expected — ``ReviewRequest(gate=Gate.CONCEPT_CHANGE)``, equality with the
    literal (``review.gate == "concept_change"``), and JSON serialization all
    keep working without a ``.value``.

      * ``concept_change``   — narrative-agreement gate (the irreplaceable-taste
        pause before any build): ddd-narrative-review.
      * ``product_findings`` — per-iteration findings review (review_mode: human):
        ddd-findings-review. A run-child, not a narrative version.
      * ``external_release`` — publish-this-docs-page gate before a public release:
        ddd-upload.
    """

    CONCEPT_CHANGE = "concept_change"
    PRODUCT_FINDINGS = "product_findings"
    EXTERNAL_RELEASE = "external_release"


# ---------------------------------------------------------------------------
# SP0.1 — WhyBrief schema
# ---------------------------------------------------------------------------


class Evidence(BaseModel):
    kind: Literal["documented", "implemented", "assumed"]
    ref: str


class SpineItem(BaseModel):
    id: str
    claim: str
    rationale: str
    evidence: list[Evidence] = []
    status: Literal["grounded", "gap"] = "gap"


class Gap(BaseModel):
    """Something the narrative needs that does not exist yet.

    ``resolved`` is how a gap gets CLOSED without being deleted. Once the work
    lands, the gap is still the record of why it happened — deleting it to make
    a beat read ``built`` throws away the reasoning, and leaving it open makes
    the beat read ``new`` forever no matter what shipped. Neither is acceptable
    on a narrative that is refreshed across cycles.

    Set ``resolved`` to a short statement of what closed it (ideally naming the
    commit, PR, or module). :func:`scripts.ddd.narrative._beat_status_map`
    ignores resolved gaps when deciding whether a beat is still frontier.
    """

    id: str
    type: Literal["RESEARCH", "CAPABILITY", "DECISION"]
    claim_ref: str
    detail: str
    proposed_action: str
    resolved: str = ""


class WhyBrief(BaseModel):
    schema_version: int = 1
    narrative_slug: str
    problem: str
    spine: list[SpineItem]
    gaps: list[Gap]


# ---------------------------------------------------------------------------
# SP0.2 — Remaining models
# ---------------------------------------------------------------------------


class Persona(BaseModel):
    name: str
    role: str
    color: str
    intro: str
    org: str = ""  # the organization this individual belongs to (e.g. "Dimagi", "LLO")


class Feature(BaseModel):
    """A single buildable, verifiable capability within a scene (DDD v3)."""

    id: str
    description: str  # concrete buildable unit — what to implement
    verify: str       # how to validate it's done (API assertion, UI state, test command)


class Finding(BaseModel):
    """One judge-emitted design finding (the ddd-concept-eval contract shape).

    The **judge owns ``severity``** (concept judge: score<=1->high, ==2->medium,
    ==3->low). Downstream consumers (``findings_review``) must treat a present
    ``severity`` as authoritative and only fall back to a heuristic
    (``derive_severity``) when it's absent — the judge has the artifact context a
    route+fix_kind heuristic lacks.

    ``route`` is one of PRODUCT / CONCEPT / RESEARCH / DEFER; ``fix_kind`` is
    mechanical / options / redesign. ``fix_recommendation`` is optional (a
    finding can describe the problem without prescribing the fix).
    """

    scene: str
    dimension: str
    route: str
    fix_kind: str
    severity: str
    detail: str
    fix_recommendation: str = ""


# Action verbs the recorder understands. Single source of truth — the recorder
# imports this tuple, and the ``Action.kind`` Literal below is unpacked from
# it so Pydantic validation and the dispatcher vocabulary can never drift.
# Keep ordering loosely by frequency-of-use (navigation + click first).
ACTION_KINDS: tuple[str, ...] = (
    "goto",        # navigate to a url (target=url)
    "click",       # click a visible text label or CSS selector (target)
    "click_menu",  # click an item inside the currently-open dropdown (target=item text)
    "fill",        # focus a field (target=label/selector) and type value
    "select",      # pick from a native <select> (target=select, value=attr/index/label)
    "type",        # type value into whatever is focused
    "press",       # press a key (value, e.g. "Enter")
    "hover",       # glide the cursor onto target and rest (no click)
    "scroll_to",   # smooth-scroll target into view
    "scroll",      # scroll the page (value: "bottom" | "top" | "<px>")
    "wait_for",    # wait for target text/selector to appear, or value=ms
    "hold",        # dwell in place for seconds (value or seconds)
    "draw",        # draw a polygon on a map/canvas (target=element, points=[[fx,fy],...] fractions)
    "map_click",   # click a NAMED Mapbox feature (target=feature name; layer/source override defaults)
    "map_zoom",    # fly the Mapbox camera to a zoom level (zoom=level; seconds=animation length)
    "capture",     # read an id off the live page into ${var} for LATER scenes (source=url|element)
    "snapshot",    # write the canonical scene_<N>.png NOW — a chosen mid-scene frame, not the end frame
)


class _ActionBase(BaseModel):
    """Shared fields for every action verb.

    ``extra="forbid"`` is the whole point of the discriminated union — a spec
    that puts ``target`` on a ``type`` action (or ``value`` on a ``click``)
    used to silently no-op at runtime; now it fails validation with the
    field name and the action it lives on. The dispatcher (``execute_action``)
    still accepts the raw dict at runtime so legacy callers that bypass the
    schema keep working.
    """

    model_config = ConfigDict(extra="forbid")

    note: str | None = None
    """Human note: what this step demonstrates (shown in render logs)."""

    say: str | None = None
    """The narration word this action's moment should land on.

    The authored half of action↔word sync: ``build_action_marks`` anchors the
    mark on this word so the warp lands the field on the instant it is spoken,
    and ``lint_narration_binding`` checks the hint actually appears in the
    scene's narration. Without it a mark falls back to guessing from field-id
    and note tokens.

    It was READ in four places in ``scripts.ddd.snippets`` and declared in
    none, so ``extra="forbid"`` rejected every spec that used it — 47 uses
    across the live recipes. The non-validating loader took them fine, so the
    recorder and the renderer worked while every VALIDATING path
    (``load_spec``, ``narrative post``, ``recipe_preflight``, ``spec_qa``)
    failed on a field the system's own docs tell authors to write.
    """

    word: str | None = None
    """Older spelling of :attr:`say`. ``_mark_words`` still prefers it, so it
    is declared rather than silently rejected; new specs should use ``say``."""

    must_succeed: bool = False
    """When True, the recorder raises (not just logs) if this action fails.

    Use for the "without this, the rest of the scene is nonsense" steps —
    the bulk-create button click whose state is the whole rest of the demo.
    Defaults to False (log + continue, one bad action never aborts the render).
    """

    timeout_ms: int | None = None
    """Per-action override of the recorder's interaction/navigation timeout.

    Use on ``must_succeed`` clicks whose post-click navigation does slow
    server-side work (a publish/submit POST that mints records before
    redirecting): Playwright's ``locator.click`` waits for the scheduled
    navigation inside the same timeout, so a slow POST aborts an otherwise
    healthy render at the global 6s default. ``None`` (default) keeps the
    RecorderConfig value; the recorder takes ``max(timeout_ms, config default)``
    so an override can only loosen, never tighten below the preset.
    """


class GotoAction(_ActionBase):
    """Navigate to a URL. ``target`` is the URL (absolute or path-relative)."""

    kind: Literal["goto"]
    target: str


class ClickAction(_ActionBase):
    """Click a visible text label or CSS selector.

    ``target`` supports the recorder's prefix syntax: ``css:#sel``,
    ``testid:foo``, ``aria:Foo``, ``role:button``, ``text:Foo`` (force the
    visible-text path). Bare strings use a heuristic — CSS-shaped → selector
    engine; English → visible-text ranking.
    """

    kind: Literal["click"]
    target: str


class ClickMenuAction(_ActionBase):
    """Click an item inside the currently-open dropdown / popover.

    Same target syntax as :class:`ClickAction`. Distinct verb because menus
    usually have shorter post-click settle than a top-level button.
    """

    kind: Literal["click_menu"]
    target: str


class FillAction(_ActionBase):
    """Focus a field (``target``) and type ``value`` character-by-character.

    Typing fires real ``input`` events — reactive form widgets that gate
    buttons on debounced input (e.g. the bulk-create line counter) WILL
    react to ``fill`` but won't react to a raw ``.value = ...`` setter.
    """

    kind: Literal["fill"]
    target: str
    value: str


class SelectAction(_ActionBase):
    """Pick an option from a native ``<select>``.

    ``value`` is interpreted as the option's ``value`` attribute first, then
    a digit-only string as the 0-based ``index``, then the visible text
    label. The recorder glides the cursor onto the select so the viewer
    sees which control is being driven (the dropdown won't visually open —
    native-control limitation).
    """

    kind: Literal["select"]
    target: str
    value: str


class TypeAction(_ActionBase):
    """Type ``value`` into whatever element currently has focus.

    No ``target`` — that's what :class:`FillAction` is for. Use ``type``
    only after an explicit focus (or right after ``fill`` to extend the
    text).
    """

    kind: Literal["type"]
    value: str


class PressAction(_ActionBase):
    """Press a keyboard key. Defaults to Enter — the most common case."""

    kind: Literal["press"]
    value: str = "Enter"


class HoverAction(_ActionBase):
    """Glide the cursor onto ``target`` and rest. No click.

    ``seconds`` overrides the default dwell — useful when the demo is
    showing a tooltip or hover-revealed control that needs time to appear.
    """

    kind: Literal["hover"]
    target: str
    seconds: float | None = None


class ScrollToAction(_ActionBase):
    """Smooth-scroll the element matching ``target`` into view."""

    kind: Literal["scroll_to"]
    target: str


class ScrollAction(_ActionBase):
    """Scroll the page. ``value`` is ``"top"``, ``"bottom"``, or a pixel offset."""

    kind: Literal["scroll"]
    value: str = "bottom"


class WaitForAction(_ActionBase):
    """Wait for ``target`` (text or selector) to appear.

    All-digits target is treated as a millisecond pause. Plain-text targets
    skip the selector engine (which would otherwise sit through its full
    timeout before falling back) — see the recorder's
    ``_lib/targets.wait_for_target``.

    ``seconds`` is a per-action timeout override. The recorder's default
    ``wait_for`` timeout is ``RecorderConfig.wait_for_timeout_ms`` (12s);
    when an author knows a particular condition might take longer (an SSE
    bulk-create stream that runs 30-90s) the spec can say
    ``seconds: 120`` to wait up to two minutes — and the recorder exits
    the moment the target appears, instead of holding blindly. The
    alternative — padding with a fixed ``hold`` after a normal-timeout
    ``wait_for`` — guarantees 100+ seconds of dead-air on a clip if the
    condition resolves early. ``None`` preserves the default.
    """

    kind: Literal["wait_for"]
    target: str
    seconds: float | None = None


class HoldAction(_ActionBase):
    """Dwell in place for ``seconds``.

    The single-purpose pause: framing time after a layout, reading time
    after a render, slack so the SSE stream finishes flushing.
    """

    kind: Literal["hold"]
    seconds: float


class DrawAction(_ActionBase):
    """Draw a polygon on a map or canvas by clicking a sequence of points.

    The recorder has no way to express map drawing through the other verbs —
    ``click`` resolves a DOM element's centre, but a Mapbox-GL-Draw polygon (or any
    canvas drawing tool) needs clicks at *coordinates on the canvas*, not on a
    labelled element. ``draw`` fills that gap.

    ``target`` is the map/canvas element (e.g. ``css:#review-map``). ``points`` is a
    list of ``[fx, fy]`` fractional positions (0-1) within that element's bounding
    box — fractions, not pixels, so the polygon is independent of viewport size. The
    synthetic cursor glides to each vertex and clicks (real Playwright pointer events
    the drawing tool receives), then double-clicks the last vertex to close the
    polygon (Mapbox finishes a polygon on double-click).

    Set ``tool`` to the draw-tool button (e.g. ``css:.mapbox-gl-draw_polygon``) and
    ``draw`` activates it first with a coordinate mouse-click — which works on the
    small map-control buttons that a normal ``click`` can't (Playwright's
    actionability checks time out on them). Omit ``tool`` if the tool is already
    active.
    """

    kind: Literal["draw"]
    target: str
    points: list[tuple[float, float]]
    tool: str | None = None


class MapClickAction(_ActionBase):
    """Click a NAMED Mapbox feature on the main map by its ``name`` property.

    The labelled-element verbs can't express it: a ward polygon is a feature INSIDE
    the map canvas with no DOM node of its own. ``map_click`` finds the map in the
    page (``window.__review.map`` for the microplans editor, else any map-shaped
    global), looks up the feature whose ``name`` equals ``target`` on the boundary
    FILL layer (falling back to the source when it's loaded-but-not-painted),
    computes a point guaranteed to lie INSIDE the polygon (a concave-safe interior
    point, not a naive centroid), projects it to screen pixels, and dispatches a REAL
    cursor click there — so the app's own ``map.on('click', FILL, …)`` handler fires
    and the boundary is added, exactly as if a person clicked the ward.

    ``target`` is the feature name (e.g. ``Attakar``); ``${var}`` substitution
    applies. ``layer`` / ``source`` override the microplans defaults
    (``mp-admin-fill`` / ``mp-admin``) for other maps. Set ``must_succeed: true``
    when the rest of the scene is nonsense without the boundary added — the recorder
    then aborts cleanly if the named feature can't be resolved.
    """

    kind: Literal["map_click"]
    target: str
    layer: str | None = None
    source: str | None = None


class MapZoomAction(_ActionBase):
    """Fly the main Mapbox map to a ``zoom`` level — a cinematic push-in / pull-out.

    The camera-move sibling of ``map_click``: finds the map via the same resolver
    and calls ``map.flyTo({zoom, duration})``. Use it to push in and reveal the
    individual building footprints (the households drawn from Overture's global
    open-buildings dataset) then pull back to the whole ward. ``zoom`` is the
    target Mapbox zoom (≈16.5 for rooftops, ≈13 for the ward); ``seconds`` (the
    shared field) sets the animation length (default 2s) and the recorder waits it
    out so the footage shows the move.
    """

    kind: Literal["map_zoom"]
    zoom: float
    seconds: float | None = None  # flyTo animation length (default ~2s)


class CaptureAction(_ActionBase):
    """Read a value off the live page into ``${var}`` for LATER scenes/actions.

    This is the half of the late-binding contract that runs ON CAMERA: a demo
    can mint an entity during recording (click "Create", which navigates to
    ``/solicitations/207/``), ``capture`` reads the new id off the resulting
    page, and every scene/action AFTER this one resolves ``${solicitation_id}``
    to ``207``. No fixed IDs, no per-render state resets — each render mints a
    fresh entity and threads its real id forward. (Contrast ``setup.outputs``,
    which only knows ids minted BEFORE the render starts.)

    ``var`` is the variable name to bind (a Python identifier — the same syntax
    ``${var}`` substitution uses). The captured value overrides nothing from
    ``setup.outputs`` unless the names collide, in which case the captured value
    wins (and the recorder warns) — the on-camera value is the fresher truth.

    Two sources:

    - ``source: url`` (default) — read the current page URL. ``pattern`` is a
      REQUIRED regex whose capture group 1 is the value (e.g.
      ``'/solicitations/(\\d+)/'`` against ``/solicitations/207/`` binds ``207``).
    - ``source: element`` — read from a DOM node. ``target`` resolves the node
      (same prefix syntax as ``click``: ``css:``/``testid:``/``text:``/…).
      ``attr`` names an attribute to read (e.g. ``href``); omit ``attr`` to read
      the element's text. ``pattern`` is OPTIONAL here — when given, group 1 is
      extracted from the attr/text; when omitted, the whole trimmed attr/text is
      the value.

    The captured value is always trimmed. If nothing is captured (URL/attr/text
    absent, ``pattern`` has no group, or the regex doesn't match) the capture
    FAILS. ``must_succeed`` (inherited, but defaulting to ``True`` for capture —
    see the recorder) decides whether a failed capture aborts the render: a
    later scene that navigates to ``/solicitations/${solicitation_id}/`` with an
    unbound var would film a literal ``${...}`` URL, so capture is load-bearing
    by default.
    """

    kind: Literal["capture"]
    var: str
    source: Literal["url", "element"] = "url"
    target: str | None = None
    attr: str | None = None
    pattern: str | None = None
    # A capture is load-bearing by design — a later ${var} that never bound
    # films a literal placeholder URL. Default True (override per-action to
    # make a capture best-effort), unlike the _ActionBase default of False.
    must_succeed: bool = True


class SnapshotAction(_ActionBase):
    """Write the canonical ``scene_<N>.png`` NOW — a chosen mid-scene frame.

    ``scene_<N>.png`` is normally the scene's END frame, and a ``goto`` is
    hoisted to scene-init, so a moment the scene then dismisses (e.g. an
    AI-result panel before the firm submits) can't otherwise be the judged
    still without splitting the scene (which breaks the flow). Place this action
    at the moment you want captured — frame it with the preceding
    ``wait_for``/``scroll_to`` and usually set the scene ``full_page: false`` so
    the capture is the current viewport. The end-of-scene snapshot is then
    suppressed for this scene.
    """

    kind: Literal["snapshot"]


# Discriminated union: Pydantic picks the right subclass from ``kind`` alone.
# Existing YAML specs (lists of dicts with ``kind: ...`` + the verb's fields)
# validate against this without any spec edits — all real-world action shapes
# I surveyed across 38 specs already match the strict per-verb classes.
Action = Annotated[
    Union[
        GotoAction, ClickAction, ClickMenuAction, FillAction, SelectAction,
        TypeAction, PressAction, HoverAction, ScrollToAction, ScrollAction,
        WaitForAction, HoldAction, DrawAction, MapClickAction, MapZoomAction, CaptureAction,
        SnapshotAction,
    ],
    Field(discriminator="kind"),
]


# Tuple form of the action classes — used by the single-source guard test
# that asserts ACTION_KINDS, the Pydantic kind Literals, and the dispatch
# table never drift.
ACTION_CLASSES: tuple[type[_ActionBase], ...] = (
    GotoAction, ClickAction, ClickMenuAction, FillAction, SelectAction,
    TypeAction, PressAction, HoverAction, ScrollToAction, ScrollAction,
    WaitForAction, HoldAction, DrawAction, MapClickAction, MapZoomAction, CaptureAction,
    SnapshotAction,
)


class Scene(BaseModel):
    id: str = ""
    """Stable, permanent identity for this scene.

    The join key between the web-owned narrative and the local render recipe,
    and the key canopy-web's vN→vN+1 diff pairs on. Authored once and never
    changed: renaming an id is deleting a scene and adding another, and reads
    that way in every diff.

    Defaults to "" only so pre-L0 specs still load; ``spec_qa`` rejects a spec
    whose scenes have no ids, and ``scripts.ddd.identity.scene_id`` falls back
    to the title slug for exactly as long as it takes the backfill to run.
    """
    persona: str
    title: str
    show: str
    concept_claim: str
    provenance: str
    role: Literal["demo", "overview"] = "demo"
    """Scene classification. ``demo`` (the default) — a scene that shows a
    buildable feature being operated; spec_qa requires ≥1 ``Feature`` with a
    concrete ``verify``. ``overview`` — a context / goal-setting beat (the
    opening "why") that establishes the WHY before the how. An ``overview`` scene
    demonstrates no capability, so it is EXEMPT from the feature requirement and
    is judged on whether it clearly establishes the goal rather than on a feature.
    It STILL carries ``concept_claim`` + ``provenance`` (typically a goal
    SpineItem in the why_brief), so the dev-facing rationale and the viewer-facing
    opening stay linked. Keep overview scenes rare (usually exactly one, first):
    a video that is all overview demonstrates nothing."""
    design_intent: str | None = None
    impressive_because: str | None = None
    features: list[Feature] = []
    actions: list[Action] = []
    """Scripted cursor interactions for the recorder — see ``Action``. Optional:
    a scene with no ``actions`` falls back to the legacy scroll-pan. Declaring
    actions is what makes a rendered demo show the feature being operated (and is
    what lifts the ``feature_use`` score off the floor)."""
    url: str | None = None
    """Optional explicit starting URL for this scene (absolute or path-relative
    to the spec's ``base_url``).

    Resolution order in the recorder:
      1. This ``url`` field — the cleanest authoring path; declarative.
      2. The first ``goto`` in ``actions`` — implicit, but easy to read.
      3. ``None`` — the recorder stays on the previous scene's ending URL.

    The ``None`` default makes multi-scene narratives work without a hardcoded
    URL map: "scene 2 clicks a link that navigates → scene 3 continues from
    there" needs no URL on scene 3. Authors who want a hard reset between
    scenes use either this ``url`` or an explicit ``goto`` action."""
    viewport: dict[str, int] | None = None
    """Optional per-scene viewport override (``{"width": int, "height": int}``).

    Default ``None`` → the scene renders at the spec-level
    ``video_viewport_width`` × ``video_viewport_height``. Set this on a single
    dense scene that needs more room (a Mapbox-heavy plan-review with a wide
    inspector panel, for example) without inflating the whole recording — the
    other scenes keep their original size and the spec author doesn't bump
    five scenes to fix one.

    Recording-canvas note: the mp4's frame size is fixed at context creation
    (``record_video_size``) — Playwright cannot change it mid-stream. A
    per-scene viewport override changes the LAYOUT viewport (and the page's
    CSS pixel dimensions) for that scene only, with the frame letterboxed /
    re-fitted into the spec-level canvas. After the scene's ``final_hold_ms``
    the recorder restores the spec-level viewport so subsequent scenes are
    unaffected.

    Authoring example::

        scenes:
          - title: "Dana drills into the plan map"
            url: "/microplans/program/133/plan/3536/review/"
            viewport: { width: 1440, height: 900 }  # this scene needs more room
            actions: ...
    """
    full_page: bool | None = None
    """Optional per-scene snapshot capture mode. Default ``None`` → full-page
    screenshot. Set ``false`` for a page that is a tall TABLE plus a map/chart
    (e.g. the plan-review page) so the snapshot is just the viewport and the
    map/chart is the hero — a full-page capture of such a page yields a 16,000px
    strip in which the map is a ~4% sliver (looks blank but rendered fine). A
    full-viewport map page (a group overlay map) captures correctly either way,
    so this only matters for table-dominant pages. WebGL/Mapbox itself renders +
    composites headlessly via the recorder's SwiftShader flags given a long
    enough ``hold`` for tiles to paint — see ddd-spec "Map / WebGL scenes"."""
    pace: Literal["teach", "flow"] | None = None
    """Optional per-scene tempo. ``teach`` (the default — ``None`` is treated as
    ``teach``) gives the beat full read-time pacing: deliberate holds, settles,
    and cursor glide, for a UI/concept the viewer is meeting for the FIRST time.
    ``flow`` marks a beat where the feature is already established and this scene
    is just CONTINUITY — the recorder compresses it (holds/settles clamped to a
    small ceiling, the post-nav settle dropped, the cursor ~1.8x faster) so it
    reads as brisk forward motion, not another lesson. Pair ``flow`` with terse
    or no narration. Compression is per-scene and applied by
    ``scripts.walkthrough._lib.config.apply_scene_pace``; teach scenes record
    byte-for-byte as before this field existed."""

    narrative: str = ""
    """Canonical per-scene narrative text — the story beat the reviewer reads.
    May be one OR MORE sentences (per gap-flexible-scene-length). When set, it
    takes precedence over sentence-split-of-spec.narrative-by-position for both
    the review UI and the apply-edits writeback. When empty (legacy / first
    edit not yet made), the renderer falls back to splitting ``UnifiedSpec.narrative``
    by sentence and taking the i-th sentence."""


class SetupBlock(BaseModel):
    """Data-setup contract: the command that puts the world in a recordable state.

    Demos backed by synthetic data need (a) a synthetic-data generation step to
    run BEFORE rendering, and (b) the dynamic IDs that step mints substituted
    into scene URLs / action targets (``${var}`` — see
    ``scripts.narrative.substitution``). Without this contract, specs hardcode
    IDs that silently go stale on every reseed — and demos that MUTATE state
    during recording (a manager flow that creates a real audit + task) film the
    wrong UI on every re-render unless the generator runs again first.
    """

    command: str
    """Shell command that generates/reseeds the demo data (the synthetic
    generator). Run with cwd = the git toplevel containing the spec file
    (``git rev-parse --show-toplevel`` from the spec's directory; falls back to
    the spec's directory outside a git repo) — so commands are written
    repo-root-relative, matching how humans run them."""

    outputs: str | None = None
    """Repo-root-relative path to a flat JSON object (string/number values) of
    variables the command emits. Parsed after the command runs; its keys
    resolve the spec's ``${var}`` placeholders. ``None`` for setup commands
    that mint nothing dynamic (pure reseed, stable URLs)."""

    rerun: Literal["per_render", "once"] = "per_render"
    """``per_render`` (default) runs the command before EVERY render
    invocation — required for state-mutating demos, where recording itself
    changes the world (the scene that creates an audit must find no audit on
    the next take). ``once`` skips the command when the outputs file already
    exists — for expensive, idempotent generators whose data survives
    re-renders."""

    timeout_seconds: int = 1200
    """Abort the render (loudly) if the setup command runs longer than this."""


class UnifiedSpec(BaseModel):
    name: str
    narrative: str
    base_url: str
    auth: dict | None = None
    setup: SetupBlock | None = None
    """Optional data-setup contract — see :class:`SetupBlock`. When present,
    the recorder runs ``setup.command`` before recording (honoring ``rerun``)
    and resolves ``${var}`` placeholders in scene URLs / action targets from
    ``setup.outputs``. When absent, any ``${...}`` placeholder in the spec is
    a hard error (catches misconfiguration before filming a literal
    ``${run_id}`` URL)."""
    prewarm: bool = False
    """Optional pre-warm pass (default off). When True, the recorder — after
    ``setup`` has run (so ``${var}`` placeholders are resolved) and auth is
    available, but BEFORE the recorded context exists — opens a separate
    NON-recorded browser context with the same cookies/storage_state and
    visits each unique resolved scene URL once, in spec order. This warms
    cold caches (first-hit page renders, remote image fetches, JIT/template
    warm-up) OFF CAMERA, so the filmed pass doesn't freeze-frame through
    them. Best-effort: a failing pre-warm page is logged and skipped, never
    fatal. CLI ``--prewarm`` / ``--no-prewarm`` overrides this value. See the
    walkthrough SKILL's "Recording time & dead space" section."""
    why_brief: str | None = None
    personas: dict[str, Persona]
    scenes: list[Scene]
    review_mode: Literal["autonomous", "human"] = "autonomous"
    """DEPRECATED + IGNORED — DDD has no modes. The orchestrator auto-applies
    every finding it can act on itself (`fix_kind: mechanical`) and posts a
    deep-linked review for what it genuinely can't decide (`options`/`redesign` +
    the two blocking gates). Kept only so old specs that still carry the key
    parse; the value has no effect. (Historical note below.)

    How judge findings are handled after each render+judge iteration.

    - ``autonomous`` (default): the orchestrator auto-applies PRODUCT findings
      with ``fix_kind: mechanical`` and only pauses on the standing gates
      (``concept_change``, ``external_release``) or a ``stop_unclear``
      options/redesign finding.
    - ``human``: after every judged iteration, ALL PRODUCT findings are posted
      to the canopy-web review surface as ONE ``product_findings`` review
      (``scripts.ddd.findings_review``) — clustered, each cluster carrying
      deck ``#scene-<N>`` and video ``#t=<seconds>`` evidence deep-links —
      and the user picks implement / skip / defer per cluster before the loop
      continues. Nothing is auto-applied.

    Lives on the spec (not the run) so the preference travels with the
    narrative artifact; ``narrative pull`` preserves it like the rest of the
    disk-only recipe."""
    narrative_locked: bool = False
    """When True, this narrative has been approved at the narrative-agreement gate
    and is durable INPUT — ddd-spec must NOT regenerate it, and a new run reuses
    the whole spec (narrative paragraph, every scene's narrative/show/design_intent/
    features/actions) verbatim. Only an explicit ``redraft`` clears the lock. Set by
    ``apply_narrative_edits`` on ``approve``; the flag lives in the spec file so it
    travels with the narrative artifact. See ``narrative.is_narrative_locked``."""
    narrative_locked_at: str | None = None
    """ISO-8601 timestamp the narrative was locked (set alongside narrative_locked)."""
    # Sync stamps (0.2.176). canopy-web is the source of truth for the narrative
    # (overview + scene beats + personas + build_order); these record which web
    # version this local spec was last hydrated-from / pushed-to, so `narrative
    # pull` can tell "web advanced" from "local edited" and refuse to clobber
    # local narrative edits that haven't been pushed. The hash covers ONLY the
    # web-owned narrative fields — editing the disk-only render recipe
    # (show/actions/url) never counts as a narrative change.
    tagline: str = ""
    """One plain-language sentence: what this is + who it's for. The uploaded docs
    page leads with it so a newcomer understands the feature before pressing play.
    The build-audience narrative/concept_claims are NOT a substitute (they carry
    internal jargon); this is the user-facing hook."""
    capabilities: list[str] = []
    """User-facing 'what you can do' bullets, phrased as the reader's benefits/outcomes
    (not the build-audience concept_claims). The docs page uses these for the
    capabilities section when present, falling back to concept_claims otherwise."""
    why_summary: str = ""
    """A short, plain-language 'why this matters' for the docs page (a couple of
    sentences, no internal jargon). The docs page uses this for the Why section when
    present, instead of the build-audience why_brief problem + spine."""
    getting_started: list[str] = []
    """Ordered, user-facing 'how do I start' steps for the docs page (what to run /
    do, in the reader's terms) — distinct from each scene's ``show`` (which is the
    demo's on-screen walkthrough, not adoption instructions)."""
    build_order: list[str] = []
    """Ordered list of scene-title slugs representing the tackle sequence.

    Empty = default to narrative (scene array) order.  Partial lists are
    allowed — unlisted scenes implicitly follow in scene order.  The slugs
    must match those produced by ``_title_slug(scene.title)`` in
    ``scripts.ddd.narrative``.
    """


class Dimension(BaseModel):
    score: float
    weight: float


# Out-of-chain fitness law (canopy#265 item 3, adopted from ACE's _eval-template):
# inflation risk is proportional to how close the grading anchor sits to the AI
# authoring chain. An eval that never touched live state (AI text graded against
# AI text) may pass, but can never claim excellence — its emittable overall_score
# is capped here, at the schema layer, so prompt drift can't erode the rule.
LIVE_STATE_UNVERIFIED_CAP: float = 4.0


class Verdict(BaseModel):
    schema_version: int = 1
    kind: str | None = None
    """Verdict family (concept | user_artifact | why | actionability | timing |
    video | ...). One unified shape for every judge (canopy#265 item 1) — the
    aggregator and convergence are generic over kinds, so adding a judge is data,
    not assembler code. ``scripts.ddd.verdicts.load_verdict`` stamps this (and the
    two fields below) from KIND_DEFAULTS when the emitter didn't."""
    gate: Literal["gating", "advisory"] = "advisory"
    """Whether this verdict participates in render-loop CONVERGENCE scoring.
    ``advisory`` verdicts are recorded and reported but a low score never blocks
    convergence (e.g. timing/video/why/actionability). Phase gates (why-eval's
    pass-to-proceed) are a separate mechanism — this field is convergence-only.

    Defaults to ``advisory`` (canopy#273 item 4): a legacy/unstamped verdict
    loaded via a bare ``model_validate`` must not become a gating, possibly
    unverified verdict that hard-blocks convergence forever. The safe direction:
    ``compute_convergence_all`` returns False when NO gating verdict is present,
    so convergence stays demonstrated, not defaulted. The two gating kinds
    (``concept``, ``user_artifact``) are stamped explicitly — by their skills at
    write time and by ``scripts.ddd.verdicts.KIND_DEFAULTS`` at load time."""
    live_state_verified: bool | None = None
    """Whether the grading anchor touched live state (rendered screenshots, the
    produced mp4, a probed repo) rather than AI-authored text. ``False`` caps
    overall_score at LIVE_STATE_UNVERIFIED_CAP and disqualifies the verdict from
    convergence gating. ``None`` = legacy/unknown (uncapped, for back-compat)."""
    calibration: Literal["calibrated", "provisional"] | None = None
    """Recorded rubric-calibration status (ACE's discipline): ``provisional``
    until the rubric passes a defect-fixture calibration run. ``None`` = the
    emitter predates the field."""
    dimensions: dict[str, Dimension]
    overall_score: float
    uncapped_overall_score: float | None = None
    """The pre-cap overall_score, recorded when the out-of-chain cap fired."""
    verdict: Literal["pass", "warn", "fail", "blocked"]
    blocking_reason: str | None = None
    fix_recommendation: str | None = None

    @model_validator(mode="after")
    def _cap_unverified_score(self) -> "Verdict":
        if (
            self.live_state_verified is False
            and self.overall_score > LIVE_STATE_UNVERIFIED_CAP
        ):
            self.uncapped_overall_score = self.overall_score
            self.overall_score = LIVE_STATE_UNVERIFIED_CAP
        return self


class Decision(BaseModel):
    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    id: str
    prompt: str
    options: list[str]
    recommended: str
    class_: str = Field(alias="class")


class NarrationItem(BaseModel):
    """One scene's narration entry in a ReviewRequest (DDD v3).

    Carries the scene's 1-based number (``scene``), its slug (``id``), the
    story-beat ``title``, the on-screen ``persona`` key, the editable story
    beat (``text`` = concept_claim), and the concrete buildable features
    declared by the spec's ``Scene.features[]``.  ``title``/``persona`` let
    the review surface render the cohesive multi-persona narrative instead of
    a generic "Scene N" label.
    """

    scene: int
    id: str
    title: str = ""
    persona: str = ""
    provenance: str = ""  # spine id this beat grounds — lets the surface co-locate grounding
    text: str
    features: list[Feature] = []
    status: Literal["built", "new"] = "new"
    """Whether this beat's underlying capability already exists (``built``) or is
    work still to do (``new``, the "frontier"). Derived (not authored) at
    review-build time from the why-brief, mirroring canopy-web's
    ``sceneIsFrontier``: a beat is ``new`` when its ``provenance`` spine item is a
    gap (status != ``grounded``) OR a why-brief gap references it by
    ``claim_ref``; otherwise ``built``. Lets the BUILD SEQUENCE panel label
    already-shipped beats so the reviewer is not asked to "build" what is already
    live. Defaults to ``new`` (safe: shows as to-do) when there is no why-brief or
    the provenance is absent from the spine."""


class ReviewRequest(BaseModel):
    schema_version: int = 1
    run_id: str
    narrative_slug: str = ""
    """The narrative this review belongs to — the explicit source of truth
    canopy-web files the review under (``request_json.narrative_slug``). Sending
    it decouples narrative identity from ``run_id`` slug-parsing, so a run whose
    ``run_id`` slug differs from its narrative_slug (e.g. after a mid-flow
    rename) still groups with the right narrative. Defaults to "" → canopy-web
    falls back to ``narrative_slug_from_run_id(run_id)``."""
    gate: str
    video: dict
    decisions: list[Decision]
    narration: list[NarrationItem | dict]
    narrative: str = ""
    """The cohesive demo narrative — the whole story the scenes decompose.

    Rendered at the top of the review surface so the reviewer reads the arc
    before the per-scene breakdown.  Populated from ``UnifiedSpec.narrative``.
    """
    personas: dict = {}
    """Persona key -> {name, role, color, intro, org}, so the surface can show who
    is on screen in each scene (multi-persona handoffs).  From ``UnifiedSpec.personas``."""
    why_brief: dict = {}
    """The resolved why-brief (problem, spine[], gaps[]) so the review surface can
    show + edit the grounding doc alongside the narrative.  Loaded by the caller
    from ``UnifiedSpec.why_brief`` (a path relative to the spec file)."""
    autonomous_audit: list[str] = []
    actionability: dict | None = None
    # ---- product_findings (run-child) gate fields ----------------------------
    # These four ride only on the ``product_findings`` gate's request_json (the
    # canopy ↔ canopy-web contract). They are absent/empty on every other gate.
    # The findings review is a RUN-CHILD, not a narrative version — it carries
    # NO ``narrative_slug`` (left ""), and canopy-web pins ``version=0`` for it.
    feature: str = ""
    """The feature/narrative slug this run belongs to (e.g. ``program-admin-report``).
    The findings review files under the RUN, keyed by ``run_id`` + ``feature`` —
    not under a narrative version. Empty on non-findings gates."""
    iteration: int = 0
    """1-based judged-iteration number this findings review is for. Lets the
    surface label "iteration N" and lets evidence resolve the right
    ``snapshots_iter<N>/`` capture set. Empty (0) on non-findings gates."""
    deck_url: str = ""
    """Hosted deck URL (``/w/<deck-id>``) supporting ``#scene-N`` anchors, used
    by ``clusters[].evidence[].deck_anchor`` deep-links. Empty on non-findings gates."""
    summary: dict = {}
    """``{concept_score, user_score, verdict}`` headline for the findings gate —
    the two judge overall_scores and the gating verdict. Empty on other gates."""
    findings: list[dict] = Field(default_factory=list, serialization_alias="clusters")
    """Machine-readable finding clusters for the ``product_findings`` gate.

    Serialized as ``clusters`` in request_json (the canopy ↔ canopy-web contract key);
    the Python attribute stays ``findings``. Construction by ``findings=`` is unaffected.

    Populated by ``scripts.ddd.findings_review.build_findings_review_request``:
    one entry per clustered PRODUCT finding in the contract shape — ``id``,
    ``title``, ``severity``, ``fix_kind``, ``route``, ``scenes`` (1-based spec
    indices), ``suggested_fix``, ``count``, and ``evidence`` (a list of
    ``{scene, thumb, deck_anchor, video_t}`` — an inline downscaled JPEG
    data-URI of that scene's screenshot, the deck ``#scene-<N>`` anchor, and the
    integer video offset in seconds). Empty for every other gate."""
    build_order: list[str] = []
    """Ordered list of scene-title slugs representing the user's chosen tackle sequence.

    Populated by ``build_narrative_review_request`` from ``spec.build_order``
    (or defaulted to scene order when the spec has no explicit order).  The
    editor returns this field in its response_json and ``apply_narrative_edits``
    persists it back onto the spec.
    """
