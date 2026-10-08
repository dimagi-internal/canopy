# tests/test_canopy_web.py
import json
from pathlib import Path
import pytest
from orchestrator import canopy_web as cw


def test_resolve_base_url_precedence(monkeypatch):
    assert cw.resolve_base_url("https://x.test/") == "https://x.test"   # arg wins, trailing slash stripped
    monkeypatch.setenv("CANOPY_WEB_API_URL", "https://env.test/")
    assert cw.resolve_base_url(None) == "https://env.test"
    monkeypatch.delenv("CANOPY_WEB_API_URL", raising=False)
    assert cw.resolve_base_url(None) == cw.DEFAULT_API


def test_resolve_token_precedence(monkeypatch, tmp_path):
    monkeypatch.setattr(cw, "TOKEN_FILE", tmp_path / "missing")
    monkeypatch.delenv("CANOPY_WEB_PAT", raising=False)
    assert cw.resolve_token("raw-arg") == "raw-arg"
    monkeypatch.setenv("CANOPY_WEB_PAT", "env-tok")
    assert cw.resolve_token(None) == "env-tok"
    monkeypatch.delenv("CANOPY_WEB_PAT", raising=False)
    tf = tmp_path / "tok"
    tf.write_text("file-tok\n")
    monkeypatch.setattr(cw, "TOKEN_FILE", tf)
    assert cw.resolve_token(None) == "file-tok"


def test_resolve_token_missing_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("CANOPY_WEB_PAT", raising=False)
    monkeypatch.setattr(cw, "TOKEN_FILE", tmp_path / "missing")
    with pytest.raises(RuntimeError, match="canopy-web PAT"):
        cw.resolve_token(None)


def test_call_uses_transport_and_parses_json():
    seen = {}

    def fake(method, url, headers, body):
        seen.update(method=method, url=url, headers=headers, body=body)
        return 200, json.dumps({"ok": True})

    out = cw.call("POST", "/api/agents/", {"slug": "x"},
                  base_url="https://x.test", token="t", transport=fake)
    assert out == {"ok": True}
    assert seen["method"] == "POST"
    assert seen["url"] == "https://x.test/api/agents/"
    assert seen["headers"]["Authorization"] == "Bearer t"
    assert json.loads(seen["body"]) == {"slug": "x"}


def test_call_raises_canopy_error_on_4xx():
    def fake(method, url, headers, body):
        return 404, "nope"
    with pytest.raises(cw.CanopyError, match="404"):
        cw.call("GET", "/api/agents/x/", base_url="https://x.test", token="t", transport=fake)


def test_call_get_has_no_body():
    def fake(method, url, headers, body):
        assert body is None
        return 200, "[]"
    assert cw.call("GET", "/api/x", base_url="https://x.test", token="t", transport=fake) == []


def test_urllib_transport_builds_request(monkeypatch):
    captured = {}

    class FakeResp:
        status = 201
        def read(self): return b'{"created": 1}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req):
        captured["method"] = req.get_method()
        captured["url"] = req.full_url
        captured["body"] = req.data
        return FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    status, text = cw.urllib_transport("PUT", "https://x.test/api/x",
                                       {"Authorization": "Bearer t"}, b'{"a":1}')
    assert (status, text) == (201, '{"created": 1}')
    assert captured["method"] == "PUT"
    assert captured["body"] == b'{"a":1}'


# --- An agent never borrows the operator's identity ----------------------------
#
# `resolve_token` prefers the agent's own PAT; the operator's
# `~/.claude/canopy/workbench-token` used to be a WARNED fallback. That was not
# enough: on the owner's machines the file is Jonathan's PAT, so every agent's board
# drain and dispatch acted as him with his owner rights (canopy#813), and a narrative
# was posted as him into the wrong workspace (ace#2805). Measured 2026-07-28 from the
# ace repo: `/api/agents/` returned HIS view — five agents across two workspaces.
#
# Now, in an agent's session the fallback is a refusal. A human working in an agent
# repo opts back in explicitly with CANOPY_ALLOW_OPERATOR_IDENTITY=1 (still warned).

