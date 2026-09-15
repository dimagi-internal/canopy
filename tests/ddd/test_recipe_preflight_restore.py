"""Preflight must put the world back before the render films it (canopy#546).

Preflight applies state-changing actions on purpose, so a scene that depends on
an earlier click is checked against the screen it will really face. That makes
it a mutator, and it already knows so — it reseeds via ``setup.command`` before
its walk. But a reseed BEFORE the walk protects preflight from the PREVIOUS
preflight and protects the render from nothing: the world the render films is
the one this walk left behind.

Whether that matters is decided by ``record_video.run_setup``, which skips the
setup command when ``rerun: once`` and the outputs file exists. So the bug is a
shape, not a coincidence:

  * ``rerun: per_render`` (default) — the render reseeds, nothing is owed;
  * ``rerun: once``           — the render SKIPS setup and films the consumed
                                state. The payoff action is a no-op and every
                                signal says it worked: the action dispatches,
                                ``must_succeed`` passes, run-report is 10/10;
  * no setup command          — nothing can restore it, and preflight's old
                                comment ASSUMED such a recipe was non-mutating.

Reproduced three times on ``hh-poverty-targeting/20260827-0323`` and again on
``spark-fcap-facilitation-2026-09-08-001``, where both LLM judges independently
caught the manufactured defect and it cost the run its gating score.

Unit-level on purpose: the decision is a pure function of the spec, so it is
testable without chromium — which is the point, since the failure it prevents
is invisible to everything the render itself reports.
"""
from __future__ import annotations

from scripts.ddd.recipe_preflight import restore_plan, scenes_mutate

# The shape from the issue: a payoff scene whose whole point is that a control
# is live — `select testid:decision-halima_abubakar -> needs_audit`.
PAYOFF_SCENES = [
    {"id": "show-the-queue", "actions": [{"kind": "goto", "target": "/labs/par/7/"}]},
    {
        "id": "record-the-disposition",
        "actions": [
            {"kind": "wait_for", "target": "testid:decision-halima_abubakar"},
            {
                "kind": "select",
                "target": "testid:decision-halima_abubakar",
                "value": "needs_audit",
                "must_succeed": True,
            },
        ],
    },
]

READ_ONLY_SCENES = [
    {"id": "tour", "actions": [
        {"kind": "goto", "target": "/labs/par/7/"},
        {"kind": "hover", "target": "testid:row-1"},
        {"kind": "scroll_to", "target": "testid:footer"},
        {"kind": "wait_for", "target": "testid:total"},
        {"kind": "capture", "target": "testid:total"},
    ]},
]

RESEEDING = {"command": "uv run python reset_demo_state.py", "outputs": "realized.json"}


def test_rerun_once_is_the_bug_shape_and_preflight_restores():
    """THE regression. The render skips setup here, so preflight owes a restore."""
    plan = restore_plan({**RESEEDING, "rerun": "once"}, PAYOFF_SCENES)

    assert plan["mutates"] is True
    assert plan["render_reseeds"] is False, "record_video skips setup on rerun=once"
    assert plan["restore_after"] is True
    # It CAN restore, so this is not a warning — it is handled.
    assert plan["warning"] is None


def test_per_render_owes_nothing_so_the_generator_is_not_run_twice():
    """The default already reseeds after us. Restoring here would be pure cost."""
    plan = restore_plan({**RESEEDING, "rerun": "per_render"}, PAYOFF_SCENES)

    assert plan["mutates"] is True
    assert plan["render_reseeds"] is True
    assert plan["restore_after"] is False


def test_rerun_defaults_to_per_render_when_absent():
    """Same default as record_video.run_setup — not re-derived, matched."""
    assert restore_plan(RESEEDING, PAYOFF_SCENES)["rerun"] == "per_render"
    assert restore_plan(RESEEDING, PAYOFF_SCENES)["restore_after"] is False


def test_a_mutating_recipe_with_no_setup_command_is_a_loud_warning():
    """Preflight has no contract to restore with — it can only say so.

    The old comment assumed a recipe without a setup block was non-mutating.
    That is an assumption, and when it is wrong it is wrong silently.
    """
    plan = restore_plan(None, PAYOFF_SCENES)

    assert plan["mutates"] is True
    assert plan["render_reseeds"] is False
    assert plan["restore_after"] is False, "nothing to restore WITH"
    assert plan["warning"] and "setup.command" in plan["warning"]
    assert "canopy#546" in plan["warning"]


