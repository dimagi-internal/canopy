"""Project check-in runs: `canopy huddle plan --checkin-run`, `file` → proposed TASKS in the
check-in's project + task backings + `checkins/runs/<id>/finished`, and `canopy checkin
material|propose|finish` — against a fake canopy-web serving the check-in routes.

The routes follow canopy-web docs/superpowers/specs/2026-10-17-project-check-ins-design.md §3,
§5, §6. The fallback onto a canopy-web without them is tests/test_huddle_group.py."""
import json
import re
from urllib.parse import unquote, urlparse

import pytest
from click.testing import CliRunner

from orchestrator import huddle as Hm
from orchestrator import huddle_cli
from orchestrator.cli import main
from tests.test_huddle_cli import H, NOW, FakeWeb, _cell, _full_huddle, detail, prop, r1, write_plan

PROJECT = {"agent": "ada", "ref": "P12", "name": "Canopy support",
           "outcome": "Every canopy support question gets a fix or a decision within a week."}
CHECKIN = {"name": "Weekly check-in", "topic": "Unblock canopy users",
           "lead_email": "jonathan@dimagi.com", "channel_name": "canopy-support",
           "agents": ["ada", "eva", "echo"], "format": "huddle"}
# CheckinRunOut (§2, §5): the check-in and its project, the read window flat.
RUN = {"id": 77, "checkin": CHECKIN, "project": PROJECT, "status": "running",
       "material": "neal: the vault alert fires twice\nmaria: +1",
       "read_messages": 2, "read_first_ts": "2026-10-14T09:00:00Z",
       "read_last_ts": "2026-10-16T08:00:00Z", "read_since": "2026-10-13T15:00:00Z",
       "participants": {"agents": ["ada", "eva", "echo"], "lead_email": "jonathan@dimagi.com"},
       "huddle_id": "", "created_at": "2026-10-16T09:00:00Z"}
PDETAIL = {**{k: PROJECT[k] for k in ("name", "outcome")}, "ext_id": "P12", "status": "active",
           "tasks": [
               {"ext_id": "T5", "title": "Dedupe the vault alert", "status": "suggested",
                "origin": "checkin", "source_url": "https://cw/w/connect/huddles/work-x",
                "updated_at": "2026-10-10T00:00:00Z"},
               {"ext_id": "T6", "title": "Old declined idea", "status": "declined",
                "notes": "not now — after the release", "origin": "checkin",
                "updated_at": "2026-10-12T00:00:00Z"},
               {"ext_id": "T1", "title": "Ancient done work", "status": "done",
                "updated_at": "2026-01-01T00:00:00Z"},
               {"ext_id": "T7", "parent_ext_id": "T5", "title": "a step", "status": "suggested"}]}
T5_BACKINGS = [{"backer": {"kind": "person", "agent": "", "name": "Neal",
                           "email": "neal@dimagi.com"},
                "stance": "backs_with_changes", "line": "only for the cloud runner",
                "stale": False}]
RUN_ID = "77"


