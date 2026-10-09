"""One gating hook applies the SESSION's agent's rails (canopy#849).

WHY THIS FILE EXISTS (2026-10-09). Every agent plugin is installed at user scope, so a gating
hook an agent plugin registers fires in EVERY session on the machine: ACE's body-file rail
blocked a `gh pr create` in an Ada session. canopy now registers one PreToolUse hook
(`agent-core/gating_guard.py --session`) that works out whose session it is and applies that
agent's rails only; the per-agent loaders that still fire during the migration stand down
outside their own agent's session.

Drives the real engine against the real shipped baseline.
Run: uv run pytest tests/hooks/test_gating_session_agent.py
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = ROOT / "plugins" / "canopy"
GUARD = PLUGIN_DIR / "agent-core" / "gating_guard.py"

# A rail only ACE carries (shape of its real body-file rail) and one only Ada carries.
ACE_RAIL = {"tool": "Bash", "pattern": r"\bgh\s+pr\s+create\b(?![^\n]*--body-file)",
            "message": "BLOCKED by ace: use --body-file."}
ADA_RAIL = {"tool": "Bash", "pattern": r"\bada-forbidden\b", "message": "BLOCKED by ada rail."}

PR_CREATE = "gh pr create --title x --body y"
RAW_SEND = "gog gmail send --to a@b.c --subject s --body b"
ZSH_EQUALS = "echo ====="
PS_POST = "Invoke-RestMethod -Method Post -Uri https://example.com/x"


def _load():
    loader = importlib.machinery.SourceFileLoader("gating_guard_session", str(GUARD))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def guard(monkeypatch, tmp_path):
    monkeypatch.setenv("CANOPY_PLUGIN_DIR", str(PLUGIN_DIR))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))   # no real plugin cache leaks in
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    return _load()


def _agent_repo(path: Path, slug: str, deny, channels=("email", "gws")) -> Path:
    (path / "config").mkdir(parents=True, exist_ok=True)
    (path / "config" / "gating.json").write_text(json.dumps(
        {"slug": slug, "channels": list(channels), "deny": list(deny), "approve": []}))
    (path / "config" / "agent.json").write_text(json.dumps({"name": slug.title()}))
    (path / ".claude-plugin").mkdir(exist_ok=True)
    (path / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": slug}))
    return path


@pytest.fixture
def fleet(tmp_path):
    """An Ada worktree (named for its TASK, as emdash names them), an ACE repo, a plain repo."""
    ada = _agent_repo(tmp_path / "worktrees" / "emdash-ace-gdrive-b7vnj", "ada", [ADA_RAIL])
    ace = _agent_repo(tmp_path / "repos" / "ace", "ace", [ACE_RAIL], channels=())
    plain = tmp_path / "repos" / "plain"
    (plain / "src").mkdir(parents=True)
    return {"ada": ada, "ace": ace, "plain": plain}


def bash(cmd, cwd=None):
    p = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    if cwd:
        p["cwd"] = str(cwd)
    return p


def session(guard, payload, **env):
    return guard.run_session(payload, env={k: str(v) for k, v in env.items()})[0]


def loader(guard, repo, payload, **env):
    return guard.run_loader(str(repo), payload, env={k: str(v) for k, v in env.items()})[0]


# ---------------------------------------------------------------- who is the session?

def test_slug_comes_from_config_not_the_worktree_name(guard, fleet):
    assert guard.session_agent({}, {"CLAUDE_PROJECT_DIR": str(fleet["ada"] / "bin")}) == \
        ("ada", str(fleet["ada"]))


def test_payload_cwd_is_the_fallback_when_no_project_dir(guard, fleet):
    assert guard.session_agent({"cwd": str(fleet["ace"])}, {})[0] == "ace"


def test_plain_repo_belongs_to_nobody(guard, fleet):
    assert guard.session_agent({}, {"CLAUDE_PROJECT_DIR": str(fleet["plain"] / "src")}) == ("", None)


def test_canopy_itself_is_not_an_agent(guard, tmp_path):
    _agent_repo(tmp_path / "canopy", "canopy", [])
    assert guard.session_agent({}, {"CLAUDE_PROJECT_DIR": str(tmp_path / "canopy")}) == ("", None)


# ---------------------------------------------------------------- Ada session vs ACE rails

def test_ada_session_ignores_ace_rails_and_applies_its_own(guard, fleet):
    """The 2026-10-09 incident: ACE's body-file rail blocked a PR in an Ada session."""
    env = {"CLAUDE_PROJECT_DIR": fleet["ada"], "CANOPY_AGENT": "ada"}
    assert session(guard, bash(PR_CREATE), **env) == 0
    assert session(guard, bash("ada-forbidden now"), **env) == 2
    assert session(guard, bash(RAW_SEND), **env) == 2          # ada mounts email
    # ACE's legacy plugin hook still fires during the migration — and must stand down here.
    assert loader(guard, fleet["ace"], bash(PR_CREATE), **env) == 0
    # Ada's own legacy loader still enforces Ada's rails in Ada's session.
    assert loader(guard, fleet["ada"], bash("ada-forbidden now"), **env) == 2


