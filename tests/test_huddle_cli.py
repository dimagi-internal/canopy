"""`canopy huddle …` against a fake canopy-web (no network) and a LocalHuddleStore.

The fake implements just the routes the engine uses, including the derived huddle API
(`GET /api/huddles/`, `GET /api/huddles/<id>`) per the canopy-web contract."""
import datetime as dt
import json
import re
from urllib.parse import parse_qs, urlparse

import pytest
from click.testing import CliRunner

from orchestrator import huddle_cli
from orchestrator.cli import main

H = "work-fleet-20261006"
NOW = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.timezone.utc)


class FakeWeb:
    # canopy-web's AgentTaskIn — a StrictModel, so any other key is a 422.
    TASK_IN_FIELDS = {"ext_id", "project", "title", "next_action", "status", "owner", "assigned",
                      "waiting_on_email", "confidence", "score", "review", "rationale",
                      "source_url", "plan", "due", "links", "notes", "position", "ask_kind",
                      "ask_body", "on_approve", "batch_key", "idempotency_key", "origin",
                      "origin_ref", "raised_by"}

    def __init__(self):
        self.patches = []
        self.calls = []
        self.huddles = []                 # list rows
        self.detail = {}                  # id -> HuddleOut
        self.tasks = {}                   # slug -> [task]
        self.projects = {}                # slug -> [project]
        self.turns = {}                   # slug -> [turn]
        self.turn_posts = []
        self.closeouts = []
        self.refuse_mode = False

    def transport(self, method, url, headers, data):
        body = json.loads(data) if data else None
        u = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = u.path
        self.calls.append((method, path, q, body))
        if path == "/api/huddles/" and method == "GET":
            rows = [r for r in self.huddles
                    if not q.get("agent") or q["agent"] == r["leader"] or q["agent"] in r["members"]]
            return 200, json.dumps(rows)
        m = re.match(r"^/api/huddles/([^/]+)$", path)
        if m and method == "GET":
            d = self.detail.get(m.group(1))
            return (200, json.dumps(d)) if d else (404, '{"detail": "not found"}')
        if "/api/agents/ghost/" in path:
            return 404, '{"detail": "Not Found"}'
        m = re.match(r"^/api/agents/([^/]+)/turns/$", path)
        if m and method == "POST":
            self.closeouts.append((m.group(1), body))
            return 201, json.dumps({"id": "anchor-uuid", "cli_session_id": body["cli_session_id"]})
        m = re.match(r"^/api/agents/([^/]+)/$", path)
        if m and method == "GET":
            return 200, json.dumps({"slug": m.group(1), "workspace": "connect"})
        m = re.match(r"^/api/agents/([^/]+)/tasks/$", path)
        if m and method == "GET":
            return 200, json.dumps(self.tasks.get(m.group(1), []))
        if m and method == "POST":
            # a BARE list; an idempotency_key already seen replays the task it made
            assert isinstance(body, list), body
            rows = self.tasks.setdefault(m.group(1), [])
            out = []
            for t in body:
                extra = set(t) - self.TASK_IN_FIELDS
                if extra:
                    return 422, json.dumps({"detail": f"extra fields {sorted(extra)}"})
                key = t.get("idempotency_key")
                seen = next((r for r in rows if key and r.get("idempotency_key") == key), None)
                if seen is None:
                    seen = {"source_url": "", "rationale": "", "plan": "", **t,
                            "ext_id": t.get("ext_id") or f"T{len(rows) + 1}"}
                    rows.append(seen)
                out.append(seen)
            return 201, json.dumps(out)
        m = re.match(r"^/api/agents/([^/]+)/tasks/([^/]+)/$", path)
        if m and method == "PATCH":
            self.patches.append((m.group(1), m.group(2), body))
            for t in self.tasks.get(m.group(1), []):
                if t.get("ext_id") == m.group(2):
                    t.update(body)
                    return 200, json.dumps(t)
            return 404, '{"detail": "no task"}'
        m = re.match(r"^/api/agents/([^/]+)/projects/$", path)
        if m and method == "GET":
            return 200, json.dumps(self.projects.get(m.group(1), []))
        if m and method == "POST":
            rows = self.projects.setdefault(m.group(1), [])
            row = {**body, "ext_id": f"P{len(rows) + 1}"}
            rows.append(row)
            return 201, json.dumps(row)
        if path == "/api/harness/turns/" and method == "GET":
            return 200, json.dumps(self.turns.get(q.get("agent"), []))
        if path == "/api/harness/turns/" and method == "POST":
            if self.refuse_mode and "turn_mode" in body:
                return 403, '{"detail": "turn_mode auto needs the agent\'s owner or an admin"}'
            self.turn_posts.append(body)
            return 201, json.dumps({"id": f"turn-{len(self.turn_posts)}", "status": "queued",
                                    "origin_ref": body.get("origin_ref")})
        return 404, json.dumps({"detail": f"no route {method} {path}"})


@pytest.fixture()
def web(monkeypatch):
    w = FakeWeb()
    monkeypatch.setattr("orchestrator.canopy_web.resolve_base_url", lambda b=None: "https://cw")
    monkeypatch.setattr("orchestrator.canopy_web.resolve_token", lambda t=None: "tok")
    monkeypatch.setattr("orchestrator.canopy_web.urllib_transport", w.transport)
    monkeypatch.setattr(huddle_cli, "_now", lambda: NOW)
    monkeypatch.setattr(huddle_cli, "_sleep", lambda s: None)
    return w


