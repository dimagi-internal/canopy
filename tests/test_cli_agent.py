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
    assert calls[-1][:2] == ("POST", "https://x.test/api/agents/")
    assert calls[-1][2]["slug"] == "echo"


def test_moving_an_agent_keeps_its_identity(fake_http):
    """2026-09-29, fizzy: `--workspace` alone 422'd (name is required), and passing
    `--name` to get past it blanked email/description/persona, since the upsert
    replaces every field. Omitted fields now come from the agent's current record."""
    calls, responses = fake_http
    responses[("GET", "agents/fizzy/")] = (200, json.dumps({
        "slug": "fizzy", "name": "Fizzy", "email": "fizzy@dimagi-ai.com",
        "description": "d", "persona": "p", "avatar_url": ""}))
    r = CliRunner().invoke(main, ["agent", "register", "--slug", "fizzy",
                                  "--workspace", "strategy"])
    assert r.exit_code == 0, r.output
    method, url, body = calls[-1]
    assert (method, url) == ("POST", "https://x.test/api/agents/")
    assert body["workspace"] == "strategy" and body["name"] == "Fizzy"
    assert body["email"] == "fizzy@dimagi-ai.com" and body["description"] == "d"


def test_an_explicit_option_wins_over_the_current_record(fake_http):
    calls, responses = fake_http
    responses[("GET", "agents/fizzy/")] = (200, json.dumps({"name": "Fizzy", "persona": "old"}))
    r = CliRunner().invoke(main, ["agent", "register", "--slug", "fizzy", "--persona", "new"])
    assert r.exit_code == 0, r.output
    assert calls[-1][2]["persona"] == "new" and calls[-1][2]["name"] == "Fizzy"


def test_a_new_agent_without_a_name_is_a_clear_error(fake_http):
    calls, responses = fake_http
    responses[("GET", "agents/fizzy/")] = (404, "not found")
    r = CliRunner().invoke(main, ["agent", "register", "--slug", "fizzy", "--workspace", "strategy"])
    assert r.exit_code != 0 and "pass --name" in r.output
    assert not any(c[0] == "POST" for c in calls)


def test_agent_actions_lists_the_pending_queue(fake_http):
    calls, responses = fake_http
    responses[("GET", "agents/echo/actions/?status=pending")] = (
        200, json.dumps([{"id": 7, "agent_slug": "echo", "task_ext_id": "T3", "action": "nudge",
                          "comment": "now please", "by": "jj", "status": "pending"}]))
    r = CliRunner().invoke(main, ["agent", "actions", "--slug", "echo"])
    assert r.exit_code == 0, r.output
    assert calls[0][:2] == ("GET", "https://x.test/api/agents/echo/actions/?status=pending")
    assert "#7" in r.output and "nudge" in r.output and "T3" in r.output
    assert "now please" in r.output


def test_agent_actions_empty_queue(fake_http):
    _, responses = fake_http
    responses[("GET", "agents/echo/actions/?status=pending")] = (200, "[]")
    r = CliRunner().invoke(main, ["agent", "actions", "--slug", "echo"])
    assert r.exit_code == 0, r.output
    assert "no pending actions" in r.output


def test_the_drain_marks_each_action_applied(fake_http):
    """The turn-start drain: list pending actions, carry each out, mark each applied."""
    calls, responses = fake_http
    responses[("GET", "agents/echo/actions/?status=pending")] = (
        200, json.dumps([{"id": 7, "task_ext_id": "T3", "action": "approve"},
                         {"id": 8, "task_ext_id": "T4", "action": "decline", "comment": "no"}]))
    runner = CliRunner()
    assert runner.invoke(main, ["agent", "actions", "--slug", "echo"]).exit_code == 0
    for action_id in ("7", "8"):
        r = runner.invoke(main, ["agent", "applied", "--slug", "echo", "--id", action_id,
                                 "--note", "done"])
        assert r.exit_code == 0, r.output
    assert [c for c in calls if c[0] == "POST"] == [
        ("POST", "https://x.test/api/agents/echo/actions/7/applied", {"result_note": "done"}),
        ("POST", "https://x.test/api/agents/echo/actions/8/applied", {"result_note": "done"}),
    ]


