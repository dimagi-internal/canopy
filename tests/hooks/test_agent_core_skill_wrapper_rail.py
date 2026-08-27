"""The fleet review-wrapper rail, and the fail-closed exemption that makes it safe
(`plugins/canopy/agent-core/gating-baseline.json` `always` + `gating_guard.py`).

WHY THIS FILE EXISTS (2026-08-27). Every agent that ships `skills/agent-turn-review/`
owns a NAMESPACED wrapper (`<slug>:agent-turn-review`) carrying its own send path,
done-claim rules and gates. `canopy:agent-turn-review` is the BODY that wrapper
delegates to. Calling the body directly — or the bare name — passes the checklist
while silently skipping every agent-specific step.

`canopy agent-review` found that bypass on two of the three agents reviewed that day.
Hal's wrapper PREDICTED it in its own opening paragraph ("a wrapper you can walk
around is not a wrapper") and was walked around anyway; ace's wrapper actively
INSTRUCTED it. Prose asking the model to comply is precisely what failed, twice,
which is the standing argument for a rail.

The rail could not exist before because of a real deadlock, documented in hal's
wrapper under "Why this isn't a gating rail": reaching a `Skill` call means putting
`Skill` in the PreToolUse matcher, and `gating_guard` fails CLOSED for any agent that
mounts channels — so a degraded canopy install would block EVERY skill invocation,
including the `/canopy:update` its own error message tells you to run. That objection
was correct about the code as it stood. It is fixed here (`FAIL_OPEN_TOOLS`) rather
than routed around, which is what lets the rail exist at all — so the exemption is
tested as carefully as the rail.

Run: uv run pytest tests/hooks/test_agent_core_skill_wrapper_rail.py
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "plugins" / "canopy" / "agent-core" / "gating_guard.py"
PLUGIN_DIR = ROOT / "plugins" / "canopy"


def _load():
    loader = importlib.machinery.SourceFileLoader("gating_guard", str(GUARD))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def guard(monkeypatch):
    monkeypatch.setenv("CANOPY_PLUGIN_DIR", str(PLUGIN_DIR))
    return _load()


def _repo(tmp_path, *, channels=("email",), with_wrapper=True):
    """A minimal agent repo: gating config, and optionally the review wrapper."""
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "gating.json").write_text(
        json.dumps({"slug": "hal", "channels": list(channels), "deny": [], "approve": []})
    )
    if with_wrapper:
        d = tmp_path / "skills" / "agent-turn-review"
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text("# wrapper\n")
    return tmp_path


def _skill_call(skill: str) -> dict:
    return {"tool_name": "Skill", "tool_input": {"skill": skill}}


# ---------------------------------------------------------------- the rail fires

@pytest.mark.parametrize("skill", ["agent-turn-review", "canopy:agent-turn-review"])
def test_blocks_the_body_and_the_bare_name(guard, tmp_path, skill):
    code, _, err = guard.run(str(_repo(tmp_path)), _skill_call(skill))
    assert code == 2
    # A rail that doesn't name the right path just stalls the turn.
    assert "hal:agent-turn-review" in err


@pytest.mark.parametrize("skill", [
    "hal:agent-turn-review",   # the agent's OWN wrapper — the whole point
    "ace:agent-turn-review",   # a sibling's wrapper is still namespaced
    "canopy:agent-review",     # different skill, shares a prefix
    "turn",
])
def test_allows_namespaced_and_unrelated_skills(guard, tmp_path, skill):
    code, _, _ = guard.run(str(_repo(tmp_path)), _skill_call(skill))
    assert code == 0


def test_rail_is_inert_for_an_agent_with_no_wrapper(guard, tmp_path):
    """`requires_path`: an agent that ships no wrapper has nothing to be routed TO,
    so blocking the fleet skill would leave it with no way to run the review at all."""
    repo = _repo(tmp_path, with_wrapper=False)
    code, _, _ = guard.run(str(repo), _skill_call("canopy:agent-turn-review"))
    assert code == 0


def test_applies_without_any_channel_mounted(guard, tmp_path):
    """`always` rails are not channel-scoped — that is the point of the section.
    A per-channel home would mean each agent opting in via its own gating.json,
    i.e. the rail missing for exactly the agent that forgot."""
    repo = _repo(tmp_path, channels=())
    code, _, err = guard.run(str(repo), _skill_call("canopy:agent-turn-review"))
    assert code == 2
    assert "hal:agent-turn-review" in err


# ------------------------------------------------- the fail-closed exemption

def test_unreadable_baseline_does_not_wedge_the_recovery_path(guard, tmp_path, monkeypatch):
    """THE deadlock that kept this rail unshipped. With the baseline unreadable, the
    fail-closed message tells the agent to run `/canopy:update` — which is itself a
    `Skill` call. Without the exemption the guard blocks its own remedy."""
    monkeypatch.setenv("CANOPY_PLUGIN_DIR", str(tmp_path / "nonexistent"))
    repo = _repo(tmp_path)
    code, _, _ = guard.run(str(repo), _skill_call("canopy:update"))
    assert code == 0, "a degraded install must never block the command that fixes it"


def test_unreadable_baseline_still_fails_closed_on_acting_tools(guard, tmp_path, monkeypatch):
    """The exemption is narrow. Bash can itself send/destroy, so losing its rails
    costs SAFETY and it must still fail closed."""
    monkeypatch.setenv("CANOPY_PLUGIN_DIR", str(tmp_path / "nonexistent"))
    repo = _repo(tmp_path)
    code, _, err = guard.run(
        str(repo), {"tool_name": "Bash", "tool_input": {"command": "gog gmail send --to x"}}
    )
    assert code == 2
    assert "fail closed" in err.lower()


def test_email_rail_still_fires(guard, tmp_path):
    """Regression: `always` rails are merged alongside channel rails, not instead."""
    code, _, err = guard.run(
        str(_repo(tmp_path)),
        {"tool_name": "Bash", "tool_input": {"command": "gog gmail send --to a@b.c"}},
    )
    assert code == 2
    assert "bin/hal-email" in err
