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
    def __init__(self):
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
        m = re.match(r"^/api/agents/([^/]+)/tasks/sync$", path)
        if m and method == "POST":
            self.tasks.setdefault(m.group(1), []).extend(body["tasks"])
            return 200, json.dumps({"synced": len(body["tasks"])})
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
    assert t["next_action"] == "start Joint Q4 brief" and t["source"] == "huddle"
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
