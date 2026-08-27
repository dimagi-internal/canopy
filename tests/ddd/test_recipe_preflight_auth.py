"""Preflight has to be able to sign in — canopy#532.

`recipe_preflight` exists to save a render by catching unresolvable targets
cheaply. For a session-authenticated spec it did the opposite: with no way to be
handed a session, it walked the recipe LOGGED OUT, every selector missed, and
the report came back `target did not resolve` on all of them — a full-red result
on a recipe that then rendered 18/18 actions ok. That failure is
indistinguishable from a genuinely broken recipe, so a careful operator has to
disprove it by hand and a less careful one "fixes" selectors that were never
broken. ACE's DDD specs point at labs dashboards behind a login, so this is
every ACE run.

The tests come in two tiers, and the split is deliberate:

  * **pure** — the flag surface, the recorder's precedence, and the
    logged-out hint. These gate in CI.
  * **live** — a real Chromium against a local server that gates its content on
    a session cookie. This is the only tier that can prove the session actually
    reaches the browser, and it is also where the non-vacuity control lives:
    the SAME authenticated walk must PASS on a good selector and still FAIL on a
    broken one. "Session-auth specs stopped reporting 100% failure" on its own
    would mean the check was disabled, not fixed. It SKIPS where no browser is
    installed (CI is such a place — `python-tests.yml` installs the playwright
    package only); run it locally before changing preflight's auth path.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from scripts.ddd.recipe_preflight import logged_out_hint, preflight


# ---------------------------------------------------------------------------
# pure — the flag surface and the hint
# ---------------------------------------------------------------------------


def test_the_cli_parses_a_value_bearing_flag_instead_of_dropping_it():
    """The old filter kept the flag's VALUE as a positional.

    `args = [a for a in sys.argv[1:] if not a.startswith("--")]` drops `--x` and
    keeps `/tmp/state.json`, so the recipe path acquires a silent neighbour and
    no option can ever carry a value. That is why the fix needs real parsing,
    not one more entry in a filter.
    """
    import sys
    from unittest.mock import patch

    seen = {}

    def _fake_preflight(recipe, **kwargs):
        seen["recipe"] = recipe
        seen.update(kwargs)
        return {"verdict": "pass", "targets_checked": 0, "unresolved": 0, "findings": []}

    argv = [
        "recipe_preflight",
        "specs/demo.yaml",
        "--storage-state",
        "/tmp/state.json",
        "--json",
    ]
    with patch.object(sys, "argv", argv), patch(
        "scripts.ddd.recipe_preflight.preflight", _fake_preflight
    ):
        from scripts.ddd.recipe_preflight import _cli

        assert _cli() == 0

    assert seen["recipe"] == "specs/demo.yaml"
    assert seen["storage_state"] == "/tmp/state.json"
    assert seen["cookies"] is None


def test_the_cli_accepts_cookies_the_way_the_recorder_does():
    import sys
    from unittest.mock import patch

    seen = {}

    def _fake_preflight(recipe, **kwargs):
        seen.update(kwargs)
        return {"verdict": "pass", "targets_checked": 0, "unresolved": 0, "findings": []}

    argv = ["recipe_preflight", "specs/demo.yaml", "--cookies", "/tmp/c.json"]
    with patch.object(sys, "argv", argv), patch(
        "scripts.ddd.recipe_preflight.preflight", _fake_preflight
    ):
        from scripts.ddd.recipe_preflight import _cli

        _cli()

    assert seen["cookies"] == "/tmp/c.json"
    assert seen["storage_state"] is None


def test_preflight_takes_the_recorders_auth_inputs():
    """Same names, same kwargs — one auth contract, not a second one."""
    import inspect

    params = inspect.signature(preflight).parameters
    assert "storage_state" in params
    assert "cookies" in params


def test_the_hint_names_the_likely_cause_when_everything_missed():
    hint = logged_out_hint(
        checked=11, unresolved=11, session_supplied=False, authenticated=False
    )
    assert hint and "no session" in hint


def test_the_hint_is_silent_when_a_session_was_supplied():
    """Then 100% unresolved really might mean the recipe is broken."""
    assert (
        logged_out_hint(
            checked=11, unresolved=11, session_supplied=True, authenticated=False
        )
        is None
    )


def test_the_hint_is_silent_when_anything_at_all_resolved():
    """One surviving target proves the browser could see the app."""
    assert (
        logged_out_hint(
            checked=11, unresolved=10, session_supplied=False, authenticated=False
        )
        is None
    )


def test_the_hint_is_silent_when_the_spec_authenticates_itself():
    """An `auth:` block or minted form identities is a session of its own."""
    assert (
        logged_out_hint(
            checked=11, unresolved=11, session_supplied=False, authenticated=True
        )
        is None
    )


# ---------------------------------------------------------------------------
# live — a real browser against a session-gated server
# ---------------------------------------------------------------------------

_SIGNED_IN = (
    "<html><body><h1>Community progression</h1>"
    "<table data-testid='communities-table'><tr><td>FCAP-C10</td></tr></table>"
    "<div data-testid='step-trail'>step trail</div>"
    "</body></html>"
)
_SIGNED_OUT = "<html><body><h1>Sign in</h1><form data-testid='login'></form></body></html>"


class _GatedHandler(BaseHTTPRequestHandler):
    """Serves the dashboard only to a request carrying `session=live`."""

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler's spelling
        body = _SIGNED_IN if "session=live" in (self.headers.get("Cookie") or "") else _SIGNED_OUT
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *_args):  # keep pytest output clean
        return


@pytest.fixture(scope="module")
def gated_server():
    server = HTTPServer(("127.0.0.1", 0), _GatedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture(scope="module")
def chromium_available():
    playwright = pytest.importorskip(
        "playwright.sync_api", reason="playwright package not installed"
    )
    try:
        manager = playwright.sync_playwright()
        pw = manager.start()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"playwright could not start: {exc}")
    try:
        browser = pw.chromium.launch()
    except Exception as exc:  # noqa: BLE001 — CI installs the package, not the browsers
        manager.__exit__(None, None, None)
        pytest.skip(f"no chromium available: {exc}")
    browser.close()
    manager.__exit__(None, None, None)
    return True


def _spec(tmp_path: Path, base_url: str, target: str, name: str) -> Path:
    spec = tmp_path / f"{name}.yaml"
    spec.write_text(
        "name: session-demo\n"
        "narrative: session-demo\n"
        f"base_url: {base_url}\n"
        "personas: {}\n"
        "scenes:\n"
        "- id: the-dashboard-opens\n"
        "  persona: p\n"
        "  provenance: S0\n"
        "  title: A scene\n"
        "  concept_claim: A claim that is specific and observable here.\n"
        "  show: the dashboard\n"
        "  narrative: The dashboard opens.\n"
        "  url: /\n"
        "  actions:\n"
        f"  - kind: wait_for\n    target: {target}\n"
    )
    return spec


def _storage_state(tmp_path: Path, base_url: str) -> Path:
    host = base_url.split("//", 1)[1].split(":")[0]
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "session",
                        "value": "live",
                        "domain": host,
                        "path": "/",
                        "expires": -1,
                        "httpOnly": False,
                        "secure": False,
                        "sameSite": "Lax",
                    }
                ],
                "origins": [],
            }
        )
    )
    return state


def test_a_session_auth_spec_with_no_session_reports_everything_unresolved(
    tmp_path, gated_server, chromium_available
):
    """The defect itself: the baseline every other result has to beat.

    Kept as a test rather than deleted, because it is what makes the two results
    below MEAN something — the same recipe, the same server, differing only in
    whether preflight was handed a session.
    """
    spec = _spec(tmp_path, gated_server, "testid:step-trail", "good")
    result = preflight(spec, timeout_ms=1500)

    assert result["verdict"] == "fail"
    assert result["unresolved"] == result["targets_checked"] == 1
    # ...and it now SAYS so, instead of reading as a broken recipe.
    assert result["hint"] and "no session" in result["hint"]


def test_a_session_auth_spec_whose_targets_resolve_now_passes(
    tmp_path, gated_server, chromium_available
):
    """canopy#532: the same recipe, handed the session the recorder gets."""
    spec = _spec(tmp_path, gated_server, "testid:step-trail", "good")
    state = _storage_state(tmp_path, gated_server)

    result = preflight(spec, timeout_ms=1500, storage_state=state)

    assert result["targets_checked"] == 1, "the target must still be CHECKED"
    assert result["verdict"] == "pass", result["findings"]
    assert result["hint"] is None