class CheckinWeb(FakeWeb):
    # The task-level twin of ProjectProposeIn (§3) — a StrictModel, so any other key is a 422.
    PROPOSE_IN = {"title", "why", "done_when", "plan", "partners", "source_url", "run",
                  "new_evidence"}
    FINISH_IN = {"huddle_id", "result_url", "summary", "tasks_proposed"}

    def __init__(self):
        super().__init__()
        self.run = json.loads(json.dumps(RUN))
        self.pdetail = json.loads(json.dumps(PDETAIL))
        self.proposed = {}          # title -> (task, body)
        self.task_backings = []     # (agent, project ref, task, backer, body)
        self.finished = []          # (run id, body)
        self.propose_status = None  # force (status, text)
        self.finish_status = None

    def transport(self, method, url, headers, data):
        body = json.loads(data) if data else None
        path = unquote(urlparse(url).path)
        m = re.match(r"^/api/checkins/runs/([^/]+)(/finished)?$", path)
        if m and method == "GET" and not m.group(2):
            self.calls.append((method, path, {}, body))
            return (200, json.dumps(self.run)) if m.group(1) == str(self.run["id"]) else (
                404, '{"detail": "check-in run 9 not found"}')
        if m and m.group(2) and method == "POST":
            self.calls.append((method, path, {}, body))
            if set(body) - self.FINISH_IN:
                return 422, json.dumps({"detail": f"extra {sorted(set(body) - self.FINISH_IN)}"})
            if self.finish_status:
                return self.finish_status, '{"detail": "run 77 is expired, not running"}'
            if self.run["status"] == "done":
                return 200, json.dumps(self.run)
            self.run["status"] = "done"
            self.finished.append((m.group(1), body))
            return 200, json.dumps({"id": int(m.group(1)), "status": "done"})
        m = re.match(r"^/api/agents/ada/projects/P12/checkins/(.+)$", path)
        if m and method == "GET":
            return (200, json.dumps({**CHECKIN, "project": PROJECT})) if m.group(1) == CHECKIN["name"] \
                else (404, '{"detail": "check-in x not found"}')
        if path == "/api/agents/ada/projects/P12" and method == "GET":
            return 200, json.dumps(self.pdetail)
        if path == "/api/agents/ada/projects/P12/checkin-runs" and method == "GET":
            return 200, json.dumps([{"id": 77}, {"id": 70, "huddle_id": "work-canopy-support-20261009"}])
        m = re.match(r"^/api/agents/ada/projects/P12/tasks/([^/]+)/backings/$", path)
        if m and method == "GET":
            return 200, json.dumps(T5_BACKINGS if m.group(1) == "T5" else [])
        if path == "/api/agents/ada/projects/P12/tasks/propose/" and method == "POST":
            self.calls.append((method, path, {}, body))
            if self.propose_status:
                return self.propose_status
            if set(body) - self.PROPOSE_IN:
                return 422, json.dumps({"detail": f"extra {sorted(set(body) - self.PROPOSE_IN)}"})
            if not body.get("why") or not body.get("done_when"):
                return 422, '{"detail": "a proposal needs a title, a why and a done-when"}'
            seen = self.proposed.get(body["title"])
            created = seen is None
            task = seen[0] if seen else {"ext_id": f"T{20 + len(self.proposed)}",
                                         "status": "suggested", "title": body["title"]}
            self.proposed[body["title"]] = (task, body)
            return (201 if created else 200), json.dumps({"created": created, "task": task})
        m = re.match(r"^/api/agents/([^/]+)/projects/propose/$", path)
        if m and method == "POST":           # the pre-check-in path (fallback)
            self.calls.append((method, path, {}, body))
            return 201, json.dumps({"created": True, "project": {
                "agent": m.group(1), "ref": "P30", "name": body["name"], "status": "proposed"}})
        m = re.match(r"^/api/agents/ada/projects/P12/tasks/([^/]+)/backings/agents/([^/]+)/$", path)
        if m and method == "PUT":
            self.task_backings = [b for b in self.task_backings if b[2:4] != m.groups()]
            if set(body) - {"stance", "line", "huddle_id"} or len(body.get("line", "")) > 280:
                return 422, '{"detail": "bad backing"}'
            self.task_backings.append(("ada", "P12", *m.groups(), body))
            return 200, json.dumps({"stance": body["stance"]})
        return super().transport(method, url, headers, data)


@pytest.fixture()
def web(monkeypatch):
    w = CheckinWeb()
    monkeypatch.setattr("orchestrator.canopy_web.resolve_base_url", lambda b=None: "https://cw")
    monkeypatch.setattr("orchestrator.canopy_web.resolve_token", lambda t=None: "tok")
    monkeypatch.setattr("orchestrator.canopy_web.urllib_transport", w.transport)
    monkeypatch.setattr(huddle_cli, "_now", lambda: NOW)
    monkeypatch.setattr(huddle_cli, "_sleep", lambda s: None)
    monkeypatch.setattr("orchestrator.provenance.resolve_parent",
                        lambda cwd=None: {"turn_id": "lead-turn"})
    monkeypatch.setenv("CANOPY_RUNNER_CONFIG", "/nonexistent/runner.json")
    return w