def test_an_empty_setup_command_counts_as_none():
    plan = restore_plan({"command": "   ", "rerun": "once"}, PAYOFF_SCENES)
    assert plan["restore_after"] is False
    assert plan["warning"] is not None


def test_a_read_only_recipe_owes_nothing_even_on_rerun_once():
    """No state-changing verb walked means nothing was consumed."""
    plan = restore_plan({**RESEEDING, "rerun": "once"}, READ_ONLY_SCENES)

    assert plan["mutates"] is False
    assert plan["restore_after"] is False
    assert plan["warning"] is None


def test_a_read_only_recipe_with_no_setup_block_is_not_warned_about():
    """The common non-mutating spec must stay quiet, or the warning is noise."""
    assert restore_plan(None, READ_ONLY_SCENES)["warning"] is None


def test_scenes_mutate_fires_on_every_state_changing_verb():
    for kind in ("click", "fill", "select", "press", "type"):
        assert scenes_mutate([{"actions": [{"kind": kind, "target": "t"}]}]) is True, kind


def test_scenes_mutate_ignores_camera_moves_and_navigation():
    """goto changes the page but persists nothing; scroll/hover/capture likewise."""
    for kind in ("goto", "scroll_to", "hover", "wait_for", "capture", "draw", "hold"):
        assert scenes_mutate([{"actions": [{"kind": kind, "target": "t"}]}]) is False, kind


def test_the_setup_block_may_be_a_model_not_a_dict():
    """SetupBlock is a pydantic model on a parsed spec, a dict on a raw one.

    Reading it as a dict only is the exact bug `_setup_block` was written for —
    the reseed silently never ran. The restore decision must not reintroduce it.
    """
    from scripts.narrative.models import SetupBlock

    block = SetupBlock(command="uv run python reset.py", outputs="realized.json", rerun="once")
    plan = restore_plan(block, PAYOFF_SCENES)

    assert plan["rerun"] == "once"
    assert plan["restore_after"] is True


def test_scenes_may_be_models_not_dicts():
    """Same duality one level down — spec.scenes are models on a parsed spec."""

    class _Action:
        def __init__(self, kind):
            self.kind = kind

    class _Scene:
        def __init__(self, kind):
            self._kind = kind

        def model_dump(self):
            return {"actions": [{"kind": self._kind, "target": "t"}]}

    assert scenes_mutate([_Scene("click")]) is True
    assert scenes_mutate([_Scene("hover")]) is False


def test_malformed_scenes_do_not_crash_the_decision():
    """A preflight that raises here would block a spec it merely cannot judge."""
    assert scenes_mutate([]) is False
    assert scenes_mutate(None) is False
    assert scenes_mutate([{"actions": None}]) is False
    assert scenes_mutate([{}]) is False
    assert scenes_mutate([{"actions": [None]}]) is False


# --- The drift guard -------------------------------------------------------
#
# `render_reseeds` is a CLAIM about another module's behaviour: that
# record_video.run_setup skips the command on rerun=once-with-outputs and runs
# it otherwise. recipe_preflight.py's own comments record that three separate
# re-derivations of the setup contract (the cwd, the outputs load, the url
# join) had already drifted, and each drift turns preflight into a check of a
# world the render will not see. So this is asserted against the real function,
# not against a copy of its rule.


def _run_setup_skipped(tmp_path, *, rerun, outputs_exists):
    """Ask record_video.run_setup itself whether it would skip."""
    from scripts.walkthrough.record_video import run_setup

    outputs = tmp_path / "realized.json"
    if outputs_exists:
        outputs.write_text("{}")
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text("scenes: []\n")
    provenance = run_setup(
        {"command": "true", "outputs": "realized.json", "rerun": rerun},
        spec_path,
    )
    return provenance["skipped"]


def test_render_reseeds_matches_what_record_video_actually_does(tmp_path, monkeypatch):
    """Not 'equivalent logic' — the real run_setup, asked directly."""
    from scripts.walkthrough import record_video

    # run_setup resolves its cwd from the git toplevel holding the spec; pin it
    # to tmp_path so `outputs:` resolves against the file we just wrote.
    monkeypatch.setattr(record_video, "resolve_setup_cwd", lambda _p: tmp_path)

    # rerun=once with outputs present: the render SKIPS -> preflight must restore.
    assert _run_setup_skipped(tmp_path, rerun="once", outputs_exists=True) is True
    assert restore_plan({**RESEEDING, "rerun": "once"}, PAYOFF_SCENES)["render_reseeds"] is False

    # per_render: the render RUNS it -> preflight owes nothing.
    assert _run_setup_skipped(tmp_path, rerun="per_render", outputs_exists=True) is False
    assert restore_plan({**RESEEDING, "rerun": "per_render"}, PAYOFF_SCENES)["render_reseeds"] is True