def run(*args):
    return CliRunner().invoke(main, ["huddle", *args], catch_exceptions=False)


def _cell(member, rnd, block=None, status="done", attempt=1, created="2026-10-06T11:30:00Z",
          **kw):
    return {"member": member, "round": rnd, "attempt": attempt, "turn_id": f"t-{member}-{rnd}",
            "status": status, "created_at": created, "finished_at": None,
            "content_hidden": False, "prompt": "p", "block": block,
            "reply_source": "closeout" if block else "none", "reply_error": "",
            "has_transcript": False, **kw}


def r1(member, priorities=("Q4 funder pipeline (Jonathan's goals sheet)",)):
    return {"huddle": H, "round": 1, "member": member, "worked_on": [f"{member} did x"],
            "priorities": list(priorities), "projects": [{"name": "Q4", "state": "on"}],
            "offers": [], "needs": []}


def prop(title, lead, with_=(), priority="Q4 funder pipeline", project="Q4 pipeline", **kw):
    return {"title": title, "lead": lead, "with": list(with_), "priority": priority,
            "project": {"name": project, "new": False}, "why": f"why {title}",
            "plan": [f"start {title}", "then more"], "effort": "M",
            "success_measure": "3 meetings", "confidence": 0.8,
            "ask_of_partners": {m: f"{m} drafts the story" for m in with_}, **kw}


def detail(cells, **kw):
    return {"id": H, "type": "work", "team": "fleet", "leader": "ada",
            "members": ["eva", "echo"], "anchor_turn_id": "anchor-uuid",
            "created_at": "2026-10-06T11:00:00Z", "finished": False, "summary": "",
            "deadline_at": None, "cells": cells, "outputs": [], **kw}


def write_plan(tmp_path, **kw):
    plan = {"id": H, "type": "work", "team": "fleet", "leader": "ada",
            "members": ["eva", "echo"], "principal": "Jonathan", "sharing_rule": "share",
            "days": 7, "since": "2026-09-29", "started_at": "2026-10-06T11:00:00+00:00",
            "anchor_turn_id": "anchor-uuid", "workspace": "connect", "base_url": "https://cw",
            "page": f"https://cw/w/connect/huddles/{H}",
            "packs": {"eva": "EVA PACK", "echo": "ECHO PACK"}, "prior_text": "none yet",
            "prior": [], **kw}
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(plan))
    return p


# ── plan ─────────────────────────────────────────────────────────────────────────
def test_plan_picks_a_free_id_tags_the_anchor_and_builds_packs(tmp_path, web):
    web.huddles = [{"id": H, "leader": "ada", "members": ["eva"]}]
    web.tasks = {"eva": [{"ext_id": "T41", "status": "in_progress", "title": "Gates roster"},
                         {"ext_id": "T40", "status": "done", "title": "old"}]}
    web.projects = {"eva": [{"ext_id": "P3", "name": "Q4 pipeline", "outcome": "3 funders",
                             "status": "active"}]}
    web.turns = {"eva": [{"created_at": "2026-10-05T09:00:00Z", "status": "done",
                          "prompt": "Draft the Gates note\nmore", "origin_ref": {}},
                         {"created_at": "2026-09-01T09:00:00Z", "status": "done",
                          "prompt": "ancient", "origin_ref": {}}]}
    local = tmp_path / "records"
    from orchestrator.huddle_store import LocalHuddleStore
    LocalHuddleStore(local).write({"id": "work-fleet-20260929", "team": "fleet", "outcomes": [
        {"title": "Old idea", "lead": "eva", "fate": "filed",
         "task": {"agent": "eva", "ext_id": "T40"}}]})
    web.tasks["eva"][1]["status"] = "declined"
    web.tasks["eva"][1]["review"] = "not this quarter"
    out = tmp_path / "plan.json"
    r = run("plan", "--leader", "ada", "--team", "fleet", "--members", "eva,echo",
            "--principal", "Jonathan", "--sharing-rule", "share freely", "--local", str(local),
            "--out", str(out))
    assert r.exit_code == 0, r.output
    plan = json.loads(out.read_text())
    assert plan["id"] == f"{H}-2"
    assert plan["anchor_turn_id"] == "anchor-uuid"
    assert plan["workspace"] == "connect"
    assert plan["page"] == f"https://cw/w/connect/huddles/{H}-2"
    slug, body = web.closeouts[0]
    assert slug == "ada" and body["cli_session_id"] == f"huddle:{H}-2"
    assert body["origin_ref"] == {"kind": "huddle", "huddle": f"{H}-2", "type": "work",
                                  "team": "fleet", "leader": "ada", "members": ["eva", "echo"]}
    assert body["emdash_task_id"] == ""
    pack = plan["packs"]["eva"]
    assert "T41 [in_progress] Gates roster" in pack and "T40" not in pack
    assert "P3 Q4 pipeline — 3 funders" in pack
    assert "Draft the Gates note" in pack and "ancient" not in pack
    # the prior record's fate is refreshed from the live board: the principal declined it
    assert "Old idea (lead eva) — declined: not this quarter" in plan["prior_text"]
    assert plan["prior"][0]["outcomes"][0]["fate"] == "declined: not this quarter"


def test_plan_survives_a_member_it_cannot_read(tmp_path, web):
    out = tmp_path / "plan.json"
    r = run("plan", "--leader", "ada", "--team", "fleet", "--members", "eva,ghost",
            "--local", str(tmp_path / "rec"), "--out", str(out))
    assert r.exit_code == 0, r.output
    plan = json.loads(out.read_text())
    assert plan["id"] == H and set(plan["packs"]) == {"eva", "ghost"}
    assert "could not read ghost's board" in plan["packs"]["ghost"]


