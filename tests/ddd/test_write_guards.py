"""DDD + walkthrough-share writes: whose name, which workspace, and where it landed.

ace#2805 / canopy-web#1289: an ACE session posted the chlorine narrative (meant
for `connect`) as Jonathan — his workbench-token — into `dimagi`, the server's
default for a flat route. Nothing said so. Three guards now stand in the way:

1. In an agent's session the operator's workbench-token is refused (test_auth.py).
2. A write with no workspace named refuses — no guessing from memberships —
   naming CANOPY_WEB_WORKSPACE and .canopy/ddd/config.yaml.
3. Every write is read back through the tenant-pinned list and the workspace it
   landed in is printed — absent there means it landed elsewhere, and that raises.

No network: canopy_web's transport is faked at `urllib_transport`.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from orchestrator import canopy_web as cw
from scripts.ddd import auth
from scripts.ddd.schemas.models import Decision, ReviewRequest

ROOT = Path(__file__).resolve().parents[2]


class FakeServer:
    """Answers canopy_web.call's GETs; records every request."""

    def __init__(self, workspaces=("connect",), landed=None):
        self.workspaces = list(workspaces)
        self.landed = landed or {}  # (workspace, app) -> [rows]
        self.requests: list[tuple[str, str]] = []

    def __call__(self, method, url, headers, body):
        self.requests.append((method, url))
        path = url.split("canopy.test", 1)[1]
        if path == "/api/workspaces/":
            return 200, json.dumps([{"slug": s} for s in self.workspaces])
        if path.startswith("/api/w/"):
            ws, app = path[len("/api/w/"):].split("/", 2)[:2]
            return 200, json.dumps(self.landed.get((ws, app), []))
        return 404, "{}"


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.delenv("CANOPY_WEB_WORKSPACE", raising=False)
    monkeypatch.setenv("CANOPY_WEB_PAT", "pat")
    monkeypatch.setenv("CANOPY_WEB_API_URL", "https://canopy.test")
    # No per-repo .canopy/ddd/config.yaml in scope.
    monkeypatch.setattr("scripts.ddd.runstate._resolve_ddd_dir", lambda: tmp_path)


def _serve(monkeypatch, server):
    monkeypatch.setattr(cw, "urllib_transport", server)
    return server


def _review():
    return ReviewRequest(
        run_id="chlorine-2026-10-07-001",
        gate="concept_change",
        video={},
        decisions=[Decision(id="d1", prompt="Agree?", options=["agree"], recommended="agree",
                              **{"class": "go_nogo"})],
        narration=[],
    )


# ---- 2. no workspace named → refuse ----------------------------------------

def test_a_configured_workspace_is_used_without_any_lookup(monkeypatch):
    server = _serve(monkeypatch, FakeServer(workspaces=("dimagi", "connect")))
    monkeypatch.setenv("CANOPY_WEB_WORKSPACE", "connect")
    assert auth.require_write_workspace() == "connect"
    assert server.requests == []


def test_repo_config_names_the_workspace(monkeypatch, tmp_path):
    (tmp_path / "config.yaml").write_text("workspace: connect\n")
    assert auth.require_write_workspace() == "connect"


def test_no_workspace_named_refuses(monkeypatch):
    with pytest.raises(auth.WorkspaceRequiredError) as exc:
        auth.require_write_workspace()
    msg = str(exc.value)
    assert "CANOPY_WEB_WORKSPACE" in msg and ".canopy/ddd/config.yaml" in msg


def test_a_single_membership_is_not_a_workspace(monkeypatch):
    """Guessing from memberships is the server's default one step removed — the
    agent must name its workspace."""
    server = _serve(monkeypatch, FakeServer(workspaces=("connect",)))
    with pytest.raises(auth.WorkspaceRequiredError):
        auth.require_write_workspace()
    assert server.requests == []


def test_narrative_post_refuses_before_writing_anything(monkeypatch):
    """The live failure path: no POST may reach the flat /api/reviews/ route."""
    from scripts.ddd import review as rv

    posts = []
    monkeypatch.setattr(rv, "_json_request", lambda *a, **k: posts.append(a) or {})
    _serve(monkeypatch, FakeServer(workspaces=("dimagi", "connect")))
    with pytest.raises(auth.WorkspaceRequiredError):
        rv.post_review_request(_review())
    assert posts == []


def test_artifact_upload_refuses_before_uploading(monkeypatch):
    from scripts.ddd import upload as up

    _serve(monkeypatch, FakeServer(workspaces=("dimagi", "connect")))
    uploads = []
    with pytest.raises(auth.WorkspaceRequiredError):
        up.publish_artifact(b"mp4", kind="video", title="t", _post=lambda *a: uploads.append(a))
    assert uploads == []


# ---- 3. read back, tenant-pinned, and say where it landed --------------------