def test_agent_tasks_lists(fake_http):
    calls, responses = fake_http
    responses[("GET", "agents/echo/tasks/")] = (
        200, json.dumps([{"ext_id": "T1", "title": "a"}, {"ext_id": "T2", "title": "b"}]))
    r = CliRunner().invoke(main, ["agent", "tasks", "--slug", "echo"])
    assert r.exit_code == 0, r.output
    assert calls[0][:2] == ("GET", "https://x.test/api/agents/echo/tasks/")
    assert "T1" in r.output and "T2" in r.output


def test_agent_applied(fake_http):
    calls, _ = fake_http
    r = CliRunner().invoke(main, ["agent", "applied", "--slug", "echo", "--id", "7", "--note", "ok"])
    assert r.exit_code == 0, r.output
    assert calls[0] == ("POST", "https://x.test/api/agents/echo/actions/7/applied", {"result_note": "ok"})


@pytest.mark.parametrize("verb", ["commands", "apply", "work", "tasks-sync"])
def test_the_old_verbs_are_gone(verb):
    r = CliRunner().invoke(main, ["agent", verb, "--help"])
    assert r.exit_code != 0


def test_agent_error_exits_nonzero(fake_http):
    calls, responses = fake_http
    responses[("POST", "agents/echo/actions/7/applied")] = (404, "missing")
    r = CliRunner().invoke(main, ["agent", "applied", "--slug", "echo", "--id", "7"])
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
    assert (method, url) == ("POST", "https://x.test/api/agents/hal/tasks/")
    task = body[0]                                     # a BARE list
    assert task["ext_id"] == "T4"                      # next free after T3; "junk" ignored
    assert task["idempotency_key"] == "hal:T4"         # a retried add replays, never duplicates
    assert task["origin"] == "task-tracker" and "source" not in task
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
    assert calls[-1][2][0]["ext_id"] == "T99"
    assert calls[-1][2][0]["status"] == "suggested"   # default


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

def test_agent_set_patches_by_ext_id_without_a_board_read(fake_http):
    """canopy-web addresses a task by its ext_id — there is no numeric id to resolve
    to, so a plain `set` costs exactly one PATCH."""
    calls, _ = fake_http
    r = CliRunner().invoke(main, ["agent", "set", "--slug", "hal",
                                  "--task-id", "T7", "--plan", "p"])
    assert r.exit_code == 0, r.output
    assert calls == [("PATCH", "https://x.test/api/agents/hal/tasks/T7/", {"plan": "p"})]


def test_agent_set_unknown_ext_id_names_the_fix(fake_http):
    calls, responses = fake_http
    responses[("PATCH", "agents/hal/tasks/T99/")] = (404, '{"detail": "task T99 not found"}')
    r = CliRunner().invoke(main, ["agent", "set", "--slug", "hal",
                                  "--task-id", "T99", "--plan", "p"])
    assert r.exit_code != 0
    assert "T99" in r.output and "canopy agent tasks" in r.output


# --- `agent set --title` so a card whose HEADLINE is wrong can be corrected ---
# The API accepted `title` on the task PATCH all along; the CLI just never passed it, so a
# task whose title asserted something false could only be corrected in fields nobody reads
# at a glance. (2026-08-07: hal's board carried "every repo's CI is dark since Aug 3" as a
# headline after that diagnosis was disproven.)

def test_agent_set_can_correct_a_wrong_title(fake_http):
    calls, _ = fake_http
    r = CliRunner().invoke(main, ["agent", "set", "--slug", "hal",
                                  "--task-id", "T86", "--title", "what was actually true"])
    assert r.exit_code == 0, r.output
    patch = [c for c in calls if c[0] == "PATCH"]
    assert patch[0][1] == "https://x.test/api/agents/hal/tasks/T86/"
    assert patch[0][2] == {"title": "what was actually true"}


def test_agent_set_title_is_omitted_when_not_passed(fake_http):
    """The patch stays sparse — an unset --title must not blank the card's headline."""
    calls, _ = fake_http
    r = CliRunner().invoke(main, ["agent", "set", "--slug", "hal",
                                  "--task-id", "T71", "--plan", "p"])
    assert r.exit_code == 0, r.output
    assert "title" not in [c for c in calls if c[0] == "PATCH"][0][2]


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