# ── prompt ───────────────────────────────────────────────────────────────────────
def test_prompt_round1_renders_the_members_pack(tmp_path, web):
    p = write_plan(tmp_path)
    out = tmp_path / "eva-r1.md"
    r = run("prompt", "--plan", str(p), "--member", "eva", "--round", "1", "--out", str(out))
    assert r.exit_code == 0, r.output
    text = out.read_text()
    assert "EVA PACK" in text and "ECHO PACK" not in text and "{{" not in text
    assert f'--session-id "huddle:{H}:eva:r1"' in text


def test_prompt_round2_carries_every_report_and_the_critique(tmp_path, web):
    web.detail[H] = detail([_cell("eva", 1, r1("eva")), _cell("echo", 1, r1("echo"))])
    p = write_plan(tmp_path)
    crit = tmp_path / "crit.json"
    crit.write_text(json.dumps({"eva": ["Why no funder follow-ups?", "Echo offers stories"]}))
    out = tmp_path / "eva-r2.md"
    r = run("prompt", "--plan", str(p), "--member", "eva", "--round", "2",
            "--critique", str(crit), "--out", str(out))
    assert r.exit_code == 0, r.output
    text = out.read_text()
    assert "### eva" in text and "### echo" in text and "echo did x" in text
    assert "- Why no funder follow-ups?" in text


def test_prompt_refuses_a_member_with_no_round1_reply(tmp_path, web):
    web.detail[H] = detail([_cell("eva", 1, r1("eva")), _cell("echo", 1, None, status="failed")])
    p = write_plan(tmp_path)
    r = run("prompt", "--plan", str(p), "--member", "echo", "--round", "2",
            "--out", str(tmp_path / "x.md"))
    assert r.exit_code == 2
    assert "no round-1 reply" in r.output


def test_prompt_round3_shows_the_partner_the_joint_asks(tmp_path, web):
    r2 = {"huddle": H, "round": 2, "member": "eva",
          "proposals": [prop("Joint Q4 brief", "eva", ["echo"]), prop("Solo thing", "eva")]}
    web.detail[H] = detail([_cell("eva", 1, r1("eva")), _cell("echo", 1, r1("echo")),
                            _cell("eva", 2, r2)])
    p = write_plan(tmp_path)
    crit = tmp_path / "crit.json"
    crit.write_text(json.dumps({"proposals": {"Joint Q4 brief": "Overlaps T41 — say how."}}))
    out = tmp_path / "echo-r3.md"
    r = run("prompt", "--plan", str(p), "--member", "echo", "--round", "3",
            "--critique", str(crit), "--out", str(out))
    assert r.exit_code == 0, r.output
    text = out.read_text()
    assert "### Joint Q4 brief (lead eva)" in text
    assert "Critique: Overlaps T41 — say how." in text
    assert "Solo thing" not in text


def test_prompt_round3_reaches_a_lead_named_by_someone_else(tmp_path, web):
    # The 2026-10-06 miss: eva proposed work with "lead": "hal"; hal's round 3 said "none".
    r2 = {"huddle": H, "round": 2, "member": "eva",
          "proposals": [prop("Diagnose chrome-sales MCP connect failures", "hal", ["hal", "eva"])]}
    web.detail[H] = detail([_cell("eva", 1, r1("eva")), _cell("hal", 1, r1("hal")),
                            _cell("eva", 2, r2), _cell("hal", 2, {"huddle": H, "round": 2,
                                                                  "member": "hal",
                                                                  "proposals": []})])
    p = write_plan(tmp_path, members=["eva", "hal"])
    out = tmp_path / "hal-r3.md"
    r = run("prompt", "--plan", str(p), "--member", "hal", "--round", "3", "--out", str(out))
    assert r.exit_code == 0, r.output
    text = out.read_text()
    assert "### Diagnose chrome-sales MCP connect failures (lead hal" in text
    assert '"title": "Diagnose chrome-sales MCP connect failures"' in text
    assert "none — no teammate" not in text


def test_proposals_counts_the_named_leads_cosign(tmp_path, web):
    r2 = {"huddle": H, "round": 2, "member": "eva", "critique_answers": [{"title": "t", "answer": "a"}],
          "proposals": [prop("Diagnose MCP", "hal", ["hal", "eva"])]}
    hal_r3 = {"huddle": H, "round": 3, "member": "hal",
              "answers": [{"title": "Diagnose MCP", "lead": "hal", "answer": "co-sign"}]}
    eva_r3 = {"huddle": H, "round": 3, "member": "eva",
              "answers": [{"title": "Diagnose MCP", "lead": "hal", "answer": "co-sign"}]}
    web.detail[H] = detail([_cell("eva", 1, r1("eva")), _cell("hal", 1, r1("hal")),
                            _cell("eva", 2, r2), _cell("hal", 3, hal_r3), _cell("eva", 3, eva_r3)])
    out = tmp_path / "props.json"
    assert run("proposals", "--huddle", H, "--out", str(out)).exit_code == 0
    [p] = json.loads(out.read_text())
    assert p["proposed_by"] == "eva" and p["answers"] == {"hal": "co-sign", "eva": "co-sign"}
    from orchestrator import huddle as Hm
    filed, held = Hm.work_gates([p], {"Q4 funder pipeline (Jonathan's goals sheet)"}, [])
    assert [x["title"] for x in filed] == ["Diagnose MCP"], held


