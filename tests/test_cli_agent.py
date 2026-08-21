# tests/test_cli_agent.py
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


def test_agent_register(fake_http):
    calls, _ = fake_http
    r = CliRunner().invoke(main, ["agent", "register", "--slug", "echo", "--name", "Echo",
                                  "--email", "echo@dimagi-ai.com", "--persona", "p"])
    assert r.exit_code == 0, r.output
    assert calls[0][:2] == ("POST", "https://x.test/api/agents/")
    assert calls[0][2]["slug"] == "echo"


def test_agent_commands_lists(fake_http):
    calls, responses = fake_http
    responses[("GET", "agents/echo/commands?status=pending")] = (
        200, json.dumps([{"id": 7, "kind": "dispatch", "task_title": "Do", "created_by": "jj", "payload": None}]))
    r = CliRunner().invoke(main, ["agent", "commands", "--slug", "echo"])
    assert r.exit_code == 0, r.output
    assert "#7" in r.output and "dispatch" in r.output


def test_agent_tasks_lists(fake_http):
    calls, responses = fake_http
    responses[("GET", "agents/echo/tasks/")] = (
        200, json.dumps([{"ext_id": "T1", "title": "a"}, {"ext_id": "T2", "title": "b"}]))
    r = CliRunner().invoke(main, ["agent", "tasks", "--slug", "echo"])
    assert r.exit_code == 0, r.output
    assert calls[0][:2] == ("GET", "https://x.test/api/agents/echo/tasks/")
    assert "T1" in r.output and "T2" in r.output


def test_agent_apply(fake_http):
    calls, _ = fake_http
    r = CliRunner().invoke(main, ["agent", "apply", "--slug", "echo", "--id", "7", "--note", "ok"])
    assert r.exit_code == 0, r.output
    assert calls[0] == ("POST", "https://x.test/api/agents/echo/commands/7/apply", {"result_note": "ok"})


def test_agent_error_exits_nonzero(fake_http):
    calls, responses = fake_http
    responses[("POST", "agents/echo/commands/7/apply")] = (404, "missing")
    r = CliRunner().invoke(main, ["agent", "apply", "--slug", "echo", "--id", "7"])
    assert r.exit_code != 0
    assert "404" in r.output


def test_agent_add_creates_task_with_next_ext_id(fake_http):
    calls, responses = fake_http
    responses[("GET", "agents/hal/tasks/")] = (
        200, json.dumps([{"ext_id": "T3", "title": "a"}, {"ext_id": "junk", "title": "b"}]))
    r = CliRunner().invoke(main, [
        "agent", "add", "--slug", "hal", "--title", "Track the thing",
        "--next-action", "Read the doc", "--status", "In progress",
        "--owner", "Jonathan", "--assigned", "Hal",
        "--links", "Thread|https://t.example, https://bare.example"])
    assert r.exit_code == 0, r.output
    method, url, body = calls[-1]
    assert (method, url) == ("POST", "https://x.test/api/agents/hal/tasks/sync")
    task = body["tasks"][0]
    assert task["ext_id"] == "T4"                      # next free after T3; "junk" ignored
    assert task["status"] == "in_progress"             # human text normalized
    assert task["links"] == [
        {"label": "Thread", "url": "https://t.example"},
        {"label": "link", "url": "https://bare.example"},
    ]
    assert json.loads(r.output)["added"] == "T4"


def test_agent_add_explicit_ext_id_skips_board_read(fake_http):
    calls, _ = fake_http
    r = CliRunner().invoke(main, ["agent", "add", "--slug", "hal",
                                  "--title", "X", "--ext-id", "T99"])
    assert r.exit_code == 0, r.output
    assert all(m != "GET" for m, _, _ in calls)        # no list_tasks round-trip
    assert calls[-1][2]["tasks"][0]["ext_id"] == "T99"
    assert calls[-1][2]["tasks"][0]["status"] == "suggested"   # default


def test_task_status_normalization_and_links_parsing():
    from orchestrator.agent_cli import normalize_task_status, parse_task_links, next_task_ext_id
    assert normalize_task_status("Shipped") == "done"
    assert normalize_task_status("won't do") == "declined"
    assert normalize_task_status("Blocked") == "in_progress"   # waiting is assigned, not status
    assert normalize_task_status("") == "suggested"
    assert parse_task_links("") == []
    assert parse_task_links("A|u1, B|u2") == [
        {"label": "A", "url": "u1"}, {"label": "B", "url": "u2"}]
    assert next_task_ext_id([]) == "T1"
    assert next_task_ext_id([{"ext_id": "T7"}, {"ext_id": "row2"}]) == "T8"


