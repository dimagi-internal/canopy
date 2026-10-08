"""Workspace targeting for DDD → canopy-web (arg → env → per-repo config → None)."""
import pytest

from orchestrator import canopy_web as cw
from scripts.ddd import auth
from scripts.ddd import review as rv
from scripts.ddd import upload as up


# ---- resolve_workspace (generic) + scoped path helpers ----------------------

def test_resolve_workspace_precedence(monkeypatch):
    monkeypatch.delenv("CANOPY_WEB_WORKSPACE", raising=False)
    assert cw.resolve_workspace("connect") == "connect"          # arg wins
    monkeypatch.setenv("CANOPY_WEB_WORKSPACE", "envws")
    assert cw.resolve_workspace(None) == "envws"                 # env next
    monkeypatch.delenv("CANOPY_WEB_WORKSPACE", raising=False)
    assert cw.resolve_workspace(None) is None                    # else None (flat)


def test_scoped_api_path_rewrites_scoped_apps_only():
    assert cw.scoped_api_path("/api/walkthroughs/", "connect") == "/api/w/connect/walkthroughs/"
    assert cw.scoped_api_path("/api/reviews/abc/submit/", "connect") == "/api/w/connect/reviews/abc/submit/"
    assert cw.scoped_api_path("/api/ddd/narratives/x/", "connect") == "/api/w/connect/ddd/narratives/x/"
    # unscoped app + no-workspace are no-ops
    assert cw.scoped_api_path("/api/sessions/", "connect") == "/api/sessions/"
    assert cw.scoped_api_path("/api/walkthroughs/", None) == "/api/walkthroughs/"


def test_scoped_app_path_prefixes_browser_route():
    assert cw.scoped_app_path("/ddd/reef/reef-2026-06-01-001", "connect") == "/w/connect/ddd/reef/reef-2026-06-01-001"
    assert cw.scoped_app_path("/ddd/reef", None) == "/ddd/reef"


# ---- per-repo config source (auth.resolve_ddd_workspace) --------------------

def test_resolve_ddd_workspace_reads_repo_config(monkeypatch, tmp_path):
    monkeypatch.delenv("CANOPY_WEB_WORKSPACE", raising=False)
    (tmp_path / "config.yaml").write_text("workspace: connect\n")
    assert auth.resolve_ddd_workspace(ddd_dir=tmp_path) == "connect"
    # env overrides the file
    monkeypatch.setenv("CANOPY_WEB_WORKSPACE", "envws")
    assert auth.resolve_ddd_workspace(ddd_dir=tmp_path) == "envws"


def test_resolve_ddd_workspace_none_without_config(monkeypatch, tmp_path):
    monkeypatch.delenv("CANOPY_WEB_WORKSPACE", raising=False)
    assert auth.resolve_ddd_workspace(ddd_dir=tmp_path) is None


# ---- end-to-end: the DDD write URLs land in the workspace -------------------

def test_review_urls_are_workspace_scoped(monkeypatch):
    monkeypatch.setenv("CANOPY_WEB_WORKSPACE", "connect")
    api = "https://labs.connect.dimagi.com/canopy"
    assert rv._url(api, "/api/reviews/") == f"{api}/api/w/connect/reviews/"
    assert rv._url(api, "/api/ddd/narratives/reef/") == f"{api}/api/w/connect/ddd/narratives/reef/"


def test_upload_package_urls_are_workspace_scoped(monkeypatch):
    monkeypatch.setenv("CANOPY_WEB_WORKSPACE", "connect")
    base = "https://labs.connect.dimagi.com/canopy"
    assert up.run_package_url("reef", "reef-2026-06-01-001", base_url=base) == \
        f"{base}/w/connect/ddd/reef/reef-2026-06-01-001"
    assert up.narrative_landing_url("reef", base_url=base) == f"{base}/w/connect/ddd/reef"


def test_write_urls_flat_without_workspace(monkeypatch):
    monkeypatch.delenv("CANOPY_WEB_WORKSPACE", raising=False)
    api = "https://labs.connect.dimagi.com/canopy"
    # Unset → flat routes (server resolves the org default, dimagi). No repo config here.
    assert rv._url(api, "/api/reviews/", workspace=None).endswith("/api/reviews/")
    # …but a link a human opens is never flat: no workspace → refuse (canopy-web#1337).
    with pytest.raises(cw.WorkspaceRequiredError):
        up.run_package_url("reef", "r1", base_url=api)