def run(*args):
    return CliRunner().invoke(main, list(args), catch_exceptions=False)


def paths(web, method, frag):
    return [c[1] for c in web.calls if c[0] == method and frag in c[1]]


# ── pure ─────────────────────────────────────────────────────────────────────────
def test_checkin_brief_is_the_topic_plus_the_projects_outcome_as_one_priority():
    brief = Hm.checkin_brief({**CHECKIN, "project": PROJECT})
    assert list(Hm.brief_items(brief)) == [1]
    assert brief.startswith("1. Unblock canopy users — project outcome: Every canopy support")
    assert "check-in Weekly check-in on project Canopy support (#canopy-support, lead " in brief
    assert Hm.priority_label(1, brief) == "priority 1: Unblock canopy users"
    # No topic: the project's outcome IS the topic (said once).
    b2 = Hm.checkin_brief({"name": "Weekly", "project": PROJECT})
    assert b2.startswith(f"1. {PROJECT['outcome']} — hard dates") and "project outcome" not in b2


def test_a_checkin_without_a_channel_says_nothing_was_read():
    sec = Hm.checkin_section({"name": "Weekly", "project": PROJECT}, "", {})
    assert "## No channel" in sec and "quoted material" not in sec
    assert "proposed TASK in project Canopy support" in sec


# ── plan / prompt ────────────────────────────────────────────────────────────────
def _plan(tmp_path, *extra, flag="--checkin-run"):
    out = tmp_path / "plan.json"
    r = run("huddle", "plan", flag, RUN_ID, "--local", str(tmp_path / "rec"),
            "--out", str(out), *extra)
    return r, (json.loads(out.read_text()) if out.exists() else None)


def test_plan_takes_members_brief_material_and_proposed_tasks_from_the_checkin(tmp_path, web):
    r, plan = _plan(tmp_path)
    assert r.exit_code == 0, r.output
    assert plan["leader"] == "ada" and plan["team"] == "canopy-support"
    assert plan["members"] == ["eva", "echo"] and "takes part as leader" in r.stderr
    assert plan["principal"] == "jonathan@dimagi.com"
    assert "project outcome: Every canopy support" in plan["priorities_brief"]
    assert plan["id"] == "work-canopy-support-20261006"
    cr = plan["checkin_run"]
    assert cr["id"] == 77 and cr["via"] == "checkins" and cr["read"]["messages"] == 2
    assert cr["project"]["ref"] == "P12" and cr["checkin"] == "Weekly check-in"
    _, anchor = web.closeouts[0]
    ref = anchor["origin_ref"]
    assert ref["checkin"] == "Weekly check-in" and ref["project"] == {"agent": "ada", "ref": "P12"}
    assert ref["run"] == 77 and "group" not in ref
    text = plan["checkin_proposed"]
    assert "- T5 Dedupe the vault alert (suggested)" in text
    assert 'neal@dimagi.com: backs_with_changes — "only for the cloud runner"' in text
    assert "- T6 Old declined idea (declined) — not now — after the release" in text
    assert "Ancient done work" not in text and "a step" not in text   # old / a child step
    assert plan["checkin_reactions"] == ""          # a project: its tasks are the memory
    # every check-in route, none of the old ones
    assert not paths(web, "GET", "/api/huddle-groups/")


def test_group_run_is_an_alias_of_checkin_run(tmp_path, web):
    r, plan = _plan(tmp_path, flag="--group-run")
    assert r.exit_code == 0, r.output
    assert plan["checkin_run"]["id"] == 77 and plan["members"] == ["eva", "echo"]