def _agent_repo(tmp_path, slug):
    repo = tmp_path / "repos" / slug
    (repo / ".claude-plugin").mkdir(parents=True)
    (repo / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": slug}))
    return repo


def _home_with_operator_token(monkeypatch, tmp_path, token="operator-token"):
    """A home whose workbench-token holds the HUMAN's PAT — the planted token."""
    home = tmp_path / "home"
    (home / ".claude" / "canopy").mkdir(parents=True)
    token_file = home / ".claude" / "canopy" / "workbench-token"
    token_file.write_text(token)
    monkeypatch.delenv("CANOPY_WEB_PAT", raising=False)
    monkeypatch.setattr(cw, "TOKEN_FILE", token_file)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    cw._reset_identity_warning()
    return home


def test_an_agent_repo_without_its_own_pat_refuses_the_operator_token(monkeypatch, tmp_path):
    _home_with_operator_token(monkeypatch, tmp_path)
    monkeypatch.chdir(_agent_repo(tmp_path, "ace"))

    with pytest.raises(cw.AgentIdentityError) as exc:
        cw.resolve_token(None)
    msg = str(exc.value)
    assert "'ace'" in msg, "the refusal must name WHICH agent"
    assert "CANOPY_WEB_PAT" in msg and "~/.ace/.env" in msg, "and how to fix it"


def test_canopy_agent_env_marks_an_agent_session_from_any_cwd(monkeypatch, tmp_path):
    _home_with_operator_token(monkeypatch, tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    monkeypatch.setenv("CANOPY_AGENT", "ace")

    with pytest.raises(cw.AgentIdentityError):
        cw.resolve_token(None)


def test_a_dispatched_turns_envelope_marks_an_agent_session(monkeypatch, tmp_path):
    """A turn canopy-web dispatched AT an agent is that agent's, wherever it runs."""
    _home_with_operator_token(monkeypatch, tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    envelope = tmp_path / "envelope.json"
    envelope.write_text(json.dumps({"turn_id": "t", "agent": "ace"}))
    monkeypatch.setenv("CANOPY_CALLER", str(envelope))

    assert cw.agent_context_slug() == "ace"
    with pytest.raises(cw.AgentIdentityError):
        cw.resolve_token(None)


def test_a_dispatched_turn_found_by_turn_id_marks_an_agent_session(monkeypatch, tmp_path):
    from orchestrator import provenance

    root = tmp_path / "caller"
    root.mkdir()
    turn = "87665c09-9fb6-43be-af49-040d02939f81"
    (root / f"{turn}.json").write_text(json.dumps({"turn_id": turn, "agent": "hal"}))
    monkeypatch.setattr(provenance, "CALLER_ROOT", root)
    monkeypatch.setenv("CANOPY_TURN_ID", turn)
    monkeypatch.chdir(tmp_path)

    assert cw.agent_context_slug() == "hal"


def test_a_project_turn_is_not_an_agent_session(monkeypatch, tmp_path):
    """A turn aimed at a REPO has no agent (`agent: null`) — the operator's own work."""
    _home_with_operator_token(monkeypatch, tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    envelope = tmp_path / "envelope.json"
    envelope.write_text(json.dumps({"turn_id": "t", "agent": None}))
    monkeypatch.setenv("CANOPY_CALLER", str(envelope))

    assert cw.agent_context_slug() == ""
    assert cw.resolve_token(None) == "operator-token"


def test_the_operator_token_copied_into_the_env_is_still_refused(monkeypatch, tmp_path):
    """`export CANOPY_WEB_PAT=$(cat workbench-token)` in a shell profile is the same
    leak by another door: the agent would inherit the human's PAT from the env."""
    _home_with_operator_token(monkeypatch, tmp_path)
    monkeypatch.chdir(_agent_repo(tmp_path, "ace"))
    monkeypatch.setenv("CANOPY_WEB_PAT", "operator-token")

    with pytest.raises(cw.AgentIdentityError, match="workbench-token"):
        cw.resolve_token(None)


def test_a_planted_workbench_token_is_never_used_in_agent_context(monkeypatch, tmp_path, capsys,
                                                                  real_identity_readback):
    """canopy#813's acceptance test: the human's PAT sits in the workbench-token, the
    agent has its own — every call, through the real CLI client, carries the agent's,
    and the write announces the identity canopy-web confirms."""
    from orchestrator.agent_client import AgentClient

    home = _home_with_operator_token(monkeypatch, tmp_path, token="HUMAN-PAT")
    (home / ".ace").mkdir()
    (home / ".ace" / ".env").write_text("CANOPY_WEB_PAT=ace-own-pat\n")
    monkeypatch.setenv("CANOPY_AGENT", "ace")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CANOPY_WEB_API_URL", "https://x.test")

    bearers = []

    def fake(method, url, headers, body):
        bearers.append(headers["Authorization"])
        if url.endswith("/api/me/"):
            who = "ace@dimagi-ai.com" if headers["Authorization"] == "Bearer ace-own-pat" \
                else "jjackson@dimagi.com"
            return 200, json.dumps({"email": who, "name": "ACE"})
        return 200, "[]"

    client = AgentClient({"slug": "ace"}, transport=fake)
    client.list_tasks()
    client.create_tasks([{"title": "x"}])
    client.patch_task("t1", status="done")

    assert bearers and set(bearers) == {"Bearer ace-own-pat"}
    assert "HUMAN-PAT" not in "".join(bearers)
    err = capsys.readouterr().err
    assert err.count("[canopy] writing as") == 1, "once per process, not per write"
    assert "ace@dimagi-ai.com" in err and "agent 'ace'" in err


def test_opting_in_lets_a_human_act_as_themselves_with_a_warning(monkeypatch, tmp_path, capsys):
    _home_with_operator_token(monkeypatch, tmp_path)
    monkeypatch.chdir(_agent_repo(tmp_path, "ace"))
    monkeypatch.setenv("CANOPY_ALLOW_OPERATOR_IDENTITY", "1")

    for _ in range(3):
        assert cw.resolve_token(None) == "operator-token"
    # One warning per process — count EMISSIONS, not mentions of the slug.
    err = capsys.readouterr().err
    assert err.count("[canopy] WARNING") == 1 and "ace" in err


def test_strict_callers_ignore_the_opt_in(monkeypatch, tmp_path):
    """DDD + walkthrough-share writes publish under a name: never as the operator."""
    _home_with_operator_token(monkeypatch, tmp_path)
    monkeypatch.chdir(_agent_repo(tmp_path, "ace"))
    monkeypatch.setenv("CANOPY_ALLOW_OPERATOR_IDENTITY", "1")

    with pytest.raises(cw.AgentIdentityError):
        cw.resolve_token(None, agent_strict=True)


# --- Writes say who they act as -------------------------------------------------

def _me_transport(seen, me_status=200):
    def fake(method, url, headers, body):
        seen.append((method, url))
        if url.endswith("/api/me/"):
            return me_status, json.dumps({"email": "who@x.test", "name": "Who"})
        return 200, "{}"
    return fake


def test_a_write_reads_back_api_me_once(monkeypatch, capsys, real_identity_readback):
    monkeypatch.setenv("CANOPY_WEB_PAT", "p")
    seen = []
    fake = _me_transport(seen)
    cw.call("POST", "/api/x/", {}, base_url="https://x.test", transport=fake)
    cw.call("PATCH", "/api/x/1/", {}, base_url="https://x.test", transport=fake)

    assert seen == [("GET", "https://x.test/api/me/"), ("POST", "https://x.test/api/x/"),
                    ("PATCH", "https://x.test/api/x/1/")]
    err = capsys.readouterr().err
    assert err.count("[canopy] writing as who@x.test (Who)") == 1


def test_reads_and_explicit_tokens_do_not_read_back(monkeypatch, real_identity_readback):
    monkeypatch.setenv("CANOPY_WEB_PAT", "p")
    seen = []
    fake = _me_transport(seen)
    cw.call("GET", "/api/x/", base_url="https://x.test", transport=fake)
    cw.call("POST", "/api/x/", {}, base_url="https://x.test", token="t", transport=fake)
    assert ("GET", "https://x.test/api/me/") not in seen


def test_a_failed_read_back_warns_but_the_write_proceeds(monkeypatch, capsys, real_identity_readback):
    monkeypatch.setenv("CANOPY_WEB_PAT", "p")
    seen = []
    assert cw.call("POST", "/api/x/", {}, base_url="https://x.test",
                   transport=_me_transport(seen, me_status=500)) == {}
    assert ("POST", "https://x.test/api/x/") in seen
    assert "could not read /api/me/" in capsys.readouterr().err


def test_no_warning_when_the_agent_has_its_own_pat(monkeypatch, tmp_path, capsys):
    import orchestrator.canopy_web as cw

    repo = _agent_repo(tmp_path, "ace")
    home = tmp_path / "home"
    (home / ".ace").mkdir(parents=True)
    (home / ".ace" / ".env").write_text("CANOPY_WEB_PAT=ace-own-token\n")

    monkeypatch.delenv("CANOPY_WEB_PAT", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.chdir(repo)
    cw._reset_identity_warning()

    assert cw.resolve_token(None) == "ace-own-token"
    assert capsys.readouterr().err == ""


def test_no_warning_outside_an_agent_repo(monkeypatch, tmp_path, capsys):
    """An operator in an ordinary repo using their own token is the normal case."""
    import orchestrator.canopy_web as cw

    plain = tmp_path / "plain"
    plain.mkdir()
    home = tmp_path / "home"
    (home / ".claude" / "canopy").mkdir(parents=True)
    token_file = home / ".claude" / "canopy" / "workbench-token"
    token_file.write_text("operator-token")

    monkeypatch.delenv("CANOPY_WEB_PAT", raising=False)
    monkeypatch.setattr(cw, "TOKEN_FILE", token_file)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.chdir(plain)
    cw._reset_identity_warning()

    assert cw.resolve_token(None) == "operator-token"
    assert capsys.readouterr().err == ""


def test_no_warning_when_the_env_pins_the_identity(monkeypatch, tmp_path, capsys):
    """A runner pinning CANOPY_WEB_PAT is an explicit statement of identity."""
    import orchestrator.canopy_web as cw

    repo = _agent_repo(tmp_path, "ace")
    monkeypatch.setenv("CANOPY_WEB_PAT", "pinned")
    monkeypatch.chdir(repo)
    cw._reset_identity_warning()

    assert cw.resolve_token(None) == "pinned"
    assert capsys.readouterr().err == ""
