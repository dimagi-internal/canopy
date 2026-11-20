import json
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest
from orchestrator.server import create_app, get_transcripts, save_label_data

FIXTURE = Path(__file__).parent / "fixtures" / "sample_transcript.jsonl"


@pytest.fixture
def app_dirs(tmp_path):
    """Set up directory structure for the server."""
    projects_dir = tmp_path / "projects" / "-test-project"
    projects_dir.mkdir(parents=True)
    shutil.copy(FIXTURE, projects_dir / "test-session-001.jsonl")

    state_dir = tmp_path / "orchestrator"
    state_dir.mkdir()

    return {
        "projects_dir": tmp_path / "projects",
        "state_dir": state_dir,
    }


class TestCreateApp:
    def test_returns_handler_class(self, app_dirs):
        handler = create_app(**app_dirs)
        assert handler is not None


class TestGetTranscripts:
    def test_returns_list(self, app_dirs):
        result = get_transcripts(app_dirs["projects_dir"], app_dirs["state_dir"])
        assert isinstance(result, list)

    def test_finds_fixture_transcript(self, app_dirs):
        result = get_transcripts(app_dirs["projects_dir"], app_dirs["state_dir"])
        assert len(result) >= 1

    def test_transcript_has_metadata(self, app_dirs):
        result = get_transcripts(app_dirs["projects_dir"], app_dirs["state_dir"])
        t = result[0]
        assert "session_id" in t
        assert "lines" in t
        assert "user_msgs" in t
        assert "first_msg" in t


class TestSaveLabels:
    def test_save_and_retrieve(self, app_dirs):
        save_label_data(app_dirs["state_dir"], "test-session-001", {
            "quality": "had-friction",
            "use_case_tags": ["test"],
            "eval_candidate": True,
            "notes": "test note",
        })
        transcripts = get_transcripts(app_dirs["projects_dir"], app_dirs["state_dir"])
        t = [t for t in transcripts if t["session_id"] == "test-session-001"][0]
        assert t["label"]["quality"] == "had-friction"


# ── the API answers only its own same-origin UI ───────────────────────────────────────────
import http.client
import threading
from http.server import HTTPServer

from orchestrator.server import request_allowed


@pytest.fixture
def live(app_dirs):
    srv = HTTPServer(("127.0.0.1", 0), create_app(**app_dirs))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_port
    srv.shutdown()


def _req(port, method, path, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    return resp.status, dict(resp.getheaders()), resp.read()


def test_the_ui_itself_can_read_transcripts(live):
    status, headers, body = _req(live, "GET", "/api/transcripts")
    assert status == 200 and json.loads(body)
    assert "Access-Control-Allow-Origin" not in headers


@pytest.mark.parametrize("headers", [
    {"Origin": "https://evil.example"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Host": "evil.example:8484"},            # DNS rebinding sends the attacker's name
])
def test_a_foreign_page_cannot_read_transcripts(live, headers):
    status, _, body = _req(live, "GET", "/api/transcripts", headers)
    assert status == 403 and b"session_id" not in body


def test_a_cross_site_post_cannot_start_a_review(live):
    with patch("orchestrator.reviewer.run_review", side_effect=AssertionError("ran")):
        status, _, _ = _req(live, "POST", "/api/review/test-session-001",
                            {"Origin": "https://evil.example"})
    assert status == 403


def test_a_post_body_must_be_json(live):
    status, _, _ = _req(live, "POST", "/api/labels/test-session-001",
                        {"Content-Type": "text/plain"}, body=b'{"quality": "x"}')
    assert status == 415


def test_no_cors_preflight_is_granted(live):
    status, headers, _ = _req(live, "OPTIONS", "/api/transcripts",
                              {"Origin": "https://evil.example"})
    assert status == 403 and "Access-Control-Allow-Origin" not in headers


def test_request_allowed_rules():
    ok = {"Host": "127.0.0.1:8484"}
    assert request_allowed(ok, 8484)
    assert request_allowed({"Host": "localhost:8484", "Origin": "http://localhost:8484",
                            "Sec-Fetch-Site": "same-origin"}, 8484)
    assert not request_allowed({"Host": "127.0.0.1:9999"}, 8484)
    assert not request_allowed({**ok, "Origin": "null"}, 8484)
    assert not request_allowed({**ok, "Sec-Fetch-Site": "same-site"}, 8484)