def test_a_run_that_names_its_checkin_reads_the_checkin(tmp_path, web):
    web.run["checkin"] = CHECKIN["name"]
    r, plan = _plan(tmp_path)
    assert r.exit_code == 0, r.output
    assert plan["members"] == ["eva", "echo"] and plan["principal"] == "jonathan@dimagi.com"


def test_checkin_material_and_proposed_tasks_reach_rounds_1_and_2(tmp_path, web):
    _, plan = _plan(tmp_path)
    p = tmp_path / "plan.json"
    out = tmp_path / "eva-r1.md"
    assert run("huddle", "prompt", "--plan", str(p), "--member", "eva", "--round", "1",
               "--out", str(out)).exit_code == 0
    text = out.read_text()
    assert "## Check-in: Weekly check-in on project Canopy support" in text
    assert "> neal: the vault alert fires twice" in text
    assert "proposed TASK in project Canopy support" in text
    assert "- T5 Dedupe the vault alert (suggested)" in text and "{{" not in text
    hid = plan["id"]
    web.detail[hid] = {**detail([_cell("eva", 1, {**r1("eva"), "huddle": hid}),
                                 _cell("echo", 1, {**r1("echo"), "huddle": hid})]), "id": hid}
    out3 = tmp_path / "eva-r3.md"
    web.detail[hid]["cells"] += [_cell("eva", 2, {"huddle": hid, "round": 2, "member": "eva",
                                                  "proposals": [], "critique_answers": []})]
    run("huddle", "prompt", "--plan", str(p), "--member", "eva", "--round", "3",
        "--out", str(out3))
    assert not out3.exists() or "## Check-in" not in out3.read_text()


def test_plan_refuses_a_solo_checkin_an_ended_run_and_a_foreign_leader(tmp_path, web):
    web.run["checkin"]["agents"] = ["ada"]
    r, _ = _plan(tmp_path)
    assert r.exit_code == 2 and "solo turn" in r.output
    web.run["checkin"]["agents"] = ["ada", "eva"]
    web.run["status"] = "skipped"
    r, _ = _plan(tmp_path)
    assert r.exit_code == 2 and "skipped" in r.output
    web.run["status"] = "running"
    r, _ = _plan(tmp_path, "--leader", "eva")
    assert r.exit_code == 2 and "led by ada, not eva" in r.output


# ── file ─────────────────────────────────────────────────────────────────────────
def _checkin_plan(tmp_path):
    return write_plan(tmp_path, priorities_brief="", principal="jonathan@dimagi.com",
                      checkin_run={"id": 77, "checkin": "Weekly check-in", "slug": "canopy-support",
                                   "project": PROJECT, "via": "checkins",
                                   "channel_name": "canopy-support",
                                   "lead_email": "jonathan@dimagi.com", "format": "huddle",
                                   "read": {"messages": 2}},
                      checkin={**CHECKIN, "slug": "canopy-support", "leader_agent": "ada",
                               "project": PROJECT})


def _file(tmp_path, web):
    p = _checkin_plan(tmp_path)
    props = tmp_path / "props.json"
    assert run("huddle", "proposals", "--huddle", H, "--out", str(props)).exit_code == 0
    return run("huddle", "file", "--plan", str(p), "--outcomes", str(props),
               "--local", str(tmp_path / "rec")), p, props