def test_narrative_post_goes_to_the_workspace_and_is_read_back(monkeypatch, capsys):
    from scripts.ddd import review as rv

    monkeypatch.setenv("CANOPY_WEB_WORKSPACE", "connect")
    posted = []

    def fake_post(method, url, token, body=None):
        posted.append(url)
        return {"id": "rev-1", "url": "https://canopy.test/review/rev-1/"}

    monkeypatch.setattr(rv, "_json_request", fake_post)
    server = _serve(monkeypatch, FakeServer(landed={("connect", "reviews"): [{"id": "rev-1"}]}))
    result = rv.post_review_request(_review())

    assert posted == ["https://canopy.test/api/w/connect/reviews/"]
    assert result["workspace"] == "connect"
    # Read back from the pinned list, filtered to this run.
    assert any("/api/w/connect/reviews/?q=chlorine-2026-10-07-001" in u for _, u in server.requests)
    assert "landed in workspace 'connect'" in capsys.readouterr().err


def test_a_write_missing_from_its_workspace_raises(monkeypatch):
    from scripts.ddd import review as rv

    monkeypatch.setenv("CANOPY_WEB_WORKSPACE", "connect")
    monkeypatch.setattr(rv, "_json_request", lambda *a, **k: {"id": "rev-1"})
    _serve(monkeypatch, FakeServer(landed={("connect", "reviews"): [{"id": "someone-else"}]}))
    with pytest.raises(auth.WorkspaceMismatchError, match="rev-1"):
        rv.post_review_request(_review())


def test_artifact_upload_is_read_back_with_its_owner(monkeypatch, capsys):
    from scripts.ddd import upload as up

    monkeypatch.setenv("CANOPY_WEB_WORKSPACE", "connect")
    _serve(monkeypatch, FakeServer(landed={
        ("connect", "walkthroughs"): [{"id": "w1", "owner_email": "ace@dimagi-ai.com"}],
    }))
    urls = []

    def fake_post(url, pat, fields, filename, content_type, file_bytes):
        urls.append(url)
        return {"id": "w1", "share_url": "https://canopy.test/walkthrough/w1?t=x"}

    assert up.publish_artifact(b"mp4", kind="video", title="t", _post=fake_post).endswith("?t=x")
    assert urls == ["https://canopy.test/api/w/connect/walkthroughs/"]
    err = capsys.readouterr().err
    assert "landed in workspace 'connect'" in err and "ace@dimagi-ai.com" in err


def test_a_failed_read_back_warns_but_does_not_hide_the_write(monkeypatch, capsys):
    calls = []

    def flaky(method, url, headers, body):
        calls.append(url)
        return 502, "bad gateway"

    monkeypatch.setattr(cw, "urllib_transport", flaky)
    assert cw.confirm_landed("walkthroughs", "w1", "connect", token="pat",
                             base_url="https://canopy.test") == {}
    assert "could not read it back" in capsys.readouterr().err


# ---- walkthrough-share: the same three guards --------------------------------

def _walkthrough_share():
    spec = importlib.util.spec_from_file_location(
        "walkthrough_share_upload", ROOT / "scripts" / "walkthrough-share" / "upload.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mp4(tmp_path):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"mp4")
    return f


def test_walkthrough_share_refuses_without_a_workspace(monkeypatch, tmp_path, capsys):
    ws = _walkthrough_share()
    _serve(monkeypatch, FakeServer(workspaces=("dimagi", "connect")))
    uploads = []
    monkeypatch.setattr(ws, "upload_multipart", lambda *a, **k: uploads.append(a) or (201, {}))
    with pytest.raises(SystemExit):
        ws.main([str(_mp4(tmp_path)), "--api-url", "https://canopy.test"])
    assert uploads == []
    assert "--workspace" in capsys.readouterr().err


def test_walkthrough_share_uploads_into_the_workspace_and_reads_back(monkeypatch, tmp_path, capsys):
    ws = _walkthrough_share()
    monkeypatch.setenv("CANOPY_WEB_WORKSPACE", "connect")
    _serve(monkeypatch, FakeServer(landed={("connect", "walkthroughs"): [{"id": "w9"}]}))
    endpoints = []

    def fake_upload(url, pat, **kw):
        endpoints.append(url)
        return 201, {"id": "w9"}

    monkeypatch.setattr(ws, "upload_multipart", fake_upload)
    assert ws.main([str(_mp4(tmp_path)), "--api-url", "https://canopy.test"]) == 0
    assert endpoints == ["https://canopy.test/api/w/connect/walkthroughs/"]
    assert "landed in workspace 'connect'" in capsys.readouterr().err


def test_walkthrough_share_refuses_the_operator_token_in_an_agent_session(monkeypatch, tmp_path, capsys):
    ws = _walkthrough_share()
    home = tmp_path / "home"
    home.mkdir()
    tok = tmp_path / "token"
    tok.write_text("operator-token")
    monkeypatch.delenv("CANOPY_WEB_PAT", raising=False)
    monkeypatch.setenv("CANOPY_AGENT", "ace")
    monkeypatch.setattr(cw, "TOKEN_FILE", tok)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    with pytest.raises(SystemExit):
        ws.main([str(_mp4(tmp_path)), "--workspace", "connect"])
    assert "CANOPY_WEB_PAT" in capsys.readouterr().err
