"""canopy-web#1411: an unpinned list by a caller in two tenant trees is a 422.

The client keeps calling flat, and on that refusal reads within the HOME
workspace's tree (CANOPY_WEB_WORKSPACE, or the agent's own ~/.<slug>/.env) —
fanned out per workspace and merged — or, with no home, raises an error that
says what to set. By-id reads and single-tree callers never see a difference.
"""
import json

import pytest

from orchestrator import canopy_web as cw

REFUSAL = json.dumps({
    "type": "https://canopy.dimagi.com/problems/workspace-required",
    "title": "Workspace required", "status": 422,
    "detail": "your workspaces span dimagi (dimagi, connect), marshellis (marshellis)",
})
WORKSPACES = [
    {"slug": "marshellis", "parent": None},
    {"slug": "connect", "parent": "dimagi"},
    {"slug": "dimagi", "parent": None},
]
AGENTS = {
    "dimagi": [{"slug": "ada", "workspace": "dimagi"}, {"slug": "eva", "workspace": "dimagi"}],
    "connect": [{"slug": "echo", "workspace": "connect"}, {"slug": "hal", "workspace": "connect"}],
    "marshellis": [{"slug": "family", "workspace": "marshellis"}],
}


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.delenv("CANOPY_WEB_WORKSPACE", raising=False)
    monkeypatch.setattr(cw.Path, "home", classmethod(lambda cls: tmp_path))
    cw._reset_tree_cache()
    cw._ANNOUNCED_TREES.clear()


def multi_tree_server(seen):
    """A canopy-web that enforces the read rule for a dimagi+connect+marshellis caller."""
    def transport(method, url, headers, body):
        path = url.split("https://x.test", 1)[1]
        seen.append((method, path))
        if path == "/api/workspaces/":
            return 200, json.dumps(WORKSPACES)
        if path.startswith("/api/w/"):
            ws, rest = path[len("/api/w/"):].split("/", 1)
            if rest.startswith("agents/?") or rest == "agents/":
                return 200, json.dumps({"items": AGENTS[ws], "total": len(AGENTS[ws]),
                                        "offset": 0, "limit": 100})
            if rest.startswith("harness/turns/"):
                return 200, json.dumps([{"id": f"t-{ws}"}])
            if rest.startswith("ddd/narratives/"):
                return 200, json.dumps({"slug": "n", "workspace": ws})
            if rest == "agents/echo/":
                return 404, '{"detail":"not found"}'
            return 200, "{}"
        if path in ("/api/agents/", "/api/harness/turns/?limit=200") or path.startswith("/api/ddd/"):
            return 422, REFUSAL
        if path in ("/api/agents/echo/", "/api/agents/ada/"):
            slug = path.split("/")[3]
            return 200, json.dumps({"slug": slug, "workspace": "connect" if slug == "echo" else "dimagi"})
        return 200, "{}"
    return transport


def test_a_by_id_read_stays_flat_and_reaches_a_sibling_workspace(monkeypatch):
    monkeypatch.setenv("CANOPY_WEB_WORKSPACE", "dimagi")
    seen = []
    out = cw.call("GET", "/api/agents/echo/", base_url="https://x.test", token="t",
                  transport=multi_tree_server(seen))
    assert out["slug"] == "echo"
    assert seen == [("GET", "/api/agents/echo/")]  # no pin → no 404 from /api/w/dimagi/


def test_a_refused_list_reads_the_home_tree_and_never_the_other_org(monkeypatch, capsys):
    monkeypatch.setenv("CANOPY_WEB_WORKSPACE", "dimagi")
    seen = []
    out = cw.call("GET", "/api/agents/", base_url="https://x.test", token="t",
                  transport=multi_tree_server(seen))
    assert sorted(a["slug"] for a in out["items"]) == ["ada", "echo", "eva", "hal"]
    assert out["total"] == 4
    assert ("GET", "/api/w/marshellis/agents/") not in seen
    assert ("GET", "/api/w/dimagi/agents/") in seen and ("GET", "/api/w/connect/agents/") in seen
    assert "reading lists within dimagi's organisation (dimagi, connect)" in capsys.readouterr().err


def test_bare_list_endpoints_merge_too(monkeypatch):
    monkeypatch.setenv("CANOPY_WEB_WORKSPACE", "connect")
    seen = []
    out = cw.call("GET", "/api/harness/turns/?limit=200", base_url="https://x.test", token="t",
                  transport=multi_tree_server(seen))
    assert [r["id"] for r in out] == ["t-connect", "t-dimagi"]  # home first
    assert ("GET", "/api/w/connect/harness/turns/?limit=200") in seen


def test_an_unmergeable_read_takes_the_home_workspace_answer(monkeypatch):
    monkeypatch.setenv("CANOPY_WEB_WORKSPACE", "dimagi")
    out = cw.call("GET", "/api/ddd/narratives/n/", base_url="https://x.test", token="t",
                  transport=multi_tree_server([]))
    assert out == {"slug": "n", "workspace": "dimagi"}


def test_no_home_workspace_is_a_clear_error_not_a_guess():
    seen = []
    with pytest.raises(cw.ReadWorkspaceRequiredError, match="CANOPY_WEB_WORKSPACE=<slug>") as ei:
        cw.call("GET", "/api/agents/", base_url="https://x.test", token="t",
                transport=multi_tree_server(seen))
    assert "marshellis" in str(ei.value)  # the server's detail is passed on
    assert isinstance(ei.value, cw.CanopyError)  # callers that degrade on CanopyError still do
    assert seen == [("GET", "/api/agents/")]  # nothing pinned, nothing guessed