def test_file_proposes_tasks_in_the_project_backs_them_and_finishes_the_run(tmp_path, web):
    _full_huddle(web)
    r, p, props = _file(tmp_path, web)
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert out["filed_as"] == "proposed_tasks" and out["checkin_run_finished"] is True
    task, body = web.proposed["Joint Q4 brief"]
    # eva leads a task in ada's project: eva and her partner each get a linked task
    assert body["partners"] == ["eva", "echo"] and body["run"] == 77
    assert body["plan"] == ["start Joint Q4 brief", "then more"] and body["done_when"] == "3 meetings"
    assert body["source_url"] == f"https://cw/w/connect/huddles/{H}"
    assert "Lead: eva." in body["why"] and "Related project: Q4 pipeline." in body["why"]
    assert not paths(web, "POST", "/projects/propose/")                 # no project proposals
    assert web.tasks.get("eva", []) == [] and web.tasks.get("echo", []) == []
    assert web.task_backings == [("ada", "P12", task["ext_id"], "echo",
                                  {"stance": "backs", "line": "", "huddle_id": H})]
    [(rid, fin)] = web.finished
    assert rid == "77" and fin["tasks_proposed"] == 1 and fin["huddle_id"] == H
    assert fin["summary"].startswith("1 proposed task, 1 held")
    rec = json.loads((tmp_path / "rec" / f"{H}.json").read_text())
    assert rec["checkin_run"]["id"] == 77
    assert rec["outcomes"][0]["task"] == {"agent": "ada", "project": "P12",
                                          "ext_id": task["ext_id"], "kind": "proposed_task"}
    # Re-run: the open proposal is revised in place, the backing replaced, done → 200.
    r = run("huddle", "file", "--plan", str(p), "--outcomes", str(props),
            "--local", str(tmp_path / "rec"))
    assert r.exit_code == 0, r.output
    assert len(web.proposed) == 1 and len(web.task_backings) == 1
    assert json.loads(r.stdout)["filed"][0]["proposed"]["created"] is False


def test_a_survivor_marked_new_project_is_still_a_task_that_says_so(tmp_path, web):
    _full_huddle(web)
    for c in web.detail[H]["cells"]:
        if c["member"] == "eva" and c["round"] == 2:
            c["block"]["proposals"][0]["project"] = {"name": "Prompt library", "new": True}
    r, *_ = _file(tmp_path, web)
    assert r.exit_code == 0, r.output
    _, body = web.proposed["Joint Q4 brief"]
    assert "its own project (Prompt library)" in body["why"] and "a person's" in body["why"]


def test_a_survivor_naming_no_project_lives_in_the_checkins_project(tmp_path, web):
    _full_huddle(web)
    for c in web.detail[H]["cells"]:
        if c["member"] == "eva" and c["round"] == 2:
            c["block"]["proposals"][0]["project"] = {}
    r, *_ = _file(tmp_path, web)
    assert r.exit_code == 0, r.output
    assert "Joint Q4 brief" in web.proposed


def test_file_falls_back_to_proposing_projects_when_tasks_propose_404s(tmp_path, web):
    _full_huddle(web)
    web.propose_status = (404, '{"detail": "Not Found"}')
    r, *_ = _file(tmp_path, web)
    assert r.exit_code == 0, r.output
    assert "FALLING BACK to proposing projects" in r.stderr
    out = json.loads(r.stdout)
    assert out["filed_as"] == "proposed_projects"
    assert paths(web, "POST", "/api/agents/eva/projects/propose/")
    assert web.finished[0][1]["tasks_proposed"] == 0
    assert web.finished[0][1]["summary"].startswith("1 proposed project")


def test_file_does_not_fall_back_when_the_project_is_missing(tmp_path, web):
    _full_huddle(web)
    web.propose_status = (404, '{"detail": "project P12 not found"}')
    p = _checkin_plan(tmp_path)
    props = tmp_path / "props.json"
    run("huddle", "proposals", "--huddle", H, "--out", str(props))
    r = CliRunner().invoke(main, ["huddle", "file", "--plan", str(p), "--outcomes", str(props),
                                  "--local", str(tmp_path / "rec")])
    assert r.exit_code == 1 and "P12 not found" in r.output and not web.finished


