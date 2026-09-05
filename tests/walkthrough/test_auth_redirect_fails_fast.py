"""An expired session must fail the run at the navigation, not five scenes later.

Background: a labs capture burned a full 12-minute cycle and then died on
``click(css:#new-study) — target_not_found``. Nothing was wrong with the spec.
The stored session had expired, labs 302'd every page to ``/labs/login/``, and
the recorder dutifully filmed the login page until a selector finally missed.
The redirect is the honest signal and it is visible at ``goto_and_settle`` —
the one place every scene navigates through.

These tests pin: (a) the auth-URL detector matches on PATH SEGMENTS, so real
pages that merely contain "login" in a word are safe; (b) a bounce raises
``SessionExpiredError``; (c) a spec that deliberately records a login flow is
unaffected.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.walkthrough._lib.config import RecorderConfig  # noqa: E402
from scripts.walkthrough._lib.orchestrator import (  # noqa: E402
    Recorder,
    _looks_like_auth_url,
)
from scripts.walkthrough._lib.results import SessionExpiredError  # noqa: E402


CONFIG = RecorderConfig(
    initial_hold_ms=0,
    final_hold_ms=0,
    min_hold_ms=0,
    goto_settle_ms=0,
    load_settle_timeout_ms=1,
    goto_timeout_ms=1000,
    crossfade=False,
)


class FakePage:
    """Page-shaped stub whose ``url`` is where the navigation LANDED."""

    def __init__(self, landed: str):
        self.url = landed

    def goto(self, url, **kw):  # noqa: ARG002
        return None

    def screenshot(self, **kw):  # noqa: ARG002
        raise RuntimeError("no screenshots in this test")

    def wait_for_load_state(self, *a, **kw):
        return None

    def wait_for_timeout(self, *a, **kw):
        return None

    def evaluate(self, *a, **kw):
        return None


@pytest.mark.parametrize(
    "url",
    [
        "https://labs.connect.dimagi.com/labs/login/?next=/microplans/",
        "https://connect.dimagi.com/o/authorize/?client_id=x",
        "https://example.com/accounts/sign-in",
        "https://example.com/sso/",
    ],
)
def test_auth_urls_are_detected(url):
    assert _looks_like_auth_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://labs.connect.dimagi.com/microplans/program/10008/",
        # the whole point of matching per-segment rather than by substring:
        "https://example.com/docs/login-guide",
        "https://example.com/blog/signing-ceremony",
        "https://example.com/authorized-dealers",
        "",
    ],
)
def test_ordinary_urls_are_not_auth(url):
    assert _looks_like_auth_url(url) is False


def _recorder():
    return Recorder(config=CONFIG, base_url="https://labs.connect.dimagi.com")


def test_bounce_to_login_raises_session_expired():
    rec = _recorder()
    page = FakePage("https://labs.connect.dimagi.com/labs/login/?next=/microplans/")
    with pytest.raises(SessionExpiredError) as err:
        rec._assert_not_bounced_to_auth(page, "https://labs.connect.dimagi.com/microplans/program/10008/")
    msg = str(err.value)
    assert "session" in msg.lower()
    # the message must name BOTH ends, or it is not actionable
    assert "/microplans/program/10008/" in msg and "/labs/login/" in msg


def test_landing_where_we_asked_is_fine():
    rec = _recorder()
    page = FakePage("https://labs.connect.dimagi.com/microplans/program/10008/")
    rec._assert_not_bounced_to_auth(page, "https://labs.connect.dimagi.com/microplans/program/10008/")


def test_spec_that_deliberately_records_a_login_page_is_untouched():
    rec = _recorder()
    page = FakePage("https://labs.connect.dimagi.com/labs/login/")
    rec._assert_not_bounced_to_auth(page, "https://labs.connect.dimagi.com/labs/login/")


def test_torn_down_page_never_blocks_navigation():
    class Exploding:
        @property
        def url(self):
            raise RuntimeError("page closed")

    _recorder()._assert_not_bounced_to_auth(Exploding(), "https://example.com/x")
