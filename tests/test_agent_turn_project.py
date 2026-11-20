"""`canopy agent turn --project`: at close-out, the turn's OWN canopy-web conversation is
declared part of the project (PUT /api/brain/sessions/{conversation}/projects/{handle}/),
best effort — an older canopy-web, a refusal or no answer never fails the close-out."""
import json

import pytest
from click.testing import CliRunner

from orchestrator import provenance
from orchestrator.canopy_web import CanopyNetworkError
from orchestrator.cli import main

CONV = "0b5c8a7e-1111-4222-8333-944445555666"
PROJECTS = [{"ext_id": "P5", "name": "Supply", "handle": "supply", "address": "connect/supply"}]


@pytest.fixture
def fake_http(monkeypatch):
    calls = []
    responses = {("GET", "agents/hal/projects/"): (200, json.dumps(PROJECTS))}

    def transport(method, url, headers, body):
        calls.append((method, url.split("/api/")[1], json.loads(body) if body else None))
        answer = responses.get((method, url.split("/api/")[1]), (200, "{}"))
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setenv("CANOPY_WEB_PAT", "t")
    monkeypatch.setenv("CANOPY_WEB_API_URL", "https://x.test")
    monkeypatch.setattr("orchestrator.canopy_web.urllib_transport", transport)
    monkeypatch.setattr(provenance, "resolve_parent",
                        lambda cwd=None: {"turn_id": "t", "session_id": CONV, "claude_session_id": ""})
    return calls, responses


def _turn(*extra):
    return CliRunner().invoke(main, ["agent", "turn", "--slug", "hal", "--session-id", "cs1",
                                     "--title", "built the stock view", *extra])


def _puts(calls):
    return [c for c in calls if c[0] == "PUT"]


@pytest.mark.parametrize("ref", ["supply", "connect/supply", "P5", "Supply"])
def test_turn_declares_its_own_conversation(fake_http, ref):
    calls, _ = fake_http
    r = _turn("--project", ref)
    assert r.exit_code == 0, r.output
    assert _puts(calls) == [("PUT", f"brain/sessions/{CONV}/projects/supply/", {"tier": "core"})]
    # The turn record went out first, unchanged.
    assert [c[1] for c in calls if c[0] == "POST"] == ["agents/hal/turns/"]


def test_adjacent_tier(fake_http):
    calls, _ = fake_http
    assert _turn("--project", "supply", "--project-tier", "adjacent").exit_code == 0
    assert _puts(calls)[0][2] == {"tier": "adjacent"}


def test_no_project_no_declaration(fake_http):
    calls, _ = fake_http
    assert _turn().exit_code == 0
    assert not _puts(calls)
    assert not any(c[1].endswith("/projects/") for c in calls)


def test_outside_a_canopy_turn_nothing_is_declared(fake_http, monkeypatch):
    calls, _ = fake_http
    monkeypatch.setattr(provenance, "resolve_parent",
                        lambda cwd=None: {"turn_id": "", "session_id": "", "claude_session_id": ""})
    assert _turn("--project", "supply").exit_code == 0
    assert not _puts(calls)


@pytest.mark.parametrize("answer", [(404, '{"detail":"Not Found"}'), (405, ""), (501, ""),
                                    (403, '{"title":"Only an agent\'s own login"}'),
                                    CanopyNetworkError("no answer")])
def test_declaration_failure_never_fails_the_close_out(fake_http, answer):
    calls, responses = fake_http
    responses[("PUT", f"brain/sessions/{CONV}/projects/supply/")] = answer
    r = _turn("--project", "supply")
    assert r.exit_code == 0, r.output
    assert json.loads(r.output.strip().splitlines()[-1]) == {}   # only the turn record's output
    assert len(_puts(calls)) == 1


def test_unknown_project_is_skipped(fake_http):
    calls, _ = fake_http
    assert _turn("--project", "P99").exit_code == 0
    assert not _puts(calls)