def test_an_authenticated_walk_still_fails_a_genuinely_broken_selector(
    tmp_path, gated_server, chromium_available
):
    """The non-vacuity control — the half that proves the check still works.

    Same session, same page, a selector that is not on it. If this passes,
    #532's fix disabled preflight for session-auth specs instead of repairing
    it, and a real recipe defect would be as invisible as the false one was
    unmissable.
    """
    spec = _spec(tmp_path, gated_server, "testid:no-such-panel", "broken")
    state = _storage_state(tmp_path, gated_server)

    result = preflight(spec, timeout_ms=1500, storage_state=state)

    assert result["verdict"] == "fail"
    assert result["findings"][0]["target"] == "testid:no-such-panel"
    assert result["findings"][0]["error"] == "target did not resolve"
    # No hint: a session WAS supplied, so all-red really might be the recipe.
    assert result["hint"] is None


def test_cookies_seed_the_session_too(tmp_path, gated_server, chromium_available):
    """The recorder's other auth input, on the recorder's own JSON shape."""
    spec = _spec(tmp_path, gated_server, "testid:communities-table", "good")
    host = gated_server.split("//", 1)[1].split(":")[0]
    cookies = tmp_path / "cookies.json"
    cookies.write_text(
        json.dumps([{"name": "session", "value": "live", "domain": host, "path": "/"}])
    )

    result = preflight(spec, timeout_ms=1500, cookies=cookies)

    assert result["verdict"] == "pass", result["findings"]
