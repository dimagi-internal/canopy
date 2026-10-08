"""Tests for scripts/ddd/review.py (SP6c).

All HTTP calls are mocked — no real network traffic, no sleeping.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from scripts.ddd.schemas.models import Decision, ReviewRequest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_review_request() -> ReviewRequest:
    return ReviewRequest(
        run_id="my-narrative_slug-2026-01-01-001",
        gate="pre-demo",
        video={"url": "https://example.com/video.mp4", "duration_s": 120},
        decisions=[
            Decision(
                id="d1",
                prompt="Ship it?",
                options=["yes", "no"],
                recommended="yes",
                **{"class": "go_nogo"},
            )
        ],
        narration=[{"scene": 0, "text": "Welcome"}],
        autonomous_audit=["no obvious regressions"],
    )


# ---------------------------------------------------------------------------
# Helpers for mocking urllib
# ---------------------------------------------------------------------------

class _FakeResponse:
    """Minimal file-like object returned by urllib.request.urlopen."""

    def __init__(self, body: dict, status: int = 200):
        self._data = json.dumps(body).encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._data


def _mock_urlopen(response_body: dict, status: int = 200):
    """Return a context-manager-compatible mock for urlopen."""
    return MagicMock(return_value=_FakeResponse(response_body, status))


# ---------------------------------------------------------------------------
# post_review_request
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("pinned_write_workspace")
class TestPostReviewRequest:
    def test_serialises_with_class_alias(self, monkeypatch):
        """The captured POST body must use the 'class' key, not 'class_'."""
        captured: list[dict] = []

        def fake_urlopen(req, **kwargs):
            body = json.loads(req.data.decode("utf-8"))
            captured.append(body)
            return _FakeResponse({"id": "r1", "url": "https://x/r/r1", "share_token": "tok"})

        monkeypatch.setenv("CANOPY_WEB_PAT", "test-token")
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            from scripts.ddd import review as rv
            rv.post_review_request(
                _make_review_request(),
                base_url="https://canopy.test",
            )

        assert len(captured) == 1
        req_json = captured[0]["request_json"]

        # Top-level keys expected by the cross-repo contract
        assert "run_id" in req_json
        assert "gate" in req_json
        assert "video" in req_json
        assert "decisions" in req_json
        assert "narration" in req_json
        assert "autonomous_audit" in req_json

        # Each decision must carry the 'class' alias key — NOT 'class_'
        decision = req_json["decisions"][0]
        assert "class" in decision, "'class' alias key missing from serialised Decision"
        assert "class_" not in decision, "'class_' field name leaked into payload"

    def test_returns_id_url_share_token(self, monkeypatch):
        monkeypatch.setenv("CANOPY_WEB_PAT", "test-token")
        expected = {"id": "abc123", "url": "https://x/r/abc123", "share_token": "s3cr3t"}
        with patch("urllib.request.urlopen", _mock_urlopen(expected)):
            from scripts.ddd import review as rv
            result = rv.post_review_request(
                _make_review_request(),
                base_url="https://canopy.test",
            )
        # Plus the workspace it was posted into (and read back from).
        assert result == {**expected, "workspace": "connect"}

    def test_visibility_sent_in_body(self, monkeypatch):
        captured: list[dict] = []

        def fake_urlopen(req, **kwargs):
            captured.append(json.loads(req.data.decode("utf-8")))
            return _FakeResponse({"id": "r1", "url": "u", "share_token": "t"})

        monkeypatch.setenv("CANOPY_WEB_PAT", "tok")
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            from scripts.ddd import review as rv
            rv.post_review_request(
                _make_review_request(),
                visibility="private",
                base_url="https://canopy.test",
            )
        assert captured[0]["visibility"] == "private"

    def test_bearer_header_sent(self, monkeypatch):
        captured_headers: list[dict] = []

        def fake_urlopen(req, **kwargs):
            captured_headers.append(dict(req.headers))
            return _FakeResponse({"id": "r1", "url": "u", "share_token": "t"})

        monkeypatch.setenv("CANOPY_WEB_PAT", "my-pat-value")
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            from scripts.ddd import review as rv
            rv.post_review_request(
                _make_review_request(),
                base_url="https://canopy.test",
            )
        auth = captured_headers[0].get("Authorization")
        assert auth == "Bearer my-pat-value"

    def test_uses_correct_endpoint(self, monkeypatch):
        captured_urls: list[str] = []

        def fake_urlopen(req, **kwargs):
            captured_urls.append(req.full_url)
            return _FakeResponse({"id": "r1", "url": "u", "share_token": "t"})

        monkeypatch.setenv("CANOPY_WEB_PAT", "tok")
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            from scripts.ddd import review as rv
            rv.post_review_request(
                _make_review_request(),
                base_url="https://canopy.test",
            )
        assert captured_urls[0] == "https://canopy.test/api/w/connect/reviews/"


# ---------------------------------------------------------------------------
# get_review
# ---------------------------------------------------------------------------

class TestGetReview:
    def test_returns_review_dict(self, monkeypatch):
        monkeypatch.setenv("CANOPY_WEB_PAT", "tok")
        body = {
            "id": "r1",
            "status": "pending",
            "request_json": {},
            "response_json": None,
            "is_owner": True,
            "share_token": "tok",
        }
        with patch("urllib.request.urlopen", _mock_urlopen(body)):
            from scripts.ddd import review as rv
            result = rv.get_review("r1", base_url="https://canopy.test")
        assert result["status"] == "pending"

    def test_uses_get_method(self, monkeypatch):
        captured: list[str] = []

        def fake_urlopen(req, **kwargs):
            captured.append(req.get_method())
            return _FakeResponse({"id": "r1", "status": "pending", "response_json": None})

        monkeypatch.setenv("CANOPY_WEB_PAT", "tok")
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            from scripts.ddd import review as rv
            rv.get_review("r1", base_url="https://canopy.test")
        assert captured[0] == "GET"

    def test_url_contains_review_id(self, monkeypatch):
        captured_urls: list[str] = []

        def fake_urlopen(req, **kwargs):
            captured_urls.append(req.full_url)
            return _FakeResponse({"id": "myid", "status": "pending", "response_json": None})

        monkeypatch.setenv("CANOPY_WEB_PAT", "tok")
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            from scripts.ddd import review as rv
            rv.get_review("myid", base_url="https://canopy.test")
        assert "myid" in captured_urls[0]
        assert captured_urls[0].endswith("/api/reviews/myid/")


# ---------------------------------------------------------------------------
# await_resolution
# ---------------------------------------------------------------------------

class TestAwaitResolution:
    def _make_response_sequence(self, statuses: list[str]) -> list[dict]:
        """Build a list of get_review responses cycling through statuses."""
        return [
            {
                "id": "r1",
                "status": s,
                "response_json": {"verdict": "approved"} if s == "resolved" else None,
            }
            for s in statuses
        ]

    def test_returns_response_json_on_resolved(self, monkeypatch):
        """pending → pending → resolved: returns response_json from last."""
        responses = self._make_response_sequence(["pending", "pending", "resolved"])
        resp_iter = iter(responses)

        def fake_get_review(review_id, **kwargs):
            return next(resp_iter)

        # Fake clock: each call to _now() advances by poll_interval so we never time out
        clock = [0.0]

        def fake_now() -> float:
            val = clock[0]
            clock[0] += 1.0  # advance 1s each call; timeout is 100s so plenty of room
            return val

        sleeps: list[float] = []

        def fake_sleep(secs: float) -> None:
            sleeps.append(secs)

        monkeypatch.setenv("CANOPY_WEB_PAT", "tok")
        with patch("scripts.ddd.review.get_review", side_effect=fake_get_review):
            from scripts.ddd import review as rv
            result = rv.await_resolution(
                "r1",
                poll_interval=5.0,
                timeout=100.0,
                base_url="https://canopy.test",
                _sleep=fake_sleep,
                _now=fake_now,
            )

        assert result == {"verdict": "approved"}
        # Two sleeps for two pending responses before resolution
        assert len(sleeps) == 2
        assert all(s == 5.0 for s in sleeps)

    def test_raises_timeout_error(self, monkeypatch):
        """Fake clock immediately exceeds timeout → TimeoutError."""

        def fake_get_review(review_id, **kwargs):
            return {"id": "r1", "status": "pending", "response_json": None}

        # _now() returns values that jump past the timeout on the first check
        # Sequence: deadline = start + timeout; first call sets start, second exceeds it.
        now_values = iter([0.0, 0.0, 999.0])

        def fake_now() -> float:
            return next(now_values)

        monkeypatch.setenv("CANOPY_WEB_PAT", "tok")
        with patch("scripts.ddd.review.get_review", side_effect=fake_get_review):
            from scripts.ddd import review as rv
            with pytest.raises(TimeoutError):
                rv.await_resolution(
                    "r1",
                    poll_interval=5.0,
                    timeout=10.0,
                    base_url="https://canopy.test",
                    _sleep=lambda _: None,
                    _now=fake_now,
                )

    def test_no_sleep_on_immediately_resolved(self, monkeypatch):
        """If status == resolved on first poll, no sleep is called."""

        def fake_get_review(review_id, **kwargs):
            return {"id": "r1", "status": "resolved", "response_json": {"ok": True}}

        sleeps: list[float] = []
        clock = [0.0]

        def fake_now() -> float:
            val = clock[0]
            clock[0] += 1.0
            return val

        monkeypatch.setenv("CANOPY_WEB_PAT", "tok")
        with patch("scripts.ddd.review.get_review", side_effect=fake_get_review):
            from scripts.ddd import review as rv
            result = rv.await_resolution(
                "r1",
                _sleep=lambda s: sleeps.append(s),
                _now=fake_now,
                base_url="https://canopy.test",
            )

        assert result == {"ok": True}
        assert sleeps == []


# ---------------------------------------------------------------------------
# Token resolution
# ---------------------------------------------------------------------------

class TestTokenResolution:
    def test_env_var_takes_precedence_over_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CANOPY_WEB_PAT", "env-token")
        # Even if the file exists with different content, env wins
        token_file = tmp_path / "workbench-token"
        token_file.write_text("file-token")

        import scripts.ddd.review as rv
        # TOKEN_FILE now lives in scripts.ddd.auth (the shared auth module);
        # _resolve_token reads it there, so patch it there.
        monkeypatch.setattr("orchestrator.canopy_web.TOKEN_FILE", token_file)

        assert rv._resolve_token(None) == "env-token"

    def test_raises_when_no_token(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CANOPY_WEB_PAT", raising=False)
        import scripts.ddd.review as rv
        # Point TOKEN_FILE (in scripts.ddd.auth) at a non-existent path
        monkeypatch.setattr("orchestrator.canopy_web.TOKEN_FILE", tmp_path / "no-token")
        with pytest.raises(RuntimeError, match="no canopy-web PAT"):
            rv._resolve_token(None)

    def test_explicit_token_wins(self, monkeypatch):
        monkeypatch.setenv("CANOPY_WEB_PAT", "env-token")
        import scripts.ddd.review as rv
        assert rv._resolve_token("explicit-token") == "explicit-token"


# ---------------------------------------------------------------------------
# get_narrative / narrative_version_exists — the upload guard's probe
# ---------------------------------------------------------------------------

class TestNarrativeExistence:
    def test_get_narrative_returns_detail(self, monkeypatch):
        monkeypatch.setenv("CANOPY_WEB_PAT", "tok")
        body = {"slug": "verified-monitoring", "current_version": {"version": 1}}
        with patch("urllib.request.urlopen", _mock_urlopen(body)):
            from scripts.ddd import review as rv
            got = rv.get_narrative("verified-monitoring", base_url="https://canopy.test")
        assert got == body

    def test_get_narrative_404_returns_none(self, monkeypatch):
        monkeypatch.setenv("CANOPY_WEB_PAT", "tok")

        def raise_404(req, **kwargs):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

        with patch("urllib.request.urlopen", side_effect=raise_404):
            from scripts.ddd import review as rv
            assert rv.get_narrative("nope", base_url="https://canopy.test") is None

    def test_get_narrative_non_404_propagates(self, monkeypatch):
        monkeypatch.setenv("CANOPY_WEB_PAT", "tok")

        def raise_500(req, **kwargs):
            raise urllib.error.HTTPError(req.full_url, 500, "Boom", {}, None)

        with patch("urllib.request.urlopen", side_effect=raise_500):
            from scripts.ddd import review as rv
            with pytest.raises(urllib.error.HTTPError):
                rv.get_narrative("x", base_url="https://canopy.test")

    def test_exists_true_when_current_version(self, monkeypatch):
        monkeypatch.setenv("CANOPY_WEB_PAT", "tok")
        body = {"current_version": {"version": 2}, "versions": []}
        with patch("urllib.request.urlopen", _mock_urlopen(body)):
            from scripts.ddd import review as rv
            assert rv.narrative_version_exists("f", base_url="https://canopy.test") is True

    def test_exists_true_when_version_row_present(self, monkeypatch):
        monkeypatch.setenv("CANOPY_WEB_PAT", "tok")
        body = {"current_version": None, "versions": [{"version": 1}, {"version": None}]}
        with patch("urllib.request.urlopen", _mock_urlopen(body)):
            from scripts.ddd import review as rv
            assert rv.narrative_version_exists("f", base_url="https://canopy.test") is True

    def test_exists_false_when_only_none_bucket(self, monkeypatch):
        """The exact 'no narrative' shape: artifacts exist but every version is
        the null bucket → no narrative version → guard must see False."""
        monkeypatch.setenv("CANOPY_WEB_PAT", "tok")
        body = {"current_version": None, "versions": [{"version": None}]}
        with patch("urllib.request.urlopen", _mock_urlopen(body)):
            from scripts.ddd import review as rv
            assert rv.narrative_version_exists("f", base_url="https://canopy.test") is False

    def test_exists_false_when_narrative_absent(self, monkeypatch):
        monkeypatch.setenv("CANOPY_WEB_PAT", "tok")

        def raise_404(req, **kwargs):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

        with patch("urllib.request.urlopen", side_effect=raise_404):
            from scripts.ddd import review as rv
            assert rv.narrative_version_exists("gone", base_url="https://canopy.test") is False


@pytest.mark.usefixtures("pinned_write_workspace")
class TestResolveReview:
    """resolve_review POSTs the decision to the canopy-web submit endpoint
    (Bearer-PAT authed → attributed), so an agent can record an approval a human
    already gave without forcing a UI click."""

    def test_posts_to_submit_endpoint_with_response_json(self, monkeypatch):
        captured = {}

        def fake_urlopen(req, **kwargs):
            captured["url"] = req.full_url
            captured["auth"] = dict(req.headers).get("Authorization")
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResponse({"id": "rev1", "status": "resolved"})

        monkeypatch.setenv("CANOPY_WEB_PAT", "pat-xyz")
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            from scripts.ddd import review as rv

            rv.resolve_review("rev1", {"publish": "publish"}, base_url="https://canopy.test")

        assert captured["url"] == "https://canopy.test/api/w/connect/reviews/rev1/submit/"
        assert captured["auth"] == "Bearer pat-xyz"
        assert captured["body"] == {"response_json": {"publish": "publish"}}