def test_ace_session_applies_ace_rails(guard, fleet):
    env = {"CLAUDE_PROJECT_DIR": fleet["ace"]}
    assert session(guard, bash(PR_CREATE), **env) == 2
    assert session(guard, bash(PR_CREATE + " --body-file b.md"), **env) == 0
    assert session(guard, bash("ada-forbidden now"), **env) == 0
    assert loader(guard, fleet["ace"], bash(PR_CREATE), **env) == 2
    assert loader(guard, fleet["ada"], bash("ada-forbidden now"), **env) == 0


# ---------------------------------------------------------------- nobody's session

def test_no_agent_repo_gets_channel_independent_baseline_only(guard, fleet):
    env = {"CLAUDE_PROJECT_DIR": fleet["plain"]}
    assert session(guard, bash(PR_CREATE), **env) == 0          # no ACE rail
    assert session(guard, bash("ada-forbidden now"), **env) == 0  # no Ada rail
    assert session(guard, bash(RAW_SEND), **env) == 0           # channel rails name an agent's path
    code, _, err = guard.run_session(bash(ZSH_EQUALS), env={"CLAUDE_PROJECT_DIR": str(fleet["plain"])})
    assert code == 2 and "{slug}" not in err                    # `always` rails still apply
    ps = {"tool_name": "PowerShell", "tool_input": {"command": PS_POST}}
    assert session(guard, ps, **env) == 0                       # agent_only rail skipped
    assert loader(guard, fleet["ace"], bash(PR_CREATE), **env) == 0
    assert loader(guard, fleet["ada"], bash("ada-forbidden now"), **env) == 0


def test_agent_only_rail_still_applies_to_agents(guard, fleet):
    ps = {"tool_name": "PowerShell", "tool_input": {"command": PS_POST}}
    assert session(guard, ps, CLAUDE_PROJECT_DIR=fleet["ada"]) == 2


def test_no_agent_session_never_fails_closed(guard, fleet, monkeypatch, tmp_path):
    monkeypatch.setenv("CANOPY_PLUGIN_DIR", str(tmp_path / "nonexistent"))
    assert session(guard, bash(RAW_SEND), CLAUDE_PROJECT_DIR=fleet["plain"]) == 0


# ---------------------------------------------------------------- runner env wins

def _install(home: Path, slug: str, version: str, deny, channels=()):
    """Lay an agent plugin into a fake ~/.claude/plugins/cache, as `claude plugin install` does."""
    path = home / ".claude" / "plugins" / "cache" / slug / slug / version
    _agent_repo(path, slug, deny, channels=channels)
    return path


def test_runner_env_slug_wins_over_cwd(guard, fleet, tmp_path):
    """A runner turn for ACE working inside an Ada checkout is still ACE's turn."""
    _install(tmp_path / "home", "ace", "0.13.9", [ACE_RAIL])
    newest = _install(tmp_path / "home", "ace", "0.13.10", [ACE_RAIL])
    env = {"CANOPY_AGENT_SLUG": "ace", "CANOPY_AGENT": "ada", "CLAUDE_PROJECT_DIR": fleet["ada"]}
    assert guard.session_agent({}, {k: str(v) for k, v in env.items()}) == ("ace", str(newest))
    assert session(guard, bash(PR_CREATE), **env) == 2
    assert session(guard, bash("ada-forbidden now"), **env) == 0
    assert loader(guard, fleet["ada"], bash("ada-forbidden now"), **env) == 0


