"""`agent_bootstrap._operator_token()`: the operator's PAT for operator-authorized calls.

canopy#945: inside an agent's session CANOPY_WEB_PAT is usually the agent's own PAT, which
`/credentials/resolve` refuses — so there the workbench-token must come first.
"""
from orchestrator import agent_bootstrap, canopy_web


def _setup(monkeypatch, tmp_path, *, env="", stored="", slug=""):
    tf = tmp_path / "workbench-token"
    if stored:
        tf.write_text(stored + "\n")
    monkeypatch.setattr(canopy_web, "TOKEN_FILE", tf)
    monkeypatch.setattr(canopy_web, "agent_context_slug", lambda *a, **k: slug)
    if env:
        monkeypatch.setenv("CANOPY_WEB_PAT", env)
    else:
        monkeypatch.delenv("CANOPY_WEB_PAT", raising=False)


def test_in_an_agent_session_the_workbench_token_beats_the_agents_own_env(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, env="lex-own", stored="operator", slug="lex")
    assert agent_bootstrap._operator_token() == "operator"


def test_outside_an_agent_session_explicit_env_still_wins(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, env="explicit", stored="operator", slug="")
    assert agent_bootstrap._operator_token() == "explicit"


def test_an_agent_session_with_no_workbench_token_falls_back_to_env(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, env="lex-own", slug="lex")
    assert agent_bootstrap._operator_token() == "lex-own"


def test_is_agents_own_token_reads_the_agents_env_file(monkeypatch, tmp_path):
    monkeypatch.setattr(canopy_web.Path, "home", lambda: tmp_path)
    (tmp_path / ".lex").mkdir()
    (tmp_path / ".lex" / ".env").write_text('CANOPY_WEB_PAT="lex-own"\n')
    assert agent_bootstrap.is_agents_own_token("lex", "lex-own")
    assert not agent_bootstrap.is_agents_own_token("lex", "operator")
    assert not agent_bootstrap.is_agents_own_token("lex", "")