def test_agent_coverage_cli_json_output(monkeypatch):
    fake = {"ok": True, "agents": [{
        "agent": "eva", "window_days": 30,
        "corpus": {"transcripts": 7, "entries": 100, "adequate": True},
        "persona": {"present": True, "path": "persona.md", "bytes": 2707},
        "activity": {}, "bursts": [{"id": 1, "start": "2026-07-01", "end": "2026-07-02",
                                    "active_days": 2, "sessions": 2}],
        "skills": [{"name": "cea-botec", "bucket": "never_live", "opportunity_bursts": [1, 2],
                    "used_bursts": [], "live": False, "evidence": []}]}]}
    monkeypatch.setattr("orchestrator.agent_coverage.run_agent_coverage",
                        lambda *a, **k: fake)
    res = CliRunner().invoke(main, ["agent", "coverage", "--slug", "eva", "--json-output"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["agents"][0]["skills"][0]["bucket"] == "never_live"


def test_agent_coverage_cli_human_output_leads_with_decayed(monkeypatch):
    fake = {"ok": True, "agents": [{
        "agent": "eva", "window_days": 30,
        "corpus": {"transcripts": 7, "entries": 100, "adequate": True},
        "persona": {"present": False, "path": None, "bytes": 0},
        "activity": {}, "bursts": [{"id": 1, "start": "2026-07-01", "end": "2026-07-02",
                                    "active_days": 2, "sessions": 2}],
        "skills": [
            {"name": "lead-outreach", "bucket": "decayed", "opportunity_bursts": [1, 2],
             "used_bursts": [1], "live": False, "evidence": []},
            {"name": "turn", "bucket": "live", "opportunity_bursts": [1, 2],
             "used_bursts": [2], "live": True, "evidence": []}]}]}
    monkeypatch.setattr("orchestrator.agent_coverage.run_agent_coverage",
                        lambda *a, **k: fake)
    res = CliRunner().invoke(main, ["agent", "coverage", "--slug", "eva"])
    assert res.exit_code == 0, res.output
    assert "decayed" in res.output and "lead-outreach" in res.output
    assert "no persona.md" in res.output


# --- `agent set --task-id` accepts the board's own T<N>, not just the DB id (#454) ---
# The ext_id is the ONLY identifier the board surfaces (card label, `agent add` output,
# `agent turn --task`, `agent dispatch --task`). Requiring the numeric id here cost every
# agent a failed call plus a JSON grep on each task patch.

def test_agent_set_accepts_ext_id(fake_http):
    calls, responses = fake_http
    responses[("GET", "agents/hal/tasks/")] = (
        200, json.dumps([{"id": 70, "ext_id": "T6"}, {"id": 71, "ext_id": "T7"}]))
    r = CliRunner().invoke(main, ["agent", "set", "--slug", "hal",
                                  "--task-id", "T7", "--plan", "p"])
    assert r.exit_code == 0, r.output
    patch = [c for c in calls if c[0] == "PATCH"]
    assert patch[0][1] == "https://x.test/api/agents/hal/tasks/71/"
    assert patch[0][2] == {"plan": "p"}


def test_agent_set_ext_id_is_case_insensitive(fake_http):
    calls, responses = fake_http
    responses[("GET", "agents/hal/tasks/")] = (200, json.dumps([{"id": 71, "ext_id": "T7"}]))
    r = CliRunner().invoke(main, ["agent", "set", "--slug", "hal",
                                  "--task-id", "t7", "--plan", "p"])
    assert r.exit_code == 0, r.output
    assert [c for c in calls if c[0] == "PATCH"][0][1].endswith("/tasks/71/")


def test_agent_set_numeric_id_still_works_without_listing(fake_http):
    """The existing contract is unchanged — and a numeric id must NOT cost a board read."""
    calls, _ = fake_http
    r = CliRunner().invoke(main, ["agent", "set", "--slug", "hal",
                                  "--task-id", "71", "--plan", "p"])
    assert r.exit_code == 0, r.output
    assert calls[0][:2] == ("PATCH", "https://x.test/api/agents/hal/tasks/71/")
    assert not [c for c in calls if c[0] == "GET"]


# --- `agent set --title` so a card whose HEADLINE is wrong can be corrected ---
# The API accepted `title` on the task PATCH all along; the CLI just never passed it, so a
# task whose title asserted something false could only be corrected in fields nobody reads
# at a glance. (2026-08-07: hal's board carried "every repo's CI is dark since Aug 3" as a
# headline after that diagnosis was disproven.)

def test_agent_set_can_correct_a_wrong_title(fake_http):
    calls, _ = fake_http
    r = CliRunner().invoke(main, ["agent", "set", "--slug", "hal",
                                  "--task-id", "86", "--title", "what was actually true"])
    assert r.exit_code == 0, r.output
    patch = [c for c in calls if c[0] == "PATCH"]
    assert patch[0][1] == "https://x.test/api/agents/hal/tasks/86/"
    assert patch[0][2] == {"title": "what was actually true"}


def test_agent_set_title_is_omitted_when_not_passed(fake_http):
    """The patch stays sparse — an unset --title must not blank the card's headline."""
    calls, _ = fake_http
    r = CliRunner().invoke(main, ["agent", "set", "--slug", "hal",
                                  "--task-id", "71", "--plan", "p"])
    assert r.exit_code == 0, r.output
    assert "title" not in [c for c in calls if c[0] == "PATCH"][0][2]


def test_agent_set_unknown_ext_id_names_the_fix(fake_http):
    calls, responses = fake_http
    responses[("GET", "agents/hal/tasks/")] = (200, json.dumps([{"id": 71, "ext_id": "T7"}]))
    r = CliRunner().invoke(main, ["agent", "set", "--slug", "hal",
                                  "--task-id", "T99", "--plan", "p"])
    assert r.exit_code != 0
    assert "T99" in r.output and "canopy agent tasks" in r.output
    assert not [c for c in calls if c[0] == "PATCH"]


# ── `agent tasks` filtering (canopy#516) ──────────────────────────────────────
# The board drain runs at the start of EVERY turn for every agent, and used to return
# the agent's entire task history — 30KB on hal for two open tasks, which overflowed the
# tool-output limit and got recovered with a hand-written filter each time.

_BOARD = [
    {"ext_id": "T1", "title": "shipped thing", "status": "done"},
    {"ext_id": "T2", "title": "not relevant", "status": "declined"},
    {"ext_id": "T3", "title": "live work", "status": "in_progress"},
    {"ext_id": "T4", "title": "an idea", "status": "suggested"},
]


def _tasks(fake_http, *args):
    _, responses = fake_http
    responses[("GET", "agents/echo/tasks/")] = (200, json.dumps(_BOARD))
    r = CliRunner().invoke(main, ["agent", "tasks", "--slug", "echo", *args])
    assert r.exit_code == 0, r.output
    return [t["ext_id"] for t in json.loads(r.output)]


def test_agent_tasks_unfiltered_by_default(fake_http):
    """The ext_id path needs the FULL set including resolved tasks — the default must
    not change, or `agent add` starts reusing ids."""
    assert _tasks(fake_http) == ["T1", "T2", "T3", "T4"]


def test_agent_tasks_open_excludes_resolved(fake_http):
    assert _tasks(fake_http, "--open") == ["T3", "T4"]


def test_agent_tasks_status_filters_to_one(fake_http):
    assert _tasks(fake_http, "--status", "in_progress") == ["T3"]


def test_agent_tasks_status_is_repeatable(fake_http):
    assert _tasks(fake_http, "--status", "done", "--status", "declined") == ["T1", "T2"]


def test_agent_tasks_status_accepts_human_spelling(fake_http):
    """`normalize_task_status` already understands "In progress"; a filter that only
    matched the canonical token would silently return nothing instead of erroring."""
    assert _tasks(fake_http, "--status", "In progress") == ["T3"]
    assert _tasks(fake_http, "--status", "wip") == ["T3"]


def test_agent_tasks_open_and_status_together_is_an_error(fake_http):
    _, responses = fake_http
    responses[("GET", "agents/echo/tasks/")] = (200, json.dumps(_BOARD))
    r = CliRunner().invoke(
        main, ["agent", "tasks", "--slug", "echo", "--open", "--status", "done"])
    assert r.exit_code != 0
    assert "alternatives" in r.output