def test_the_rerun_vocabulary_is_the_recorders(tmp_path, monkeypatch):
    """A third value would make restore_plan's default silently wrong."""
    import pytest

    from scripts.walkthrough import record_video
    from scripts.walkthrough.record_video import SetupError, run_setup

    monkeypatch.setattr(record_video, "resolve_setup_cwd", lambda _p: tmp_path)
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text("scenes: []\n")
    with pytest.raises(SetupError):
        run_setup({"command": "true", "rerun": "sometimes"}, spec_path)


# --- The wiring ------------------------------------------------------------
#
# restore_plan deciding correctly is only half the fix; preflight has to call it
# at the RIGHT END. The whole defect is the setup contract applied at the wrong
# end of the walk, so a test that does not pin the ORDER would pass on the
# original bug — there the reseed happened too, just never again afterwards.



def _payoff_spec(tmp_path, *, rerun: str):
    """A minimal VALID spec in the bug's shape: one mutating payoff action."""
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        "name: payoff-demo\n"
        "narrative: payoff-demo\n"
        "base_url: https://example.test\n"
        "personas: {}\n"
        "setup:\n"
        "  command: reset-the-world\n"
        "  outputs: realized.json\n"
        f"  rerun: {rerun}\n"
        "scenes:\n"
        "- id: record-the-disposition\n"
        "  persona: p\n"
        "  provenance: S0\n"
        "  title: The disposition is recorded\n"
        "  concept_claim: The reviewer can set a disposition and it persists.\n"
        "  show: the review queue\n"
        "  narrative: The reviewer marks the row for audit.\n"
        "  url: /\n"
        "  actions:\n"
        "  - kind: click\n"
        "    target: 'testid:decision'\n"
    )
    (tmp_path / "realized.json").write_text("{}")
    return spec


def _stub_browser(monkeypatch, on_resolve):
    """Replace playwright + the two helpers preflight drives it through."""
    from unittest.mock import MagicMock

    import playwright.sync_api as pw

    from scripts.walkthrough import identities as ident
    from scripts.walkthrough._lib import targets

    monkeypatch.setattr(pw, "sync_playwright", lambda: MagicMock())
    monkeypatch.setattr(ident, "mint_identities", lambda *a, **k: {})
    monkeypatch.setattr(targets, "resolve_target", on_resolve)


def test_preflight_reseeds_before_the_walk_and_restores_after_it(tmp_path, monkeypatch):
    """The order IS the fix: setup -> walk -> setup, not setup -> walk."""
    import scripts.ddd.recipe_preflight as rp
    from scripts.walkthrough import record_video

    calls: list[str] = []

    spec_path = _payoff_spec(tmp_path, rerun="once")

    monkeypatch.setattr(record_video, "resolve_setup_cwd", lambda _p: tmp_path)
    monkeypatch.setattr(
        rp, "_run_setup_command",
        lambda command, cwd, timeout, *, label, failure: calls.append(label.split()[0]),
    )
    _stub_browser(monkeypatch, lambda *a, **k: calls.append("walk"))

    rp.preflight(spec_path)

    assert calls == ["reseeding", "walk", "restoring"], calls


def test_per_render_reseeds_once_and_does_not_restore(tmp_path, monkeypatch):
    """The control: the default shape must not pay for a second generator run."""
    import scripts.ddd.recipe_preflight as rp
    from scripts.walkthrough import record_video

    calls: list[str] = []

    spec_path = _payoff_spec(tmp_path, rerun="per_render")

    monkeypatch.setattr(record_video, "resolve_setup_cwd", lambda _p: tmp_path)
    monkeypatch.setattr(
        rp, "_run_setup_command",
        lambda command, cwd, timeout, *, label, failure: calls.append(label.split()[0]),
    )
    _stub_browser(monkeypatch, lambda *a, **k: calls.append("walk"))

    rp.preflight(spec_path)

    assert calls == ["reseeding", "walk"], calls