def test_prompt_round3_carries_the_members_own_proposals_and_their_critique(tmp_path, web):
    eva_r2 = {"huddle": H, "round": 2, "member": "eva",
              "proposals": [prop("Solo Q4 follow-ups", "eva"),
                            prop("Joint Q4 brief", "eva", ["echo"])]}
    web.detail[H] = detail([_cell("eva", 1, r1("eva")), _cell("echo", 1, r1("echo")),
                            _cell("eva", 2, eva_r2)])
    p = write_plan(tmp_path)
    crit = tmp_path / "crit.json"
    crit.write_text(json.dumps({"proposals": {"Solo Q4 follow-ups": "Which funders? Name them."}}))
    out = tmp_path / "eva-r3.md"
    r = run("prompt", "--plan", str(p), "--member", "eva", "--round", "3",
            "--critique", str(crit), "--out", str(out))
    assert r.exit_code == 0, r.output
    text = out.read_text()
    assert "{{" not in text
    assert "### Solo Q4 follow-ups" in text and '"why": "why Solo Q4 follow-ups"' in text
    assert "Critique: Which funders? Name them." in text
    assert "### Joint Q4 brief" in text
    own = text.index("### Solo Q4 follow-ups")
    assert text.index("Which funders?") > own


def test_proposals_lets_the_proposer_revise_a_proposal_it_named_another_lead_for(tmp_path, web):
    r2 = {"huddle": H, "round": 2, "member": "eva",
          "proposals": [prop("Diagnose MCP", "hal", ["hal"])]}
    r3 = {"huddle": H, "round": 3, "member": "eva", "answers": [],
          "proposals": [prop("Diagnose MCP", "hal", ["hal"], why="revised why")]}
    web.detail[H] = detail([_cell("eva", 1, r1("eva")), _cell("eva", 2, r2), _cell("eva", 3, r3)])
    [p] = json.loads(run("proposals", "--huddle", H).stdout)
    assert p["why"] == "revised why" and p["revised"] is True


def test_file_creates_tasks_with_every_field_and_repatches_on_rerun(tmp_path, web):
    """POST /tasks/ takes source_url/rationale/plan at creation (the old sync dropped
    them); a re-run reuses the task it filed and re-patches those three by ext_id."""
    _full_huddle(web)
    p = write_plan(tmp_path)
    props = tmp_path / "props.json"
    run("proposals", "--huddle", H, "--out", str(props))
    r = run("file", "--plan", str(p), "--outcomes", str(props), "--local", str(tmp_path / "r"))
    assert r.exit_code == 0, r.output
    page = f"https://cw/w/connect/huddles/{H}"
    lead, = web.tasks["eva"]
    assert lead["source_url"] == page
    assert lead["rationale"].startswith("Serves: Q4 funder pipeline")
    assert lead["plan"] == "- start Joint Q4 brief\n- then more"
    assert lead["origin"] == "huddle"
    assert lead["idempotency_key"] == f"eva:{lead['ext_id']}"
    assert web.tasks["echo"][0]["source_url"] == page and web.tasks["echo"][0]["rationale"]
    assert web.patches == []                               # first run: create only
    r = run("file", "--plan", str(p), "--outcomes", str(props), "--local", str(tmp_path / "r"))
    assert r.exit_code == 0, r.output
    assert {(slug, ref) for slug, ref, _ in web.patches} == {
        ("eva", lead["ext_id"]), ("echo", web.tasks["echo"][0]["ext_id"])}   # re-run re-patches
    assert len(web.tasks["eva"]) == 1 and len(web.tasks["echo"]) == 1


def test_file_reuses_a_pre_fix_task_with_an_empty_source_url(tmp_path, web):
    _full_huddle(web)
    page = f"https://cw/w/connect/huddles/{H}"
    web.tasks = {"eva": [{"ext_id": "T42", "title": "Joint Q4 brief", "source_url": "",
                          "links": [{"label": "Huddle", "url": page}]}],
                 "echo": [{"ext_id": "T1", "source_url": "",
                           "title": "Joint Q4 brief — echo's part (lead eva)",
                           "links": [{"label": "Huddle", "url": page}]}]}
    p = write_plan(tmp_path)
    props = tmp_path / "props.json"
    run("proposals", "--huddle", H, "--out", str(props))
    r = run("file", "--plan", str(p), "--outcomes", str(props), "--local", str(tmp_path / "r"))
    assert r.exit_code == 0, r.output
    assert len(web.tasks["eva"]) == 1 and len(web.tasks["echo"]) == 1
    assert {(s, i) for s, i, _ in web.patches} == {("eva", "T42"), ("echo", "T1")}
    assert web.tasks["eva"][0]["source_url"] == page
    assert json.loads(r.stdout)["filed"][0]["task"]["reused"] is True


# ── dispatch ─────────────────────────────────────────────────────────────────────
def test_dispatch_tags_the_round_parents_it_and_never_pins(tmp_path, web):
    p = write_plan(tmp_path)
    f = tmp_path / "eva-r1.md"
    f.write_text("do round 1")
    r = run("dispatch", "--plan", str(p), "--member", "eva", "--round", "1",
            "--prompt-file", str(f))
    assert r.exit_code == 0, r.output
    body = web.turn_posts[0]
    assert body["agent_slug"] == "eva" and body["origin"] == "api"
    assert body["idempotency_key"] == f"huddle-{H}-eva-r1-a1"
    assert body["parent"] == {"turn": "anchor-uuid"}
    ref = body["origin_ref"]
    assert ref["kind"] == "huddle_round" and ref["huddle"] == H and ref["round"] == 1
    assert ref["member"] == "eva" and ref["attempt"] == 1
    assert ref["thread_key"] == f"huddle-{H}-eva-r1-a1" and ref["dispatched_by"] == "ada"
    assert "runner_id" not in body
    assert body["turn_mode"] == "auto"
    assert body["prompt"].startswith("do round 1") and "canopy:dispatched-prompt" in body["prompt"]
    assert json.loads(r.stdout)["turn_id"] == "turn-1"