def test_registry_record_is_preferred_over_a_cache_scan(guard, fleet, tmp_path):
    home = tmp_path / "home"
    old = _install(home, "ace", "0.1.0", [ACE_RAIL])
    _install(home, "ace", "0.9.0", [ACE_RAIL])
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"plugins": {"ace@ace": [{"installPath": str(old)}]}}))
    assert guard.installed_agent_repo("ace") == str(old)


def test_known_agent_with_no_config_assumes_every_channel(guard, fleet):
    """Slug from env, config nowhere: a missing config must never let a raw send through."""
    env = {"CANOPY_AGENT_SLUG": "zed", "CLAUDE_PROJECT_DIR": fleet["plain"]}
    code, _, err = guard.run_session(bash(RAW_SEND), env={k: str(v) for k, v in env.items()})
    assert code == 2
    assert session(guard, bash("git status"), **env) == 0


# ---------------------------------------------------------------- fail closed, own agent

def test_own_agent_still_fails_closed_without_baseline(guard, fleet, monkeypatch, tmp_path):
    monkeypatch.setenv("CANOPY_PLUGIN_DIR", str(tmp_path / "nonexistent"))
    env = {"CLAUDE_PROJECT_DIR": fleet["ada"]}
    code, _, err = guard.run_session(bash("git status"), env={k: str(v) for k, v in env.items()})
    assert code == 2 and "fail closed" in err.lower()
    assert loader(guard, fleet["ada"], bash("git status"), **env) == 2
    # ...but a sibling's loader in Ada's session has no business failing anything closed.
    assert loader(guard, fleet["ace"], bash("git status"), **env) == 0


# ---------------------------------------------------------------- wiring

def test_canopy_plugin_registers_the_session_hook():
    pre = json.loads((PLUGIN_DIR / "hooks" / "hooks.json").read_text())["hooks"]["PreToolUse"]
    entries = [e for e in pre if any("gating_guard.py" in h.get("command", "") and
                                     "--session" in h.get("command", "") for h in e["hooks"])]
    assert len(entries) == 1
    import re
    for tool in ("Bash", "PowerShell", "Edit", "Write", "Skill",
                 "mcp__plugin_ace_ace-gdrive__drive_create_file"):
        assert re.fullmatch(entries[0]["matcher"], tool), tool


def _hook(args, payload, env):
    full = {k: v for k, v in os.environ.items()
            if k not in ("CANOPY_AGENT", "CANOPY_AGENT_SLUG", "CANOPY_AGENT_REPO", "CLAUDE_PROJECT_DIR")}
    full.update({k: str(v) for k, v in env.items()})
    return subprocess.run([sys.executable, str(GUARD), *args], input=json.dumps(payload),
                          capture_output=True, text=True, env=full, timeout=30)


def test_session_mode_end_to_end(fleet, tmp_path):
    env = {"CANOPY_PLUGIN_DIR": PLUGIN_DIR, "HOME": tmp_path / "home"}
    blocked = _hook(["--session"], bash(PR_CREATE), {**env, "CLAUDE_PROJECT_DIR": fleet["ace"]})
    assert blocked.returncode == 2 and "body-file" in blocked.stderr
    assert _hook(["--session"], bash(PR_CREATE),
                 {**env, "CLAUDE_PROJECT_DIR": fleet["ada"]}).returncode == 0


def test_loader_mode_end_to_end_stands_down_for_a_sibling(fleet, tmp_path):
    env = {"CANOPY_PLUGIN_DIR": PLUGIN_DIR, "HOME": tmp_path / "home",
           "CANOPY_AGENT_REPO": fleet["ace"]}
    assert _hook([], bash(PR_CREATE), {**env, "CLAUDE_PROJECT_DIR": fleet["ada"]}).returncode == 0
    assert _hook([], bash(PR_CREATE), {**env, "CLAUDE_PROJECT_DIR": fleet["ace"]}).returncode == 2
