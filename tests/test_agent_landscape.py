"""`canopy agent landscape` — one workspace tree's agents, and the skill-based membership rule."""
import datetime as dt
import json

from click.testing import CliRunner

from orchestrator import agent_landscape as L
from orchestrator.agent_cli import agent

GROUPS = [{"name": "Decks", "keywords": ["deck", "slides"]}, {"name": "Apps", "keywords": ["app", "nova"]}]


def _fake(calls=None):
    def call(method, path, *a, **k):
        if calls is not None:
            calls.append(path)
        if path == "/api/agents/ada/":
            return {"slug": "ada", "workspace": "connect"}
        if path == "/api/workspaces/":
            return [{"slug": "connect", "root": "org"}, {"slug": "org", "root": "org"}]
        if path.endswith("agent-topology"):
            return {"root": "org", "workspaces": [{"slug": "org"}, {"slug": "connect"}],
                    "agents": [{"slug": "echo", "workspace": "connect", "owner_email": "o@x"},
                               {"slug": "sift", "workspace": "org", "owner_email": "m@x"},
                               {"slug": "ada", "workspace": "org", "owner_email": "o@x"}],
                    "edges": [{"source": "ada", "target": "echo", "access": "full", "basis": "admin"},
                              {"source": "ada", "target": "sift", "access": "full", "basis": "editor"}]}
        if path.endswith("/skills/"):
            return [{"name": "deck-judge"}, {"name": "slides-writer"}, {"name": "agent-turn-review"}]
        return {"items": [{"created_at": "2026-10-16T09:00:00Z"}, {"created_at": "2026-08-01T09:00:00Z"}]}
    return call


def test_default_root_is_the_conductors_organisation():
    assert L.default_root(_fake(), "ada") == "org"


def test_fetch_reads_one_tree_and_marks_who_the_conductor_may_run():
    calls = []
    land = L.fetch(_fake(calls), "ada", "org", now=dt.datetime(2026, 10, 16, tzinfo=dt.timezone.utc))
    assert calls[0] == "/api/workspaces/org/agent-topology"
    by = {a["slug"]: a for a in land["agents"]}
    assert by["echo"]["may_run"] and not by["sift"]["may_run"]
    assert by["echo"]["turns_recent"] == 1 and by["echo"]["last_turn"] == "2026-10-16"


def test_names_not_descriptions_and_no_boilerplate():
    skills = [{"name": "story-draft", "description": "a deck"}, {"name": "agent-turn-review"},
              {"name": "slides-writer"}]
    assert L.matched_skills(skills, ["deck", "slides", "review"]) == ["slides-writer"]


def test_select_members_needs_two_skills_and_may_run():
    agents = [{"slug": "echo", "may_run": True, "skills": [{"name": "deck-judge"}, {"name": "slides-writer"}]},
              {"slug": "lex", "may_run": True, "skills": [{"name": "kc-status-digest"}]},
              {"slug": "sift", "may_run": False, "owner": "m@x",
               "skills": [{"name": "deck-judge"}, {"name": "slides-writer"}]},
              {"slug": "ada", "may_run": False, "skills": [{"name": "deck-judge"}, {"name": "slides-writer"}]}]
    sel = L.select_members(agents, GROUPS, "ada")
    assert sel["members"] == ["echo"] and sel["left_out"] == ["lex"]
    assert sel["could_bring"] == [{"slug": "sift", "owner": "m@x", "groups": ["Decks"]}]


def test_cli_members(monkeypatch, tmp_path):
    from orchestrator import canopy_web
    monkeypatch.setattr(canopy_web, "call", _fake())
    g = tmp_path / "g.json"
    g.write_text(json.dumps({"work_groups": GROUPS}))
    r = CliRunner().invoke(agent, ["landscape", "--slug", "ada", "--groups-file", str(g), "--members"])
    assert r.exit_code == 0, r.output
    assert r.output.strip() == "echo"
    r = CliRunner().invoke(agent, ["landscape", "--slug", "ada"])
    assert "org and its sub-workspaces" in r.output and "| sift | org | m@x | no |" in r.output