def test_dispatch_attempt_and_mode_fallback(tmp_path, web):
    web.refuse_mode = True
    p = write_plan(tmp_path)
    f = tmp_path / "eva-r1.md"
    f.write_text("again")
    r = CliRunner().invoke(main, ["huddle", "dispatch", "--plan", str(p), "--member", "eva",
                                  "--round", "1", "--prompt-file", str(f), "--attempt", "2"])
    assert r.exit_code == 0, r.output
    body = web.turn_posts[0]
    assert "turn_mode" not in body
    assert body["idempotency_key"].endswith("-a2")
    assert json.loads(r.stdout)["mode_fallback"] is True


# ── await / status ───────────────────────────────────────────────────────────────
def test_await_exits_0_when_every_dispatched_member_settled(tmp_path, web):
    bad = {"huddle": H, "round": 1, "member": "echo"}                 # missing fields
    web.detail[H] = detail([_cell("eva", 1, r1("eva")), _cell("echo", 1, bad)])
    r = run("await", "--huddle", H, "--round", "1", "--budget-seconds", "0")
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert out["states"] == {"eva": "replied", "echo": "malformed"}
    assert any("worked_on" in p for p in out["problems"]["echo"])


def test_await_exits_3_while_a_member_is_still_working(tmp_path, web):
    # `done` on a round turn means SPAWNED — no block yet is still pending.
    web.detail[H] = detail([_cell("eva", 1, r1("eva")), _cell("echo", 1, None, status="done")])
    r = run("await", "--huddle", H, "--round", "1", "--budget-seconds", "0")
    assert r.exit_code == 3, r.output
    assert json.loads(r.stdout)["states"]["echo"] == "pending:done"


def test_await_deadline_times_out_stragglers(tmp_path, web):
    web.detail[H] = detail([_cell("eva", 1, r1("eva"), created="2026-10-06T10:00:00Z"),
                            _cell("echo", 1, None, status="running",
                                  created="2026-10-06T10:01:00Z"),
                            _cell("hal", 1, None, status="failed",
                                  created="2026-10-06T10:01:00Z")])
    r = run("await", "--huddle", H, "--round", "1", "--budget-seconds", "0")
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["states"] == {"eva": "replied", "echo": "timed_out",
                                              "hal": "failed"}


def test_await_with_nothing_dispatched_is_an_error(web):
    web.detail[H] = detail([])
    r = run("await", "--huddle", H, "--round", "2", "--budget-seconds", "0")
    assert r.exit_code == 2


def test_status_lists_member_rounds(web):
    web.detail[H] = detail([_cell("eva", 1, r1("eva")), _cell("echo", 1, None, status="queued")])
    r = run("status", "--huddle", H)
    assert r.exit_code == 0
    assert "eva" in r.output and "r1 replied (closeout)" in r.output
    assert "r1 pending:queued" in r.output


# ── resume ───────────────────────────────────────────────────────────────────────
def test_resume_finds_the_unfinished_huddle_and_says_what_is_next(web):
    web.huddles = [{"id": H, "leader": "ada", "members": ["eva", "echo"], "finished": False,
                    "created_at": "2026-10-06T11:00:00Z"},
                   {"id": "work-fleet-20261001", "leader": "ada", "members": ["eva"],
                    "finished": False, "created_at": "2026-10-01T11:00:00Z"}]
    web.detail[H] = detail([_cell("eva", 1, r1("eva")), _cell("echo", 1, None, status="failed")])
    r = run("resume", "--leader", "ada")
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert out["huddle"] == H
    assert any("echo" in n and "--attempt 2" in n for n in out["next"])


def test_resume_none(web):
    web.huddles = [{"id": "work-fleet-20261001", "leader": "ada", "members": ["eva"],
                    "finished": False, "created_at": "2026-10-01T11:00:00Z"}]
    r = run("resume", "--leader", "ada")
    assert json.loads(r.stdout) == {"huddle": None}


# ── proposals + file ─────────────────────────────────────────────────────────────
def _full_huddle(web):
    eva_r2 = {"huddle": H, "round": 2, "member": "eva",
              "proposals": [prop("Joint Q4 brief", "eva", ["echo"]),
                            prop("Solo thing", "eva", priority="nobody said this")],
              "critique_answers": [{"title": "follow-ups", "answer": "T41 covers them"}]}
    echo_r2 = {"huddle": H, "round": 2, "member": "echo", "proposals": [],
               "critique_answers": []}
    echo_r3 = {"huddle": H, "round": 3, "member": "echo",
               "answers": [{"title": "joint q4 brief", "lead": "eva", "answer": "co-sign",
                            "note": ""}]}
    web.detail[H] = detail([_cell("eva", 1, r1("eva")), _cell("echo", 1, r1("echo")),
                            _cell("eva", 2, eva_r2), _cell("echo", 2, echo_r2),
                            _cell("echo", 3, echo_r3)])