def test_a_home_the_caller_is_not_in_is_refused(monkeypatch):
    monkeypatch.setenv("CANOPY_WEB_WORKSPACE", "acme")
    with pytest.raises(cw.ReadWorkspaceRequiredError, match="not one of your"):
        cw.call("GET", "/api/agents/", base_url="https://x.test", token="t",
                transport=multi_tree_server([]))


def test_an_agent_reads_its_home_from_its_own_env_file(monkeypatch, tmp_path):
    (tmp_path / ".ada").mkdir()
    (tmp_path / ".ada" / ".env").write_text("CANOPY_WEB_PAT=p\nCANOPY_WEB_WORKSPACE=dimagi\n")
    monkeypatch.setenv("CANOPY_AGENT", "ada")
    assert cw.resolve_home_workspace() == "dimagi"
    monkeypatch.setenv("CANOPY_WEB_WORKSPACE", "connect")  # env still wins
    assert cw.resolve_home_workspace() == "connect"


def test_the_agent_env_home_does_not_pin_scoped_apps(monkeypatch, tmp_path):
    """The env-file home only answers a refusal; it must not narrow what already works."""
    (tmp_path / ".ada").mkdir()
    (tmp_path / ".ada" / ".env").write_text("CANOPY_WEB_WORKSPACE=dimagi\n")
    monkeypatch.setenv("CANOPY_AGENT", "ada")
    assert cw.scoped_api_path("/api/projects/") == "/api/projects/"


def test_a_single_tree_caller_sees_no_change():
    seen = []

    def transport(method, url, headers, body):
        seen.append(url)
        return 200, json.dumps({"items": [{"slug": "ada"}], "total": 1})

    out = cw.call("GET", "/api/agents/", base_url="https://x.test", token="t", transport=transport)
    assert out["total"] == 1 and seen == ["https://x.test/api/agents/"]


def test_call_text_reads_a_refused_path_in_the_home_workspace(monkeypatch):
    monkeypatch.setenv("CANOPY_WEB_WORKSPACE", "dimagi")
    seen = []

    def transport(method, url, headers, body):
        path = url.split("https://x.test", 1)[1]
        seen.append(path)
        if path == "/api/workspaces/":
            return 200, json.dumps(WORKSPACES)
        if path.startswith("/api/w/"):
            return 200, "line\n"
        return 422, REFUSAL

    assert cw.call_text("GET", "/api/x/", base_url="https://x.test", token="t",
                        transport=transport) == "line\n"
    assert seen == ["/api/x/", "/api/workspaces/", "/api/w/dimagi/x/"]


def test_a_refused_write_is_pinned_to_home_only(monkeypatch):
    monkeypatch.setenv("CANOPY_WEB_WORKSPACE", "dimagi")
    seen = []

    def transport(method, url, headers, body):
        path = url.split("https://x.test", 1)[1]
        seen.append((method, path))
        if path == "/api/workspaces/":
            return 200, json.dumps(WORKSPACES)
        if path.startswith("/api/w/"):
            return 200, json.dumps({"reset": 1})
        return 422, REFUSAL

    out = cw.call("POST", "/api/canopy-sessions/reset/", {}, base_url="https://x.test",
                  token="t", transport=transport)
    assert out == {"reset": 1}
    assert ("POST", "/api/w/connect/canopy-sessions/reset/") not in seen


@pytest.mark.parametrize("path", ["/api/me/", "/api/workspaces/", "/api/workspaces/dimagi/members/",
                                  "/api/invites/abc/accept/", "/api/harness/runners/r1/claim/",
                                  "/api/w/dimagi/agents/", "/healthz"])
def test_pinned_api_path_leaves_flat_only_routes_alone(path):
    assert cw.pinned_api_path(path, "dimagi") == path


@pytest.mark.parametrize("path,want", [
    ("/api/agents/", "/api/w/dimagi/agents/"),
    ("/api/harness/turns/?limit=200", "/api/w/dimagi/harness/turns/?limit=200"),
    ("/api/canopy-sessions/search?q=x", "/api/w/dimagi/canopy-sessions/search?q=x"),
    ("/api/tasks/", "/api/w/dimagi/tasks/"),
])
def test_pinned_api_path_pins_every_tenant_app(path, want):
    assert cw.pinned_api_path(path, "dimagi") == want


def test_is_workspace_required_matches_only_the_problem_type():
    assert cw.is_workspace_required(422, REFUSAL)
    assert not cw.is_workspace_required(422, json.dumps({"type": "about:blank", "detail": "bad"}))
    assert not cw.is_workspace_required(404, REFUSAL)


def test_an_agent_with_no_workspace_configured_reads_in_its_records_organisation(monkeypatch):
    """No env, no env-file value: the agent record says where Ada lives — no re-provisioning."""
    monkeypatch.setenv("CANOPY_AGENT", "ada")
    seen = []
    out = cw.call("GET", "/api/agents/", base_url="https://x.test", token="t",
                  transport=multi_tree_server(seen))
    assert sorted(a["slug"] for a in out["items"]) == ["ada", "echo", "eva", "hal"]
    assert ("GET", "/api/agents/ada/") in seen
    assert ("GET", "/api/w/marshellis/agents/") not in seen