def test_file_holds_a_task_declined_before(tmp_path, web):
    _full_huddle(web)
    web.propose_status = (409, '{"detail": "declined before; bring new_evidence"}')
    r, *_ = _file(tmp_path, web)
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert out["filed"] == [] and any("declined before" in h["held"] for h in out["held"])
    assert web.finished[0][1]["tasks_proposed"] == 0


# ── canopy checkin ───────────────────────────────────────────────────────────────
def test_material_prints_the_material_with_project_and_window_on_stderr(web):
    for verb in ("checkin", "huddle-group"):
        r = run(verb, "material", "--run", RUN_ID)
        assert r.exit_code == 0, r.output
        assert r.stdout.strip() == RUN["material"]
        assert "Weekly check-in" in r.stderr and "project ada/P12 (Canopy support)" in r.stderr
        assert "2 messages" in r.stderr and "huddle" in r.stderr


def test_propose_files_one_task_in_the_checkins_project(web):
    r = run("checkin", "propose", "--run", RUN_ID, "--title", "Dedupe alerts",
            "--why", "neal saw it twice", "--step", "find the cron", "--step", "fix it",
            "--partner", "hal", "--partner", "ada", "--source-url", "https://slack/x")
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert out["filed_as"] == "proposed_task" and out["created"] is True and out["task"] == "T20"
    _, body = web.proposed["Dedupe alerts"]
    assert body == {"title": "Dedupe alerts", "why": "neal saw it twice",
                    "done_when": "Dedupe alerts", "plan": ["find the cron", "fix it"],
                    "partners": ["hal"], "source_url": "https://slack/x", "run": 77}


def test_propose_new_project_says_so_and_a_declined_title_is_refused(web):
    r = run("checkin", "propose", "--run", RUN_ID, "--title", "Big thing", "--why", "w",
            "--new-project")
    assert r.exit_code == 0, r.output
    assert "should become its own project" in web.proposed["Big thing"][1]["why"]
    web.propose_status = (409, '{"detail": "declined before"}')
    r = run("checkin", "propose", "--run", RUN_ID, "--title", "Big thing", "--why", "w")
    assert r.exit_code == 2 and "--new-evidence" in r.output


def test_propose_falls_back_to_a_suggested_task_on_an_older_server(web):
    web.propose_status = (404, '{"detail": "Not Found"}')
    r = run("checkin", "propose", "--run", RUN_ID, "--title", "Dedupe alerts", "--why", "w",
            "--step", "find the cron")
    assert r.exit_code == 0, r.output
    assert "FALLING BACK" in r.stderr and json.loads(r.stdout)["filed_as"] == "suggested_task"
    [t] = web.tasks["ada"]
    assert t["project"] == "P12" and t["status"] == "suggested" and t["next_action"] == "find the cron"
    # idempotent by title within the project
    t["project_ext_id"] = "P12"
    r = run("checkin", "propose", "--run", RUN_ID, "--title", "Dedupe alerts", "--why", "w")
    assert json.loads(r.stdout)["created"] is False and len(web.tasks["ada"]) == 1


def test_finish_posts_tasks_proposed_and_is_idempotent_and_refuses_an_ended_run(web):
    r = run("checkin", "finish", "--run", RUN_ID, "--result-url", "https://cw/p",
            "--summary", "2 proposed tasks", "--tasks-proposed", "2")
    assert r.exit_code == 0, r.output
    assert web.finished == [("77", {"result_url": "https://cw/p", "summary": "2 proposed tasks",
                                    "tasks_proposed": 2})]
    assert run("checkin", "finish", "--run", RUN_ID, "--result-url", "u",
               "--summary", "s").exit_code == 0
    web.finish_status = 409
    r = run("checkin", "finish", "--run", RUN_ID, "--result-url", "u", "--summary", "s")
    assert r.exit_code == 2 and "no longer running" in r.output


def test_an_unknown_run_is_a_clear_error_not_a_fallback_success(web):
    r = run("checkin", "material", "--run", "9")
    assert r.exit_code == 2 and "cannot read check-in run 9" in r.output