def test_proposals_merges_answers_and_critique(tmp_path, web):
    _full_huddle(web)
    out = tmp_path / "props.json"
    r = run("proposals", "--huddle", H, "--out", str(out))
    assert r.exit_code == 0, r.output
    props = {p["title"]: p for p in json.loads(out.read_text())}
    assert props["Joint Q4 brief"]["answers"] == {"echo": "co-sign"}
    assert props["Joint Q4 brief"]["critique_answered"] is True
    assert props["Solo thing"]["answers"] == {}


def test_file_creates_tasks_writes_record_and_finishes_the_anchor(tmp_path, web):
    _full_huddle(web)
    web.tasks = {"eva": [{"ext_id": "T41", "title": "x", "status": "in_progress"}]}
    web.projects = {"eva": [{"ext_id": "P3", "name": "Q4 pipeline"}]}
    p = write_plan(tmp_path)
    props = tmp_path / "props.json"
    assert run("proposals", "--huddle", H, "--out", str(props)).exit_code == 0
    local = tmp_path / "records"
    digest = tmp_path / "digest.md"
    r = run("file", "--plan", str(p), "--outcomes", str(props), "--local", str(local),
            "--digest-out", str(digest))
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert [f["title"] for f in out["filed"]] == ["Joint Q4 brief"]
    assert out["held"][0]["title"] == "Solo thing"
    page = f"https://cw/w/connect/huddles/{H}"
    lead_task = [t for t in web.tasks["eva"] if t.get("source_url") == page]
    assert len(lead_task) == 1
    t = lead_task[0]
    assert t["ext_id"] == "T42" and t["project"] == "P3" and t["status"] == "suggested"
    assert t["owner"] == "Jonathan" and t["assigned"] == "eva" and t["confidence"] == "high"
    assert t["next_action"] == "start Joint Q4 brief" and t["origin"] == "huddle"
    assert {"label": "Huddle", "url": page} in t["links"]
    partner = web.tasks["echo"]
    assert len(partner) == 1 and partner[0]["title"] == "Joint Q4 brief — echo's part (lead eva)"
    assert partner[0]["next_action"] == "echo drafts the story"
    rec = json.loads((local / f"{H}.json").read_text())
    assert rec["outcomes"][0]["fate"] == "filed"
    assert rec["outcomes"][0]["task"]["ext_id"] == "T42"
    assert rec["outcomes"][1]["fate"].startswith("held: priority not stated")
    slug, finish = web.closeouts[-1]
    assert slug == "ada" and finish["cli_session_id"] == f"huddle:{H}"
    assert finish["origin_ref"]["finished_at"]
    text = digest.read_text()
    assert page in text and "Joint Q4 brief" in text and len(text.split()) <= 200

    # Re-run: nothing duplicated, record replaced.
    r = run("file", "--plan", str(p), "--outcomes", str(props), "--local", str(local))
    assert r.exit_code == 0, r.output
    assert len([t for t in web.tasks["eva"] if t.get("source_url") == page]) == 1
    assert len(web.tasks["echo"]) == 1
    assert json.loads(r.stdout)["filed"][0]["task"]["reused"] is True


def test_file_creates_the_project_when_the_lead_has_none(tmp_path, web):
    _full_huddle(web)
    p = write_plan(tmp_path)
    props = tmp_path / "props.json"
    run("proposals", "--huddle", H, "--out", str(props))
    r = run("file", "--plan", str(p), "--outcomes", str(props), "--local", str(tmp_path / "r"))
    assert r.exit_code == 0, r.output
    assert web.projects["eva"][0]["name"] == "Q4 pipeline"
    assert H in web.projects["eva"][0]["notes"]


def _one_proposal_huddle(web, project_name):
    eva_r2 = {"huddle": H, "round": 2, "member": "eva",
              "proposals": [prop("Cascade demo", "eva", ["echo"], project=project_name)],
              "critique_answers": [{"title": "t", "answer": "a"}]}
    echo_r3 = {"huddle": H, "round": 3, "member": "echo",
               "answers": [{"title": "Cascade demo", "lead": "eva", "answer": "co-sign"}]}
    web.detail[H] = detail([_cell("eva", 1, r1("eva")), _cell("echo", 1, r1("echo")),
                            _cell("eva", 2, eva_r2), _cell("echo", 3, echo_r3)])


def _file_one(tmp_path, web):
    p = write_plan(tmp_path)
    props = tmp_path / "props.json"
    assert run("proposals", "--huddle", H, "--out", str(props)).exit_code == 0
    r = run("file", "--plan", str(p), "--outcomes", str(props), "--local", str(tmp_path / "r"))
    assert r.exit_code == 0, r.output
    page = f"https://cw/w/connect/huddles/{H}"
    return ([t for t in web.tasks["eva"] if t.get("source_url") == page][0],
            web.tasks["echo"][0])


def test_file_resolves_a_project_by_its_ext_id_in_parentheses(tmp_path, web):
    _one_proposal_huddle(web, "Spark cascade (P3)")
    web.projects = {"eva": [{"ext_id": "P1", "name": "Other"},
                            {"ext_id": "P3", "name": "Spark facilitator programme cascade demo"}],
                    "echo": [{"ext_id": "P9", "name": "Spark facilitator programme cascade demo"}]}
    lead, partner = _file_one(tmp_path, web)
    assert lead["project"] == "P3" and len(web.projects["eva"]) == 2
    # the partner resolves the lead's REAL project name on its own board
    assert partner["project"] == "P9"


