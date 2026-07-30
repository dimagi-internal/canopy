"""The recorder becomes a scene's persona off camera, BEFORE the nav.

The ordering is the whole feature. Swap cookies after the goto and the page has
already rendered as the previous persona, so the frame the judge scores — and the
frame the viewer sees — is the wrong seat. Swap before it and the identity
changes between two frames of ordinary navigation, with no login form on film.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.walkthrough._lib.config import RecorderConfig  # noqa: E402
from scripts.walkthrough._lib.orchestrator import Recorder  # noqa: E402

CONFIG = RecorderConfig(initial_hold_ms=0, final_hold_ms=0, min_hold_ms=0, goto_settle_ms=0)

ADA = [{"name": "sessionid", "value": "ada"}]
TOMAS = [{"name": "sessionid", "value": "tomas"}]


class FakeContext:
    def __init__(self):
        self.cleared = 0
        self.added: list[list[dict]] = []

    def clear_cookies(self):
        self.cleared += 1

    def add_cookies(self, cookies):
        self.added.append(cookies)


class FakePage:
    """Records the interleaving of cookie swaps and navigations."""

    def __init__(self, url="https://x.test/start"):
        self.url = url
        self.context = FakeContext()
        self.events: list[str] = []

    def goto(self, url, *, wait_until=None, timeout=None):
        self.events.append(f"goto:{url}")
        self.url = url

    def wait_for_timeout(self, _ms):
        pass

    def wait_for_load_state(self, *_a, **_kw):
        pass


class TracingRecorder(Recorder):
    """Logs identity swaps onto the page's event list, in order."""

    def apply_scene_identity(self, page, scene):
        applied = super().apply_scene_identity(page, scene)
        if applied:
            page.events.append(f"identity:{applied}")
        return applied


def _recorder(**kw):
    return TracingRecorder(config=CONFIG, base_url="https://x.test", **kw)


def _scene(persona, url):
    return {"title": persona, "persona": persona, "url": url, "actions": []}


def test_identity_is_applied_before_the_nav():
    page = FakePage()
    rec = _recorder(identities={"ada": ADA})
    rec.run_scene(page, _scene("ada", "/dashboard"))

    assert page.events == ["identity:ada", "goto:https://x.test/dashboard"], (
        "the cookie swap must precede the goto, or the page loads as the wrong persona"
    )


def test_switching_personas_clears_the_previous_session():
    """Without the clear, the old session cookie can survive and win."""
    page = FakePage()
    rec = _recorder(identities={"ada": ADA, "tomas": TOMAS})

    rec.run_scene(page, _scene("ada", "/a"))
    rec.run_scene(page, _scene("tomas", "/b"))

    assert page.context.cleared == 2
    assert page.context.added == [ADA, TOMAS]


def test_same_persona_twice_does_not_re_swap():
    """Consecutive scenes in one seat should not churn the session."""
    page = FakePage()
    rec = _recorder(identities={"ada": ADA})

    rec.run_scene(page, _scene("ada", "/a"))
    rec.run_scene(page, _scene("ada", "/b"))

    assert page.context.added == [ADA]
    assert page.events.count("identity:ada") == 1


def test_no_identities_configured_is_a_no_op():
    """Every existing spec sets `persona` as a label only — none must change."""
    page = FakePage()
    rec = _recorder()
    rec.run_scene(page, _scene("ada", "/a"))

    assert page.context.added == []
    assert page.events == ["goto:https://x.test/a"]


def test_unmapped_persona_is_a_no_op():
    page = FakePage()
    rec = _recorder(identities={"ada": ADA})
    rec.run_scene(page, _scene("narrator", "/a"))

    assert page.context.added == []


def test_scene_with_no_persona_leaves_the_identity_alone():
    page = FakePage()
    rec = _recorder(identities={"ada": ADA})

    rec.run_scene(page, _scene("ada", "/a"))
    rec.run_scene(page, {"title": "x", "url": "/b", "actions": []})

    assert page.context.added == [ADA]


def test_no_login_url_is_ever_navigated():
    """The property that makes this feature worth having: authentication does not
    appear in the recorded navigation sequence at all."""
    page = FakePage()
    rec = _recorder(identities={"ada": ADA, "tomas": TOMAS})

    rec.run_scene(page, _scene("ada", "/console"))
    rec.run_scene(page, _scene("tomas", "/review"))

    navs = [e for e in page.events if e.startswith("goto:")]
    assert navs == ["goto:https://x.test/console", "goto:https://x.test/review"]
    assert not any("login" in n for n in navs)


def test_starting_identity_can_be_declared():
    """A context already seeded with one persona's cookies (storage_state) should
    not be re-swapped on that persona's first scene."""
    page = FakePage()
    rec = _recorder(identities={"ada": ADA}, identity="ada")
    rec.run_scene(page, _scene("ada", "/a"))

    assert page.context.added == []
