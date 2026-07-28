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


# --- Task: borrowing the operator's identity must not be silent --------------
#
# `resolve_token` prefers the agent's own PAT and falls back to the operator's
# `~/.claude/canopy/workbench-token`. The fallback is correct — a human working in
# an agent repo should not be blocked — but it was SILENT, so an agent whose env
# was never materialized acted as the human with nothing to reveal it.
#
# That is not hypothetical. ACE's `.env.tpl` carried a placeholder `op://` ref in
# a comment; `op inject` is all-or-nothing and aborted, so `~/.ace/.env` never
# existed (dimagi-internal/ace#1005). Measured 2026-07-28 before the fix, from the
# ace repo: `resolve_token` returned Jonathan's workbench token and `/api/agents/`
# returned HIS view — five agents across two workspaces — where ace alone should
# have been visible. Every canopy-web write ACE made on a laptop was attributed to
# a human, and ACE read tenants it has no permission for.
#
# Warn, don't refuse: refusing would break a human legitimately working inside an
# agent repo, which is common. The requirement is only that it stop being silent.

def _agent_repo(tmp_path, slug):
    repo = tmp_path / "repos" / slug
    (repo / ".claude-plugin").mkdir(parents=True)
    (repo / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": slug}))
    return repo


def test_falling_back_to_the_operator_token_inside_an_agent_repo_warns(monkeypatch, tmp_path, capsys):
    import orchestrator.canopy_web as cw

    repo = _agent_repo(tmp_path, "ace")
    home = tmp_path / "home"
    (home / ".claude" / "canopy").mkdir(parents=True)
    token_file = home / ".claude" / "canopy" / "workbench-token"
    token_file.write_text("operator-token")

    monkeypatch.delenv("CANOPY_WEB_PAT", raising=False)
    monkeypatch.setattr(cw, "TOKEN_FILE", token_file)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.chdir(repo)
    cw._reset_identity_warning()

    assert cw.resolve_token(None) == "operator-token"      # still works
    err = capsys.readouterr().err
    assert "ace" in err, "the warning must name WHICH agent is borrowing"
    assert "workbench-token" in err or "operator" in err.lower()


def test_the_warning_fires_once_per_process_not_per_call(monkeypatch, tmp_path, capsys):
    """A single command makes many calls; warning on each would train people to ignore it."""
    import orchestrator.canopy_web as cw

    repo = _agent_repo(tmp_path, "ace")
    home = tmp_path / "home"
    (home / ".claude" / "canopy").mkdir(parents=True)
    token_file = home / ".claude" / "canopy" / "workbench-token"
    token_file.write_text("operator-token")

    monkeypatch.delenv("CANOPY_WEB_PAT", raising=False)
    monkeypatch.setattr(cw, "TOKEN_FILE", token_file)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.chdir(repo)
    cw._reset_identity_warning()

    for _ in range(3):
        cw.resolve_token(None)
    # Count EMISSIONS, not mentions: one warning names the slug several times
    # (the repo, the env path, the remedy), so counting "ace" would read 6.
    assert capsys.readouterr().err.count("[canopy] WARNING") == 1


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