def test_file_resolves_a_project_ignoring_a_trailing_suffix_and_case(tmp_path, web):
    _one_proposal_huddle(web, "connect-labs  Reliability (T15/T37/T26)")
    web.projects = {"eva": [{"ext_id": "P2", "name": "Connect-labs reliability"}]}
    lead, _ = _file_one(tmp_path, web)
    assert lead["project"] == "P2" and len(web.projects["eva"]) == 1


def test_file_creates_a_missing_project_without_the_suffix(tmp_path, web):
    _one_proposal_huddle(web, "Spark cascade demo (P7)")      # P7 is not on eva's board
    web.projects = {"eva": [{"ext_id": "P1", "name": "Other"}]}
    lead, _ = _file_one(tmp_path, web)
    assert web.projects["eva"][-1]["name"] == "Spark cascade demo"
    assert lead["project"] == web.projects["eva"][-1]["ext_id"] == "P2"


def test_file_dry_run_writes_nothing(tmp_path, web):
    _full_huddle(web)
    p = write_plan(tmp_path)
    props = tmp_path / "props.json"
    run("proposals", "--huddle", H, "--out", str(props))
    r = run("file", "--plan", str(p), "--outcomes", str(props), "--local", str(tmp_path / "r"),
            "--dry-run")
    assert r.exit_code == 0, r.output
    assert web.tasks == {} and not (tmp_path / "r").exists()
    assert not [c for c in web.calls if c[0] == "POST"]
    assert json.loads(r.stdout)["dry_run"] is True


def test_file_rejects_an_overlong_field_before_writing(tmp_path, web):
    _full_huddle(web)
    p = write_plan(tmp_path)
    props = tmp_path / "props.json"
    run("proposals", "--huddle", H, "--out", str(props))
    rows = json.loads(props.read_text())
    rows[0]["plan"] = ["x" * 400]
    props.write_text(json.dumps(rows))
    r = CliRunner().invoke(main, ["huddle", "file", "--plan", str(p), "--outcomes", str(props),
                                  "--local", str(tmp_path / "r")])
    assert r.exit_code != 0 and "next_action" in r.output
    assert not [c for c in web.calls if c[0] == "POST"]


def test_file_reports_created_tasks_when_the_record_cannot_be_written(tmp_path, web, monkeypatch):
    _full_huddle(web)
    p = write_plan(tmp_path)
    props = tmp_path / "props.json"
    run("proposals", "--huddle", H, "--out", str(props))
    from orchestrator import huddle_store

    def boom(self, record):
        raise huddle_store.HuddleStoreError("drive down")
    monkeypatch.setattr(huddle_store.LocalHuddleStore, "write", boom)
    r = CliRunner().invoke(main, ["huddle", "file", "--plan", str(p), "--outcomes", str(props),
                                  "--local", str(tmp_path / "r")])
    assert r.exit_code == 1
    assert "drive down" in r.output and "T1" in r.output
    # the anchor is NOT marked finished
    assert not any((b.get("origin_ref") or {}).get("finished_at") for _, b in web.closeouts)


def test_digest_stays_short_with_five_outcomes():
    filed = [{**prop(f"Proposal number {i} with ten words in it ok", "eva", ["echo", "hal"],
                     priority="A long priority statement " * 6),
              "task": {"agent": "eva", "ext_id": f"T{i}"}} for i in range(5)]
    held = [{"title": f"H{i}", "held": "echo has not co-signed"} for i in range(4)]
    text = huddle_cli.digest_text(H, filed, held, "https://cw/w/connect/huddles/" + H,
                                  "https://cw", "connect")
    assert len(text.split()) <= 200 and "https://cw/w/connect/huddles/" + H in text


def test_view_prints_the_page(web):
    web.detail[H] = detail([])
    r = run("view", "--huddle", H)
    assert r.output.strip() == f"https://cw/w/connect/huddles/{H}"


# ── round 4 (resolve an amend) ───────────────────────────────────────────────────
def _amended_huddle(web, r4=None, lead="eva", proposer="eva", with_=("echo",)):
    """eva proposes joint work with echo; echo amends it in round 3."""
    r2 = {"huddle": H, "round": 2, "member": proposer,
          "proposals": [prop("Joint Q4 brief", lead, list(with_)), prop("Solo thing", proposer)],
          "critique_answers": [{"title": "t", "answer": "a"}]}
    echo_r3 = {"huddle": H, "round": 3, "member": "echo",
               "answers": [{"title": "Joint Q4 brief", "lead": lead, "answer": "amend",
                            "note": "public material only; due 10/8 as a gdoc"}]}
    cells = [_cell("eva", 1, r1("eva")), _cell("echo", 1, r1("echo")),
             _cell(proposer, 2, r2), _cell("echo", 3, echo_r3)]
    if lead != proposer:
        cells += [_cell(lead, 1, r1(lead)),
                  _cell(lead, 3, {"huddle": H, "round": 3, "member": lead,
                                  "answers": [{"title": "Joint Q4 brief", "lead": lead,
                                               "answer": "co-sign"}]})]
    if r4:
        cells.append(_cell(r4["member"], 4, r4))
    web.detail[H] = detail(cells)


