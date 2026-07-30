"""Off-camera persona sign-in.

The property under test is the one that made this feature necessary: a
multi-persona narrative must never film a login form, because the recorded
context's video starts with the page and cannot be paused.
"""
import pytest

from scripts.walkthrough.identities import (
    IdentityError,
    mint_identities,
    personas_in_spec,
)


# ---------------------------------------------------------------------------
# personas_in_spec — only sign in the seats the narrative actually visits
# ---------------------------------------------------------------------------


def test_collects_personas_in_first_appearance_order():
    spec = {
        "scenes": [
            {"persona": "ada"},
            {"persona": "amina"},
            {"persona": "tomas"},
            {"persona": "ada"},
        ]
    }
    assert personas_in_spec(spec) == ["ada", "amina", "tomas"]


def test_ignores_scenes_with_no_persona():
    spec = {"scenes": [{"persona": "ada"}, {}, {"persona": ""}, {"title": "x"}]}
    assert personas_in_spec(spec) == ["ada"]


def test_no_scenes_is_no_personas():
    assert personas_in_spec({}) == []


# ---------------------------------------------------------------------------
# mint_identities
# ---------------------------------------------------------------------------


class _FakePage:
    def __init__(self, context, *, fail_success=False):
        self._context = context
        self._fail_success = fail_success
        self.filled: dict[str, str] = {}
        self.clicked: list[str] = []
        self.gotos: list[str] = []

    def goto(self, url, **_kw):
        self.gotos.append(url)

    def fill(self, selector, value):
        self.filled[selector] = value

    def click(self, selector):
        self.clicked.append(selector)

    def wait_for_load_state(self, *_a, **_kw):
        pass

    def wait_for_selector(self, _selector, **_kw):
        if self._fail_success:
            raise TimeoutError("never appeared")


class _FakeContext:
    def __init__(self, cookies, *, fail_success=False):
        self._cookies = cookies
        self.closed = False
        self.page = None
        self._fail_success = fail_success
        self.recorded_video = False  # a recorded context would set this

    def new_page(self):
        self.page = _FakePage(self, fail_success=self._fail_success)
        return self.page

    def cookies(self):
        return self._cookies

    def close(self):
        self.closed = True


class _FakeBrowser:
    """Hands out one context per new_context() call, in order."""

    def __init__(self, contexts):
        self._contexts = list(contexts)
        self.new_context_kwargs: list[dict] = []
        self.made: list[_FakeContext] = []

    def new_context(self, **kwargs):
        self.new_context_kwargs.append(kwargs)
        ctx = self._contexts.pop(0)
        self.made.append(ctx)
        return ctx


def _spec(**auth_over):
    auth = {
        "type": "form",
        "login_url": "/supply/login/",
        "fields": {"email": 'input[name="email"]', "password": 'input[name="password"]'},
        "submit": 'button[type="submit"]',
        "password_env": "DEMO_PW",
        "personas": {"ada": "lead@oes.example", "tomas": "review@oes.example"},
    }
    auth.update(auth_over)
    return {"auth": auth, "scenes": [{"persona": "ada"}, {"persona": "tomas"}]}


def test_signs_in_each_persona_and_returns_its_cookies(monkeypatch):
    monkeypatch.setenv("DEMO_PW", "s3cret")
    ada = _FakeContext([{"name": "sessionid", "value": "ada-session"}])
    tomas = _FakeContext([{"name": "sessionid", "value": "tomas-session"}])
    browser = _FakeBrowser([ada, tomas])

    out = mint_identities(browser, _spec(), "https://labs.example")

    assert set(out) == {"ada", "tomas"}
    assert out["ada"][0]["value"] == "ada-session"
    assert out["tomas"][0]["value"] == "tomas-session"
    assert ada.page.filled['input[name="email"]'] == "lead@oes.example"
    assert ada.page.filled['input[name="password"]'] == "s3cret"
    assert ada.page.gotos == ["https://labs.example/supply/login/"]


