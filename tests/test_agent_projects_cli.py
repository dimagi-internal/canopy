"""`canopy agent projects|project-add|project-set` and `--project` on a task.

A project is the state behind a `Projects/<name>` Drive folder: what is open,
what is parked on a person, whether it is still running. The folder keeps the
files; these commands are how an agent files its work against one.
"""
import json

import pytest
from click.testing import CliRunner

from orchestrator.cli import main


@pytest.fixture
def fake_http(monkeypatch):
    calls = []
    responses = {}

    def transport(method, url, headers, body):
        calls.append((method, url, json.loads(body) if body else None))
        return responses.get((method, url.split("/api/")[1]), (200, "{}"))

    monkeypatch.setenv("CANOPY_WEB_PAT", "t")
    monkeypatch.setenv("CANOPY_WEB_API_URL", "https://x.test")
    monkeypatch.setattr("orchestrator.canopy_web.urllib_transport", transport)
    return calls, responses


def _projects(responses, *rows):
    responses[("GET", "agents/hal/projects/")] = (200, json.dumps(list(rows)))


def _project(ext_id="P1", name="UNGA 2026 conference planning", status="active"):
    return {"id": 1, "ext_id": ext_id, "name": name, "status": status,
            "task_count": 0, "open_task_count": 0}


def test_projects_lists_them(fake_http):
    calls, responses = fake_http
    _projects(responses, _project(), _project("P2", "Coefficient EOI"))

    r = CliRunner().invoke(main, ["agent", "projects", "--slug", "hal"])

    assert r.exit_code == 0, r.output
    assert calls[0][:2] == ("GET", "https://x.test/api/agents/hal/projects/")
    assert [p["ext_id"] for p in json.loads(r.output)] == ["P1", "P2"]


def test_active_drops_the_finished_ones(fake_http):
    _, responses = fake_http
    _projects(responses, _project(), _project("P2", "Old", status="done"))

    r = CliRunner().invoke(main, ["agent", "projects", "--slug", "hal", "--active"])

    assert [p["ext_id"] for p in json.loads(r.output)] == ["P1"]


def test_project_add_posts_the_folder_with_it(fake_http):
    calls, _ = fake_http

    r = CliRunner().invoke(main, [
        "agent", "project-add", "--slug", "hal", "--name", "UNGA 2026 conference planning",
        "--outcome", "Everyone briefed", "--drive-folder-url", "https://drive/x",
        "--links", "brief|https://doc/1",
    ])

    assert r.exit_code == 0, r.output
    method, url, body = calls[0]
    assert (method, url) == ("POST", "https://x.test/api/agents/hal/projects/")
    assert body["name"] == "UNGA 2026 conference planning"
    assert body["drive_folder_url"] == "https://drive/x"
    assert body["links"] == [{"label": "brief", "url": "https://doc/1"}]


def test_project_set_closes_one_by_its_name(fake_http):
    """The name is what an agent has in hand — it is the Drive folder it worked
    in, while `P1` lives only on canopy-web."""
    calls, responses = fake_http
    _projects(responses, _project())

    r = CliRunner().invoke(main, ["agent", "project-set", "--slug", "hal",
                                  "--project", "unga 2026 CONFERENCE planning",
                                  "--status", "done"])

    assert r.exit_code == 0, r.output
    assert calls[-1][:2] == ("PATCH", "https://x.test/api/agents/hal/projects/P1/")
    assert calls[-1][2] == {"status": "done"}


def test_an_unknown_project_writes_nothing_and_says_what_exists(fake_http):
    """The server keeps a task whose project reference is a typo and files it
    nowhere — right for an API, useless in a CLI, where it would report success
    and leave the work unfiled. This is the only place the difference shows."""
    calls, responses = fake_http
    _projects(responses, _project())

    r = CliRunner().invoke(main, ["agent", "add", "--slug", "hal", "--title", "Book the room",
                                  "--project", "UNGA 2027"])

    assert r.exit_code != 0
    assert "P1 UNGA 2026 conference planning" in r.output
    assert not [c for c in calls if c[0] == "POST"]


def test_add_files_the_task_into_the_project(fake_http):
    calls, responses = fake_http
    _projects(responses, _project())
    responses[("GET", "agents/hal/tasks/")] = (200, json.dumps([{"ext_id": "T3"}]))

    r = CliRunner().invoke(main, ["agent", "add", "--slug", "hal", "--title", "Book the room",
                                  "--project", "P1"])

    assert r.exit_code == 0, r.output
    posted = [c for c in calls if c[1].endswith("/tasks/sync")][0][2]
    assert posted["tasks"][0]["project"] == "P1"
    assert posted["tasks"][0]["ext_id"] == "T4"


def test_add_without_a_project_stays_a_one_off(fake_http):
    """Plenty of work is a one-off, and a project per task is what the Drive
    layout warns against — so this must not nag or invent one."""
    calls, responses = fake_http
    responses[("GET", "agents/hal/tasks/")] = (200, json.dumps([]))

    r = CliRunner().invoke(main, ["agent", "add", "--slug", "hal", "--title", "Reply to Beth"])

    assert r.exit_code == 0, r.output
    assert [c for c in calls if c[1].endswith("/projects/")] == []   # not even looked up
    assert [c for c in calls if c[1].endswith("/tasks/sync")][0][2]["tasks"][0]["project"] == ""


def test_set_moves_a_task_into_a_project(fake_http):
    calls, responses = fake_http
    _projects(responses, _project())
    responses[("GET", "agents/hal/tasks/")] = (200, json.dumps([{"id": 9, "ext_id": "T1"}]))

    r = CliRunner().invoke(main, ["agent", "set", "--slug", "hal", "--task-id", "T1",
                                  "--project", "P1"])

    assert r.exit_code == 0, r.output
    assert calls[-1][2] == {"project": "P1"}


def test_set_with_an_empty_project_takes_the_task_out_of_one(fake_http):
    """Omitted and empty differ: omitting leaves the filing alone, so a title
    edit cannot quietly unfile a task."""
    calls, responses = fake_http
    responses[("GET", "agents/hal/tasks/")] = (200, json.dumps([{"id": 9, "ext_id": "T1"}]))

    r = CliRunner().invoke(main, ["agent", "set", "--slug", "hal", "--task-id", "T1",
                                  "--project", ""])

    assert r.exit_code == 0, r.output
    assert calls[-1][2] == {"project": ""}
    assert [c for c in calls if c[1].endswith("/projects/")] == []   # nothing to resolve


def test_two_projects_sharing_a_name_ask_for_the_ext_id(fake_http):
    _, responses = fake_http
    _projects(responses, _project(), _project("P2"))

    r = CliRunner().invoke(main, ["agent", "projects", "--slug", "hal"])
    assert r.exit_code == 0

    r = CliRunner().invoke(main, ["agent", "project-set", "--slug", "hal",
                                  "--project", "UNGA 2026 conference planning",
                                  "--status", "done"])
    assert r.exit_code != 0
    assert "P1, P2" in r.output