def test_prompt_round4_quotes_the_proposal_and_each_amend(tmp_path, web):
    _amended_huddle(web)
    p = write_plan(tmp_path)
    out = tmp_path / "eva-r4.md"
    r = run("prompt", "--plan", str(p), "--member", "eva", "--round", "4", "--out", str(out))
    assert r.exit_code == 0, r.output
    text = out.read_text()
    assert "{{" not in text
    assert "### Joint Q4 brief (lead eva)" in text
    assert '"why": "why Joint Q4 brief"' in text
    assert 'echo: "public material only; due 10/8 as a gdoc"' in text
    assert "Solo thing" not in text
    assert f'--session-id "huddle:{H}:eva:r4"' in text


def test_prompt_round4_refuses_a_member_with_nothing_to_resolve(tmp_path, web):
    _amended_huddle(web)
    p = write_plan(tmp_path)
    r = run("prompt", "--plan", str(p), "--member", "echo", "--round", "4",
            "--out", str(tmp_path / "x.md"))
    assert r.exit_code == 2 and "nothing for echo to resolve" in r.output


def test_prompt_round4_reaches_the_proposer_of_a_named_lead(tmp_path, web):
    _amended_huddle(web, lead="hal", proposer="eva", with_=("hal", "echo"))
    p = write_plan(tmp_path, members=["eva", "echo", "hal"])
    for m in ("hal", "eva"):
        out = tmp_path / f"{m}-r4.md"
        r = run("prompt", "--plan", str(p), "--member", m, "--round", "4", "--out", str(out))
        assert r.exit_code == 0, r.output
        assert "### Joint Q4 brief (lead hal)" in out.read_text()


def _r4(member, resolution, proposal=None, note=""):
    res = {"title": "Joint Q4 brief", "lead": "eva", "resolution": resolution, "note": note}
    if proposal is not None:
        res["proposal"] = proposal
    return {"huddle": H, "round": 4, "member": member, "resolutions": [res]}


def test_accepted_amend_files_the_revised_proposal(tmp_path, web):
    revised = prop("Joint Q4 brief", "eva", ["echo"], why="public material only; gdoc by 10/8")
    _amended_huddle(web, r4=_r4("eva", "accept", revised))
    [p, _solo] = json.loads(run("proposals", "--huddle", H).stdout)
    assert p["why"] == "public material only; gdoc by 10/8" and p["revised"] is True
    assert p["answers"] == {"echo": "amend→accepted"}
    assert p["answer_notes"]["echo"] == "public material only; due 10/8 as a gdoc"
    assert p["resolution"]["resolution"] == "accept" and p["resolution"]["by"] == "eva"
    plan = write_plan(tmp_path)
    props = tmp_path / "props.json"
    props.write_text(json.dumps([p]))
    r = run("file", "--plan", str(plan), "--outcomes", str(props), "--local", str(tmp_path / "r"))
    assert r.exit_code == 0, r.output
    assert [f["title"] for f in json.loads(r.stdout)["filed"]] == ["Joint Q4 brief"]
    page = f"https://cw/w/connect/huddles/{H}"
    [t] = [t for t in web.tasks["eva"] if t.get("source_url") == page]
    assert "public material only; gdoc by 10/8" in t["rationale"]
    rec = json.loads((tmp_path / "r" / f"{H}.json").read_text())
    assert rec["outcomes"][0]["cosign"] == {"echo": "amend→accepted"}


def test_rejected_amend_stays_held(tmp_path, web):
    _amended_huddle(web, r4=_r4("eva", "reject", note="the gdoc is out of scope"))
    [p, _solo] = json.loads(run("proposals", "--huddle", H).stdout)
    assert p["answers"] == {"echo": "amend→rejected"} and p["why"] == "why Joint Q4 brief"
    assert p["resolution"]["note"] == "the gdoc is out of scope"
    plan = write_plan(tmp_path)
    props = tmp_path / "props.json"
    props.write_text(json.dumps([p]))
    r = run("file", "--plan", str(plan), "--outcomes", str(props), "--local", str(tmp_path / "r"),
            "--dry-run")
    out = json.loads(r.stdout)
    assert out["filed"] == [] and out["held"][0]["held"] == "amend rejected by lead (echo)"


def test_a_resolution_from_someone_not_entitled_is_ignored(tmp_path, web):
    revised = prop("Joint Q4 brief", "eva", ["echo"], why="hijacked")
    _amended_huddle(web, r4=_r4("echo", "accept", revised))
    [p, _solo] = json.loads(run("proposals", "--huddle", H).stdout)
    assert p["answers"] == {"echo": "amend"} and p["why"] == "why Joint Q4 brief"


def test_accept_without_a_revised_proposal_leaves_the_amend_unresolved(web):
    _amended_huddle(web, r4=_r4("eva", "accept"))
    [p, _solo] = json.loads(run("proposals", "--huddle", H).stdout)
    assert p["answers"] == {"echo": "amend"}


def test_a_huddle_without_amends_never_needs_round4(tmp_path, web):
    _full_huddle(web)
    for c in web.detail[H]["cells"]:
        c["created_at"] = "2026-10-06T11:30:00Z"
    p = write_plan(tmp_path)
    for m in ("eva", "echo"):
        r = run("prompt", "--plan", str(p), "--member", m, "--round", "4",
                "--out", str(tmp_path / "x.md"))
        assert r.exit_code == 2 and "nothing for" in r.output
    web.huddles = [{"id": H, "leader": "ada", "members": ["eva", "echo"], "finished": False,
                    "created_at": "2026-10-06T11:00:00Z"}]
    nxt = json.loads(run("resume", "--leader", "ada").stdout)["next"]
    assert any("proposals" in n and "canopy huddle agree" in n for n in nxt), nxt
    assert not any("continue with round 4" in n for n in nxt)