def test_each_persona_gets_its_own_context_and_it_is_closed(monkeypatch):
    """One context per seat, so sessions cannot bleed between personas — and
    every one is closed, so no login context survives into the recording."""
    monkeypatch.setenv("DEMO_PW", "s3cret")
    ada = _FakeContext([{"name": "sessionid", "value": "a"}])
    tomas = _FakeContext([{"name": "sessionid", "value": "t"}])
    browser = _FakeBrowser([ada, tomas])

    mint_identities(browser, _spec(), "https://labs.example")

    assert len(browser.made) == 2
    assert ada.closed and tomas.closed


def test_never_asks_for_a_recorded_context(monkeypatch):
    """The whole point: these contexts must not be recording video."""
    monkeypatch.setenv("DEMO_PW", "s3cret")
    browser = _FakeBrowser(
        [_FakeContext([{"name": "s", "value": "1"}]), _FakeContext([{"name": "s", "value": "2"}])]
    )

    mint_identities(browser, _spec(), "https://labs.example")

    for kwargs in browser.new_context_kwargs:
        assert "record_video_dir" not in kwargs
        assert "record_video_size" not in kwargs


def test_non_form_auth_is_a_no_op():
    """Every existing spec uses auth.type url (or none) and must be unaffected."""
    browser = _FakeBrowser([])
    assert mint_identities(browser, {"auth": {"type": "url", "url": "/x"}}, "https://x") == {}
    assert mint_identities(browser, {}, "https://x") == {}


def test_label_only_persona_is_skipped_not_fatal(monkeypatch):
    """`persona` is a narrative field that predates this feature; most specs set
    it purely as a label, so an unmapped value must not break them."""
    monkeypatch.setenv("DEMO_PW", "s3cret")
    spec = _spec(personas={"ada": "lead@oes.example"})
    spec["scenes"] = [{"persona": "ada"}, {"persona": "narrator"}]
    browser = _FakeBrowser([_FakeContext([{"name": "s", "value": "1"}])])

    out = mint_identities(browser, spec, "https://labs.example")

    assert set(out) == {"ada"}


def test_missing_password_env_is_a_loud_error(monkeypatch):
    monkeypatch.delenv("DEMO_PW", raising=False)
    browser = _FakeBrowser([_FakeContext([])])
    with pytest.raises(IdentityError, match=r"\$DEMO_PW is empty"):
        mint_identities(browser, _spec(), "https://labs.example")


def test_no_password_env_declared_is_a_loud_error():
    spec = _spec()
    del spec["auth"]["password_env"]
    browser = _FakeBrowser([_FakeContext([])])
    with pytest.raises(IdentityError, match="auth.password_env is not set"):
        mint_identities(browser, spec, "https://labs.example")


def test_per_persona_password_env_wins(monkeypatch):
    monkeypatch.setenv("DEMO_PW", "shared")
    monkeypatch.setenv("ADA_PW", "special")
    spec = _spec(personas={"ada": "lead@oes.example"}, password_envs={"ada": "ADA_PW"})
    spec["scenes"] = [{"persona": "ada"}]
    ctx = _FakeContext([{"name": "s", "value": "1"}])

    mint_identities(_FakeBrowser([ctx]), spec, "https://labs.example")

    assert ctx.page.filled['input[name="password"]'] == "special"


def test_login_that_sets_no_cookies_is_an_error(monkeypatch):
    """A silent logged-out render looks like a product bug and costs a judge cycle."""
    monkeypatch.setenv("DEMO_PW", "s3cret")
    spec = _spec(personas={"ada": "lead@oes.example"})
    spec["scenes"] = [{"persona": "ada"}]
    with pytest.raises(IdentityError, match="set no cookies"):
        mint_identities(_FakeBrowser([_FakeContext([])]), spec, "https://labs.example")


def test_success_selector_that_never_appears_is_an_error(monkeypatch):
    monkeypatch.setenv("DEMO_PW", "wrong-password")
    spec = _spec(personas={"ada": "lead@oes.example"}, success=".navitem")
    spec["scenes"] = [{"persona": "ada"}]
    ctx = _FakeContext([{"name": "s", "value": "1"}], fail_success=True)
    with pytest.raises(IdentityError, match="never appeared"):
        mint_identities(_FakeBrowser([ctx]), spec, "https://labs.example")
