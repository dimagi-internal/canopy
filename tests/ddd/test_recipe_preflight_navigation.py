"""Preflight has to walk the recipe the way the recorder will.

Both cases here are drawn from one real false failure. `oes-supply-base` scene 6
switches persona mid-scene — the buyer publishes a solicitation, then a `goto` to
a dev-login lands on the qualified supplier's side to show the same call arriving
there. Preflight reported the supplier's nav item as unresolvable and the next
three scenes as broken. Every one of those recipes was correct; preflight was
looking at the wrong page.

The two defects are independent, so they are tested independently:

  * a mid-scene ``goto`` was never applied, so everything after it in that scene
    was checked against the PRE-goto page;
  * scene navigation compared the previous scene's DECLARED url, where
    ``SkipSameUrlRecorder`` compares the browser's ACTUAL url — so a scene
    reached through a redirect looked like a new destination, and preflight
    navigated (wiping an open modal) where the recorder would not.

The second is the dangerous one in the other direction too: a selector that
happens to exist on both pages produces a false PASS, and the render is the
thing that finds out.
"""
from __future__ import annotations

from scripts.ddd.recipe_preflight import _NAVIGATING, _TARGETED, _scene_steps


def test_a_mid_scene_goto_is_walked_not_dropped():
    scene = {
        "id": "buyer-then-supplier",
        "actions": [
            {"kind": "click", "target": "css:.navitem:has-text('Solicitations')"},
            {"kind": "goto", "target": "/supply/dev-login/?persona=amina&next=/supply/"},
            {"kind": "click", "target": "css:.navitem:has-text('Solicitations & bids')"},
        ],
    }
    steps = _scene_steps(scene)

    assert [k for _i, k, _t in steps] == ["click", "goto", "click"]
    # Indexes stay the recipe's own, so a finding points at the right action.
    assert [i for i, _k, _t in steps] == [0, 1, 2]


def test_a_goto_is_navigated_not_resolved_as_a_selector():
    """Its target is a URL — resolving it as a selector would always fail."""
    assert "goto" in _NAVIGATING
    assert "goto" not in _TARGETED


def test_an_action_without_a_target_is_skipped():
    scene = {"actions": [{"kind": "hold", "seconds": 2.0}, {"kind": "goto"}]}
    assert _scene_steps(scene) == []


def test_scene_navigation_compares_normalised_urls():
    """The recorder's comparison, so preflight agrees with it about redirects.

    `/supply/dev-login/?next=/supply/` lands the browser on `/supply/`. A later
    scene declaring `/supply/` is therefore ALREADY THERE, and re-navigating
    would destroy the modal the previous scene opened.
    """
    from scripts.walkthrough._lib.urls import normalize_url as _normalize_url

    assert _normalize_url("http://localhost:8009/supply/") == _normalize_url("http://localhost:8009/supply")
    assert _normalize_url("http://localhost:8009/supply/#x") == _normalize_url("http://localhost:8009/supply")
    assert _normalize_url("http://localhost:8009/supply/") != _normalize_url(
        "http://localhost:8009/supply/dev-login/?persona=ada"
    )
